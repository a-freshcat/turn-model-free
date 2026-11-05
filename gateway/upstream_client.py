"""上游驱动、错误分类、JSON 与 SSE 解析；路由不依赖具体渠道协议。"""

import json
import math
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from account_manager import UpstreamFailure
from config import Account
from tencent_guard import scrub_messages

JSON = dict[str, Any]
MAX_JSON = 8 * 1024 * 1024
MAX_EVENT = 1024 * 1024
MAX_ERROR = 64 * 1024
DONE = b"data: [DONE]\n\n"
NEWLINE = re.compile(rb"\r\n|\r|\n")
UNAPPROVED = re.compile(
    r"unapproved\s+channel|unauthori[sz]ed\s+channel|未授权(?:调用)?渠道|非法(?:调用)?渠道|非授权渠道",
    re.I,
)


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _json_object(body: str | bytes) -> JSON | None:
    try:
        obj = json.loads(body, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _is_gate(obj: JSON | None, raw: str = "") -> bool:
    if obj is None:
        return bool(UNAPPROVED.search(raw))
    for item in (obj, obj.get("error"), obj.get("data")):
        if isinstance(item, str) and UNAPPROVED.search(item):
            return True
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if code in (11128, "11128"):
            return True
        for msg in (item.get("message"), item.get("msg")):
            if isinstance(msg, str) and (
                UNAPPROVED.search(msg)
                or ("11128" in msg and re.search(r"illegal|unapproved", msg, re.I))
            ):
                return True
    return False


def _is_gate_body(raw: str) -> bool:
    obj = _json_object(raw)
    if obj is not None:
        return _is_gate(obj)
    for line in raw.splitlines():
        item = _json_object(line.strip().removeprefix("data:").strip())
        if item is not None and _is_gate(item):
            return True
    return _is_gate(None, raw)


def _retry_after(header: str | None) -> float:
    if not header:
        return 0
    try:
        if header.isascii() and header.isdecimal() and len(header) <= 12:
            seconds = float(header)
        else:
            seconds = parsedate_to_datetime(header).timestamp() - time.time()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0
    return max(0, min(86400, seconds)) if math.isfinite(seconds) else 0


async def _body(response: httpx.Response, limit: int, *, prefix: bool = False) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = limit - len(body)
        if len(chunk) > remaining and not prefix:
            raise UpstreamFailure("response_too_large")
        body.extend(chunk[:remaining])
        if prefix and len(body) >= limit:
            break
    return bytes(body)


async def _lines(response: httpx.Response) -> AsyncIterator[bytes]:
    """分块可能切在 UTF-8 字符或 CRLF 中间；按字节找边界，再解码事件。"""
    pending = bytearray()
    skip_lf = False
    async for block in response.aiter_bytes():
        if not block:
            continue
        start = int(skip_lf and block.startswith(b"\n"))
        skip_lf = False
        for match in NEWLINE.finditer(block, start):
            if len(pending) + match.start() - start > MAX_EVENT:
                raise UpstreamFailure("sse_line_too_large")
            pending.extend(block[start:match.start()])
            yield bytes(pending)
            pending.clear()
            start = match.end()
            skip_lf = start == len(block) and match.group() == b"\r"
        if len(pending) + len(block) - start > MAX_EVENT:
            raise UpstreamFailure("sse_line_too_large")
        pending.extend(block[start:])
    # SSE 规范要求空行提交事件；未结束的尾部不会作为成功数据发给客户端。


async def _events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    parts: list[bytes] = []
    size = 0
    kind = ""
    first = True
    async for line in _lines(response):
        if first:
            line = line.removeprefix(b"\xef\xbb\xbf")
            first = False
        if not line:
            if parts:
                try:
                    data = b"\n".join(parts).decode("utf-8")
                except UnicodeDecodeError:
                    raise UpstreamFailure("invalid_sse_encoding") from None
                yield kind, data
            parts, size, kind = [], 0, ""
            continue
        field, _, value = line.partition(b":")
        value = value.removeprefix(b" ")
        if field == b"data":
            size += len(value) + 1
            if size > MAX_EVENT or len(parts) >= 4096:
                raise UpstreamFailure("sse_event_too_large")
            parts.append(value)
        elif field == b"event":
            kind = "error" if value == b"error" else ""


def encode_event(value: JSON) -> bytes:
    return b"data: " + json.dumps(value, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n\n"


def _completion(value: JSON | None, model: str, *, stream: bool) -> JSON:
    if _is_gate(value):
        raise UpstreamFailure("channel_11128")
    if value is None or value.get("error") is not None:
        raise UpstreamFailure("upstream_error_body")
    expected = "chat.completion.chunk" if stream else "chat.completion"
    choices = value.get("choices")
    if (
        value.get("object") != expected or not isinstance(value.get("id"), str)
        or type(value.get("created")) is not int or not isinstance(choices, list)
        or (not choices and (not stream or not isinstance(value.get("usage"), dict)))
    ):
        raise UpstreamFailure("invalid_completion_shape")
    field = "delta" if stream else "message"
    if any(not isinstance(c, dict) or type(c.get("index")) is not int
           or not isinstance(c.get(field), dict) for c in choices):
        raise UpstreamFailure("invalid_choice_shape")
    # 保留 tools、usage、logprobs 等标准字段，对外统一显示客户端选用的别名。
    return {**value, "model": model}


async def _chunks(response: httpx.Response, model: str) -> AsyncIterator[JSON | None]:
    seen: set[int] = set()
    finished: set[int] = set()
    try:
        async for event, data in _events(response):
            if data.strip() == "[DONE]" and event != "error":
                if not seen or seen != finished:
                    raise UpstreamFailure("missing_finish_reason")
                yield None
                return
            if not data and event != "error":
                continue
            value = _json_object(data)
            if _is_gate(value, data):
                raise UpstreamFailure("channel_11128")
            if event == "error":
                raise UpstreamFailure("upstream_sse_error")
            chunk = _completion(value, model, stream=True)
            for choice in chunk["choices"]:
                seen.add(choice["index"])
                if choice.get("finish_reason") is not None:
                    finished.add(choice["index"])
            yield chunk
    except httpx.HTTPError:
        raise UpstreamFailure("upstream_stream_transport") from None
    raise UpstreamFailure("missing_sse_done")


def _has_output(chunk: JSON) -> bool:
    return any(c.get("finish_reason") is not None
               or any(key != "role" and value for key, value in c["delta"].items())
               for c in chunk["choices"])


@dataclass
class PreparedStream:
    prefix: list[bytes]
    chunks: AsyncIterator[JSON | None]

    async def frames(self) -> AsyncIterator[bytes]:
        for frame in self.prefix:
            yield frame
        async for chunk in self.chunks:
            if chunk is None:
                yield DONE
                return
            yield encode_event(chunk)


class UpstreamClient:
    def __init__(self, transport_factory: Callable[[Account], httpx.AsyncBaseTransport | None]):
        self.transport_factory = transport_factory
        self.clients: dict[str, httpx.AsyncClient] = {}
        self.antigravity = None

    async def aclose(self) -> None:
        for client in self.clients.values():
            await client.aclose()
        if self.antigravity is not None:
            await self.antigravity.aclose()

    def _client(self, account: Account) -> httpx.AsyncClient:
        # Cookie jar 与连接池均按账号隔离。禁止跳转，避免凭据被转发到其他域。
        if account.id not in self.clients:
            self.clients[account.id] = httpx.AsyncClient(
                transport=self.transport_factory(account), follow_redirects=False, trust_env=False,
                proxy=(os.environ.get("GATEWAY_UPSTREAM_PROXY") or None) if account.driver == "antigravity" else None,
                timeout=httpx.Timeout(60, connect=5, pool=2, write=15),
                limits=httpx.Limits(max_connections=account.max_concurrency,
                                    max_keepalive_connections=account.max_concurrency),
            )
        return self.clients[account.id]

    async def send_upstream_request(
        self, account: Account, messages: list[JSON], stream: bool = True, *,
        model: str, options: JSON, lifetime: AsyncExitStack,
    ) -> JSON | PreparedStream:
        """驱动入口。异构渠道可在这里按 driver 分派并归一成 JSON / PreparedStream。"""
        if account.driver == "antigravity":
            from antigravity_driver import send_native
            if self.antigravity is None:
                raise UpstreamFailure("antigravity_auth_not_initialized")
            return await send_native(self.antigravity, self._client(account), account,
                                     messages, stream, model=model, options=options, lifetime=lifetime)
        client = self._client(account)
        headers = {
            **account.device_headers, "User-Agent": account.user_agent or "local-chat-gateway/1",
            "Accept": "text/event-stream" if stream else "application/json",
            "Accept-Encoding": "identity",
        }
        if account.token is not None:
            headers["Authorization"] = f"Bearer {account.token.get_secret_value()}"
        if account.cookie is not None:
            headers["Cookie"] = account.cookie.get_secret_value()
        request = client.build_request(
            "POST", account.endpoint or "http://demo.invalid/v1/chat/completions",
            headers=headers, json={
                **options, "model": account.models[model], "stream": stream,
                "messages": scrub_messages(messages, account.tencent_prompt_level),
            },
        )
        retry = 0.0
        try:
            response = await client.send(request, stream=True)
            lifetime.push_async_callback(response.aclose)
            retry = _retry_after(response.headers.get("retry-after"))
            if not response.is_success:
                raw = (await _body(response, MAX_ERROR, prefix=True)).decode("utf-8", errors="replace")
                reason = "channel_11128" if _is_gate_body(raw) else f"http_{response.status_code}"
                raise UpstreamFailure(reason, retry_after=retry)
            media = response.headers.get("content-type", "").partition(";")[0].strip().lower()
            if stream and media == "text/event-stream":
                chunks = _chunks(response, model)
                lifetime.push_async_callback(chunks.aclose)
                # 暂存空 delta / role 元数据。它们之后仍可能立即出现 11128。
                prefix: list[bytes] = []
                size = 0
                async for chunk in chunks:
                    if chunk is None:
                        raise UpstreamFailure("sse_without_output")
                    frame = encode_event(chunk)
                    size += len(frame)
                    if len(prefix) >= 64 or size > 2 * MAX_EVENT:
                        raise UpstreamFailure("sse_preamble_too_large")
                    prefix.append(frame)
                    if _has_output(chunk):
                        return PreparedStream(prefix, chunks)
                raise UpstreamFailure("sse_without_output")

            raw = await _body(response, MAX_JSON if not stream else MAX_ERROR)
            value = _json_object(raw)
            if (_is_gate(value) if value is not None else _is_gate_body(raw.decode("utf-8", errors="replace"))):
                raise UpstreamFailure("channel_11128", retry_after=retry)
            if stream:
                raise UpstreamFailure("expected_event_stream")
            return _completion(value, model, stream=False)
        except httpx.HTTPError:
            raise UpstreamFailure("upstream_transport", retry_after=retry) from None
        except UpstreamFailure as exc:
            if retry > exc.retry_after:
                raise UpstreamFailure(exc.reason, retry_after=retry) from None
            raise
