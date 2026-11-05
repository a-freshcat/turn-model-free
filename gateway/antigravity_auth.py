"""Antigravity OAuth and account discovery, independent of the channel UI.

Protocol references: CLIProxyAPI internal/auth/antigravity and sdk/auth.
Google PKCE/state rules follow the official installed-app OAuth specification.
"""

import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import httpx

from credential_store import CredentialStore

ROOT = Path(__file__).resolve().parent
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USER_URL = "https://www.googleapis.com/oauth2/v2/userinfo?alt=json"
API_BASE = "https://daily-cloudcode-pa.googleapis.com"
SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/aicode",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)


def model_pool(name: str, id_: str = "") -> str:
    """User-confirmed three-pool display policy; never infer from equal quota."""
    identity = (name + " " + id_).lower()
    if "argon" in identity:
        return "argon"
    if "claude" in identity or "gpt" in identity:
        return "claude_gpt"
    return "gemini_shared"


class OAuthFailure(Exception):
    """Only fixed, sanitized messages may cross this boundary."""

    def __init__(self, message: str, *, reauth: bool = False):
        super().__init__(message)
        self.reauth = reauth


class OAuthCallbackRedaction:
    """Remove OAuth query secrets before Uvicorn produces its access log."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/oauth-callback":
            raw = scope.get("query_string", b"")
            scope["query_string"] = b""
            params = {}
            if len(raw) <= 16384:
                try:
                    parsed = parse_qs(raw.decode("ascii"), max_num_fields=12)
                    for key in ("state", "code", "error"):
                        values = parsed.get(key, [])
                        if len(values) == 1 and len(values[0]) <= 8192:
                            params[key] = values[0]
                except (ValueError, UnicodeError):
                    pass
            scope.setdefault("state", {})["oauth_callback_params"] = params
        await self.app(scope, receive, send)


class AntigravityAuth:
    def __init__(self, root: Path):
        defaults = json.loads((ROOT / "antigravity_defaults.json").read_text())
        self.client_id = os.environ.get("ANTIGRAVITY_CLIENT_ID") or defaults["client_id"]
        self.client_secret = os.environ.get("ANTIGRAVITY_CLIENT_SECRET") or defaults["client_secret"]
        self.user_agent = os.environ.get("ANTIGRAVITY_USER_AGENT") or \
            "antigravity/hub/2.29.0 (aidev_client; windows_amd64)"
        self.store = CredentialStore(root)
        self.client: httpx.AsyncClient | None = None
        # 一个出口代理一个连接池。OAuth 刷新与配额查询用的是**同一份凭据**，
        # 必须和对话请求从同一个 IP 出去；早先这里只认全局环境变量，账号级
        # proxy 只作用于对话请求，于是同一账号的令牌刷新从另一个 IP 出网 ——
        # 那正好是「一个凭据多个出口」的典型特征。
        self.clients: dict[str | None, httpx.AsyncClient] = {}
        self.pending: dict[str, dict] = {}
        self.states: dict[str, str] = {}
        self.lock = anyio.Lock()
        self.refresh_locks: dict[str, anyio.Lock] = {}

    @staticmethod
    def resolve_proxy(proxy: str | None) -> str | None:
        """账号级代理优先，其次全局，都没有则直连。与上游对话请求同一套规则。"""
        return proxy or (os.environ.get("GATEWAY_UPSTREAM_PROXY") or None)

    def http(self, proxy: str | None = None):
        # 调用方/测试注入的客户端优先：它代表「所有出口都用这个 transport」。
        if self.client is not None:
            return self.client
        resolved = self.resolve_proxy(proxy)
        client = self.clients.get(resolved)
        if client is None:
            client = self.clients[resolved] = httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=8), follow_redirects=False, trust_env=False,
                proxy=resolved,
            )
        return client

    async def aclose(self):
        for client in self.clients.values():
            await client.aclose()
        self.clients.clear()
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def json_request(self, method, url, *, token=None, proxy=None, **kwargs):
        headers = {"User-Agent": self.user_agent}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            async with self.http(proxy).stream(method, url, headers=headers, **kwargs) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 8 * 1024 * 1024:
                        raise OAuthFailure("Google 响应过大")
                    body.extend(chunk)
                try:
                    value = json.loads(body)
                except (ValueError, RecursionError):
                    raise OAuthFailure("Google 返回了无效响应") from None
                if not isinstance(value, dict):
                    raise OAuthFailure("Google 返回了无效响应")
                if not response.is_success:
                    if value.get("error") == "invalid_grant":
                        raise OAuthFailure("Google 授权已失效，请重新登录", reauth=True)
                    if response.status_code == 401:
                        raise OAuthFailure("Google 凭据已失效，请重新登录", reauth=True)
                    if response.status_code == 403:
                        raise OAuthFailure("Google 拒绝了该账号的访问，请在官方客户端检查账号权限或验证要求")
                    raise OAuthFailure("Google 请求失败（HTTP " + str(response.status_code) + "）")
                return value
        except httpx.HTTPError:
            raise OAuthFailure("无法连接 Google，请检查网络或 GATEWAY_UPSTREAM_PROXY") from None

    async def begin(self, base: str):
        url = urlsplit(base)
        port = url.port or (443 if url.scheme == "https" else 80)
        # Desktop OAuth always returns to this machine, never to a caller's host.
        redirect = "http://localhost:" + str(port) + "/oauth-callback"
        now = time.monotonic()
        async with self.lock:
            self.pending = {key: row for key, row in self.pending.items() if row["expires"] > now}
            self.states = {state: id_ for state, id_ in self.states.items() if id_ in self.pending}
            if len(self.pending) >= 32:
                raise OAuthFailure("待授权窗口过多，请稍后再试")
            id_ = secrets.token_urlsafe(24)
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            self.pending[id_] = {
                "expires": now + 600, "state": state, "verifier": verifier,
                "redirect": redirect, "status": "pending", "provider": "antigravity",
            }
            self.states[state] = id_
        query = {
            "client_id": self.client_id, "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        }
        return {"accountId": id_, "loginUrl": AUTH_URL + "?" + urlencode(query)}

    async def poll(self, id_: str):
        row = self.pending.get(id_)
        if row is None or row["expires"] <= time.monotonic():
            return {"done": True, "success": False, "error": "授权窗口已过期，请重新添加账号"}
        status = row["status"]
        return {"done": status in {"authorized", "error"}, "success": status == "authorized",
                "login": row.get("login", ""), "error": row.get("error", "")}

    @staticmethod
    def _token_data(reply, *, previous=None):
        token = reply.get("access_token")
        refresh = reply.get("refresh_token") or (previous or {}).get("refresh_token")
        expires = reply.get("expires_in", 3600)
        if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
            raise OAuthFailure("Google 未返回完整离线授权，请重新授权并确认授权范围")
        if type(expires) not in (int, float) or not math.isfinite(expires) or not 0 < expires <= 86400:
            raise OAuthFailure("Google 返回的令牌有效期无效")
        return {**(previous or {}), "access_token": token, "refresh_token": refresh,
                "expires_at": time.time() + expires}

    async def complete(self, params: dict, save_account):
        state = params.get("state", "")
        async with self.lock:
            id_ = self.states.pop(state, None)
            row = self.pending.get(id_)
            if row is None or row["expires"] <= time.monotonic() or row["status"] != "pending":
                raise OAuthFailure("授权状态无效、已使用或已过期，请重新添加账号")
            row["status"] = "processing"
        try:
            if params.get("error"):
                raise OAuthFailure("Google 授权未完成或已取消")
            code = params.get("code")
            if not code:
                raise OAuthFailure("回调中缺少授权码")
            reply = await self.json_request("POST", TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": row["redirect"], "code_verifier": row["verifier"],
            })
            credential = self._token_data(reply)
            identity = await self.json_request("GET", USER_URL, token=credential["access_token"])
            email = identity.get("email")
            if not isinstance(email, str) or not email or len(email) > 320:
                raise OAuthFailure("Google 未返回有效的账号身份")
            credential["email"] = email
            credential["subject"] = str(identity.get("id", email))
            credential["status"] = "authorized"
            account_id = "ag-" + hashlib.sha256(credential["subject"].encode()).hexdigest()[:24]
            warning = ""
            try:
                await self.discover(credential)
            except OAuthFailure as exc:
                warning = str(exc)
                credential["catalog_error"] = warning
            await anyio.to_thread.run_sync(self.store.save, account_id, credential)
            await save_account(account_id, credential)
            row.update(status="authorized", login=email, warning=warning)
            return warning
        except (OAuthFailure, OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, OAuthFailure) else "本机凭据或账号配置保存失败"
            row.update(status="error", error=message)
            raise OAuthFailure(message) from None
        finally:
            row.pop("verifier", None)
            row.pop("state", None)

    async def load(self, id_: str):
        try:
            return await anyio.to_thread.run_sync(self.store.load, id_)
        except (OSError, ValueError, KeyError):
            raise OAuthFailure("本机授权凭据不可读取，请重新登录", reauth=True) from None

    async def credential(self, id_: str, *, force=False, proxy=None):
        lock = self.refresh_locks.setdefault(id_, anyio.Lock())
        async with lock:
            value = await self.load(id_)
            if not force and value.get("expires_at", 0) > time.time() + 90:
                return value
            try:
                reply = await self.json_request("POST", TOKEN_URL, proxy=proxy, data={
                    "client_id": self.client_id, "client_secret": self.client_secret,
                    "refresh_token": value["refresh_token"], "grant_type": "refresh_token",
                })
                updated = self._token_data(reply, previous=value)
                updated["status"] = "authorized"
                updated.pop("auth_error", None)
            except OAuthFailure as exc:
                if exc.reauth:
                    value.update(status="reauth", auth_error=str(exc))
                    await anyio.to_thread.run_sync(self.store.save, id_, value)
                raise
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.store.save, id_, updated)
            return updated

    async def discover(self, credential: dict, proxy=None):
        token = credential["access_token"]
        project = credential.get("project_id")
        if not project:
            loaded = await self.json_request("POST", API_BASE + "/v1internal:loadCodeAssist",
                                             token=token, proxy=proxy,
                                             json={"metadata": {"ideType": "ANTIGRAVITY"}})
            for key in ("cloudaicompanionProject", "projectId", "project"):
                value = loaded.get(key)
                if isinstance(value, dict):
                    value = value.get("id")
                if isinstance(value, str) and value.strip():
                    project = value.strip()
                    break
            if not project:
                # Do not silently accept first-use terms or enroll a project.
                raise OAuthFailure("Google 已授权，但项目尚未就绪；请先在官方 Antigravity 完成首次使用，再刷新状态")
            credential["project_id"] = project
        reply = await self.json_request("POST", API_BASE + "/v1internal:fetchAvailableModels",
                                         token=token, proxy=proxy, json={"project": project})
        models = reply.get("models")
        if not isinstance(models, dict) or len(models) > 1024:
            raise OAuthFailure("Google 未返回有效的账号模型列表")
        sanitized = {}
        for id_, metadata in models.items():
            if not isinstance(id_, str) or not id_.strip() or len(id_) > 256:
                continue
            meta = metadata if isinstance(metadata, dict) else {}
            quota = meta.get("quotaInfo") if isinstance(meta.get("quotaInfo"), dict) else {}
            remaining = quota.get("remainingFraction")
            valid = type(remaining) in (int, float) and math.isfinite(remaining) and 0 <= remaining <= 1
            sanitized[id_] = {
                "name": str(meta.get("displayName") or id_)[:256],
                "remaining": round(remaining * 100, 2) if valid else None,
                "reset_time": str(quota.get("resetTime") or "")[:64],
                "pool": model_pool(str(meta.get("displayName") or id_), id_),
            }
        credential["catalog"] = sanitized
        credential["catalog_at"] = time.time()
        credential.pop("catalog_error", None)
        return credential

    async def refresh_catalog(self, id_: str, proxy=None):
        lock = self.refresh_locks.setdefault(id_, anyio.Lock())
        # credential() holds the same per-account lock only during refresh.
        value = await self.credential(id_, proxy=proxy)
        async with lock:
            # Reload the latest token before writing catalog metadata.
            value = await self.load(id_)
            await self.discover(value, proxy=proxy)
            await anyio.to_thread.run_sync(self.store.save, id_, value)
        return value
