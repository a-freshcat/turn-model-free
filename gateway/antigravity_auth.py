"""Antigravity OAuth and account discovery, independent of the channel UI.

Protocol references: CLIProxyAPI internal/auth/antigravity and sdk/auth.
Google PKCE/state rules follow the official installed-app OAuth specification.
"""

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import httpx

from credential_store import CredentialStore
from native_history import ToolSignatureCache

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


# 上游 resetTime 的两种形状：
#   1) ISO 8601（`2026-11-06T10:33:19Z`）—— 直接用；
#   2) 本地化文本（`6/11/2026 下午 6:33:19`）—— 月/日顺序随区域设置，**不可猜**。
# 第 2 种用 day/month 两种解释各算一次，取「不早于现在」里较早的那个：
# 重置时间必然在未来，把已经过去的解释丢掉就唯一确定了。
_LOCALIZED = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*(上午|下午|AM|PM)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$",
    re.IGNORECASE,
)


def _reset_epoch(raw) -> float | None:
    """把上游 resetTime 归一成 epoch 秒；认不出返回 None（前端显示「未知」）。

    绝不返回一个「猜出来」的时间：错的重置时间比没有更糟——它会让人以为配额
    刚刚重置过，从而误判账号是不是还有额度。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    # 形状 1：ISO 8601（含 Z / 时区偏移）
    iso = text.replace("Z", "+00:00")
    candidate = iso
    try:
        stamp = datetime.fromisoformat(candidate)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except ValueError:
        pass

    # 形状 2：本地化文本
    found = _LOCALIZED.match(text)
    if not found:
        return None
    first, second, year, meridiem, hour, minute, second_text = found.groups()
    hour = int(hour)
    minute = int(minute)
    sec = int(second_text or 0)
    if meridiem:
        afternoon = meridiem in ("下午", "PM", "pm")
        if hour == 12:
            hour = 12 if afternoon else 0
        elif afternoon:
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= sec <= 59):
        return None

    now = time.time()
    options = []
    for day, month in ((int(first), int(second)), (int(second), int(first))):
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        try:
            stamp = datetime(int(year), month, day, hour, minute, sec).timestamp()
        except ValueError:
            continue
        options.append(stamp)
    if not options:
        return None
    future = [stamp for stamp in options if stamp >= now - 60]
    if future:
        return min(future)
    # 两种解释都已在过去：说明这是「已过期的重置点」，取较晚的那个更接近事实。
    return max(options)


class OAuthFailure(Exception):
    """Only fixed, sanitized messages may cross this boundary."""

    def __init__(self, message: str, *, reauth: bool = False, reason='antigravity_authorization', retry_after=0):
        super().__init__(message)
        self.reauth = reauth
        self.reason = reason
        self.retry_after = retry_after


# 鉴权/配额 RPC 的端点名白名单。URL 里可能有 project / account 片段，所以**不记
# 原始 path**，只记一个固定标签。
_AUTH_RPC_LABELS = (
    ("loadcodeassist", "loadCodeAssist"),
    ("retrieveuserquotasummary", "retrieveUserQuotaSummary"),
    ("fetchavailablemodels", "fetchAvailableModels"),
    ("oauth2", "oauth2_token"),
    ("userinfo", "userinfo"),
    ("onboarduser", "onboardUser"),
)


def _auth_rpc_label(url: str) -> str:
    lowered = str(url).lower()
    for needle, label in _AUTH_RPC_LABELS:
        if needle in lowered:
            return label
    return "other"


def _capture_auth_rate_limit(status, body: bytes, headers, url) -> None:
    """把鉴权/配额 RPC 的 429 写进同一份脱敏证据文件。

    只补观测，不改变控制流：调用方仍会照原样抛 `OAuthFailure`。

    为什么要做：生成请求的 429 有完整证据，鉴权 RPC 的 429 却完全没有。
    于是「两个账号 3 秒内一起 429、配额桶却健康」这个现象，既无法归因到
    生成请求限流（形状/密度/输出量都已实测排除），也无法检查是不是鉴权 RPC
    在限流 —— 证据从一开始就没被采集。

    刻意**自包含**，不调用 `antigravity_driver._resource_limit_record`：
    两个模块的版本会各自漂移（本机启动目录与开发仓库就不是同一版本），
    依赖对端内部函数会让这里的捕获在没有该函数时静默失效 —— 诊断代码
    静默失效比没有诊断更糟，因为它看起来在工作。
    """
    try:
        error = _auth_error_object(body)
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": "auth_rpc",
            "rpc": _auth_rpc_label(url),
            "http_status": status,
            # 与生成请求记录同形，事后只需要一个解析器。这里的窗口是**进程内**
            # 上游发送计数（含生成请求），对鉴权 RPC 而言是背景信息而非自身速率。
            "rate_window": _auth_rate_window(),
            "error_keys": sorted(error) if isinstance(error, dict) else [],
            "error_code": error.get("code") if error.get("code") in (429, "429") else None,
            "error_status": error.get("status") if isinstance(error.get("status"), str) else None,
            "message_len": len(error["message"]) if isinstance(error.get("message"), str) else None,
            "message_labels": _auth_message_labels(error.get("message")),
            "rpc_types": [],
            "detail_keys": [],
            "reason": "unknown",
            "reason_values_seen": [],
            "reason_filtered": False,
            "domains": [],
            "retry_delay_seconds": [],
            "quota_ids": [],
            "retry_after_header": _auth_retry_after(headers),
            "request_id_fingerprints": _auth_request_ids(headers),
        }
        path = Path(__file__).resolve().parent / ".gateway-rate-limit.jsonl"
        if path.exists() and path.stat().st_size > 256 * 1024:
            path.replace(path.with_suffix(".jsonl.1"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        logging.getLogger(__name__).warning(
            "Antigravity auth RPC rate limit: rpc=%s http_status=%s upstream_status=%s",
            record["rpc"], status, record.get("error_status") or "unknown")
    except Exception:  # noqa: BLE001
        # 与生成请求的捕获同样的不变式：诊断永不破坏请求路径。
        try:
            logging.getLogger(__name__).debug("auth rate-limit capture skipped", exc_info=True)
        except Exception:  # noqa: BLE001
            pass


def _auth_error_object(body: bytes) -> dict:
    """Extract the error object; malformed bodies degrade to an empty dict."""
    try:
        value = json.loads(body) if body else None
    except (ValueError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    inner = value.get("error")
    return inner if isinstance(inner, dict) else value


def _auth_rate_window() -> dict:
    """进程内上游发送计数，与生成请求记录同形。

    取不到就返回空值而不是省略键：证据文件的**结构**要保持一致，
    否则事后按字段读取的脚本会在不同来源之间遇到缺键。
    """
    try:
        import antigravity_driver as drv
        return drv.rate_window_snapshot()
    except Exception:  # noqa: BLE001
        return {"last_60s": None, "last_300s": None, "last_900s": None}


def _auth_message_labels(message) -> list[str]:
    """Whitelisted phrases only — the raw sentence never lands on disk."""
    labels = (
        ("resource has been exhausted", "resource_exhausted"),
        ("quota", "quota"),
        ("rate limit", "rate_limit"),
        ("exceeded", "exceeded"),
        ("permission", "permission"),
        ("unsupported location", "unsupported_location"),
        ("not available in your location", "unsupported_location"),
        ("temporarily", "temporarily"),
        ("try again", "try_again"),
    )
    if not isinstance(message, str) or len(message) > 4096:
        return []
    lowered = message.lower()
    return [label for needle, label in labels if needle in lowered]


def _auth_retry_after(headers) -> float:
    try:
        from upstream_client import _retry_after
        return _retry_after(headers.get("retry-after")) if headers is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def _auth_request_ids(headers) -> list[str]:
    """Opaque upstream ids are hashed, never stored raw."""
    found = []
    if headers is None:
        return found
    for key in ("x-goog-request-id", "x-request-id", "x-cloud-trace-context"):
        value = headers.get(key)
        if isinstance(value, str) and value:
            head = value.split("/", 1)[0]
            if re.fullmatch(r"[!-~]{1,256}", head):
                found.append(hashlib.sha256(head.encode("ascii")).hexdigest()[:16])
    return found


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
        self.tool_signatures=ToolSignatureCache(path=root / "tool-signatures.json")
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
        from upstream_client import _retry_after
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
                    if response.status_code in (408,429,500,502,503,504):
                        reason = 'antigravity_http_'+str(response.status_code)
                        # 鉴权/配额 RPC 的 429 此前完全不留证据：直接抛 OAuthFailure，
                        # 既不写日志也不落 `.gateway-rate-limit.jsonl`。于是「某个 429
                        # 其实来自 loadCodeAssist / retrieveUserQuota 而不是生成请求」
                        # 这条最可能的解释，永远无法证实也无法排除 —— 两个账号会在
                        # 3 秒内先后 429 而配额桶健康，正是这种形状。
                        #
                        # 这里只补证据，不改变控制流：同一个 OAuthFailure 照旧抛出。
                        # 记录函数自带脱敏（只写固定枚举、数值、键名，不写正文/令牌）。
                        if response.status_code == 429:
                            _capture_auth_rate_limit(response.status_code, body,
                                                     response.headers, url)
                        raise OAuthFailure('Google 暂时无法处理请求（HTTP '+str(response.status_code)+'）',
                                           reason=reason,
                                           retry_after=_retry_after(response.headers.get('retry-after')))
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
                        raise OAuthFailure("Google 拒绝了该账号的访问，请在官方客户端检查账号权限或验证要求",
                                           reason='antigravity_http_403')
                    raise OAuthFailure("Google 请求失败（HTTP " + str(response.status_code) + "）")
                return value
        except httpx.HTTPError:
            raise OAuthFailure("无法连接 Google，请检查网络或 GATEWAY_UPSTREAM_PROXY",
                               reason='antigravity_auth_transport') from None

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
                # 上游的 resetTime 是**本地化字符串**（`6/11/2026 下午 6:33:19`），
                # 月/日顺序在不同区域设置下会反过来：浏览器按美式读成 6 月 11 日，
                # 而它其实是 11 月 6 日，于是倒计时算出「已到时间」这种假读数。
                # 这里补一个不依赖区域设置的 epoch 秒，前端只用它做倒计时。
                "reset_at": _reset_epoch(quota.get("resetTime")),
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
