"""FastAPI OpenAI Chat Completions 入口。启动一个 worker 管理一个独立号池。"""

import hashlib
import hmac
import json
import logging
import mimetypes
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import anyio
import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.types import Receive, Scope, Send

from account_manager import AccountManager, InvalidRequest, Lease, PoolUnavailable, UpstreamFailure
from config import load_settings
from demo_upstream import transport_for
from plugin_gateway import PluginGateway, PluginGatewayError
from upstream_client import PreparedStream, UpstreamClient, encode_event
from web_api import (CHANNELS, LOCAL_CHANNELS, WebManager, authorized as web_authorized,
                     failure as web_failure, router as web_router)
from antigravity_auth import OAuthCallbackRedaction
from request_limits import RequestLimitsMiddleware
from protection import ProtectionBlocked, ProtectionMiddleware, SSEProtectionObserver, observe_error_value

ROOT = Path(__file__).resolve().parent


class Message(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    role: Literal["developer", "system", "user", "assistant", "tool", "function"]
    content: str | list[dict[str, JsonValue]] | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    include_usage: bool | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    model: str = Field(min_length=1, max_length=256)
    messages: list[Message] = Field(min_length=1, max_length=4096)
    stream: bool | None = Field(default=False, strict=True)
    stream_options: StreamOptions | None = None


def error_body(message: str, code: str, kind: str = "server_error", param: str | None = None):
    return {"error": {"message": message, "type": kind, "param": param, "code": code}}


def error_response(status: int, message: str, code: str, kind: str = "server_error",
                   param: str | None = None, headers: dict[str, str] | None = None):
    return JSONResponse(error_body(message, code, kind, param), status_code=status, headers=headers)


class ChatResponse(Response):
    """统一监听断开：等待租约、首帧、转发 SSE 三阶段都能取消。"""

    def __init__(self, payload: ChatRequest, pool: AccountManager, upstream: UpstreamClient):
        super().__init__()
        self.payload, self.pool, self.upstream = payload, pool, upstream
        self.request_id = "req_" + uuid4().hex

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as group:
            async def watch_disconnect():
                while (await receive())["type"] != "http.disconnect":
                    pass
                group.cancel_scope.cancel()

            group.start_soon(watch_disconnect)
            try:
                await self._reply(scope, receive, send)
            finally:
                group.cancel_scope.cancel()

    async def _reply(self, scope: Scope, receive: Receive, send: Send) -> None:
        payload = self.payload.model_dump(mode="json", exclude_unset=True)
        model, messages = payload.pop("model"), payload.pop("messages")
        stream = payload.pop("stream", False) is True
        headers = {"X-Request-ID": self.request_id}

        async def attempt(lease: Lease, attempt_lifetime: AsyncExitStack):
            result = await self.upstream.send_upstream_request(
                lease.account, messages, stream, model=model, options=payload, lifetime=attempt_lifetime,
            )
            return lease, result

        lifetime = AsyncExitStack()
        try:
            try:
                lease, result = await self.pool.failover(model, attempt, lifetime,
                    timeout_seconds=(self.pool.settings.first_output_timeout_seconds if stream
                                     else self.pool.settings.stream_timeout_seconds),
                    timeout_reason="first_output_timeout" if stream else "completion_timeout")
            except ProtectionBlocked as exc:
                await exc.response()(scope, receive, send)
                return
            except InvalidRequest as exc:
                await error_response(400, str(exc), "invalid_request", "invalid_request_error",
                                     headers=headers)(scope, receive, send)
                return
            except PoolUnavailable as exc:
                # 429 引发的退避不该被报成「无可用账号」。单账号部署里，一次限流
                # 退避就等于全池暂时不可用；把它报成 503 + 无 Retry-After，客户端
                # 会当成故障立刻重试三次，进一步加深限流。这里按真实原因回 429，
                # 并给出本地估算的等待秒数，让客户端自己等。
                #
                # 两种情况都要覆盖：
                #   1. 本轮请求自己撞到 429 → failure_reason 有值；
                #   2. 账号**已经**在上一次 429 后的冷却里，本轮一次上游都没打
                #      → failure_reason 是 None，只有 cooling_reason。
                # 只判断第 1 种（旧实现）会漏掉最常见的第 2 种，日志里 21 个
                # `status=503` 全是它。
                rate_limited = exc.rate_limited
                if exc.retry_after is not None:
                    headers["Retry-After"] = str(exc.retry_after)
                elif rate_limited:
                    headers["Retry-After"] = str(max(1, self.pool.settings.network_cooldown_seconds))
                if exc.failure_reason in {"first_output_timeout", "completion_timeout"}:
                    await error_response(504,
                        "Upstream generation timed out. Retry after the temporary backoff.",
                        "upstream_timeout", headers=headers)(scope, receive, send)
                    return
                if rate_limited:
                    # 关键区分：这个 429 是「本轮真撞到上游限流」，还是「账号还在
                    # 冷却里、本轮一次上游都没打」？两者对客户端来说一模一样，但对
                    # 排查完全不同——后者说明限流早就发生过了，现在只是在等退避。
                    #
                    # 旧实现把两者写成同一行日志、同一个响应，于是「上游持续限流」
                    # 和「网关在自己等」无法区分。这正是上一轮我把 pool_wait 调大后
                    # 看起来「还是一直 429」的原因：其实那些 429 全是本地冷却。
                    source = ("upstream" if exc.failure_reason in
                              {"antigravity_http_429", "gemini_http_429"} else "local_cooldown")
                    logging.getLogger("gateway.shape").warning(
                        "rate limited: source=%s failure_reason=%s cooling_reason=%s "
                        "retry_after=%s", source, exc.failure_reason, exc.cooling_reason,
                        exc.retry_after)
                    await error_response(429,
                        "Upstream rate limit. Wait for Retry-After seconds before retrying.",
                        "upstream_rate_limited", "rate_limit_error",
                        headers={**headers, "X-Rate-Limit-Source": source})(scope, receive, send)
                    return
                response = (
                    error_response(502, "Upstream retry budget exhausted.", "upstream_retry_exhausted",
                                   headers=headers) if exc.budget_exhausted
                    else error_response(503, "No upstream account is currently available.",
                                        "account_pool_unavailable", headers=headers)
                )
                await response(scope, receive, send)
                return

            if isinstance(result, PreparedStream):
                await self._stream(send, lease, result, headers)
            else:
                # ensure_ascii 也能安全转发来自兼容上游的转义 Unicode。
                await lease.succeed()
                response = Response(json.dumps(result, separators=(",", ":"), allow_nan=False),
                                    media_type="application/json", headers=headers)
                await response(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await lifetime.aclose()

    async def _stream(self, send: Send, lease: Lease, result: PreparedStream,
                      headers: dict[str, str]) -> None:
        headers.update({
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        })
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(k.lower().encode("ascii"), v.encode("ascii")) for k, v in headers.items()]})
        try:
            with anyio.fail_after(self.pool.settings.stream_timeout_seconds):
                async for frame in result.frames():
                    await send({"type": "http.response.body", "body": frame, "more_body": True})
        except (UpstreamFailure, TimeoutError) as exc:
            await lease.fail(exc if isinstance(exc, UpstreamFailure) else UpstreamFailure("stream_timeout"))
            # HTTP 200 已发送；错误帧之后立即结束，不发 [DONE]，避免把截断伪装成成功。
            await send({"type": "http.response.body", "body": encode_event(error_body(
                "The upstream stream ended before completion.", "upstream_stream_interrupted",
            )), "more_body": True})
        except InvalidRequest as exc:
            await send({"type": "http.response.body", "body": encode_event(error_body(
                str(exc), "unsupported_output", "invalid_request_error",
            )), "more_body": True})
        else:
            await lease.succeed()
        await send({"type": "http.response.body", "body": b"", "more_body": False})


@asynccontextmanager
async def lifespan(app: FastAPI):
    key = os.environ.get("GATEWAY_API_KEY")
    if not key or any(not 33 <= ord(c) <= 126 for c in key):
        raise RuntimeError("Set GATEWAY_API_KEY to a nonempty printable ASCII token")
    app.state.api_key = key.encode("ascii")
    app.state.pool = AccountManager(load_settings())
    app.state.upstream = UpstreamClient(transport_for)
    app.state.web = WebManager(app.state.pool,
                               Path(os.environ.get("GATEWAY_ACCOUNTS", str(ROOT / "accounts.json"))),
                               app.state.upstream)
    try:
        yield
    finally:
        await app.state.upstream.aclose()


app = FastAPI(title="Local Chat Gateway", lifespan=lifespan)


class RequestShapeLogger:
    """在 GATEWAY_LOG_REQUESTS 打开时，记录每个 /v1/chat 请求的**形状摘要**。

    为什么需要：agent 客户端（DSH）发的请求和我手写的探针差别很大 —— 消息条数、
    工具数量、总字符数、是否流式都可能几十倍差距，而这些正是唯一还没排除的变量。
    访问日志只记状态码，看不到形状；而抓完整 body 又会把提示词写进磁盘。

    因此只记计数与布尔，不记任何文本：模型别名、消息条数、每条 role 分布、
    工具个数、序列化总字节数、stream 标志、message 里是否含图片块。
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _authorized(scope, app) -> bool:
        """未认证的请求一个 body 字节都不该被读。

        `test_audit_regressions.test_auth_rejects_before_reading_large_body` 的
        不变式：401 之前不能消费 body，否则未认证的客户端可以让网关把任意大的
        请求体读进内存。本中间件在最外层（Starlette 后注册者先执行），所以必须
        自己先判一次鉴权，而不是等下游的 Depends(bearer)。
        """
        state = getattr(app, "state", None)
        key = getattr(state, "api_key", None)
        if not key:
            return False
        for name, value in scope.get("headers") or []:
            if name.lower() != b"authorization":
                continue
            try:
                text = value.decode("latin-1")
            except (UnicodeDecodeError, AttributeError):
                return False
            scheme, _, token = text.partition(" ")
            if scheme.lower() != "bearer":
                return False
            try:
                return hmac.compare_digest(token.encode("latin-1"), key)
            except (UnicodeEncodeError, AttributeError):
                return False
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/chat"):
            return await self.app(scope, receive, send)
        # 鉴权通过前不读 body：直接把请求交给下游，由它回 401。
        if not self._authorized(scope, self.app):
            return await self.app(scope, receive, send)

        chunks = []
        consumed = False

        async def peek():
            """缓存并重放请求体，绝不能把 body 从下游手里吃掉。

            第一版实现直接 `await receive()` 旁路读取，结果 body 被本中间件消费，
            下游的 pydantic 校验收到空 body 并回 400 —— 观测必须对被测请求完全透明。
            """
            nonlocal consumed
            if consumed:
                # 下游再次读取（流式接口会轮询），交给原始 receive。
                return await receive()
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    consumed = True
            return message

        status = {"code": None}

        async def watch(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, peek, watch)
        finally:
            _log_request_shape(b"".join(chunks), status["code"], scope)


_HEADER_ALLOW = (
    "user-agent", "accept", "accept-encoding", "accept-language", "content-type",
    "content-encoding", "content-length", "connection", "cache-control", "pragma",
    "x-stainless-lang", "x-stainless-package-version", "x-stainless-os",
    "x-stainless-arch", "x-stainless-runtime", "x-stainless-runtime-version",
    "x-stainless-retry-count", "x-stainless-timeout",
    "openai-beta", "anthropic-version", "x-api-key", "origin", "referer", "host",
)


def _log_request_headers(scope):
    """记录客户端发到网关的请求头。

    为什么要记：DSH 的 429 与我的探针 429 结果不同（同账号、同模型、同形状，
    探针 200、DSH 429），而请求体已被逐字节复刻排除。剩下唯一没被对比的就是
    **头部**——pi-ai 这类 SDK 会自带 `x-stainless-*`、`openai-beta` 等标头，
    而探针没有。头部值是相当强的客户端指纹，若有任何按客户端维度的限流，
    只可能从这里看出来。

    脱敏边界：只记录白名单里的固定头，且值只输出长度 + 短哈希前缀。
    `authorization` / `cookie` 永不出现在允许列表里。
    """
    if not scope:
        return
    try:
        rows = []
        for raw_key, raw_value in scope.get("headers") or []:
            key = raw_key.decode("latin-1").lower()
            if key not in _HEADER_ALLOW:
                continue
            value = raw_value.decode("latin-1")
            # 不落原文：只留长度与哈希前缀，足够判断「有没有变」。
            digest = hashlib.sha256(value.encode("latin-1", "replace")).hexdigest()[:8]
            rows.append(f"{key}(len={len(value)},h={digest})")
        logging.getLogger("gateway.shape").warning(
            "request headers: %s", " ".join(sorted(rows)) or "<none>")
    except Exception:  # noqa: BLE001
        logging.getLogger("gateway.shape").debug("header logging skipped", exc_info=True)


def _log_request_shape(body: bytes, status, scope=None):
    _log_request_headers(scope)
    try:
        value = json.loads(body) if body else {}
    except (ValueError, UnicodeError):
        logging.getLogger("gateway.shape").warning(
            "request shape: unparseable body bytes=%d status=%s", len(body), status)
        return
    if not isinstance(value, dict):
        return
    messages = value.get("messages")
    messages = messages if isinstance(messages, list) else []
    roles = {}
    for message in messages:
        if isinstance(message, dict):
            role = message.get("role")
            roles[str(role)] = roles.get(str(role), 0) + 1
    tools = value.get("tools")
    tools = tools if isinstance(tools, list) else []
    total_chars = len(json.dumps(messages, ensure_ascii=False)) if messages else 0
    # tools 数组的体积此前完全没被计入：`chars` 只统计 messages。86 个工具带
    # 完整 JSON Schema 可能有几十到上百 KB，是 messages 之外最大的一块载荷。
    tools_bytes = len(json.dumps(tools, ensure_ascii=False)) if tools else 0
    logging.getLogger("gateway.shape").warning(
        "request shape: model=%s messages=%d roles=%s chars=%d tools=%d "
        "tools_bytes=%d total_bytes=%d stream=%s images=%s status=%s",
        value.get("model"), len(messages),
        ",".join(f"{k}:{v}" for k, v in sorted(roles.items())),
        total_chars, len(tools), tools_bytes, total_chars + tools_bytes,
        value.get("stream") is True,
        "data:image/" in json.dumps(messages, ensure_ascii=False)[:200000],
        status,
    )
    _log_system_fingerprint(messages)


# system 提示词只留结构指纹：长度、行数、以及命中的**固定关键词**。
# 不写原文任何片段 —— 那里面可能含项目名、路径、内部规则。
_SYSTEM_PROBES = (
    "you are", "antigravity", "claude", "codex", "gemini", "assistant",
    "tool", "function", "system prompt", "instruction", "rule", "sandbox",
    "workspace", "agent", "reasoning", "thinking", "safety", "policy",
    "refuse", "disallow", "must not", "do not", "never",
    "system_instruction", "<", "```", "{{", "}}",
)


def _log_system_fingerprint(messages):
    texts = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts.append(block["text"])
    if not texts:
        return
    joined = "\n".join(texts)
    lowered = joined.lower()
    hits = [probe for probe in _SYSTEM_PROBES if probe in lowered]
    logging.getLogger("gateway.shape").warning(
        "system fingerprint: len=%d lines=%d nonascii=%s keyword_hits=%s "
        "starts_with_role_marker=%s has_xml=%s has_json_braces=%s",
        len(joined), joined.count("\n") + 1,
        any(ord(c) > 127 for c in joined),
        ",".join(hits) or "-",
        lowered.lstrip().startswith(("#", "you are", "role")),
        ">" in joined and "<" in joined,
        "{" in joined and "}" in joined,
    )


if os.environ.get("GATEWAY_LOG_REQUESTS") == "1":
    app.add_middleware(RequestShapeLogger)
    # 形状日志走独立 logger，必须挂到桌面日志文件，否则 pythonw 下无处可去。
    logging.getLogger("gateway.shape").setLevel(logging.INFO)

app.add_middleware(OAuthCallbackRedaction)
app.add_middleware(ProtectionMiddleware)
# 放在最后注册 = 最外层：鉴权与体积上限必须早于任何读取 body 的中间件。
# 少了它，形状日志会先把未认证请求的 body 读进内存。
app.add_middleware(RequestLimitsMiddleware)
bearer = HTTPBearer(auto_error=False, scheme_name="GatewayBearer")
app.include_router(web_router)
from image_gateway import router as image_router
app.include_router(image_router)
from search_gateway import router as search_router
app.include_router(search_router)
# Windows registry MIME associations can otherwise label .mjs as text/plain.
mimetypes.add_type("text/javascript", ".mjs")
app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")


CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
NO_STORE = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def console_page():
    """本机号池控制台。

    页面上每个数字都来自 `/ui/quota`（配额与重置时刻）、`/ui/state`（并发、
    失败计数、冷却）、`/ui/protection`（调用保护）与 `provider.status`（各渠道
    账号与模型统计）。页面自身不含任何写死的账号、状态或百分比——旧界面把
    「示例数据」和「真实读数」混在一张表里，用户没法分辨哪一格是真的。
    """
    return FileResponse(ROOT / "web" / "console.html", headers=dict(NO_STORE))


@app.get("/", include_in_schema=False)
async def dashboard():
    return console_page()


@app.get("/quota", include_in_schema=False)
async def quota_page():
    """配额看板已并入控制台；旧地址保留，免得收藏夹和旧链接失效。"""
    return console_page()


@app.get("/connect", include_in_schema=False)
async def connect_page():
    """本机导入窗口：兼容上游与 Gemini 官方 API Key；插件渠道另走登录桥。"""
    return FileResponse(ROOT / "web" / "connect.html", headers=dict(NO_STORE))


def auth_error(request: Request) -> JSONResponse | None:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.encode(), request.app.state.api_key):
        return error_response(401, "Invalid or missing gateway API key.", "invalid_api_key",
                              "invalid_request_error", headers={"WWW-Authenticate": "Bearer"})
    return None


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v1/"):
        unauthorized = auth_error(request)
        if unauthorized is not None:
            return unauthorized
    return error_response(400, "The request body does not match the expected schema.",
                          "invalid_request", "invalid_request_error")


@app.exception_handler(ProtectionBlocked)
async def protection_blocked(request: Request, exc: ProtectionBlocked):
    return exc.response()


@app.get("/healthz")
async def health():
    """进程探活；不发送上游请求，也不公开账号或凭据。"""
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(bearer)])
async def models(request: Request):
    unauthorized = auth_error(request)
    if unauthorized is not None:
        return unauthorized
    listed = list(request.app.state.pool.models)
    # 12 家的模型由插件网关提供；连不上时只是这批模型缺席，不影响本机号池。
    # 过滤掉不在 CHANNELS 白名单内的未支持渠道（如匿名渠道 opencode 等），
    # 以及本机渠道的模型：它们已经由 pool.models 报了，再经插件重复列一次会让
    # 同一个名字出现两遍，客户端下拉里就分不清哪个是本机哪个是插件。
    try:
        probe_models = await request.app.state.web.plugin.probe()
        listed += [m for m in probe_models
                   if m not in listed and PluginGateway.provider_of(m) in CHANNELS
                   and PluginGateway.provider_of(m) not in LOCAL_CHANNELS]
    except PluginGatewayError:
        pass
    return {"object": "list", "data": [
        {"id": model, "object": "model", "created": 0, "owned_by": "gateway"}
        for model in listed
    ]}


@app.get("/admin/accounts", dependencies=[Depends(bearer)])
async def account_status(request: Request):
    unauthorized = auth_error(request)
    if unauthorized is not None:
        return unauthorized
    return {"accounts": await request.app.state.pool.snapshot()}


@app.post("/v1/chat/completions", dependencies=[Depends(bearer)])
async def completions(body: ChatRequest, request: Request):
    unauthorized = auth_error(request)
    if unauthorized is not None:
        return unauthorized
    return await make_chat_response(body, request)


@app.post("/ui/chat", include_in_schema=False)
async def web_chat(body: ChatRequest, request: Request):
    if not web_authorized(request):
        return web_failure("请先连接本机网关", 401)
    return await make_chat_response(body, request)


async def make_chat_response(body: ChatRequest, request: Request):
    pool: AccountManager = request.app.state.pool
    bridge_model = body.model not in pool.models
    if bridge_model:
        # 12 家的模型由插件网关提供（id 形如 `codearts/GLM-5.2`）。本机号池不认识
        # 它们，但插件认识——转发过去即可，不必在这里重实现各家的推理协议。
        # 本机渠道（antigravity / gemini_api）必须排除：它们的前缀也在 CHANNELS 里，
        # 只判 `not in CHANNELS` 会把一个「已被停用」的本机模型转发给插件，而插件
        # 那边没有这个渠道的凭据，只会回一个与真实原因无关的错误。
        provider = PluginGateway.provider_of(body.model)
        if not provider or provider in LOCAL_CHANNELS or provider not in CHANNELS:
            return error_response(404, "The requested model is not configured.", "model_not_found",
                                  "invalid_request_error", "model")
        return await _plugin_chat(body, request)
    if body.stream is not True and body.stream_options is not None:
        return error_response(400, "stream_options requires stream=true.", "invalid_request",
                              "invalid_request_error", "stream_options")
    # 预先校验 JSON 数字和编码，避免客户端输入错误占用租约并触发冷却。
    try:
        json.dumps(body.model_dump(mode="json"), allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):
        return error_response(400, "The request contains an invalid JSON value.",
                              "invalid_request", "invalid_request_error")
    return ChatResponse(body, pool, request.app.state.upstream)


async def _plugin_chat(body: ChatRequest, request: Request):
    """把 12 家的对话转发给插件网关，按客户端要求做流式或整体透传。"""
    gateway: PluginGateway = request.app.state.web.plugin
    guard = request.app.state.web.protection
    await guard.before_send()
    payload = body.model_dump(mode="json", exclude_none=True)
    try:
        upstream = await gateway.chat(payload, stream=body.stream is True)
    except PluginGatewayError as exc:
        await guard.observe_failure('plugin_transport')
        return error_response(503, str(exc), "plugin_gateway_unavailable", "api_error")
    if upstream.status_code != 200:
        try:
            await guard.observe_failure(f'http_{upstream.status_code}')
            guard.check()
            detail = await upstream.aread()
        finally:
            await upstream.aclose()
        return Response(content=detail, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type", "application/json"))
    if body.stream is not True:
        try:
            content = await upstream.aread()
            await observe_error_value(guard, content)
            guard.check()
        finally:
            await upstream.aclose()
        return Response(content=content, status_code=200,
                        media_type=upstream.headers.get("content-type", "application/json"))

    async def frames():
        observer = SSEProtectionObserver(guard)
        try:
            async for chunk in upstream.aiter_bytes():
                await observer.feed(chunk)
                guard.check()
                yield chunk
        except ProtectionBlocked:
            yield encode_event(error_body('模型调用已暂停，请在管理页复制诊断信息交给 AI 排查。',
                                          'dispatch_paused'))
        except httpx.HTTPError:
            await guard.observe_failure('plugin_stream_transport')
            yield encode_event(error_body('上游流连接中断，请查看管理页调用保护状态。',
                                          'upstream_stream_error'))
        finally:
            await upstream.aclose()

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})
