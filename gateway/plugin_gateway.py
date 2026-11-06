"""DSH 插件自带的 OpenAI 兼容网关（channel-pack 的 openai-gateway）。

为什么要用它而不是自己实现 12 家的推理协议
------------------------------------------
`dsh-our-free-model` 的 channel-pack 除了登录，还内置了一个 OpenAI 兼容网关：
它已经实现了 13 家渠道的请求构造、模型目录、限流与重试。网关若自己再实现一遍，
等于把同一份协议维护两遍，并且从此要跟着厂商改两次。

所以这里只做**发现 + 代理**：
  - 密钥来自 `<dsh_home>/openai-gateway/api-key`（插件自己生成，43 字符 base64url）；
  - 地址默认 `127.0.0.1:8326`，可用 `DSH_OPENAI_GATEWAY_PORT` 覆盖；
  - 探不到就明确说「插件网关没在运行」，不假装成功。

这样 12 家渠道的账号、凭据、模型与对话全部由插件负责，网关只提供 Antigravity
的原生实现和统一入口。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import anyio

DEFAULT_PORT = 8326
DEFAULT_HOST = "127.0.0.1"
# 插件生成的密钥是 43 字符 base64url。显式 ASCII 字符集，避免把 Unicode
# 的 `isalnum()` 结果当成合法密钥。
_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class PluginGatewayError(RuntimeError):
    """插件网关不可用。"""


def dsh_home() -> Path:
    """与 pack.js 的 resolveChannelPackHome 保持一致的解析顺序。"""
    for name in ("DSH_CHANNEL_PACK_STATE_DIR", "DSH_HOME"):
        value = os.environ.get(name)
        if value and value.strip():
            return Path(value.strip())
    return Path.home() / ".dsh"


class PluginGateway:
    """插件 OpenAI 网关的发现与代理。

    实例是惰性的：第一次真正需要时才读密钥并探测端口，避免只用 Antigravity 的
    用户被无谓的连接拖慢启动。
    """

    def __init__(self, *, home: Path | None = None, host: str | None = None,
                 port: int | None = None, key: str | None = None,
                 timeout: float = 30.0):
        self.home = home or dsh_home()
        self.host = host or DEFAULT_HOST
        raw_port = os.environ.get("DSH_OPENAI_GATEWAY_PORT")
        self.port = port or (int(raw_port) if raw_port and raw_port.strip().isdigit() else DEFAULT_PORT)
        self._key = key or os.environ.get("DSH_OPENAI_GATEWAY_API_KEY") or None
        self.timeout = timeout
        self._models: list[str] | None = None

    # ---- 发现 ----

    @property
    def key_file(self) -> Path:
        return self.home / "openai-gateway" / "api-key"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def read_key(self) -> str:
        if self._key is not None:
            return self._key
        try:
            value = self.key_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise PluginGatewayError(
                "DSH 插件的网关密钥不存在。请先在 DSH 里启用「白嫖模型接入」的"
                "本机 OpenAI 网关，或设置 DSH_OPENAI_GATEWAY_API_KEY。") from None
        # 插件生成的是 43 字符 base64url。用显式 ASCII 字符集判断：Python 的
        # `str.isalnum()` 对中文也返回 True，用它会把「这不是密钥」当成合法值。
        if not value or not _KEY_PATTERN.fullmatch(value):
            raise PluginGatewayError(f"DSH 插件网关密钥格式异常：{self.key_file}")
        self._key = value
        return value

    def headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.read_key()}

    async def probe(self) -> list[str]:
        """探测可用性并缓存模型清单；不可用时抛 PluginGatewayError。"""
        try:
            headers = self.headers()
        except PluginGatewayError:
            raise
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=4),
                                         trust_env=False) as client:
                response = await client.get(self.base_url + "/v1/models", headers=headers)
        except httpx.HTTPError as exc:
            raise PluginGatewayError(
                f"连不上 DSH 插件的网关 {self.base_url}（{type(exc).__name__}）。"
                "请确认 DSH 正在运行且「白嫖模型接入」的网关已启用。") from None
        if response.status_code == 401:
            raise PluginGatewayError("DSH 插件网关拒绝了密钥，请在 DSH 设置页重新复制")
        if response.status_code != 200:
            raise PluginGatewayError(f"DSH 插件网关返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise PluginGatewayError("DSH 插件网关返回了非 JSON 响应") from None
        rows = payload.get("data") if isinstance(payload, dict) else None
        models = [row["id"] for row in rows or []
                  if isinstance(row, dict) and isinstance(row.get("id"), str)]
        self._models = models
        return models

    def cached_models(self) -> list[str]:
        return list(self._models or [])

    @staticmethod
    def provider_of(model: str) -> str:
        """`codearts/GLM-5.2` → `codearts`；没有前缀时返回空串。"""
        head, sep, _ = model.partition("/")
        return head if sep else ""

    # ---- 代理 ----

    async def chat(self, body: dict, *, stream: bool) -> httpx.Response:
        """把对话请求转发给插件网关。

        返回**未读取的 httpx 响应**：调用方自己决定是流式转发还是整体读取，
        这样流式语义（尤其是 SSE 的逐帧转发）不被这里吞掉。
        """
        headers = {**self.headers(), "Content-Type": "application/json",
                   "Accept": "text/event-stream" if stream else "application/json"}
        client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout + 240, connect=5),
                                   trust_env=False)
        try:
            request = client.build_request("POST", self.base_url + "/v1/chat/completions",
                                           headers=headers, json=body)
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            with anyio.CancelScope(shield=True):
                await client.aclose()
            raise PluginGatewayError(
                f"转发到 DSH 插件网关失败（{type(exc).__name__}）") from None
        except BaseException:
            with anyio.CancelScope(shield=True):
                await client.aclose()
            raise
        return _OwnedResponse(response, client)


class _OwnedResponse:
    """带关闭回调的响应包装：读取方结束时一并释放底层 client。"""

    def __init__(self, response: httpx.Response, client: httpx.AsyncClient):
        self._response = response
        self._client = client

    def __getattr__(self, name):
        return getattr(self._response, name)

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            try:
                await self._response.aclose()
            finally:
                await self._client.aclose()
