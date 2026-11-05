"""Native Antigravity text/tool Chat Completions transport.

The public OpenAI response schema is produced here; OAuth lives separately.
"""

import hashlib
import json
import time
from uuid import uuid4

import httpx

from account_manager import InvalidRequest, UpstreamFailure
from antigravity_auth import API_BASE, OAuthFailure


def native_request(messages, options):
    contents, system, call_names = [], [], {}
    for message in messages:
        role = message["role"]
        content = message.get("content")
        parts = []
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    parts.append({"text": block.get("text", "")})
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
            response = {"result": content}
            parts = [{"functionResponse": {"name": name, "response": response}}]
            native_role = "user"
        elif role == "assistant":
            native_role = "model"
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (ValueError, TypeError):
                    raise InvalidRequest("Tool arguments must be a JSON object.") from None
                if not isinstance(arguments, dict) or not isinstance(function.get("name"), str):
                    raise InvalidRequest("Tool calls must provide a name and JSON object arguments.")
                call_names[call.get("id")] = function["name"]
                part = {"functionCall": {"name": function["name"], "args": arguments}}
                signature = (call.get("extra_content") or {}).get("google", {}).get("thought_signature")
                if isinstance(signature, str) and signature:
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
            declarations.append({k: function[k] for k in ("name", "description", "parameters") if k in function})
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
            raise UpstreamFailure("antigravity_unsupported_output")
    delta = {}
    if text:
        delta["content"] = "".join(text)
    if thought:
        delta["reasoning_content"] = "".join(thought)
    if calls:
        delta["tool_calls"] = calls
    return delta


async def send_native(auth, client, account, messages, stream, *, model, options, lifetime):
    from upstream_client import (PreparedStream, MAX_EVENT, MAX_JSON, _body, _events,
                                 _json_object, _retry_after, encode_event)
    wire = native_request(messages, options)
    # 令牌刷新、项目探测、模型目录查询必须和对话请求走同一条出口：
    # 同一份 OAuth 凭据在两个 IP 上出现，正是「一个账号多处使用」的特征。
    proxy = auth.resolve_proxy(account.proxy)
    try:
        credential = await auth.credential(account.credential_id, proxy=proxy)
        if not credential.get("project_id"):
            credential = await auth.refresh_catalog(account.credential_id, proxy=proxy)
    except OAuthFailure as exc:
        raise UpstreamFailure("antigravity_authorization", reauth=exc.reauth) from None
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
            response = await client.send(client.build_request("POST", API_BASE + path,
                headers={"Authorization": "Bearer " + credential["access_token"],
                         "User-Agent": auth.user_agent,
                         "Accept": "text/event-stream" if stream else "application/json",
                         "Accept-Encoding": "identity"}, json=envelope), stream=True)
            lifetime.push_async_callback(response.aclose)
            if response.status_code != 401 or attempt:
                break
            await response.aclose()
            try:
                credential = await auth.credential(account.credential_id, force=True, proxy=proxy)
            except OAuthFailure as exc:
                raise UpstreamFailure("antigravity_authorization", reauth=exc.reauth) from None
        if not response.is_success:
            retry = _retry_after(response.headers.get("retry-after"))
            raise UpstreamFailure("antigravity_http_" + str(response.status_code),
                                  retry_after=retry, reauth=response.status_code == 401)
        meta = {"id": "chatcmpl-" + uuid4().hex, "created": int(time.time()), "model": model}
        if not stream:
            value = unwrap(_json_object(await _body(response, MAX_JSON)))
            choices = []
            for candidate in value.get("candidates") or []:
                delta = candidate_delta(candidate)
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
                        delta = candidate_delta(candidate)
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
