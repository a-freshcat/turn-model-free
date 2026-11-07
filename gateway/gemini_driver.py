"""Gemini 官方 API Key（Generative Language API）传输层。

与 Antigravity 共用同一套 Gemini 协议内核 —— `contents` / `parts` /
`functionDeclarations` / `thoughtSignature` / 三池模型名全部一致，差异只有三处：

  1. 地址：`/v1beta/models/<model>:generateContent`，不是内部 `v1internal`
     信封（没有 `{"model","project","request"}` 包装，也没有 `sessionId`）；
  2. 凭据：`x-goog-api-key`，没有 OAuth 令牌、没有 project 探测、没有一次性
     `refresh_token`，所以这条路径**不会**出现 `STS5.1806` 那类掉线；
  3. 响应：直接是 `{"candidates": [...]}`，没有 `response` 包装。

为什么不复用 `antigravity_driver.send_native`：那一条把 OAuth 刷新、project
探测、信封组装写死在一起。把它改成双模式，等于让已经稳定的 Antigravity 路径
跟着一起承担改动风险；这里只复用纯函数（请求体构造、候选帧解析），传输部分
独立一份。

上游错误分类的原则与 Antigravity 一致：**参数错不惩罚账号**。400（除
`API_KEY_INVALID`）抛 `InvalidRequest`，不进入冷却；429 走短退避（官方免费档
的 RPM/TPM 限流是分钟级的，不是账号坏了）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from urllib.parse import quote
from uuid import uuid4

import httpx

from account_manager import InvalidRequest, UpstreamFailure
from antigravity_driver import candidate_delta, finish, native_request, usage
from upstream_client import (MAX_ERROR, MAX_EVENT, MAX_JSON, PreparedStream, _body,
                             _events, _json_object, _retry_after, encode_event)

DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"
API_BASE = (os.environ.get("GEMINI_API_BASE") or DEFAULT_BASE).rstrip("/")

# 模型名只允许字面量 id。它会被拼进 URL path，所以不接受 `/`、`?`、空格。
_MODEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KEY_INVALID = re.compile(
    r"api\s*key\s*(?:not\s*valid|invalid|expired)|API_KEY_INVALID|API_KEY_EXPIRED", re.I)

# 官方 `finishReason` 比内部端点更杂。内部端点认不出来的值在这里补上：
# `OTHER` / `MALFORMED_FUNCTION_CALL` 属于「这一轮就这样结束了」，报成上游故障
# 只会让号池毫无必要地换号重试。
_EXTRA_FINISH = {
    "OTHER": "stop",
    "MALFORMED_FUNCTION_CALL": "stop",
    "UNEXPECTED_TOOL_CALL": "stop",
    "TOO_MANY_TOOL_CALLS": "stop",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "IMAGE_RECITATION": "content_filter",
    "IMAGE_OTHER": "content_filter",
}

# 只把这些字段名回给客户端：详情里可能夹带上游原文、提示词或凭据。
_ALLOWED_FIELDS = {
    "contents", "parts", "role", "text", "tools", "functionDeclarations",
    "function_declarations", "parameters", "properties", "required", "type",
    "thinkingConfig", "thinking_config", "thinkingBudget", "thinking_budget",
    "systemInstruction", "system_instruction", "toolConfig", "tool_config",
    "generationConfig", "generation_config", "responseMimeType", "response_mime_type",
    "responseJsonSchema", "response_json_schema", "responseSchema", "response_schema",
    "temperature", "topP", "top_p", "candidateCount", "candidate_count",
    "maxOutputTokens", "max_output_tokens", "model", "safetySettings",
}


def gemini_finish(reason, tools=False):
    mapped = finish(reason, tools)
    if mapped is None and isinstance(reason, str):
        mapped = _EXTRA_FINISH.get(reason.upper())
    return mapped


def quota_exhaustion(raw: bytes):
    """解析 429 的配额细节，判断是「今天用完了」还是「刚才打太快」。

    官方把这两件事都回 429，但后果差一个数量级：每分钟超限等几十秒，每天超限
    要等十几小时（免费档每个模型每天 20 次）。分不清就会把可用的模型标成坏的，
    或者把当天报废的模型标成好的。只在这些细节确实指向日限时返回说明，其余
    一律当「暂时限流」处理（返回 None）。
    """
    value = _json_object(raw) or {}
    error = value.get("error") if isinstance(value.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), list) else []
    per_day = False
    retry = 0.0
    for detail in details:
        if not isinstance(detail, dict):
            continue
        violations = detail.get("violations")
        for violation in violations if isinstance(violations, list) else []:
            if not isinstance(violation, dict):
                continue
            quota_id = str(violation.get("quotaId") or "")
            description = str(violation.get("description") or "")
            if "PerDay" in quota_id or "per day" in description.lower():
                per_day = True
            try:
                retry = max(retry, float(str(violation.get("quotaValue") or 0)))
            except ValueError:
                pass
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                retry = max(retry, float(delay[:-1]))
            except ValueError:
                pass
    if not per_day:
        return None
    return {"per_day": True, "retry_after": retry}


def gemini_http_error(status: int, raw: bytes, retry_after: float = 0):
    value = _json_object(raw) or {}
    error = value.get("error") if isinstance(value.get("error"), dict) else {}
    upstream_status = str(error.get("status") or "")
    message = str(error.get("message") or "")
    if _KEY_INVALID.search(message) or upstream_status == "UNAUTHENTICATED":
        return UpstreamFailure("gemini_key_invalid", reauth=True, retry_after=retry_after)
    if status in (401, 403):
        return UpstreamFailure("gemini_http_" + str(status), reauth=True, retry_after=retry_after)
    if status == 429:
        # 免费档是每分钟令牌桶。用短退避而不是整轮 600 秒冷却。
        return UpstreamFailure("gemini_http_429", retry_after=retry_after)
    if status == 400:
        fields: list[str] = []
        details = error.get("details") if isinstance(error.get("details"), list) else []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            violations = detail.get("fieldViolations")
            for violation in violations if isinstance(violations, list) else []:
                field = violation.get("field") if isinstance(violation, dict) else None
                if not isinstance(field, str) or len(field) > 256:
                    continue
                if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\[\d{1,5}\]|\.[A-Za-z_][A-Za-z_0-9]*)*", field):
                    continue
                segments = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", field)
                if segments and all(segment in _ALLOWED_FIELDS for segment in segments):
                    fields.append(field)
        label = upstream_status if upstream_status in (
            "INVALID_ARGUMENT", "FAILED_PRECONDITION", "OUT_OF_RANGE") else "BAD_REQUEST"
        hint = ("；检查字段：" + ", ".join(fields[:6])) if fields else \
            "；请检查模型名、工具声明和生成参数"
        return InvalidRequest("Gemini 拒绝了这次请求参数（HTTP 400 / " + label + "）"
                              + hint + "。账号未因此进入冷却。")
    if status == 404:
        # 404 是「这个模型 id 在你这里不存在」，不是「账号坏了」。Google 会随时
        # 下线旧模型（`This model is no longer available to new users`），所以
        # 一把完全健康的钥匙也可能在这个模型上吃到 404。按账号故障处理会让它
        # 白白冷却十分钟，而且每个新用户都会撞上同一个已下线的探活模型。
        #
        # 这里抛 InvalidRequest：选模型是网关自己的决定，选错了不该记在账号账上。
        return InvalidRequest("Gemini 没有这个模型（HTTP 404 / " + (
            upstream_status or "NOT_FOUND") + "）。模型可能已下线，"
            "请在该账号上点「刷新」重新读取官方模型目录。账号未因此进入冷却。")
    return UpstreamFailure("gemini_http_" + str(status), retry_after=retry_after)


def native_model_of(account, model: str) -> str:
    native = str(account.models.get(model) or "").strip()
    if native.startswith("models/"):
        native = native[len("models/"):]
    if not _MODEL_OK.fullmatch(native):
        raise InvalidRequest("Gemini 模型名必须是字面量模型 id（例如 gemini-2.5-flash）。")
    return native


def endpoint_base(account) -> str:
    return (account.endpoint or API_BASE).rstrip("/")


def request_url(base: str, native_model: str, stream: bool) -> str:
    path = "/models/" + quote(native_model, safe="-._")
    return base + path + (":streamGenerateContent?alt=sse" if stream else ":generateContent")


def _blocked(value) -> bool:
    """整轮被安全策略挡下（`promptFeedback.blockReason`）。

    这不是上游故障：请求本身被过滤了，换号重试只会拿同一段提示词再撞一次墙。
    按 `content_filter` 如实回给客户端，而不是伪造一个 500 让号池白忙。
    """
    feedback = value.get("promptFeedback")
    return isinstance(feedback, dict) and bool(feedback.get("blockReason"))


def _value(value):
    """官方响应直接是候选集合；任何 `error` 都算上游故障而不是空结果。"""
    if not isinstance(value, dict):
        raise UpstreamFailure("gemini_invalid_response")
    if value.get("error") is not None:
        raise UpstreamFailure("gemini_error_body")
    if not isinstance(value.get("candidates"), list):
        if _blocked(value):
            return {**value, "candidates": []}
        raise UpstreamFailure("gemini_invalid_response")
    return value


def _api_key(account) -> str:
    secret = account.token
    key = secret.get_secret_value() if secret is not None else ""
    if not key:
        raise UpstreamFailure("gemini_missing_api_key", reauth=True)
    return key


async def send_gemini(signatures, client: httpx.AsyncClient, account, messages, stream: bool, *,
                      model: str, options, lifetime):
    """一条 Gemini 官方 API Key 请求；返回 JSON 或 `PreparedStream`。

    `signatures` 可为 None（没有签名缓存时只是不借用签名，不是错误）。
    """
    key = _api_key(account)
    native_model = native_model_of(account, model)
    lookup = (lambda call_id, name, args:
              signatures.get(account.id, native_model, call_id, name, args)) \
        if signatures is not None else None
    borrow = (lambda name: signatures.borrow(name)) if signatures is not None else None
    wire = native_request(messages, options, signature_lookup=lookup, signature_borrow=borrow)

    def remember(delta):
        if signatures is None:
            return
        for call in delta.get("tool_calls") or []:
            signature = (call.get("extra_content") or {}).get("google", {}).get("thought_signature")
            if not signature:
                continue
            try:
                arguments = json.loads(call["function"]["arguments"])
            except (KeyError, TypeError, ValueError):
                continue
            signatures.put(account.id, native_model, call["id"],
                           call["function"]["name"], arguments, signature)

    headers = {
        "x-goog-api-key": key,
        "Accept": "text/event-stream" if stream else "application/json",
        "Accept-Encoding": "identity",
    }
    url = request_url(endpoint_base(account), native_model, stream)
    try:
        response = await client.send(client.build_request(
            "POST", url, headers=headers, json=wire,
            timeout=httpx.Timeout(None, connect=5, pool=2, write=15)), stream=True)
        lifetime.push_async_callback(response.aclose)
        if not response.is_success:
            retry = _retry_after(response.headers.get("retry-after"))
            raise gemini_http_error(response.status_code,
                                    await _body(response, MAX_ERROR, prefix=True), retry)
        meta = {"id": "chatcmpl-" + uuid4().hex, "created": int(time.time()), "model": model}
        if not stream:
            value = _value(_json_object(await _body(response, MAX_JSON)))
            choices = []
            for candidate in value["candidates"]:
                if not isinstance(candidate, dict):
                    raise UpstreamFailure("gemini_invalid_candidate")
                delta = candidate_delta(candidate)
                remember(delta)
                reason = gemini_finish(candidate.get("finishReason"), bool(delta.get("tool_calls")))
                if reason is None:
                    raise UpstreamFailure("gemini_missing_finish")
                choices.append({"index": candidate.get("index", len(choices)),
                                "message": {"role": "assistant",
                                            "content": delta.pop("content", None), **delta},
                                "finish_reason": reason})
            if not choices and _blocked(value):
                choices.append({"index": 0, "message": {"role": "assistant", "content": None},
                                "finish_reason": "content_filter"})
            if not choices:
                raise UpstreamFailure("gemini_empty_completion")
            result = {**meta, "object": "chat.completion", "choices": choices}
            tokens = usage(value)
            if tokens is not None:
                result["usage"] = tokens
            return result
        if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
            raise UpstreamFailure("gemini_expected_sse")

        async def chunks():
            seen, finished, call_counts, tools_seen = set(), set(), {}, set()
            final_usage = None
            blocked = False
            try:
                async for event, data in _events(response):
                    if event == "error":
                        raise UpstreamFailure("gemini_stream_error")
                    if data.strip() == "[DONE]":
                        break
                    value = _value(_json_object(data))
                    final_usage = usage(value) or final_usage
                    if _blocked(value) and not value["candidates"]:
                        blocked = True
                    for candidate in value["candidates"]:
                        if not isinstance(candidate, dict):
                            raise UpstreamFailure("gemini_invalid_candidate")
                        index = candidate.get("index", 0)
                        if type(index) is not int or index < 0:
                            raise UpstreamFailure("gemini_invalid_choice")
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
                        reason = gemini_finish(candidate.get("finishReason"), index in tools_seen)
                        if candidate.get("finishReason") and reason is None:
                            raise UpstreamFailure("gemini_finish_error")
                        if reason:
                            finished.add(index)
                        if delta or reason:
                            yield {**meta, "object": "chat.completion.chunk", "choices": [
                                {"index": index, "delta": delta, "finish_reason": reason}]}
                if blocked and not seen:
                    # 提示词整段被安全策略拦下：如实回一帧 content_filter，而不是
                    # 报「流不完整」让号池以为上游坏了、换号重打同一段内容。
                    yield {**meta, "object": "chat.completion.chunk", "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": "content_filter"}]}
                elif not seen or seen != finished:
                    raise UpstreamFailure("gemini_incomplete_stream")
            except httpx.HTTPError:
                raise UpstreamFailure("gemini_stream_transport") from None
            if (options.get("stream_options") or {}).get("include_usage") and final_usage:
                yield {**meta, "object": "chat.completion.chunk", "choices": [], "usage": final_usage}
            yield None

        iterator = chunks()
        lifetime.push_async_callback(iterator.aclose)
        prefix, size = [], 0
        async for chunk in iterator:
            if chunk is None:
                raise UpstreamFailure("gemini_no_output")
            frame = encode_event(chunk)
            prefix.append(frame)
            size += len(frame)
            if len(prefix) > 64 or size > MAX_EVENT * 2:
                raise UpstreamFailure("gemini_preamble_too_large")
            if any(c["finish_reason"] or any(k != "role" and v for k, v in c["delta"].items())
                   for c in chunk["choices"]):
                return PreparedStream(prefix, iterator)
        raise UpstreamFailure("gemini_no_output")
    except httpx.HTTPError:
        raise UpstreamFailure("gemini_transport") from None


# 官方目录里混着大量「接口签名支持、但你这把钥匙用不了」的模型：
#   - 404 `no longer available to new users`（旧版本 flash/pro 已对新用户下线）
#   - 429 免费额度不足（pro、image、omni、nano-banana 等）
#   - 400 只要图像/音频输出（image、tts、lyria、transcribe）
#   - 500 服务端对该模型持续报错（gemma-4-31b-it 实测如此）
# `supportedGenerationMethods` 只说明它是生成式模型，不说明你付得起。列出来
# 用户在客户端选到就是一次必然失败的请求，所以保存/刷新时逐个真打一次最小
# 请求，只留下返回 200 的。这不是「猜」——目录是候选，实测才是结果。
_PROBE_SKIP = re.compile(
    r"(?:^|[-_])(?:tts|image|lyria|embedding|aqa|robotics|computer-use|deep-research"
    r"|transcribe|audio|veo|imagen|banana)(?:$|[-_])", re.I)
_PROBE_CONCURRENCY = 1
# 用户点「保存/刷新」后愿意等的时间上限。到点就带着**已确认可用**的模型返回：
# 部分清单仍然是真的，比让页面转 70 秒好。但要先把「最可能是主力对话模型」
# 的排到前面，否则预算会全花在已被下线的旧版本上（实测前 20 个几乎全是
# 404/429，只剩 1 个可用）。
_PROBE_BUDGET_SECONDS = 60.0

# 排序权重：越小越先测。free 档实际能用的几乎都在 flash-lite / flash 族，
# 带 preview/experimental 的次之；没有版本号后缀的 latest 别名最稳。
_PROBE_RANK = (
    (re.compile(r"^gemini-\d+(?:\.\d+)?-flash-lite$"), 0),
    (re.compile(r"^gemini-\d+(?:\.\d+)?-flash$"), 1),
    (re.compile(r"^gemini-flash-lite-latest$"), 2),
    (re.compile(r"^gemini-flash-latest$"), 3),
    (re.compile(r"^gemini-\d+(?:\.\d+)?-flash-lite-preview"), 4),
    (re.compile(r"^gemini-\d+(?:\.\d+)?-flash-preview"), 5),
    (re.compile(r"^gemma-"), 6),
)


def _chat_candidates(catalog):
    """从官方目录里挑出「可能能用」的模型名，按可用概率排序。"""
    names = [name for name in catalog if not _PROBE_SKIP.search(name)]

    def rank(name):
        for index, (pattern, weight) in enumerate(_PROBE_RANK):
            if pattern.match(name):
                # 同族内让版本号高的先测（3.6 比 3.0 更可能还活着）。
                return (weight, _version_key(name), index)
        # pro 与其它未列出的：放最后，免费档基本都 429。
        return (99, _version_key(name), 99)

    return sorted(names, key=rank)


def _version_key(name: str):
    match = re.search(r"(\d+(?:\.\d+)?)", name)
    try:
        return -float(match.group(1)) if match else 0.0
    except ValueError:
        return 0.0


async def probe_chat_models(client: httpx.AsyncClient, key: str, names, *, base: str | None = None):
    """对每个候选模型真发一次最小请求，返回确实可用的那些。

    免费档的额度限制按模型计：同一把钥匙可能 flash 能用而 pro 全 429。
    因此「可用模型清单」必须实测得出，不能从目录签名推断。
    """
    root = (base or API_BASE).rstrip("/")
    usable: list[str] = []
    # 探测预算是硬约束：这是用户在页面上点「保存/刷新」后要等的时长。
    # 25 个候选串行约 70 秒，全部测完没必要——先测到的通常就是能用的那几个。
    deadline = time.monotonic() + _PROBE_BUDGET_SECONDS
    semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def one(name: str):
        url = root + "/models/" + quote(name, safe="-._") + ":generateContent"
        payload = {"contents": [{"role": "user", "parts": [{"text": "OK"}]}],
                   "generationConfig": {"maxOutputTokens": 8}}
        async with semaphore:
            try:
                response = await client.post(url, json=payload,
                                             headers={"x-goog-api-key": key,
                                                      "Accept": "application/json",
                                                      "Accept-Encoding": "identity"},
                                             timeout=httpx.Timeout(30, connect=8, pool=4, write=20))
            except (httpx.HTTPError, TimeoutError):
                return None, None
            if response.status_code in (401, 403):
                raise gemini_http_error(response.status_code,
                                        await _body(response, MAX_ERROR, prefix=True),
                                        _retry_after(response.headers.get("retry-after")))
            raw = await _body(response, MAX_JSON)
            if response.is_success:
                value = _json_object(raw) or {}
                if value.get("candidates"):
                    return name, None
                return None, None
            # 429 有两种，必须分开：**每分钟**超限是暂时的（等一下就好），
            # **每天**超限是今天这个模型报废了（免费档每模型每天 20 次）。把
            # 后者当成「模型不可用」会把一个能用的模型永久丢掉，当成「可用」
            # 又会让用户选到一个必然 429 的模型。
            if response.status_code == 429:
                info = quota_exhaustion(raw)
                return None, info
            return None, None

    daily_exhausted: list[str] = []
    for name in names:
        if time.monotonic() > deadline:
            break
        result, info = await one(name)
        if result:
            usable.append(result)
        elif info is not None:
            daily_exhausted.append(name)
    return usable, daily_exhausted


async def usable_models(client: httpx.AsyncClient, key: str, *, base: str | None = None):
    """目录 → 候选 → 实测。

    返回 `(可用, 今日额度已耗尽)`。后者是「目录里有、今天调不动」的模型：免费档
    每个模型每天只有 20 次，撞上限后要等十几小时才恢复。它们既不属于「可用」，
    也不该被当成「这个模型不存在」永久丢掉 —— 明天刷新就能回来。
    """
    catalog = await list_models(client, key, base=base)
    candidates = _chat_candidates(catalog)
    if not candidates:
        raise UpstreamFailure("gemini_no_chat_models")
    usable, exhausted = await probe_chat_models(client, key, candidates, base=base)
    if not usable:
        raise UpstreamFailure("gemini_no_usable_models")
    # 输出按官方目录顺序，别让「先测谁」的排序泄漏到模型列表里。
    order = {name: index for index, name in enumerate(catalog)}
    usable.sort(key=lambda n: order.get(n, 10 ** 6))
    exhausted.sort(key=lambda n: order.get(n, 10 ** 6))
    return ({name: catalog[name] for name in usable},
            {name: catalog[name] for name in exhausted})


async def list_models(client: httpx.AsyncClient, key: str, *, base: str | None = None):
    """Read every official catalog page and expose only generateContent models."""
    out: dict[str, str] = {}
    page_token = None
    seen_tokens: set[str] = set()
    for _ in range(20):
        params = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        response = await client.get((base or API_BASE).rstrip("/") + "/models",
                                    params=params,
                                    headers={"x-goog-api-key": key, "Accept": "application/json",
                                             "Accept-Encoding": "identity"})
        if not response.is_success:
            raise gemini_http_error(response.status_code,
                                    await _body(response, MAX_ERROR, prefix=True),
                                    _retry_after(response.headers.get("retry-after")))
        value = _json_object(await _body(response, MAX_JSON)) or {}
        if value.get("error") is not None:
            raise UpstreamFailure("gemini_error_body")
        for row in value.get("models") if isinstance(value.get("models"), list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            if name.startswith("models/"):
                name = name[len("models/"):]
            if not _MODEL_OK.fullmatch(name):
                continue
            methods = row.get("supportedGenerationMethods")
            if not isinstance(methods, list) or "generateContent" not in methods:
                continue
            out[name] = str(row.get("displayName") or name)
        page_token = value.get("nextPageToken")
        if not page_token:
            break
        if not isinstance(page_token, str) or page_token in seen_tokens:
            raise UpstreamFailure("gemini_invalid_model_pagination")
        seen_tokens.add(page_token)
    else:
        raise UpstreamFailure("gemini_model_catalog_too_large")
    if not out:
        raise UpstreamFailure("gemini_no_models")
    return out