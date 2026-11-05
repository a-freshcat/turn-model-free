"""FastAPI OpenAI Chat Completions 入口。启动一个 worker 管理一个独立号池。"""

import hmac
import json
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
from web_api import CHANNELS, WebManager, authorized as web_authorized, failure as web_failure, router as web_router
from antigravity_auth import OAuthCallbackRedaction
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
                if exc.retry_after is not None:
                    headers["Retry-After"] = str(exc.retry_after)
                if exc.failure_reason in {"first_output_timeout", "completion_timeout"}:
                    await error_response(504,
                        "Upstream generation timed out. Retry after the temporary backoff.",
                        "upstream_timeout", headers=headers)(scope, receive, send)
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
app.add_middleware(OAuthCallbackRedaction)
app.add_middleware(ProtectionMiddleware)
bearer = HTTPBearer(auto_error=False, scheme_name="GatewayBearer")
app.include_router(web_router)
# Windows registry MIME associations can otherwise label .mjs as text/plain.
mimetypes.add_type("text/javascript", ".mjs")
app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")


@app.get("/", include_in_schema=False)
@app.get("/connect", include_in_schema=False)
async def dashboard():
    return FileResponse(ROOT / "web" / "index.html", headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/quota", include_in_schema=False)
async def quota_page():
    response = await dashboard()
    return FileResponse(ROOT / "web" / "quota.html", headers=dict(response.headers))


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
    try:
        listed += [m for m in await request.app.state.web.plugin.probe() if m not in listed]
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
        provider = PluginGateway.provider_of(body.model)
        if not provider or provider == "antigravity" or provider not in CHANNELS:
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
