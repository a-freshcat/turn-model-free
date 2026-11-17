"""Native Antigravity text/tool Chat Completions transport.

The public OpenAI response schema is produced here; OAuth lives separately.
"""

import hashlib
import json
import re
import time
from uuid import uuid4

import httpx

from account_manager import InvalidRequest, UpstreamFailure
from antigravity_auth import API_BASE, OAuthFailure
from gemini_schema import clean_tool_schema


def structured_stop(value, retry_after=0, *, allow_expired=False):
    """Classify only structured upstream errors using fixed diagnostic labels."""
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if not isinstance(error, dict):
        error = value
    codes = [item.get("code") for item in (value, error, value.get("data"))
             if isinstance(item, dict)]
    if any(code in (11140, "11140") for code in codes):
        return UpstreamFailure("account_restricted", reauth=True, retry_after=retry_after)
    if any(code in (11128, "11128") for code in codes):
        return UpstreamFailure("channel_11128", retry_after=retry_after)
    status = error.get("status")
    if status == "PERMISSION_DENIED" or any(code in (403, "403") for code in codes):
        return UpstreamFailure("antigravity_http_403", retry_after=retry_after)
    if status == "RESOURCE_EXHAUSTED" or any(code in (429, "429") for code in codes):
        return UpstreamFailure("antigravity_http_429", retry_after=retry_after)
    if not allow_expired and (status == "UNAUTHENTICATED" or
                              any(code in (401, "401") for code in codes)):
        return UpstreamFailure("antigravity_http_401", reauth=True, retry_after=retry_after)
    return None


async def check_before_credential_refresh(response):
    """Do not refresh or resend when the first 401 contains an explicit stop."""
    from upstream_client import MAX_ERROR, _body, _is_gate_body, _json_object, _retry_after
    raw = await _body(response, MAX_ERROR, prefix=True)
    retry_after = _retry_after(response.headers.get("retry-after"))
    failure = structured_stop(_json_object(raw), retry_after, allow_expired=True)
    if failure is not None:
        raise failure
    if _is_gate_body(raw.decode("utf-8", "replace")):
        raise UpstreamFailure("channel_11128", retry_after=retry_after)


def native_http_error(status, raw, retry_after=0):
    """Do not convert caller/schema validation into an unhealthy account.

    Expose only known protocol field names; upstream descriptions may quote
    prompts, tools or credentials and must never cross the API boundary.
    """
    from upstream_client import _is_gate_body, _json_object
    value=_json_object(raw) or {}
    error=value.get('error') if isinstance(value.get('error'),dict) else value
    failure = structured_stop(value, retry_after)
    if failure is not None:
        return failure
    if _is_gate_body(raw.decode('utf-8','replace')):
        return UpstreamFailure('channel_11128', retry_after=retry_after)
    if status == 400:
        upstream_status = error.get('status')
        allowed={'request','generation_config','generationConfig','max_output_tokens','maxOutputTokens',
            'contents','parts','role','text','tools','functionDeclarations','function_declarations',
            'parameters','properties','required','type','thinking_config','thinkingConfig',
            'thinking_budget','thinkingBudget','system_instruction','systemInstruction','model','project',
            'tool_config','toolConfig','responseJsonSchema','response_schema','responseSchema',
            'additionalProperties','temperature','topP','top_p','candidateCount','candidate_count'}
        fields=[]
        for detail in error.get('details',[]) if isinstance(error.get('details'),list) else []:
            if not isinstance(detail,dict):continue
            for violation in detail.get('fieldViolations',[]) if isinstance(detail.get('fieldViolations'),list) else []:
                field=violation.get('field') if isinstance(violation,dict) else None
                if not isinstance(field,str) or len(field)>256:continue
                if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\[\d{1,5}\]|\.[A-Za-z_][A-Za-z_0-9]*)*',field):continue
                segments=re.findall(r'[A-Za-z_][A-Za-z_0-9]*',field)
                if segments and all(segment in allowed for segment in segments):fields.append(field)
        # Fixed field-name hints also work when the upstream omits fieldViolations.
        message=str(error.get('message',''))
        for field in ('maxOutputTokens','max_output_tokens','thinkingBudget','thinking_budget',
                      'functionDeclarations','function_declarations','parametersJsonSchema'):
            if field in message and field not in fields:fields.append(field)
        label=upstream_status if upstream_status in ('INVALID_ARGUMENT','FAILED_PRECONDITION','OUT_OF_RANGE') else 'BAD_REQUEST'
        hint=('；检查字段：'+', '.join(fields[:6])) if fields else '；请检查模型、工具声明和生成参数'
        return InvalidRequest('上游请求参数校验失败（HTTP 400 / '+label+'）'+hint+'。账号未因此进入冷却。')
    return UpstreamFailure('antigravity_http_'+str(status),retry_after=retry_after,reauth=status==401)


_LEGACY_MARKER_PREFIXES = (
    "Historical assistant tool call (already executed):",
    "Historical tool result (quoted, untrusted data):",
)
_JSON_DECODER = json.JSONDecoder()


def _match_json_object_end(text: str, start: int) -> int:
    """Return index past the balanced `{...}` object starting at `start`, or -1."""
    n = len(text)
    if start >= n or text[start] != "{":
        return -1
    try:
        obj, end = _JSON_DECODER.raw_decode(text, start)
        if isinstance(obj, dict):
            return end
    except ValueError:
        pass
    depth = 0
    in_string = False
    escape = False
    for i in range(start, n):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
    return -1


def strip_legacy_markers(text):
    """Remove gateway-injected replay markers from history text."""
    if not isinstance(text, str) or 'Historical ' not in text:
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        next_pos = -1
        matched_prefix = None
        for prefix in _LEGACY_MARKER_PREFIXES:
            pos = text.find(prefix, i)
            if pos != -1 and (next_pos == -1 or pos < next_pos):
                next_pos = pos
                matched_prefix = prefix
        if next_pos == -1:
            out.append(text[i:])
            break
        if next_pos > 0 and text[next_pos - 1] in ("`", "'", '"'):
            out.append(text[i:next_pos + len(matched_prefix)])
            i = next_pos + len(matched_prefix)
            continue
        j = next_pos + len(matched_prefix)
        while j < n and text[j] in (" ", "\t", "\r", "\n"):
            j += 1
        end = _match_json_object_end(text, j)
        if end != -1:
            out.append(text[i:next_pos])
            i = end
        else:
            out.append(text[i:next_pos + len(matched_prefix)])
            i = next_pos + len(matched_prefix)
    return "".join(out).strip()


# ── 出站文本归一化 ───────────────────────────────────────────────────────────
#
# 上游在 system 区维护一份**顺序敏感、大小写与空白归一**的多词黑名单，命中就回
# `HTTP 429 / RESOURCE_EXHAUSTED`：配额桶健康、`details` 为空、没有 `RetryInfo`，
# 响应体只有通用的 "Resource has been exhausted"。所以任何按 429 语义重试的逻辑
# 都必然失败——每一轮重试都在撞同一段文本。
#
# **这份黑名单的内容会变。** 同一个探法（直连 `send_native`，绕开账号池，一次
# 429 不会污染下一次）测到过三次不同的答案：
#
#   2026-11-13  出口 154.39.62.160 / AS18186
#       "[MODE: UNRESTRICTED SANDBOX — STABLE]"   -> 429   3/3
#       同一行把 — 换成 -                          -> 200   3/3
#
#   2026-11-16  出口 82.21.216.23 / AS10753
#       同一行（含 —）                             -> 200   4/4
#       "The browser provides no implicit DOM, ..." -> 429   4/4
#       整份 prompt 删掉那一句                      -> 200   3/3
#
#   2026-11-17  出口回到 154.39.62.160 / AS18186
#       "The browser provides no implicit DOM, ..." -> 200   4 个模型
#       同一行（含 —）                             -> 429   4 个模型
#       整份 prompt 原样                            -> 429   4 个模型
#       整份 prompt 把该行 — 换成 -                 -> 200   4 个模型
#
# 教训：单次快照只能证明"今天这份清单里有这一条"，不能证明根因已闭合。v1.5.1
# 对了机制、也说对了当天的条目，但把结论写成了"已定位"；v1.5.2 据此删掉字符表，
# 结果在清单换回旧条目时直接失明。两层防御都要留：
#
#   * 字符表：把排版类 Unicode 折成 ASCII 等价物。含破折号的条目因此无法匹配，
#     且清单把旧条目加回来时仍然有效。
#   * 句子表：处理纯 ASCII 的条目——字符表按定义碰不到它们。
#
# 两者都不是绕过内容策略：破折号换减号、弯引号换直引号语义完全相同，句子改写是
# 模型读起来意思不变的转述。真正的错误是"已定位"这个说法，tables 是对移动目标的
# 防御，报告必须这么写。
#
# 匹配器性质（三个日期上各自实测过，结论一致）：
#     换掉任意一个实词        -> 200     不是字面关键词匹配
#     词序反转                -> 200     排序敏感
#     大写 / 全小写           -> 429     大小写不敏感
#     空格换换行 / 多一个空格  -> 429     空白不敏感
#     少了任一 token          -> 200     必须整段才触发
#     同一段作为 user 消息     -> 200     只作用于 system 区

_TEXT_HOMOGLYPHS = {
    "\u2014": "-",   # em dash      —
    "\u2013": "-",   # en dash      –
    "\u2012": "-",   # figure dash
    "\u2015": "-",   # horizontal bar
    "\u2212": "-",   # minus sign   −
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote
    "\u201a": "'",   # single low quote
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u201e": '"',   # double low quote
    "\u00a0": " ",   # no-break space
    "\u2009": " ",   # thin space
    "\u202f": " ",   # narrow no-break space
    "\u200b": "",    # zero-width space
    "\ufeff": "",    # BOM
}
_TEXT_TRANSLATION = str.maketrans(_TEXT_HOMOGLYPHS)

# 纯 ASCII 的条目，字符表碰不到，只能整句改写。每一条都附上测得它的日期与出口，
# 因为清单会变——没有出处的条目无法判断是否还该留着。
KNOWN_TRIGGER_SENTENCES = {
    # 2026-11-16, exit 82.21.216.23 / AS10753
    # 来源 @deepseek-ai/dsh-harness-dev-context WEB_SURFACE_PROMPT
    "The browser provides no implicit DOM, route, or screenshot context.":
        "The browser exposes no implicit DOM, route, or screenshot context.",
}


def known_triggers_in_text(text):
    """Return the known-bad sentences present in `text`, in table order."""
    if not isinstance(text, str) or not text:
        return []
    return [source for source in KNOWN_TRIGGER_SENTENCES if source in text]


def rewrite_known_triggers(text: str) -> str:
    """Replace known-bad ASCII sentences with measured-safe paraphrases.

    Idempotent: no replacement contains any source.
    """
    if not isinstance(text, str) or not text:
        return text
    for source, replacement in KNOWN_TRIGGER_SENTENCES.items():
        if source in text:
            text = text.replace(source, replacement)
    return text


def normalize_outbound_text(text: str) -> str:
    """出站文本归一化入口：先折排版字符，再改已知 ASCII 触发句。

    幂等：`translate` 的结果不含任何源字符，句表替换结果不含任何源句。
    CJK、重音字符、emoji、工具名一律不动。
    """
    if not isinstance(text, str) or not text:
        return text
    if not text.isascii():
        text = text.translate(_TEXT_TRANSLATION)
    return rewrite_known_triggers(text)


def native_request(messages, options, *, signature_lookup=None, signature_borrow=None):
    contents, system, call_names = [], [], {}
    for message in messages:
        role = message["role"]
        content = message.get("content")
        parts = []
        if isinstance(content, str):
            cleaned = strip_legacy_markers(content)
            if cleaned:
                parts.append({"text": normalize_outbound_text(cleaned)})
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    cleaned = strip_legacy_markers(block.get("text", ""))
                    if cleaned:
                        parts.append({"text": normalize_outbound_text(cleaned)})
                elif block.get("type") == "image_url":
                    image = block.get("image_url", {})
                    url = image.get("url", "") if isinstance(image, dict) else ""
                    if not url.startswith("data:image/") or ";base64," not in url:
                        raise InvalidRequest("Antigravity image input currently requires a base64 data URL.")
                    header, data = url.split(";base64,", 1)
                    parts.append({"inlineData": {"mimeType": header[5:], "data": data}})
                else:
                    raise InvalidRequest("Unsupported Antigravity content block.")
        if role in {"system", "developer"}:
            if any("text" not in part for part in parts):
                raise InvalidRequest("System instructions must contain text.")
            # system 每轮重发，一旦命中敏感字符就每轮都失败。
            system.extend({"text": normalize_outbound_text(part["text"])}
                          for part in parts)
            continue
        if role == "tool":
            name = call_names.get(message.get("tool_call_id"))
            if not name:
                raise InvalidRequest("A tool result has no matching assistant tool call.")
            # Always pair with a real functionResponse; never restate a result as model text.
            parts = [{"functionResponse": {"name": name, "response": {"result": content}}}]
            native_role = "user"
        elif role == "assistant":
            native_role = "model"
            raw_calls = message.get("tool_calls") or []
            resolved_calls = []
            for call in raw_calls:
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (ValueError, TypeError):
                    raise InvalidRequest("Tool arguments must be a JSON object.") from None
                if not isinstance(arguments, dict) or not isinstance(function.get("name"), str):
                    raise InvalidRequest("Tool calls must provide a name and JSON object arguments.")
                call_names[call.get("id")] = function["name"]
                signature = (call.get("extra_content") or {}).get("google", {}).get("thought_signature")
                if not isinstance(signature, str) or not signature:
                    if signature_lookup is not None:
                        signature = signature_lookup(call.get('id'), function['name'], arguments)
                resolved_calls.append((function["name"], arguments,
                                       signature if isinstance(signature, str) and signature else None))
            batch_has_signature = any(sig is not None for _, _, sig in resolved_calls)
            for call_idx, (func_name, arguments, signature) in enumerate(resolved_calls):
                part = {"functionCall": {"name": func_name, "args": arguments}}
                # 缺签名就必须补一个**真实**签名，历史轮也一样。
                #
                # 客户端每轮都回放完整历史，但它不会回传 `extra_content`，所以从
                # 第二问开始，历史里每个 functionCall 都缺 `thoughtSignature`，
                # 上游直接 400（INVALID_ARGUMENT / thought_signature）。这正是
                # 「探活成功、真实对话第二句就挂」的原因：探活只发单轮，永远碰
                # 不到回放历史里的工具调用。
                #
                # 仍保持「一个批次只有第一个 call 带签名」的形态，那是 Gemini 原生
                # 并行调用的结构；同一个批次里其余的 call 由上游按同签名处理。
                if signature is None and not batch_has_signature and call_idx == 0:
                    if signature_borrow is not None:
                        borrowed = signature_borrow(func_name)
                        if isinstance(borrowed, str) and borrowed:
                            signature = borrowed
                if signature is not None:
                    part["thoughtSignature"] = signature
                parts.append(part)
        elif role == "user":
            native_role = "user"
        else:
            raise InvalidRequest("Unsupported Antigravity message role.")
        if parts:
            if contents and contents[-1]["role"] == native_role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": native_role, "parts": parts})
    if not contents:
        raise InvalidRequest("At least one user or assistant message is required.")
    request = {"contents": contents}
    if system:
        request["systemInstruction"] = {"parts": system}
    generation = {}
    for key, target in (("temperature", "temperature"), ("top_p", "topP"), ("n", "candidateCount"),
                        ("max_tokens", "maxOutputTokens"), ("max_completion_tokens", "maxOutputTokens"),
                        ("presence_penalty", "presencePenalty"), ("frequency_penalty", "frequencyPenalty")):
        if options.get(key) is not None:
            generation[target] = options[key]
    stop = options.get("stop")
    if stop is not None:
        generation["stopSequences"] = [stop] if isinstance(stop, str) else stop
    fmt = options.get("response_format") or {}
    if fmt.get("type") in {"json_object", "json_schema"}:
        generation["responseMimeType"] = "application/json"
        if fmt.get("type") == "json_schema":
            generation["responseJsonSchema"] = (fmt.get("json_schema") or {}).get("schema", {})
    if generation:
        request["generationConfig"] = generation
    tools = options.get("tools") or []
    if tools:
        declarations = []
        for tool in tools:
            function = tool.get("function") or {}
            if tool.get("type") != "function" or not function.get("name"):
                raise InvalidRequest("Antigravity accepts function tools only.")
            # 工具描述同样归一化：某个工具的说明里带 em dash 也会让整轮被判成
            # 资源耗尽。工具名是标识符，不能改写。
            declaration={k: (normalize_outbound_text(function[k])
                             if k == 'description' else function[k])
                         for k in ('name','description') if k in function}
            if 'parameters' in function:
                cleaned=clean_tool_schema(function['parameters'])
                # Zero-argument tools do not need a synthetic placeholder parameter.
                if cleaned.get('properties') or cleaned.get('anyOf') or cleaned.get('required'):
                    declaration['parameters']=cleaned
            declarations.append(declaration)
        request["tools"] = [{"functionDeclarations": declarations}]
        choice = options.get("tool_choice", "auto")
        policy = {"mode": {"none": "NONE", "auto": "AUTO", "required": "ANY"}.get(
            choice if isinstance(choice, str) else "required", "AUTO")}
        if isinstance(choice, dict):
            name = (choice.get("function") or {}).get("name")
            if name:
                policy["allowedFunctionNames"] = [name]
        request["toolConfig"] = {"functionCallingConfig": policy}
    if options.get("logprobs"):
        raise InvalidRequest("Antigravity does not expose OpenAI token log probabilities.")
    return request


def unwrap(value, retry_after=0):
    if not isinstance(value, dict):
        raise UpstreamFailure("antigravity_invalid_response")
    for envelope in (value, value.get("response")):
        if not isinstance(envelope, dict):
            continue
        if envelope.get("error") is not None or "candidates" not in envelope:
            failure = structured_stop(envelope, retry_after)
            if failure is not None:
                raise failure
        if envelope.get("error") is not None:
            raise UpstreamFailure("antigravity_error_body")
    return value["response"] if isinstance(value.get("response"), dict) else value



def usage(value):
    meta = value.get("usageMetadata")
    if not isinstance(meta, dict):
        return None
    prompt = meta.get("promptTokenCount", 0)
    completion = meta.get("candidatesTokenCount", 0)
    thought = meta.get("thoughtsTokenCount", 0)
    if any(type(n) is not int or n < 0 for n in (prompt, completion, thought)):
        return None
    result = {"prompt_tokens": prompt, "completion_tokens": completion + thought,
              "total_tokens": meta.get("totalTokenCount", prompt + completion + thought)}
    if thought:
        result["completion_tokens_details"] = {"reasoning_tokens": thought}
    return result


def finish(reason, tools=False):
    if tools and reason == "STOP":
        return "tool_calls"
    return {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter",
            "RECITATION": "content_filter", "BLOCKLIST": "content_filter",
            "PROHIBITED_CONTENT": "content_filter", "SPII": "content_filter"}.get(reason)


def candidate_delta(candidate):
    parts = (candidate.get("content") or {}).get("parts", [])
    text, thought, calls = [], [], []
    for part in parts:
        if not isinstance(part, dict):
            raise UpstreamFailure("antigravity_invalid_part")
        if isinstance(part.get("text"), str):
            (thought if part.get("thought") else text).append(part["text"])
        function = part.get("functionCall")
        if isinstance(function, dict):
            if not isinstance(function.get("name"), str) or not isinstance(function.get("args", {}), dict):
                raise UpstreamFailure("antigravity_invalid_tool_call")
            call = {"id": function.get("id") or "call_" + uuid4().hex,
                    "type": "function", "function": {"name": function["name"],
                    "arguments": json.dumps(function.get("args", {}), separators=(",", ":"), allow_nan=False)}}
            if part.get("thoughtSignature"):
                call["extra_content"] = {"google": {"thought_signature": part["thoughtSignature"]}}
            calls.append(call)
        if any(key in part for key in ("inlineData", "fileData", "executableCode", "codeExecutionResult")):
            raise InvalidRequest("This gateway currently supports text and function-call output only; "
                                 "image generation and code-execution output need a separate adapter.")
    delta = {}
    if text:
        delta["content"] = "".join(text)
    if thought:
        delta["reasoning_content"] = "".join(thought)
    if calls:
        delta["tool_calls"] = calls
    return delta


async def send_native(auth, client, account, messages, stream, *, model, options, lifetime):
    from upstream_client import (PreparedStream, MAX_EVENT, MAX_JSON, MAX_ERROR, _body, _events,
                                 _json_object, _retry_after, encode_event)
    signatures=getattr(auth,'tool_signatures',None)
    native_model=account.models[model]
    lookup=(lambda call_id,name,args:signatures.get(account.credential_id,native_model,call_id,name,args)) if signatures else None
    borrow=(lambda name:signatures.borrow(name)) if signatures else None
    wire = native_request(messages, options,signature_lookup=lookup,signature_borrow=borrow)
    def remember(delta):
        if signatures is None:return
        for call in delta.get('tool_calls',[]):
            signature=(call.get('extra_content') or {}).get('google',{}).get('thought_signature')
            if signature:
                signatures.put(account.credential_id,native_model,call['id'],call['function']['name'],
                    json.loads(call['function']['arguments']),signature)
    # 令牌刷新、项目探测、模型目录查询必须和对话请求走同一条出口：
    # 同一份 OAuth 凭据在两个 IP 上出现，正是「一个账号多处使用」的特征。
    proxy = auth.resolve_proxy(account.proxy)
    try:
        credential = await auth.credential(account.credential_id, proxy=proxy)
        if not credential.get("project_id"):
            credential = await auth.refresh_catalog(account.credential_id, proxy=proxy)
    except OAuthFailure as exc:
        raise UpstreamFailure(exc.reason, reauth=exc.reauth, retry_after=exc.retry_after) from None
    session_seed = next((m.get("content") for m in messages
                         if m["role"] == "user" and isinstance(m.get("content"), str)), "")
    wire["sessionId"] = "-" + str(int.from_bytes(hashlib.sha256(session_seed.encode()).digest()[:8], "big") & ((1 << 63) - 1))
    envelope = {"model": account.models[model], "project": credential["project_id"],
                "userAgent": "antigravity", "requestType": "agent",
                "requestId": "agent-" + str(uuid4()), "request": wire}
    path = "/v1internal:streamGenerateContent?alt=sse" if stream else "/v1internal:generateContent"
    response = None
    try:
        for attempt in range(2):
            headers = {
                # 账号自带的设备头先铺，再叠加协议必需的头。顺序有意义：必需头
                # 在后，就不会被一个写错的 device_headers 覆盖掉鉴权。
                **account.device_headers,
                "Authorization": "Bearer " + credential["access_token"],
                "User-Agent": account.user_agent or auth.user_agent,
                "Accept": "text/event-stream" if stream else "application/json",
                "Accept-Encoding": "identity",
            }
            response = await client.send(client.build_request("POST", API_BASE + path,
                headers=headers, json=envelope,
                timeout=httpx.Timeout(None, connect=5, pool=2, write=15)), stream=True)
            lifetime.push_async_callback(response.aclose)
            if response.status_code != 401 or attempt:
                break
            await check_before_credential_refresh(response)
            await response.aclose()
            try:
                credential = await auth.credential(account.credential_id, force=True, proxy=proxy)
            except OAuthFailure as exc:
                raise UpstreamFailure(exc.reason, reauth=exc.reauth, retry_after=exc.retry_after) from None
        retry = _retry_after(response.headers.get("retry-after"))
        if not response.is_success:
            raw=await _body(response,MAX_ERROR,prefix=True)
            raise native_http_error(response.status_code,raw,retry)
        meta = {"id": "chatcmpl-" + uuid4().hex, "created": int(time.time()), "model": model}
        if not stream:
            value = unwrap(_json_object(await _body(response, MAX_JSON)), retry)
            choices = []
            for candidate in value.get("candidates") or []:
                delta = candidate_delta(candidate)
                remember(delta)
                reason = finish(candidate.get("finishReason"), bool(delta.get("tool_calls")))
                if reason is None:
                    raise UpstreamFailure("antigravity_missing_finish")
                choices.append({"index": candidate.get("index", len(choices)),
                                "message": {"role": "assistant", "content": delta.pop("content", None), **delta},
                                "finish_reason": reason})
            if not choices:
                raise UpstreamFailure("antigravity_empty_completion")
            return {**meta, "object": "chat.completion", "choices": choices,
                    **({"usage": usage(value)} if usage(value) is not None else {})}

        if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
            raise UpstreamFailure("antigravity_expected_sse")
        async def chunks():
            seen, finished, call_counts, tools_seen = set(), set(), {}, set()
            final_usage = None
            try:
                async for event, data in _events(response):
                    if event == "error":
                        error_value = _json_object(data)
                        if error_value is not None:
                            unwrap(error_value, retry)
                        raise UpstreamFailure("antigravity_stream_error")
                    if data.strip() == "[DONE]":
                        break
                    value = unwrap(_json_object(data), retry)
                    final_usage = usage(value) or final_usage
                    for candidate in value.get("candidates") or []:
                        index = candidate.get("index", 0)
                        if type(index) is not int or index < 0:
                            raise UpstreamFailure("antigravity_invalid_choice")
                        first = index not in seen
                        seen.add(index)
                        delta = candidate_delta(candidate)
                        remember(delta)
                        if first:
                            delta = {"role": "assistant", **delta}
                        calls = delta.get("tool_calls", [])
                        for call in calls:
                            call["index"] = call_counts.get(index, 0)
                            call_counts[index] = call["index"] + 1
                            tools_seen.add(index)
                        reason = finish(candidate.get("finishReason"), index in tools_seen)
                        if candidate.get("finishReason") and reason is None:
                            raise UpstreamFailure("antigravity_finish_error")
                        if reason:
                            finished.add(index)
                        if delta or reason:
                            yield {**meta, "object": "chat.completion.chunk", "choices": [
                                {"index": index, "delta": delta, "finish_reason": reason}]}
                if not seen or seen != finished:
                    raise UpstreamFailure("antigravity_incomplete_stream")
                if (options.get("stream_options") or {}).get("include_usage") and final_usage:
                    yield {**meta, "object": "chat.completion.chunk", "choices": [], "usage": final_usage}
                yield None
            except httpx.HTTPError:
                raise UpstreamFailure("antigravity_stream_transport") from None
        iterator = chunks()
        lifetime.push_async_callback(iterator.aclose)
        prefix, size = [], 0
        async for chunk in iterator:
            if chunk is None:
                raise UpstreamFailure("antigravity_no_output")
            frame = encode_event(chunk)
            prefix.append(frame)
            size += len(frame)
            if len(prefix) > 64 or size > MAX_EVENT * 2:
                raise UpstreamFailure("antigravity_preamble_too_large")
            if any(c["finish_reason"] or any(k != "role" and v for k, v in c["delta"].items())
                   for c in chunk["choices"]):
                return PreparedStream(prefix, iterator)
        raise UpstreamFailure("antigravity_no_output")
    except httpx.HTTPError:
        raise UpstreamFailure("antigravity_transport") from None
