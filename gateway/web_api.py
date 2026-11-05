"""Original channel-page RPC adapter. No credentials are returned to the UI."""

import copy
import hmac
import json
import math
import os
import secrets
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from html import escape
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from account_manager import AccountManager, PoolUnavailable, UpstreamFailure, _State
from bridge_client import BridgeError, PackBridge
from config import Settings
from antigravity_auth import AntigravityAuth, OAuthFailure, model_pool

router = APIRouter()
CHANNELS = ("codearts", "buddy", "workbuddy", "lobsterai", "qoder", "qodercn",
            "trae", "cline", "loomy", "raccoon", "minimax", "zcode", "antigravity")
COOKIE = "gateway_ui_session"
TTL = 8 * 3600


def channel_capabilities(channel: str) -> dict:
    """本机网关对该渠道**实际实现**的能力，供页面决定渲染哪些按钮。

    页面沿用原插件的静态能力表（`CHANNEL_CREDITS`），那张表描述的是插件侧
    13 家全部实现时的状态；本机网关是 Python 侧的子集实现。两边不一致时，
    页面就会渲染出一个点下去必然失败的按钮，用户只会看到「操作失败」而不知道
    是自己配错了还是根本没做。能力以这里为准。
    """
    real_quota = channel == "antigravity"
    return {
        "claim": False,        # 没有任何渠道在这一侧接入积分领取
        "checkin": False,      # 没有后台签到引擎
        "creditStatus": False,  # 没有「今日已签到」这类状态
        "balances": real_quota,  # 只有 antigravity 有真实配额目录
        "refresh": real_quota,   # 只有 antigravity 有可刷新的上游目录
    }


def failure(message: str, status: int = 400):
    return JSONResponse({"ok": False, "error": {"message": message}}, status_code=status)


def unsupported(provider: str | None, feature: str, hint: str = "") -> str:
    """统一的「本机网关没做这件事」文案。

    宁可显式报错，也不要返回 `{"accounts": []}` 这种空成功：空成功在页面上
    长成「—」或「未返回余额数据」，和「接口临时 502」看不出区别，用户只能反复
    重试一个永远不会成功的东西。这里把「没实现」和「查询失败」分开。
    """
    scope = f"{provider} 渠道" if provider else "本机网关"
    text = f"{scope}的{feature}尚未接入本机网关"
    return f"{text}：{hint}" if hint else text


def same_origin(request: Request) -> bool:
    return request.headers.get("origin") == str(request.base_url).rstrip("/")


def authorized(request: Request) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and hmac.compare_digest(value.encode(), request.app.state.api_key):
        return True
    manager = request.app.state.web
    now = time.monotonic()
    manager.sessions = {key: end for key, end in manager.sessions.items() if end > now}
    session = request.cookies.get(COOKIE, "")
    return manager.sessions.get(session, 0) > now and (
        request.method in {"GET", "HEAD"} or same_origin(request)
    )


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    key: str = Field(default="", max_length=512)
    bootstrap: str = Field(default="", max_length=512)


class RPCInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    method: str = Field(max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    request_id: str = Field(max_length=128)
    account: dict[str, Any]


class WebManager:
    def __init__(self, pool: AccountManager, path: Path, upstream):
        self.pool, self.path, self.upstream = pool, path, upstream
        self.raw = json.loads(path.read_text(encoding="utf-8-sig"))
        for account in self.raw["accounts"]:
            if account.get("channel") == "gemini":
                account["channel"] = "antigravity"
        self.demo = all(a.driver == "demo" for a in pool.settings.accounts)
        self.sessions: dict[str, float] = {}
        self.pending: dict[str, dict] = {}
        self.lock = anyio.Lock()
        self.bootstrap = os.environ.pop("GATEWAY_UI_BOOTSTRAP", "")
        self.ag = AntigravityAuth(path.parent / ".gateway-auth")
        self.upstream.antigravity = self.ag
        # 12 家渠道的登录委托给插件的 channel-pack（Node 子进程）。
        # 桥是惰性启动的：只用 antigravity 的用户不必为它付进程开销。
        self.bridge = PackBridge()
        self.bridged: dict[str, dict] = {}

    def _write(self, raw, path=None):
        # Atomic replacement prevents a partial credential/config file on crash.
        name = None
        try:
            target = path or self.path
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent,
                                             prefix=".accounts-", suffix=".tmp", delete=False) as handle:
                name = handle.name
                json.dump(raw, handle, ensure_ascii=True, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
            name = None
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    async def commit(self, transform, *, activate_real=False):
        # Mutations are infrequent. Keep the state lock during the short atomic
        # disk commit so no account can acquire a lease between delete checks.
        with anyio.CancelScope(shield=True):
            async with self.lock, self.pool.condition:
                target = self.path
                raw = copy.deepcopy(self.raw)
                if activate_real and self.demo:
                    target = self.path.parent / "accounts.json"
                    if target.exists() and target != self.path:
                        try:
                            raw = json.loads(target.read_text(encoding="utf-8-sig"))
                            prior = Settings.model_validate(raw)
                            if any(a.driver == "demo" for a in prior.accounts):
                                raise ValueError()
                        except (ValueError, OSError, ValidationError):
                            raise ValueError("已有真实账号配置无法读取，请先检查 accounts.json") from None
                    else:
                        raw["accounts"] = []
                transform(raw)
                try:
                    settings = Settings.model_validate(raw)
                except (ValidationError, ValueError, TypeError):
                    raise ValueError("账号配置无效，请检查 ID、地址、模型映射和凭据") from None
                old = {state.account.id: state for state in self.pool.states}
                new_ids = {a.id for a in settings.accounts}
                removed = [state for id_, state in old.items() if id_ not in new_ids]
                if any(state.in_flight for state in removed):
                    raise ValueError("账号正在处理请求，请稍后移除")
                if not self.demo or activate_real:
                    await anyio.to_thread.run_sync(self._write, raw, target)
                states = []
                for account in settings.accounts:
                    state = old.get(account.id)
                    if state is None:
                        state = _State(account, account.status, account.fail_count,
                                       time.monotonic() + settings.cooldown_seconds
                                       if account.status == "cooldown" else 0)
                    else:
                        if state.account.status != account.status:
                            state.status = account.status
                            state.cooldown_until = time.monotonic() + settings.cooldown_seconds \
                                if account.status == "cooldown" else 0
                        state.account = account
                    states.append(state)
                self.pool.states = states
                self.pool.settings = settings
                self.pool.cursor = self.pool.cursor % len(states) if states else 0
                self.raw = raw
                self.path = target
                if activate_real:
                    self.demo = False
                self.pool.condition.notify_all()
            for state in removed:
                client = self.upstream.clients.pop(state.account.id, None)
                if client is not None:
                    await client.aclose()
                if state.account.driver == "antigravity":
                    async with self.ag.refresh_locks.setdefault(state.account.credential_id, anyio.Lock()):
                        await anyio.to_thread.run_sync(self.ag.store.delete, state.account.credential_id)

    async def save_oauth_account(self, id_: str, credential: dict):
        models = {"antigravity/" + name: name for name in credential.get("catalog", {})}
        account = {
            "id": id_, "channel": "antigravity", "driver": "antigravity",
            "credential_id": id_, "nickname": credential["email"], "models": models,
            "status": "active", "max_concurrency": 1,
        }
        def update(raw):
            existing = next((a for a in raw["accounts"] if a["id"] == id_), None)
            if existing is None:
                raw["accounts"].append(account)
            else:
                existing.update(account, max_concurrency=existing.get("max_concurrency", 1),
                                disabled_models=[m for m in existing.get("disabled_models", []) if m in models])
        await self.commit(update, activate_real=True)

    async def refresh_oauth_account(self, id_: str):
        # 配额目录查询与对话请求共用同一凭据，出口也必须一致。
        account = next((a for a in self.pool.settings.accounts if a.id == id_), None)
        value = await self.ag.refresh_catalog(id_, proxy=self.ag.resolve_proxy(
            account.proxy if account else None))
        models = {"antigravity/" + name: name for name in value.get("catalog", {})}
        def update(raw):
            entry = next((a for a in raw["accounts"] if a["id"] == id_), None)
            if entry is None:
                raise ValueError("账号已被移除")
            entry["models"] = models
            entry["disabled_models"] = [m for m in entry.get("disabled_models", []) if m in models]
        await self.commit(update)
        return value

    async def rows(self):
        async with self.pool.condition:
            self.pool._eligible("", set())
            now = time.monotonic()
            result = [
                {"id": s.account.id, "provider": s.account.channel,
                 "driver": s.account.driver,
                 "nickname": (s.account.nickname or s.account.id) +
                 (f" · 冷却 {max(0, math.ceil(s.cooldown_until - now))}s"
                  if s.status == "cooldown" else ""),
                 "enabled": s.status != "disabled", "status": s.status,
                 "expiresAt": 0, "models": dict(s.account.models),
                 "disabled_models": list(s.account.disabled_models),
                 "in_flight": s.in_flight, "fail_count": s.fail_count,
                 "max_concurrency": s.account.max_concurrency}
                for s in self.pool.states
            ]
        for row in result:
            if row["driver"] == "antigravity":
                try:
                    value = await self.ag.load(row["id"])
                    row["refreshable"] = value.get("status") != "reauth"
                    row["expiresAt"] = int(value.get("expires_at", 0) * 1000)
                    row["authStatus"] = value.get("status", "authorized")
                    row["model_details"] = value.get("catalog", {})
                except OAuthFailure:
                    row.update(refreshable=False, authStatus="reauth")
        return result

    async def rpc(self, method, payload, base):
        provider = payload.get("provider")
        account_id = payload.get("accountId")
        rows = await self.rows()
        if provider is not None and provider not in CHANNELS:
            raise ValueError("未知渠道")
        selected = [r for r in rows if r["provider"] == provider]

        def find_account(raw):
            account = next((a for a in raw["accounts"] if a["id"] == account_id), None)
            if account is None:
                raise ValueError("账号不存在")
            return account

        def model_rows(group):
            aliases = sorted({alias for row in group for alias in row["models"]})
            return [{"id": alias, "name": meta.get("name", alias),
                     "pool": model_pool(meta.get("name", alias), alias) if native else None,
                     "disabled": all(alias in r["disabled_models"] for r in group if alias in r["models"])}
                    for alias in aliases
                    for matches in [[r for r in group if alias in r["models"]]]
                    for native in [any(r.get("driver") == "antigravity" for r in matches)]
                    for meta in [next((r.get("model_details", {}).get(r["models"][alias], {})
                                      for r in matches if r.get("model_details", {}).get(r["models"][alias])), {})]]

        if method == "provider.status":
            return {"statuses": {
                channel: {
                    "accounts": {"total": len(group), "enabled": sum(r["status"] == "active" for r in group)},
                    "models": {"total": len(models), "disabled": sum(m["disabled"] for m in models)},
                    "closed": bool(group) and all(r["status"] == "disabled" for r in group),
                    "capabilities": channel_capabilities(channel),
                }
                for channel in CHANNELS
                for group in [[r for r in rows if r["provider"] == channel]]
                for models in [model_rows(group)]
            }}
        if method == "account.list":
            return {"accounts": [{k: v for k, v in r.items() if k not in {"models", "disabled_models", "model_details"}}
                                 for r in selected]}
        if method == "model.list":
            return {"models": model_rows(selected)}
        if method == "credits.status":
            # 只有 buddy / minimax 在页面里读这个字段（今日签到徽标）。本机网关没有
            # 签到体系，返回空数组就是「没有可展示的签到状态」，页面不渲染徽标。
            return {"accounts": []}
        if method == "credits.balances":
            if provider == "antigravity":
                balances = []
                for row in selected:
                    try:
                        value = await self.refresh_oauth_account(row["id"]) if payload.get("force") else await self.ag.load(row["id"])
                        packages = [{"name": meta["name"], "remaining": meta["remaining"],
                                     "modelId": id_, "resetTime": meta.get("reset_time", ""),
                                     "pool": meta.get("pool", model_pool(meta["name"], id_))}
                                    for id_, meta in value.get("catalog", {}).items() if meta.get("remaining") is not None]
                        # In the original quota UI, total is only a "known data"
                        # flag/count. Never average independent quota pools.
                        balance = {"total": len(packages),
                                   "packages": packages} if packages else None
                        balances.append({"accountId": row["id"], "balance": balance,
                                         **({"error": value["catalog_error"]} if value.get("catalog_error") else {})})
                    except OAuthFailure as exc:
                        balances.append({"accountId": row["id"], "balance": None, "error": str(exc)})
                return {"accounts": balances}
            # 其余渠道没有可查询的余额接口。返回空数组会让每一行显示「未返回余额数据」，
            # 与「接口 502」完全同形；这里把原因写进每个账号，页面逐行照出来。
            reason = unsupported(provider, "余额查询",
                                 "该渠道在本机只能是导入的兼容上游，余额以你自己的上游为准")
            return {"accounts": [{"accountId": row["id"], "balance": None, "error": reason}
                                 for row in selected]}
        if method == "credits.claimAll":
            # 只有 antigravity 有真实配额；其余渠道在本机网关里是「导入的兼容上游」，
            # 没有可签到的账号体系。以前这里报的是「当前驱动尚未接入」，页面把它当
            # 成一次失败的写操作，用户会反复重试一个永远不可能成功的按钮。
            raise ValueError(unsupported(provider, "积分领取",
                                         "多数渠道只有导入的兼容上游，没有签到接口"))
        if method == "usage.autoCheckin":
            if "enabled" in payload:
                raise ValueError(unsupported(provider, "自动签到", "本机网关没有后台签到引擎"))
            # 读操作返回 None 是「本机没有这个引擎」，页面据此不渲染那个开关；
            # 这比返回 {enabled:false} 更诚实——后者看起来像「关着」，其实点不动。
            return {"autoCheckin": None}
        if method == "account.create":
            if provider not in CHANNELS:
                raise ValueError("请选择渠道")
            if provider == "antigravity":
                # antigravity 有自己的 Python OAuth（凭据落在 .gateway-auth/，
                # 由本进程直接使用），不走桥。
                return await self.ag.begin(base)
            # 其余 12 家用插件自己的 channel-pack 登录：厂商端点、PKCE、DPoP、
            # 二维码页、轮询节奏都在那边，网关只负责把 URL 交出去。
            try:
                value = await self.bridge.call("account.create", {"provider": provider})
            except BridgeError as exc:
                raise ValueError(f"无法启动 {provider} 登录：{exc}") from None
            if not isinstance(value, dict) or not value.get("loginUrl"):
                raise ValueError(f"{provider} 登录未返回登录地址")
            # 记住归属：login.poll 需要 provider 才能问到对的账号。
            self.bridged[value["accountId"]] = {"provider": provider,
                                                "expires": time.monotonic() + 1800}
            self.bridged = {k: v for k, v in self.bridged.items() if v["expires"] > time.monotonic()}
            return {"accountId": value["accountId"], "loginUrl": value["loginUrl"]}
        if method == "account.importWindow":
            # 显式的导入窗口。原先 12 家的 account.create 直接返回这个页面，
            # 现在它们走厂商登录，导入仍要可达——用户手上已有的兼容上游配置
            # 不该因为上游登录能力补齐而失去入口。
            if provider not in CHANNELS or provider == "antigravity":
                raise ValueError("该渠道不支持导入")
            self.pending = {k: v for k, v in self.pending.items() if v["expires"] > time.monotonic()}
            if len(self.pending) >= 32:
                raise ValueError("待添加窗口过多，请稍后重试")
            id_ = secrets.token_urlsafe(24)
            self.pending[id_] = {"provider": provider, "expires": time.monotonic() + 600}
            return {"accountId": id_, "loginUrl": base + "/connect?request=" + id_}
        if method == "login.poll":
            if account_id in self.ag.pending:
                return await self.ag.poll(account_id)
            if account_id in self.bridged:
                entry = self.bridged[account_id]
                if entry["expires"] <= time.monotonic():
                    self.bridged.pop(account_id, None)
                    return {"done": True, "success": False, "error": "授权窗口已过期，请重新添加账号"}
                try:
                    value = await self.bridge.call("login.poll", {
                        "accountId": account_id, "provider": entry["provider"]})
                except BridgeError as exc:
                    return {"done": True, "success": False, "error": str(exc)}
                if isinstance(value, dict) and value.get("done") is True:
                    self.bridged.pop(account_id, None)
                    if value.get("success") is True:
                        # 凭据由桥接进程持有；这里只报告成功，不复制令牌——
                        # 网关读不到也用不上别的渠道的凭据。
                        return {"done": True, "success": True, "login": entry["provider"]}
                    return {"done": True, "success": False,
                            "error": str(value.get("error") or "登录未完成")}
                return {"done": False}
            pending = self.pending.get(account_id)
            if pending is not None and pending["expires"] >= time.monotonic():
                return {"done": bool(pending.get("saved")), "success": bool(pending.get("saved")),
                        "login": pending.get("saved", "")}
            return {"done": True, "success": False, "error": "添加窗口已过期"}
        if method == "account.delete":
            def remove(raw):
                find_account(raw)
                raw["accounts"] = [a for a in raw["accounts"] if a["id"] != account_id]
            await self.commit(remove)
            return {"ok": True}
        if method == "account.update":
            patch = payload.get("patch", {})
            if not isinstance(patch, dict) or type(patch.get("enabled")) is not bool or set(patch) != {"enabled"}:
                raise ValueError("仅支持启用或停用账号")
            await self.commit(lambda raw: find_account(raw).update(
                status="active" if patch["enabled"] else "disabled"))
            return {"ok": True}
        if method == "account.refresh":
            target = next((r for r in rows if r["id"] == account_id), None)
            if target is None:
                raise ValueError("账号不存在")
            if target["driver"] == "antigravity":
                await self.refresh_oauth_account(account_id)
            return {"ok": True}
        if method == "model.clearDead":
            # 页面在 model.dead === true 时把开关渲染成「恢复」。本机网关的模型表
            # 来自账号配置或 OAuth 目录，没有「上游已下架」这个标记，所以永远没有
            # 可恢复的项。显式说明，而不是落到末尾那句「尚未实现该操作」。
            raise ValueError(unsupported(provider, "下架模型恢复",
                                         "模型表直接取自账号配置，不存在需要恢复的下架项"))
        if method in {"model.setDisabled", "model.setAllDisabled"}:
            if provider not in CHANNELS or type(payload.get("disabled")) is not bool:
                raise ValueError("模型开关参数无效")
            model_id = payload.get("modelId")
            if method == "model.setDisabled" and not any(model_id in row["models"] for row in selected):
                raise ValueError("模型不存在")
            def change(raw):
                for account in raw["accounts"]:
                    if account.get("channel", "buddy") != provider:
                        continue
                    targets = set(account["models"]) if method == "model.setAllDisabled" else \
                        {model_id} & account["models"].keys()
                    disabled = set(account.get("disabled_models", []))
                    account["disabled_models"] = sorted(disabled | targets if payload["disabled"] else disabled - targets)
            await self.commit(change)
            return {"ok": True}
        if method == "account.test":
            target = next((r for r in rows if r["id"] == account_id), None)
            if target is None:
                raise ValueError("账号不存在")
            aliases = [m for m in target["models"] if m not in target["disabled_models"]]
            if not aliases:
                return {"ok": False, "error": "该账号的模型已全部关闭"}
            lifetime = AsyncExitStack()
            start = time.monotonic()
            try:
                lease = await self.pool.acquire(aliases[0], {r["id"] for r in rows if r["id"] != account_id}, lifetime)
                try:
                    with anyio.fail_after(self.pool.settings.first_output_timeout_seconds):
                        await self.upstream.send_upstream_request(
                            lease.account, [{"role": "user", "content": "Reply OK."}], False,
                            model=aliases[0], options={"max_tokens": 8}, lifetime=lifetime,
                        )
                except (UpstreamFailure, TimeoutError) as exc:
                    issue = exc if isinstance(exc, UpstreamFailure) else UpstreamFailure("test_timeout")
                    await lease.fail(issue)
                    return {"ok": False, "error": issue.reason}
                return {"ok": True, "ms": round((time.monotonic() - start) * 1000)}
            except PoolUnavailable:
                return {"ok": False, "error": "账号已停用、冷却中或并发已满"}
            finally:
                with anyio.CancelScope(shield=True):
                    await lifetime.aclose()
        raise ValueError("当前驱动尚未实现该操作")


@router.post("/ui/session")
async def login(body: SessionInput, request: Request):
    if not same_origin(request):
        return failure("请从本机页面连接", 403)
    manager = request.app.state.web
    async with manager.lock:
        boot_ok = bool(manager.bootstrap and body.bootstrap
                       and hmac.compare_digest(manager.bootstrap.encode(), body.bootstrap.encode()))
        key_ok = bool(body.key and hmac.compare_digest(body.key.encode(), request.app.state.api_key))
        if not boot_ok and not key_ok:
            return failure("网关密钥无效", 401)
        if boot_ok:
            manager.bootstrap = ""
        now = time.monotonic()
        manager.sessions = {key: expiry for key, expiry in manager.sessions.items() if expiry > now}
        if len(manager.sessions) >= 64:
            return failure("会话过多，请稍后再试", 429)
        session = secrets.token_urlsafe(32)
        manager.sessions[session] = now + TTL
    response = JSONResponse({"ok": True})
    response.set_cookie(COOKIE, session, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=TTL, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/ui/state")
async def state(request: Request):
    if not authorized(request):
        return failure("请先连接本机网关", 401)
    manager = request.app.state.web
    snapshot = await request.app.state.pool.snapshot()
    rows = await manager.rows()
    model_details = {}
    for row in rows:
        for alias, native_id in row["models"].items():
            meta = row.get("model_details", {}).get(native_id, {})
            model_details[alias] = {"name": meta.get("name", alias),
                                    "pool": model_pool(meta.get("name", alias), native_id)
                                    if row["driver"] == "antigravity" else None}
    return JSONResponse({"ok": True, "demo": manager.demo, "persisted": not manager.demo,
                         "api_base": str(request.base_url).rstrip("/") + "/v1",
                         "accounts": snapshot,
                         "models": request.app.state.pool.models, "model_details": model_details},
                        headers={"Cache-Control": "no-store"})


@router.get("/ui/quota")
async def quota_snapshot(request: Request):
    if not authorized(request):
        return failure("请先连接本机网关", 401)
    manager = request.app.state.web
    result = []
    for row in await manager.rows():
        if row["driver"] != "antigravity":
            continue
        try:
            value = await manager.ag.load(row["id"])
            models = [{"id": id_, "name": meta["name"], "remaining": meta.get("remaining"),
                       "reset_time": meta.get("reset_time", ""),
                       "pool": meta.get("pool", model_pool(meta["name"], id_))}
                      for id_, meta in value.get("catalog", {}).items()]
            result.append({"id": row["id"], "name": value.get("email", row["nickname"]),
                           "status": row["status"], "models": models,
                           "updated_at": value.get("catalog_at"), "error": value.get("catalog_error", "")})
        except OAuthFailure as exc:
            result.append({"id": row["id"], "name": row["nickname"], "status": "reauth",
                           "models": [], "error": str(exc)})
    return JSONResponse({"ok": True, "accounts": result}, headers={"Cache-Control": "no-store"})


@router.post("/ui/quota/refresh")
async def quota_refresh(body: RPCInput, request: Request):
    if not authorized(request):
        return failure("请先连接本机网关", 401)
    id_ = body.payload.get("accountId")
    errors = []
    for row in await request.app.state.web.rows():
        if row["driver"] != "antigravity" or (id_ and row["id"] != id_):
            continue
        try:
            await request.app.state.web.refresh_oauth_account(row["id"])
        except (OAuthFailure, ValueError, OSError) as exc:
            errors.append(str(exc) if isinstance(exc, OAuthFailure) else "本机账号状态更新失败")
    return {"ok": True, "errors": errors}


@router.post("/ui/rpc")
async def rpc(body: RPCInput, request: Request):
    if not authorized(request):
        return failure("请先连接本机网关", 401)
    try:
        value = await request.app.state.web.rpc(body.method, body.payload, str(request.base_url).rstrip("/"))
        return JSONResponse({"ok": True, "value": value}, headers={"Cache-Control": "no-store"})
    except (ValueError, OAuthFailure) as exc:
        return failure(str(exc))
    except OSError:
        return failure("配置写入失败，请检查文件权限", 500)


@router.get("/oauth-callback", include_in_schema=False)
async def oauth_callback(request: Request):
    params = getattr(request.state, "oauth_callback_params", {})
    try:
        with anyio.CancelScope(shield=True):
            with anyio.fail_after(100):
                warning = await request.app.state.web.ag.complete(
                    params, request.app.state.web.save_oauth_account)
        message = "Google 授权已保存，账号已加入本机号池。"
        if warning:
            message += " 模型或配额暂未就绪：" + warning
        status = 200
    except OAuthFailure as exc:
        message, status = str(exc), 400
    except TimeoutError:
        message, status = "授权处理超时，请回到管理页重新尝试。", 504
    html = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Antigravity 授权</title>" \
        "<body><h2>Antigravity 授权</h2><p>" + escape(message) + \
        "</p><p>可以关闭此窗口，返回本机管理页查看账号。</p></body></html>"
    return HTMLResponse(html, status_code=status, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/ui/import/{request_id}")
async def import_details(request_id: str, request: Request):
    if not authorized(request):
        return failure("请先连接本机网关", 401)
    manager = request.app.state.web
    pending = manager.pending.get(request_id)
    if pending is None or pending["expires"] <= time.monotonic():
        return failure("添加窗口已过期", 404)
    return {"ok": True, "provider": pending["provider"], "demo": manager.demo}


@router.post("/ui/import")
async def import_account(body: ImportInput, request: Request):
    if not authorized(request):
        return failure("请先连接本机网关", 401)
    manager = request.app.state.web
    async with manager.lock:
        pending = manager.pending.get(body.request_id)
        if pending is None or pending["expires"] <= time.monotonic() or pending.get("saved") or pending.get("saving"):
            return failure("添加窗口已完成或已过期", 409)
        provider = pending["provider"]
        pending["saving"] = True
    account = copy.deepcopy(body.account)
    account["channel"] = provider
    account["driver"] = "demo" if manager.demo else "openai"
    def add(raw):
        if any(a["id"] == account.get("id") for a in raw["accounts"]):
            raise ValueError("账号 ID 已存在")
        raw["accounts"].append(account)
    try:
        # Pending completion and config commit are serialized by the RPC
        # management lock inside commit. Account IDs are additionally unique.
        await manager.commit(add)
    except ValueError as exc:
        pending.pop("saving", None)
        return failure(str(exc))
    except OSError:
        pending.pop("saving", None)
        return failure("配置写入失败，请检查文件权限", 500)
    pending["saved"] = account.get("nickname") or account.get("id", "")
    pending.pop("saving", None)
    return {"ok": True}
