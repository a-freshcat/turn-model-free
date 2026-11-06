"""Native Antigravity text/tool Chat Completions transport.

The public OpenAI response schema is produced here; OAuth lives separately.
"""

import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from uuid import uuid4

import httpx
import anyio

from account_manager import InvalidRequest, UpstreamFailure
from antigravity_auth import API_BASE, OAuthFailure
from gemini_schema import clean_tool_schema

GENERATED_DIR = Path(__file__).resolve().parent / "web" / "generated"
LOGGER = logging.getLogger(__name__)
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def is_image_model(native_model: str) -> bool:
    return isinstance(native_model, str) and "image" in native_model.lower()


def save_generated_image(mime_type: str, b64_data: str, output_path: str | None = None) -> dict:
    try:
        raw_bytes = base64.b64decode(b64_data, validate=False)
    except Exception as exc:
        raise UpstreamFailure("antigravity_invalid_image_data") from exc
    if not raw_bytes:
        raise UpstreamFailure("antigravity_empty_image_data")
    ext = _MIME_EXT.get((mime_type or "").lower().strip(), ".jpg")
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"img_{int(time.time())}_{uuid4().hex[:8]}{ext}"
    saved_path = GENERATED_DIR / filename
    saved_path.write_bytes(raw_bytes)
    final_local_path = saved_path
    if isinstance(output_path, str) and output_path.strip():
        try:
            target = Path(output_path.strip()).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw_bytes)
            final_local_path = target
        except OSError:
            pass
    url = f"http://127.0.0.1:8199/assets/generated/{filename}"
    return {
        "url": url,
        "local_path": str(final_local_path).replace("\\", "/"),
        "gateway_path": str(saved_path).replace("\\", "/"),
        "mime_type": mime_type or "image/jpeg",
    }


def native_http_error(status, raw, retry_after=0):
    """Do not convert caller/schema validation into an unhealthy account.

    Expose only known protocol field names; upstream descriptions may quote
    prompts, tools or credentials and must never cross the API boundary.
    """
    from upstream_client import _is_gate_body, _json_object
    value=_json_object(raw) or {}
    error=value.get('error') if isinstance(value.get('error'),dict) else value
    codes=[item.get('code') for item in (value,error,value.get('data')) if isinstance(item,dict)]
    if any(code in (11140,'11140') for code in codes):
        return UpstreamFailure('account_restricted',reauth=True,retry_after=retry_after)
    if _is_gate_body(raw.decode('utf-8','replace')):
        return UpstreamFailure('channel_11128',retry_after=retry_after)
    if status==400:
        upstream_status=error.get('status')
        if upstream_status in ('PERMISSION_DENIED','UNAUTHENTICATED','RESOURCE_EXHAUSTED'):
            mapped={'PERMISSION_DENIED':403,'UNAUTHENTICATED':401,'RESOURCE_EXHAUSTED':429}[upstream_status]
            return UpstreamFailure('antigravity_http_'+str(mapped),retry_after=retry_after,reauth=mapped==401)
        allowed={'request','generation_config','generationConfig','max_output_tokens','maxOutputTokens',
            'contents','parts','role','text','tools','functionDeclarations','function_declarations',
            'parameters','properties','required','type','thinking_config','thinkingConfig',
            'thinking_budget','thinkingBudget','system_instruction','systemInstruction','model','project',
            'tool_config','toolConfig','tool_choice','toolChoice','functionCallingConfig',
            'function_calling_config','functionCall','function_call','thoughtSignature',
            'thought_signature','responseJsonSchema','response_schema','responseSchema',
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
                      'functionDeclarations','function_declarations','parametersJsonSchema',
                      'thoughtSignature','thought_signature','tool_choice','toolChoice'):
            if field in message and field not in fields:fields.append(field)
        label=upstream_status if upstream_status in ('INVALID_ARGUMENT','FAILED_PRECONDITION','OUT_OF_RANGE') else 'BAD_REQUEST'
        hint=('；检查字段：'+', '.join(fields[:6])) if fields else '；请检查模型、工具声明和生成参数'
        if re.search(r'\btool_choice\b',message,re.I) and re.search(r'\bany\b',message,re.I) and 'not supported' in message.lower():
            hint='；该上游模型不支持强制工具调用（tool_choice=required 或指定函数），可改用 auto'
        return InvalidRequest('上游请求参数校验失败（HTTP 400 / '+label+'）'+hint+'。账号未因此进入冷却。')
    return UpstreamFailure('antigravity_http_'+str(status),retry_after=retry_after,reauth=status==401)


def supports_native_search(native_model: str) -> bool:
    """实测仅 Gemini 系列模型在 Antigravity v1internal 端点支持原生 googleSearch。"""
    return isinstance(native_model, str) and "gemini" in native_model.lower()


def _is_native_search_tool(tool) -> bool:
    if not isinstance(tool, dict):
        return False
    if "googleSearch" in tool or "google_search" in tool:
        return True
    return tool.get("type") in {"google_search", "web_search_preview"}


def wants_native_search(options: dict) -> bool:
    if not isinstance(options, dict):
        return False
    if options.get("enable_search") is True:
        return True
    if options.get("web_search_options") or options.get("google_search"):
        return True
    return any(_is_native_search_tool(t) for t in (options.get("tools") or []))


def extract_grounding(candidate: dict) -> dict | None:
    meta = candidate.get("groundingMetadata") if isinstance(candidate, dict) else None
    if not isinstance(meta, dict) or not meta:
        return None
    raw_chunks = meta.get("groundingChunks") or []
    raw_supports = meta.get("groundingSupports") or []
    snippets_by_idx: dict[int, list[str]] = {}
    for sup in raw_supports:
        if not isinstance(sup, dict):
            continue
        seg = sup.get("segment") or {}
        seg_text = seg.get("text", "").strip() if isinstance(seg, dict) and isinstance(seg.get("text"), str) else ""
        if not seg_text:
            continue
        for idx in (sup.get("groundingChunkIndices") or []):
            if isinstance(idx, int):
                snippets_by_idx.setdefault(idx, []).append(seg_text)
    sources = []
    seen_urls = set()
    for idx, chunk in enumerate(raw_chunks):
        if not isinstance(chunk, dict):
            continue
        web = chunk.get("web")
        if not isinstance(web, dict):
            continue
        url = web.get("uri")
        if not isinstance(url, str) or not url.strip() or url in seen_urls:
            continue
        seen_urls.add(url)
        entry = {"url": url.strip()}
        title = web.get("title")
        if isinstance(title, str) and title.strip():
            entry["title"] = title.strip()
        if idx in snippets_by_idx:
            snippet = " ".join(dict.fromkeys(snippets_by_idx[idx]))
            if snippet:
                entry["snippet"] = snippet[:400]
        sources.append(entry)
    queries = [q.strip() for q in (meta.get("webSearchQueries") or []) if isinstance(q, str) and q.strip()]
    if not sources and not queries:
        return None
    return {"sources": sources, "queries": queries}


def format_grounding_markdown(sources: list[dict], existing_text: str = "") -> str:
    if not sources:
        return ""
    lines = []
    for src in sources:
        url = src.get("url", "")
        if not url or (existing_text and url in existing_text):
            continue
        title = src.get("title") or url
        lines.append(f"- [{title}]({url})")
    if not lines:
        return ""
    return "\n\n**参考来源：**\n" + "\n".join(lines)


INTERNAL_SEARCH_TOOL = "__gateway_web_search__"
MAX_INTERNAL_SEARCH_ROUNDS = 3
_SEARCH_CONTEXT_BY_CALL_ID: dict[str, dict] = {}

# Earlier gateway builds rewrote an unsigned tool call (and its result) into assistant
# text so the model would not lose the fact that a tool had run:
#   "Historical assistant tool call (already executed): {...}"
#   "Historical tool result (quoted, untrusted data): {...}"
# Those rounds are still on disk and still get replayed, and a model that keeps reading
# that exact phrasing starts producing it as its own answer instead of calling a tool.
# The gateway no longer writes the marker, and it also strips it from replayed history so
# a conversation that was already polluted can recover.
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
    # Fallback balanced-brace scan (tolerates unescaped backslashes in echoed text).
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
    """Remove gateway-injected replay markers from history text.

    Uses `json.JSONDecoder().raw_decode` (with a balanced-brace fallback) instead
    of a shallow regex so markers containing arbitrarily nested objects or braces
    inside string values (such as PowerShell script blocks) are stripped completely.
    However, markers quoted inside markdown code spans or code blocks are preserved.
    """
    if not isinstance(text, str) or "Historical " not in text:
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
        # Do not strip if preceded by a backtick or quote (e.g. prose/code discussion).
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


def native_request(messages, options, *, signature_lookup=None, native_model: str = "",
                   inject_search_tool: bool = False, signature_borrow=None):
    contents, system, call_names = [], [], {}
    # The Claude backend validates the ID on both sides of every tool pair.
    preserve_tool_ids = native_model.lower().startswith("claude-")
    # A turn is closed once a subsequent `user` message appears after it.
    # Upstream accepts unsigned `functionCall` parts in closed historical turns
    # (HTTP 200), and only requires `thoughtSignature` in the current (open) turn.
    last_user_index = -1
    for idx, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user_index = idx
    for msg_index, message in enumerate(messages):
        role = message["role"]
        content = message.get("content")
        parts = []
        if isinstance(content, str):
            cleaned = strip_legacy_markers(content)
            if cleaned:
                parts.append({"text": cleaned})
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    cleaned = strip_legacy_markers(block.get("text", ""))
                    if cleaned:
                        parts.append({"text": cleaned})
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
            system.extend(parts)
            continue
        if role == "tool":
            name = call_names.get(message.get("tool_call_id"))
            if not name:
                raise InvalidRequest("A tool result has no matching assistant tool call.")
            # Always pair with a real functionResponse. Never restate a tool result as
            # assistant/model text: that text is replayed forever and the model starts
            # imitating it.
            parts = [{"functionResponse": {"name": name, "response": {"result": content}}}]
            if preserve_tool_ids:
                parts[0]["functionResponse"]["id"] = message["tool_call_id"]
            native_role = "user"
        elif role == "assistant":
            native_role = "model"
            is_open_turn = msg_index > last_user_index
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
                resolved_calls.append((call.get("id"), function["name"], arguments,
                                       signature if isinstance(signature, str) and signature else None))
            # Only borrow a fallback signature when:
            # 1) this assistant message belongs to the current (open) turn, AND
            # 2) NONE of the calls in this assistant batch has a real signature yet, AND
            # 3) we only attach the borrowed signature to the FIRST call in the batch
            #    (matching Gemini's native parallel-call structure where only the first
            #    functionCall part carries `thoughtSignature`).
            batch_has_signature = any(sig is not None for _, _, _, sig in resolved_calls)
            for call_idx, (call_id, func_name, arguments, signature) in enumerate(resolved_calls):
                part = {"functionCall": {"name": func_name, "args": arguments}}
                if preserve_tool_ids:
                    if not isinstance(call_id, str) or not call_id:
                        raise InvalidRequest("Claude tool history requires a tool call ID.")
                    part["functionCall"]["id"] = call_id
                if signature is None and is_open_turn and not batch_has_signature and call_idx == 0:
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
    search_requested = wants_native_search(options)
    if search_requested and native_model and not supports_native_search(native_model):
        raise InvalidRequest(
            f"当前模型 ({native_model}) 不支持 Antigravity 原生联网搜索（仅 Gemini 系列模型支持）。账号未进入冷却。"
        )
    declarations = []
    for tool in tools:
        if _is_native_search_tool(tool):
            continue
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(function, dict) or not function.get("name"):
            raise InvalidRequest("Antigravity accepts function or google_search tools only.")
        declaration={k:function[k] for k in ('name','description') if k in function}
        if 'parameters' in function:
            cleaned=clean_tool_schema(function['parameters'])
            # Zero-argument tools do not need a synthetic placeholder parameter.
            if cleaned.get('properties') or cleaned.get('anyOf') or cleaned.get('required'):
                declaration['parameters']=cleaned
        declarations.append(declaration)
    if declarations and not is_image_model(native_model):
        choice = options.get("tool_choice", "auto")
        if search_requested:
            if options.get("allow_proxied_search") is False or choice == "none":
                raise InvalidRequest(
                    "Antigravity 端点暂不支持在同一请求中同时启用原生 googleSearch 与自定义 function 工具"
                    "（且当前禁用了网关代理搜索或设置了 tool_choice=none）。请二选一，或允许网关自动代理搜索。账号未进入冷却。"
                )
            if inject_search_tool and not any(d.get("name") == INTERNAL_SEARCH_TOOL for d in declarations):
                declarations.append({
                    "name": INTERNAL_SEARCH_TOOL,
                    "description": (
                        "Search the web via Google Search for current facts, releases, documentation, "
                        "or real-time information before answering or calling other tools."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "The web search query string."}
                        },
                        "required": ["query"],
                    },
                })
        request["tools"] = [{"functionDeclarations": declarations}]
        policy = {"mode": {"none": "NONE", "auto": "AUTO", "required": "ANY"}.get(
            choice if isinstance(choice, str) else "required", "AUTO")}
        if isinstance(choice, dict):
            name = (choice.get("function") or {}).get("name")
            if name:
                policy["allowedFunctionNames"] = [name]
        request["toolConfig"] = {"functionCallingConfig": policy}
    elif search_requested and not is_image_model(native_model):
        request["tools"] = [{"googleSearch": {}}]
    if options.get("logprobs"):
        raise InvalidRequest("Antigravity does not expose OpenAI token log probabilities.")
    return request


def unwrap(value):
    if not isinstance(value, dict):
        raise UpstreamFailure("antigravity_invalid_response")
    if value.get("error") is not None:
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


def finish(reason, tools=False, *, model=None):
    if tools and reason == "STOP":
        return "tool_calls"
    if reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
        LOGGER.warning("Antigravity content filter: model=%s upstream_finish_reason=%s", model, reason)
        return "content_filter"
    return {"STOP": "stop", "MAX_TOKENS": "length"}.get(reason)


def candidate_delta(candidate, *, append_sources_markdown: bool = False,
                    allow_inline_image: bool = False, output_path: str | None = None):
    parts = (candidate.get("content") or {}).get("parts", [])
    text, thought, calls, images = [], [], [], []
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
        inline = part.get("inlineData")
        if isinstance(inline, dict) and allow_inline_image:
            mime_t = str(inline.get("mimeType") or "image/jpeg")
            b64_str = inline.get("data")
            if isinstance(b64_str, str) and b64_str.strip():
                img_info = save_generated_image(mime_t, b64_str, output_path=output_path)
                images.append(img_info)
                md_block = f"![Generated Image]({img_info['url']})\n\n已保存图片至本地：`{img_info['local_path']}`"
                text.append(("\n\n" + md_block) if text else md_block)
                continue
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
    if images:
        delta["images"] = images
    grounding = extract_grounding(candidate)
    if grounding:
        delta["grounding_metadata"] = grounding
        if grounding["sources"]:
            delta["annotations"] = [
                {
                    "type": "url_citation",
                    "url_citation": {
                        "url": src["url"],
                        "title": src.get("title", src["url"]),
                    },
                }
                for src in grounding["sources"]
            ]
            if append_sources_markdown:
                citation_md = format_grounding_markdown(grounding["sources"], delta.get("content") or "")
                if citation_md:
                    delta["content"] = (delta.get("content") or "") + citation_md
    return delta


def _merge_grounding(delta: dict, grounding: dict, *, append_markdown: bool) -> None:
    sources = grounding.get("sources") or []
    queries = grounding.get("queries") or []
    if not sources and not queries:
        return
    existing = delta.get("grounding_metadata") or {"sources": [], "queries": []}
    seen_urls = {s["url"] for s in existing.get("sources", []) if isinstance(s, dict) and s.get("url")}
    merged_sources = list(existing.get("sources", []))
    for src in sources:
        if isinstance(src, dict) and src.get("url") and src["url"] not in seen_urls:
            seen_urls.add(src["url"])
            merged_sources.append(src)
    merged_queries = list(dict.fromkeys((existing.get("queries") or []) + queries))
    delta["grounding_metadata"] = {"sources": merged_sources, "queries": merged_queries}
    if merged_sources:
        delta["annotations"] = [
            {
                "type": "url_citation",
                "url_citation": {"url": s["url"], "title": s.get("title", s["url"])},
            }
            for s in merged_sources
        ]
        if append_markdown and not delta.get("tool_calls"):
            citation_md = format_grounding_markdown(merged_sources, delta.get("content") or "")
            if citation_md:
                delta["content"] = (delta.get("content") or "") + citation_md


async def send_native(auth, client, account, messages, stream, *, model, options, lifetime):
    from upstream_client import (PreparedStream, MAX_EVENT, MAX_JSON, MAX_ERROR, _body, _events,
                                 _json_object, _retry_after, encode_event)
    signatures=getattr(auth,'tool_signatures',None)
    native_model=account.models[model]
    lookup=(lambda call_id,name,args:signatures.get(account.credential_id,native_model,call_id,name,args)) if signatures else None
    borrow=(lambda name:signatures.borrow(name)) if signatures else None
    has_func_tools = any(isinstance(t, dict) and not _is_native_search_tool(t) for t in (options.get("tools") or []))
    choice_opt = options.get("tool_choice", "auto")
    pinned_user_func = (choice_opt.get("function") or {}).get("name") if isinstance(choice_opt, dict) else None
    proxied_search = bool(wants_native_search(options) and has_func_tools and not pinned_user_func)
    # 先做一次参数与模型能力校验（若禁用代理搜索或模型不支持则立即抛 InvalidRequest 400，不冷却账号）
    wire = native_request(messages, options, signature_lookup=lookup, native_model=native_model,
                          inject_search_tool=proxied_search, signature_borrow=borrow)
    allow_inline_img = is_image_model(native_model) or bool(options.get("allow_image_output"))
    search_active = wants_native_search(options) and (
        proxied_search or "googleSearch" in json.dumps(wire.get("tools") or [])
    )
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
    session_id = "-" + str(int.from_bytes(hashlib.sha256(session_seed.encode()).digest()[:8], "big") & ((1 << 63) - 1))

    working_messages = []
    collected_grounding: dict = {"sources": [], "queries": []}
    search_summaries: list[str] = []
    restored_from_call = False
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") in _SEARCH_CONTEXT_BY_CALL_ID:
            cached = _SEARCH_CONTEXT_BY_CALL_ID.get(m["tool_call_id"]) or {}
            restored_from_call = True
            for src in (cached.get("grounding") or {}).get("sources") or []:
                if src.get("url") and not any(x.get("url") == src["url"] for x in collected_grounding["sources"]):
                    collected_grounding["sources"].append(src)
            for q_item in (cached.get("grounding") or {}).get("queries") or []:
                if q_item not in collected_grounding["queries"]:
                    collected_grounding["queries"].append(q_item)
            extra_ctx = "\n\n".join(cached.get("summaries") or [])
            if extra_ctx and isinstance(m.get("content"), str):
                m = {**m, "content": f"{m['content']}\n\n[Web Search Context]:\n{extra_ctx}"}
        working_messages.append(m)

    if proxied_search and not restored_from_call:
        for _round in range(MAX_INTERNAL_SEARCH_ROUNDS):
            round_wire = native_request(working_messages, options, signature_lookup=lookup,
                                        native_model=native_model, inject_search_tool=True,
                                        signature_borrow=borrow)
            round_wire["sessionId"] = session_id
            round_env = {"model": native_model, "project": credential["project_id"],
                         "userAgent": "antigravity", "requestType": "agent",
                         "requestId": "agent-" + str(uuid4()), "request": round_wire}
            probe_resp = None
            for attempt in range(2):
                probe_resp = await client.send(client.build_request(
                    "POST", API_BASE + "/v1internal:generateContent",
                    headers={"Authorization": "Bearer " + credential["access_token"],
                             "User-Agent": auth.user_agent,
                             "Accept": "application/json",
                             "Accept-Encoding": "identity"},
                    json=round_env,
                    timeout=httpx.Timeout(None, connect=5, pool=2, write=15),
                ), stream=True)
                lifetime.push_async_callback(probe_resp.aclose)
                if probe_resp.status_code != 401 or attempt:
                    break
                await probe_resp.aclose()
                try:
                    credential = await auth.credential(account.credential_id, force=True, proxy=proxy)
                except OAuthFailure as exc:
                    raise UpstreamFailure(exc.reason, reauth=exc.reauth, retry_after=exc.retry_after) from None
            if not probe_resp.is_success:
                retry = _retry_after(probe_resp.headers.get("retry-after"))
                raw = await _body(probe_resp, MAX_ERROR, prefix=True)
                raise native_http_error(probe_resp.status_code, raw, retry)
            round_val = unwrap(_json_object(await _body(probe_resp, MAX_JSON)))
            await probe_resp.aclose()
            cands = round_val.get("candidates") or []
            if not cands:
                raise UpstreamFailure("antigravity_empty_completion")
            cand0 = cands[0]
            delta0 = candidate_delta(cand0, append_sources_markdown=False)
            remember(delta0)
            all_calls = delta0.get("tool_calls") or []
            internal_calls = [c for c in all_calls if (c.get("function") or {}).get("name") == INTERNAL_SEARCH_TOOL]
            user_calls = [c for c in all_calls if (c.get("function") or {}).get("name") != INTERNAL_SEARCH_TOOL]
            if not internal_calls:
                # 模型已完成内部搜索（或无需搜索），直接返回结果或外部工具调用
                if user_calls and (collected_grounding["sources"] or search_summaries):
                    while len(_SEARCH_CONTEXT_BY_CALL_ID) >= 64:
                        _SEARCH_CONTEXT_BY_CALL_ID.pop(next(iter(_SEARCH_CONTEXT_BY_CALL_ID)))
                    for ucall in user_calls:
                        _SEARCH_CONTEXT_BY_CALL_ID[ucall["id"]] = {
                            "grounding": collected_grounding,
                            "summaries": list(search_summaries),
                        }
                _merge_grounding(delta0, collected_grounding, append_markdown=True)
                reason = finish(cand0.get("finishReason"), bool(user_calls), model=model)
                if reason is None:
                    raise UpstreamFailure("antigravity_missing_finish")
                meta = {"id": "chatcmpl-" + uuid4().hex, "created": int(time.time()), "model": model}
                final_usage = usage(round_val)
                if not stream:
                    choice_msg = {"role": "assistant", "content": delta0.pop("content", None), **delta0}
                    return {**meta, "object": "chat.completion",
                            "choices": [{"index": 0, "message": choice_msg, "finish_reason": reason}],
                            **({"usage": final_usage} if final_usage is not None else {})}
                stream_delta = {"role": "assistant", **delta0}
                for idx_c, call in enumerate(stream_delta.get("tool_calls") or []):
                    call["index"] = idx_c
                chunk_frame = encode_event({
                    **meta, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": stream_delta, "finish_reason": reason}],
                })
                async def _rest():
                    if (options.get("stream_options") or {}).get("include_usage") and final_usage:
                        yield {**meta, "object": "chat.completion.chunk", "choices": [], "usage": final_usage}
                    yield None
                it = _rest()
                lifetime.push_async_callback(it.aclose)
                return PreparedStream([chunk_frame], it)

            # 执行网关内部搜索并将结果回填到对话上下文
            working_messages.append({
                "role": "assistant",
                "content": delta0.get("content"),
                "tool_calls": internal_calls,
            })
            for icall in internal_calls:
                try:
                    q_obj = json.loads((icall.get("function") or {}).get("arguments") or "{}")
                    q_str = str(q_obj.get("query") or "").strip()
                except (ValueError, TypeError):
                    q_str = ""
                try:
                    s_res = await execute_native_search(auth, client, account, q_str or session_seed, max_results=6)
                    if s_res.get("content"):
                        search_summaries.append(s_res["content"])
                    for src in s_res.get("sources") or []:
                        if src.get("url") and not any(x.get("url") == src["url"] for x in collected_grounding["sources"]):
                            collected_grounding["sources"].append(src)
                    for q_item in s_res.get("queries") or []:
                        if q_item not in collected_grounding["queries"]:
                            collected_grounding["queries"].append(q_item)
                    tool_payload = json.dumps({
                        "content": s_res.get("content", ""),
                        "sources": s_res.get("sources", []),
                    }, ensure_ascii=False)
                except InvalidRequest as exc:
                    tool_payload = json.dumps({"error": str(exc), "sources": []}, ensure_ascii=False)
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": icall["id"],
                    "content": tool_payload,
                })
        # 已达最大内部搜索轮数，最后一轮不再注入内部搜索工具
        wire = native_request(working_messages, options, signature_lookup=lookup,
                              native_model=native_model, inject_search_tool=False,
                              signature_borrow=borrow)
    elif restored_from_call:
        wire = native_request(working_messages, options, signature_lookup=lookup,
                              native_model=native_model, inject_search_tool=False,
                              signature_borrow=borrow)

    wire["sessionId"] = session_id
    envelope = {"model": account.models[model], "project": credential["project_id"],
                "userAgent": "antigravity", "requestType": "agent",
                "requestId": "agent-" + str(uuid4()), "request": wire}
    path = "/v1internal:streamGenerateContent?alt=sse" if stream else "/v1internal:generateContent"
    response = None
    try:
        for attempt in range(2):
            for connection_attempt in range(2):
                request = client.build_request("POST", API_BASE + path,
                    headers={"Authorization": "Bearer " + credential["access_token"],
                             "User-Agent": auth.user_agent,
                             "Accept": "text/event-stream" if stream else "application/json",
                             "Accept-Encoding": "identity"}, json=envelope,
                    # The route owns the response deadline.
                    timeout=httpx.Timeout(None, connect=5, pool=2, write=15))
                try:
                    response = await client.send(request, stream=True)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if connection_attempt:
                        raise
                    await anyio.sleep(0.25)
            lifetime.push_async_callback(response.aclose)
            if response.status_code != 401 or attempt:
                break
            await response.aclose()
            try:
                credential = await auth.credential(account.credential_id, force=True, proxy=proxy)
            except OAuthFailure as exc:
                raise UpstreamFailure(exc.reason, reauth=exc.reauth, retry_after=exc.retry_after) from None
        if not response.is_success:
            retry = _retry_after(response.headers.get("retry-after"))
            raw=await _body(response,MAX_ERROR,prefix=True)
            raise native_http_error(response.status_code,raw,retry)
        meta = {"id": "chatcmpl-" + uuid4().hex, "created": int(time.time()), "model": model}
        if not stream:
            value = unwrap(_json_object(await _body(response, MAX_JSON)))
            choices = []
            for candidate in value.get("candidates") or []:
                delta = candidate_delta(candidate, append_sources_markdown=search_active,
                                        allow_inline_image=allow_inline_img)
                _merge_grounding(delta, collected_grounding, append_markdown=search_active)
                remember(delta)
                reason = finish(candidate.get("finishReason"), bool(delta.get("tool_calls")), model=model)
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
            accumulated_text: dict[int, list[str]] = {}
            final_usage = None
            try:
                async for event, data in _events(response):
                    if event == "error":
                        raise UpstreamFailure("antigravity_stream_error")
                    if data.strip() == "[DONE]":
                        break
                    value = unwrap(_json_object(data))
                    final_usage = usage(value) or final_usage
                    for candidate in value.get("candidates") or []:
                        index = candidate.get("index", 0)
                        if type(index) is not int or index < 0:
                            raise UpstreamFailure("antigravity_invalid_choice")
                        first = index not in seen
                        seen.add(index)
                        delta = candidate_delta(candidate, append_sources_markdown=False,
                                                allow_inline_image=allow_inline_img)
                        if isinstance(delta.get("content"), str):
                            accumulated_text.setdefault(index, []).append(delta["content"])
                        if search_active and delta.get("grounding_metadata") and delta["grounding_metadata"].get("sources"):
                            joined = "".join(accumulated_text.get(index, []))
                            citation_md = format_grounding_markdown(delta["grounding_metadata"]["sources"], joined)
                            if citation_md:
                                delta["content"] = (delta.get("content") or "") + citation_md
                                accumulated_text.setdefault(index, []).append(citation_md)
                        remember(delta)
                        if first:
                            delta = {"role": "assistant", **delta}
                        calls = delta.get("tool_calls", [])
                        for call in calls:
                            call["index"] = call_counts.get(index, 0)
                            call_counts[index] = call["index"] + 1
                            tools_seen.add(index)
                        reason = finish(candidate.get("finishReason"), index in tools_seen, model=model)
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


async def execute_native_search(auth, client, account, query: str, *, max_results: int = 8) -> dict:
    """使用 Antigravity 原生 googleSearch 执行独立检索，供本地 /v1/search 与 DSH web_search 复用。
    注意：此函数仅抛 InvalidRequest 或返回明确错误描述，绝不触发聊天账号故障计数或冷却。
    """
    from upstream_client import MAX_JSON, MAX_ERROR, _body, _json_object
    q = (query or "").strip()
    if not q:
        raise InvalidRequest("搜索关键词不能为空。")
    # 优先选用轻量快速的 Gemini Flash 模型，不消耗 Claude / Argon 额度
    preferred_native = "gemini-2.5-flash"
    for public_id, nat in (account.models or {}).items():
        if isinstance(nat, str) and "gemini" in nat.lower() and "flash" in nat.lower():
            preferred_native = nat
            break
    proxy = auth.resolve_proxy(account.proxy)
    try:
        credential = await auth.credential(account.credential_id, proxy=proxy)
        if not credential.get("project_id"):
            credential = await auth.refresh_catalog(account.credential_id, proxy=proxy)
    except OAuthFailure as exc:
        raise InvalidRequest(f"Antigravity 搜索凭据不可用（{exc.reason}），请检查账号登录状态。") from None

    prompt = (
        f"Search the web for current, factual information about: {q}\n"
        "Provide a concise, well-structured factual summary with key details, dates, and version numbers."
    )
    wire = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
        "sessionId": "-" + str(int.from_bytes(hashlib.sha256(q.encode()).digest()[:8], "big") & ((1 << 63) - 1)),
    }
    candidates_to_try = [preferred_native]
    if "gemini-2.5-flash" not in candidates_to_try:
        candidates_to_try.insert(0, "gemini-2.5-flash")
    last_error = "unknown"
    for target_model in candidates_to_try:
        envelope = {
            "model": target_model,
            "project": credential["project_id"],
            "userAgent": "antigravity",
            "requestType": "agent",
            "requestId": "agent-" + str(uuid4()),
            "request": wire,
        }
        try:
            response = await client.send(
                client.build_request(
                    "POST",
                    API_BASE + "/v1internal:generateContent",
                    headers={
                        "Authorization": "Bearer " + credential["access_token"],
                        "User-Agent": auth.user_agent,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    json=envelope,
                    timeout=httpx.Timeout(25.0, connect=5.0, pool=2.0, write=10.0),
                ),
                stream=True,
            )
            try:
                if not response.is_success:
                    last_error = f"HTTP {response.status_code}"
                    continue
                value = unwrap(_json_object(await _body(response, MAX_JSON)))
            finally:
                await response.aclose()
            cands = value.get("candidates") or []
            if not cands:
                last_error = "empty_candidates"
                continue
            cand = cands[0]
            delta = candidate_delta(cand, append_sources_markdown=False)
            grounding = delta.get("grounding_metadata") or {"sources": [], "queries": []}
            sources = list(grounding.get("sources") or [])
            truncated = False
            if isinstance(max_results, int) and max_results > 0 and len(sources) > max_results:
                sources = sources[:max_results]
                truncated = True
            return {
                "content": (delta.get("content") or "").strip(),
                "sources": sources,
                "queries": grounding.get("queries") or [q],
                "truncated": truncated,
                "model": target_model,
            }
        except httpx.HTTPError as exc:
            # 只记录异常类型，避免把请求地址、令牌等细节带进对外错误信息。
            last_error = type(exc).__name__
            continue
        except (InvalidRequest, UpstreamFailure, ValueError, KeyError, TypeError) as exc:
            last_error = type(exc).__name__
            continue
    raise InvalidRequest(f"Antigravity 原生搜索暂不可用（{last_error}），未对聊天账号执行冷却。")


async def execute_native_image_generation(
    auth, client, account, prompt: str, *, output_path: str | None = None
) -> dict:
    """使用 Antigravity 内置的 gemini-3.1-flash-image (Nano Banana 2) 生成图片并落盘。
    任何异常只返回明确错误描述，不计入聊天账号故障或触发冷却。
    """
    from upstream_client import MAX_JSON, _body, _json_object
    p = (prompt or "").strip()
    if not p:
        raise InvalidRequest("生图提示词 (prompt) 不能为空。")
    target_model = "gemini-3.1-flash-image"
    for _pub, nat in (account.models or {}).items():
        if isinstance(nat, str) and "image" in nat.lower():
            target_model = nat
            break
    proxy = auth.resolve_proxy(account.proxy)
    try:
        credential = await auth.credential(account.credential_id, proxy=proxy)
        if not credential.get("project_id"):
            credential = await auth.refresh_catalog(account.credential_id, proxy=proxy)
    except OAuthFailure as exc:
        raise InvalidRequest(f"Antigravity 生图凭据不可用（{exc.reason}），请检查账号登录状态。") from None

    wire = {
        "contents": [{"role": "user", "parts": [{"text": p}]}],
        "sessionId": "-" + str(int.from_bytes(hashlib.sha256(p.encode()).digest()[:8], "big") & ((1 << 63) - 1)),
    }
    envelope = {
        "model": target_model,
        "project": credential["project_id"],
        "userAgent": "antigravity",
        "requestType": "agent",
        "requestId": "agent-" + str(uuid4()),
        "request": wire,
    }
    try:
        response = await client.send(
            client.build_request(
                "POST",
                API_BASE + "/v1internal:generateContent",
                headers={
                    "Authorization": "Bearer " + credential["access_token"],
                    "User-Agent": auth.user_agent,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                json=envelope,
                timeout=httpx.Timeout(60.0, connect=5.0, pool=2.0, write=15.0),
            ),
            stream=True,
        )
        try:
            if not response.is_success:
                raise InvalidRequest(f"Antigravity 生图请求返回 HTTP {response.status_code}（账号未进入冷却）。")
            value = unwrap(_json_object(await _body(response, MAX_JSON)))
        finally:
            await response.aclose()
        cands = value.get("candidates") or []
        if not cands:
            raise InvalidRequest("Antigravity 生图未返回候选结果（账号未进入冷却）。")
        delta = candidate_delta(cands[0], append_sources_markdown=False,
                                allow_inline_image=True, output_path=output_path)
        images = delta.get("images") or []
        if not images:
            text_reply = (delta.get("content") or "").strip()
            raise InvalidRequest(f"模型未返回图片数据（回复：{text_reply[:120] or '空'}）。")
        img0 = images[0]
        return {
            "url": img0["url"],
            "local_path": img0["local_path"],
            "gateway_path": img0["gateway_path"],
            "mime_type": img0["mime_type"],
            "content": (delta.get("content") or "").strip(),
            "model": target_model,
        }
    except InvalidRequest:
        raise
    except Exception as exc:
        raise InvalidRequest(f"Antigravity 生图暂不可用（{type(exc).__name__}），未对聊天账号执行冷却。") from None
