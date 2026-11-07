"""Original channel-page RPC adapter. No credentials are returned to the UI."""

import copy
import hmac
import json
import math
import os
import re
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
from plugin_gateway import PluginGateway, PluginGatewayError
from protection import DispatchProtection, ProtectionBlocked

router = APIRouter()
# 已接入的渠道。`trae`（字节）与 `loomy`（讯飞）已下线：插件包里仍认得这两个
# id，但登录与模型都不再可用，留在这里只会让渠道页出现点不通的条目。
#
# `gemini_api` 是本机渠道：Google Generative Language 的官方 API Key，凭据与
# 出站都在本进程，和插件里的 `gemini`（网页 Cookie 逆向）完全是两回事。它必须
# 出现在 CHANNELS 里，否则 `/v1/models` 会把它过滤掉、渠道页也没有入口。
CHANNELS = ("codearts", "buddy", "workbuddy", "lobsterai", "qoder", "qodercn",
            "cline", "raccoon", "zcode", "minimax", "antigravity", "gemini_api")
RETIRED_CHANNELS = ("trae", "loomy")
# 本机持有的渠道（凭据在本进程，不转发给 pack.js）。写成集合而不是散落的
# `!= "antigravity"` 判断：新增一个本机渠道时，漏改一处就会让它被当成插件渠道
# 去调 bridge，页面表现为「账号消失了」或「操作失败」。
LOCAL_CHANNELS = frozenset({"antigravity", "gemini_api"})
COOKIE = "gateway_ui_session"
TTL = 8 * 3600


def channel_capabilities(channel: str) -> dict:
    """本机网关对该渠道**实际实现**的能力，供页面决定渲染哪些按钮。

    页面沿用原插件的静态能力表（`CHANNEL_CREDITS`），那张表描述的是插件侧
    13 家全部实现时的状态；本机网关是 Python 侧的实现。两边不一致时，
    页面就会渲染出一个点下去必然失败的按钮，用户只会看到「操作失败」而不知道
    是自己配错了还是根本没做。能力以这里为准。

    12 家插件渠道的积分查询/领取/签到现在**转发给插件**（凭据在
    `~/.dsh/.credentials.yaml`，由桥接进程读取），所以能力取决于 pack.js 里
    有没有对应分支，而不是取决于本机网关写了多少 Python。下面两张表就是
    pack.js `credits.status` / `credits.claimAll` 的分支清单；改那边要同步改这里，
    否则页面又会渲染出点不动的按钮。

    `plugin` 是给前端的分流标志：True 表示账号、凭据、积分都在插件的
    channel-pack 里，页面应当按插件行去渲染、并且只对这类渠道调
    `credits.balances` / `credits.status`。早先前端靠 `id !== 'antigravity'`
    判断，加一个本机渠道后就会把本机账号当插件渠道去查积分。
    """
    if channel == "antigravity":
        # 只有 antigravity 的配额目录在本机（OAuth 凭据在 .gateway-auth/）。
        return {"claim": False, "checkin": False, "creditStatus": False,
                "balances": True, "refresh": True, "plugin": False, "login": "oauth"}
    if channel == "gemini_api":
        # 官方 API Key 按 RPM/TPM 限流，没有积分包、没有签到、也没有配额一览接口；
        # 声明 balances=False，页面就不会为它渲染一行永远「无读数」的进度条。
        #
        # refresh=True 是真的：按钮按下会重拉 `/v1beta/models`，既对齐官方模型
        # 目录，也顺带用一次目录查询验证密钥还有效（被撤销时 400 API_KEY_INVALID，
        # 账号会被停用）。声明 False 会让页面留一个看着能点、实际什么都不做的按钮。
        return {"claim": False, "checkin": False, "creditStatus": False,
                "balances": False, "refresh": True, "plugin": False, "login": "key"}
    return {
        "claim": channel in _PLUGIN_CLAIM,
        "checkin": False,       # 没有后台签到引擎（自动签到是插件的调度，不在本机）
        "creditStatus": channel in _PLUGIN_CREDIT_STATUS,
        "balances": True,       # pack.js 对 12 家都有 credits.balances 分支
        "refresh": True,        # 令牌续期转发给插件的 account.refresh
        "plugin": True,
        "login": "vendor",
    }


# pack.js `credits.status` 有分支的渠道。qoder 系、raccoon 没有独立状态
# 端点，调用会得到 `unsupported provider`，所以不声明。
_PLUGIN_CREDIT_STATUS = frozenset({
    "codearts", "buddy", "workbuddy", "lobsterai", "trae", "cline", "minimax",
})

# pack.js `credits.claimAll` 有签到实现的渠道。cline / workbuddy / raccoon 明确
# 返回「后端没有签到接口」，不声明。
_PLUGIN_CLAIM = frozenset({
    "codearts", "buddy", "lobsterai", "qoder", "qodercn", "trae",
    "minimax",
})


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


# ── 插件探活结果的翻译 ──
#
# 上游把两种完全不同的拒绝写成同一个 `msg: "request illegal"`：
#   11140 → 账号存在异常行为，已被限制使用（HTTP 403，retry_policy.retry=false）
#   11128 → 这次调用没通过渠道校验，请重新发送（HTTP 400，可重发）
# 只看 `msg` 分不出来，必须认 `displayMsg.zh`（pack.js 的 `errorDetail2` 把
# 本地化文案排在最前，所以中文那句会出现在 message 里）。
#
# 而 pack.js 在 401/403 的收尾分支里把 `rotated` **写死成 true**
# （`contentRejectionError(productId, status, body, true)`，见 pack.js 36816-36823），
# 于是「本渠道只有 1 个账号、一次换号都没发生」也会报成
# 「全部账号均被服务端安全策略拦截（已逐个换号重试）」。用户没法从这句话里知道
# 真实情况，只能猜自己是不是又配错了。网关按事实重写：说清是哪个账号、
# 是账号级还是渠道级、能不能重试、这个渠道到底有没有可换的号。
_PROBE_RESTRICTED = "account-restricted"
_PROBE_GATE = "channel-verification"
_PROBE_EXPIRED = "credential-expired"


def probe_failure_kind(message: str) -> str | None:
    """从插件返回的失败文案里认出上游的裁定类别。认不出返回 None。"""
    text = message or ""
    lowered = text.lower()
    if "账号存在异常行为" in text or "帳號存在異常行為" in text \
            or "restricted due to unusual activity" in lowered:
        return _PROBE_RESTRICTED
    if "code 11128" in text or "渠道校验" in text or "请求被拦截" in text \
            or "request blocked" in lowered:
        return _PROBE_GATE
    if "所有账号均被拒绝" in text or "credential expired and refresh failed" in lowered:
        return _PROBE_EXPIRED
    return None


def _probe_detail(message: str) -> str:
    """取上游自己那句话：`…更换被拦账号：账号存在异常行为，已被限制使用 request illegal`。"""
    detail = (message or "").rsplit("：", 1)[-1].strip()
    detail = re.sub(r"\s*request\s*illegal\s*$", "", detail, flags=re.IGNORECASE)
    return detail or (message or "").strip()


def _probe_status(message: str) -> str:
    found = re.search(r"HTTP\s*(\d{3})", message or "")
    return found.group(1) if found else ""


def probe_failure_scope(total: int | None, usable: int | None) -> str:
    """这个渠道到底有没有可换的号——决定「换号」这句建议说不说得出口。"""
    if isinstance(total, int) and total <= 1:
        return ("本渠道只有这 1 个账号，没有可换的号：重试、续期、换模型都不会放行，"
                "要么等上游解封，要么再登录一个同渠道账号，要么用这个号到客户端提交反馈申诉。")
    if isinstance(total, int) and total > 1:
        free = usable if isinstance(usable, int) else total
        return (f"本渠道共 {total} 个账号（{free} 个还在调度里）：这次探活测的就是这个号，"
                "换号只影响真实对话，不会让本次测试变绿。")
    return "这是账号级限制，不是本机网关的故障：重试、续期、换模型都不会放行。"


def explain_plugin_probe_failure(value: dict, account_id: str = "", provider: str = "",
                                 nickname: str = "", total: int | None = None,
                                 usable: int | None = None) -> dict:
    """把插件 `account.test` 的失败结果改写成可读、可执行的说明。

    `ok` 为真、或认不出上游裁定的结果**原样返回**——网关不替插件编故事，
    只纠正它那句已被证伪的「已逐个换号重试」。
    """
    if not isinstance(value, dict) or value.get("ok") is not False:
        return value
    message = str(value.get("message") or value.get("error") or "")
    kind = probe_failure_kind(message)
    name = nickname or value.get("nickname") or account_id or "该账号"
    label = f"{name}（{account_id}）" if account_id and account_id != name else str(name)
    status = _probe_status(message)
    detail = _probe_detail(message)
    out = {**value, "raw": message}

    if kind == _PROBE_RESTRICTED:
        evidence = " · ".join(part for part in (f"HTTP {status}" if status else "",
                                                "11140", "上游 retry_policy.retry=false") if part)
        out.update({
            "code": _PROBE_RESTRICTED, "retryable": False, "restricted": True,
            "upstreamCode": 11140, "accountId": account_id, "provider": provider,
            "message": (f"账号 {label}被服务端限制使用：{detail}。（{evidence}）"
                        + probe_failure_scope(total, usable)),
        })
        return out
    if kind == _PROBE_GATE:
        status_text = f"HTTP {status} · " if status else ""
        # 「已升档」= pack.js 已经把内容防护逐级拉到最高档重发过，再说「重发一次」
        # 就是让用户白点。
        retry_hint = ("插件已把内容防护升到最高档仍被拒，稍后原样重试或换个模型即可"
                      if "已升档" in message else "原样重发一次即可")
        out.update({
            "code": _PROBE_GATE, "retryable": True, "restricted": False,
            "upstreamCode": 11128, "accountId": account_id, "provider": provider,
            "message": (f"渠道校验未通过（{status_text}code 11128）：这次调用被上游判为"
                        f"「不是官方客户端发出」，{retry_hint}；与账号额度和余额无关。"),
        })
        return out
    if kind == _PROBE_EXPIRED:
        status_text = f"HTTP {status}" if status else "HTTP 401"
        out.update({
            "code": _PROBE_EXPIRED, "retryable": False, "restricted": False,
            "upstreamCode": 401, "accountId": account_id, "provider": provider,
            "message": f"凭据已失效（{status_text}）：这个账号的令牌续期失败，请在插件里重新登录。",
        })
        return out
    # 认不出类别时只做一件最小的事：撤掉那条已被证伪的换号声明。
    if "已逐个换号重试" in message and isinstance(total, int) and total <= 1:
        out["message"] = (message.replace("，已逐个换号重试", "")
                          .replace("已逐个换号重试", ""))
    return out


def find_account_in(raw: dict, account_id: str) -> dict:
    """按 id 取本机 accounts.json 里的一条记录；没有就抛一句能读懂的话。

    与 `rpc()` 里的闭包 `find_account` 同义，但带 id 参数，供不在这层作用域里的
    方法（刷新/停用）复用 —— 闭包版本每次都要重写一遍 `next(...)`，漏掉一次
    就会把 `AttributeError` 冒到页面上。
    """
    entry = next((a for a in raw["accounts"] if a["id"] == account_id), None)
    if entry is None:
        raise ValueError("账号已被移除")
    return entry


def live_rate_limits(entry: dict, now_ms: int) -> dict[str, float]:
    """该账号身上**还没过期**的模型限流/封禁标记（`{模型: 解禁时刻ms}`）。

    pack.js 的 `modelRateLimits` 会把过期的标记留在表里（只有
    `getAvailableAccount` 顺手剔除），所以这里按时刻过滤：过期的标记不能让
    配额看板把账号显示成「冷却」。
    """
    raw = entry.get("modelRateLimits") if isinstance(entry, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for model, reset_at in raw.items():
        if not isinstance(model, str):
            continue
        if isinstance(reset_at, bool) or not isinstance(reset_at, (int, float)):
            continue
        if reset_at > now_ms:
            out[model] = float(reset_at)
    return out


def same_origin(request: Request) -> bool:
    return request.headers.get("origin") == str(request.base_url).rstrip("/")


def authorized(request: Request) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    # 带了 Origin 就必须是本机页面发出的。状态变更（POST）尤其如此：只认 Bearer
    # 会让「浏览器从任意站点发一个跨域 POST」直接改到号池与保护状态——Bearer
    # 头本来就该由非浏览器客户端发出，它不该同时成为绕过 CSRF 的通道。
    origin = request.headers.get("origin")
    if origin is not None and not same_origin(request):
        return False
    if scheme.lower() == "bearer" and hmac.compare_digest(value.encode(), request.app.state.api_key):
        return True
    manager = request.app.state.web
    now = time.monotonic()
    manager.sessions = {key: end for key, end in manager.sessions.items() if end > now}
    session = request.cookies.get(COOKIE, "")
    return manager.sessions.get(session, 0) > now and (
        request.method in {"GET", "HEAD"} or same_origin(request)
    )


def guard_rejection(request: Request):
    """区分「没登录」与「跨源被拒」。

    两者都返回 401 会让用户看到「请先连接本机网关」，而他明明刚连上——
    真正该说的是「这个请求不是本机页面发的」。
    """
    origin = request.headers.get("origin")
    if origin is not None and not same_origin(request):
        return failure("请求来源不是本机页面，已拒绝", 403)
    return failure("请先连接本机网关", 401)


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
        # 12 家的账号、凭据、模型与对话都由插件负责；这里只发现并代理。
        self.plugin = PluginGateway(bridge=self.bridge)
        self._balance_cache: dict[str, tuple[float, dict]] = {}
        # 调用保护必须是**号池正在用的那一个**，否则「上游拒绝 → 暂停」只写进
        # 一个没人读的实例：页面上看不到暂停，号池也照样换下一个号继续打。
        # lifespan 会注入进程级实例；这里先接住号池已有的，没有才自建一个，
        # 并立刻挂回号池与上游，保证三者永远指向同一处计数器。
        guard = getattr(pool, "protection", None)
        if guard is None:
            guard = DispatchProtection(path.parent / ".gateway-protection.json")
        self.protection = guard
        pool.protection = guard
        if getattr(upstream, "protection", None) is None:
            upstream.protection = guard

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

    async def refresh_gemini_account(self, id_: str):
        """重新拉官方模型目录，刷新一个 API Key 账号的模型表。

        这一步也是对密钥是否仍有效的真实检验：`/v1beta/models` 在密钥被撤销时
        回 400 `API_KEY_INVALID`，在上游封禁时回 403。以前「刷新」在 API Key
        账号上什么都不做，按钮看着成功、密钥其实早就废了。而且它会把**新增**
        的模型并进来（官方模型名会变），不覆盖用户已有的映射。
        """
        account = next((a for a in self.pool.settings.accounts if a.id == id_), None)
        if account is None:
            raise ValueError("账号已被移除")
        secret = account.token
        key = secret.get_secret_value() if secret is not None else ""
        if not key:
            raise ValueError("该账号没有可用的 API Key")
        from gemini_driver import endpoint_base, usable_models
        try:
            # 用号池自己的客户端，不临时新建：一个账号只能有一个出口，
            # 目录查询与对话必须走同一条代理链路（同 Antigravity 的约束）。
            client = self.upstream._client(account)
            catalog, exhausted = await usable_models(client, key, base=endpoint_base(account))
        except UpstreamFailure as exc:
            if exc.reauth:
                # 密钥作废：停用调度并如实告知。不停用的话它会被反复选中、
                # 每次都失败，把同一渠道其它账号（如果有）挡在后面。
                await self.commit(lambda raw: find_account_in(raw, id_).update(status="disabled"))
                raise ValueError("API Key 已失效或被撤销，已停用该账号，请重新添加密钥") from None
            raise ValueError(f"刷新失败：{exc.reason}") from None
        models = {f"gemini_api/{name}": name for name in catalog}
        # 今日额度耗尽的模型单独记：它们今天调不动，但明天能回来，不是「坏模型」。
        # 页面上按「今日额度用完」呈现，而不是从列表里悄悄消失。
        spent = {f"gemini_api/{name}": name for name in exhausted}
        def update(raw):
            entry = find_account_in(raw, id_)
            entry["models"] = {**models, **spent}
            entry["disabled_models"] = sorted(set(spent) - set(models))
        await self.commit(update)
        return {"ok": True, "models": len(models), "exhausted": len(spent)}

    @staticmethod
    def _pick_probe_model(aliases, patterns):
        """按「便宜的通用模型族」挑一个真实存在的别名，不写死版本号。

        Google 会下线旧模型 id（`no longer available to new users`），把
        `gemini-2.5-flash` 写进探活路径，等于让每个新账号第一次探活就吃 404。
        这里用正则匹配账号**实际拥有**的模型，优先 lite、再普通 flash。
        """
        for pattern in patterns:
            for alias in aliases:
                if re.fullmatch(pattern, alias):
                    return alias
        return None

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
            elif row["driver"] == "gemini":
                # 官方 API Key 不会过期，也没有可续期的令牌：能不能用只由上游决定。
                # 这里不写 `refreshable: True` 骗过页面的「令牌已死」判据，而是
                # 明确说明「没有令牌这回事」——refreshable 为 None 时页面既不显示
                # 需重登、也不会把刷新按钮画成可用。
                row.update(refreshable=None, expiresAt=0, authStatus="api_key")
        return result

    async def _delete_from_plugin(self, account_id: str) -> bool:
        """账号在插件里就由插件删；返回是否处理了。

        判据是「插件的账号表里有这个 id」，不是「本机表里没有」。先问插件再动
        本机表，避免把一个插件账号误判成「不存在」。
        """
        if not account_id:
            return False
        for provider in CHANNELS:
            if provider in LOCAL_CHANNELS:
                continue
            try:
                value = await self.bridge.call("account.list", {"provider": provider})
            except BridgeError:
                continue
            ids = {row.get("id") for row in (value or {}).get("accounts") or []}
            if account_id not in ids:
                continue
            try:
                await self.bridge.call("account.delete", {"accountId": account_id}, attempts=1)
            except BridgeError as exc:
                raise ValueError(f"删除失败：{exc}") from None
            self.bridged.pop(account_id, None)
            return True
        return False

    async def _plugin_channel_accounts(self, provider: str) -> list[dict]:
        """该渠道在插件里的账号原始行（含 `modelRateLimits`）。查不到返回空表。"""
        try:
            value = await self.bridge.call("account.list", {"provider": provider})
        except BridgeError:
            return []
        return [row for row in (value or {}).get("accounts") or []
                if isinstance(row, dict) and row.get("id")]

    async def _plugin_owner(self, account_id: str) -> str | None:
        """这个 id 属于插件里的哪个渠道；不在插件里返回 None。

        `account.update` / `account.refresh` / `account.test` 以前只看本机
        `accounts.json`，于是插件账号一按下就报「账号不存在」——账号明明在页面
        上、就在上一个接口里返回给前端过。三个动作的归属判断共用这一处，避免
        逐个补丁又漏掉一个。
        """
        if not account_id:
            return None
        for provider in CHANNELS:
            if provider in LOCAL_CHANNELS:
                continue
            for row in await self._plugin_channel_accounts(provider):
                if row.get("id") == account_id:
                    return provider
        return None

    async def _update_in_plugin(self, account_id: str, enabled: bool) -> None:
        try:
            await self.bridge.call("account.update", {
                "accountId": account_id, "patch": {"enabled": enabled}}, attempts=1)
        except BridgeError as exc:
            raise ValueError(f"更新失败：{exc}") from None

    async def _discard_placeholder(self, account_id: str) -> None:
        """删掉一个登录未完成的占位账号。失败只记日志，不阻断调用方。"""
        try:
            await self.bridge.call("account.delete", {"accountId": account_id}, attempts=1)
        except BridgeError:
            pass

    async def _reap_expired_windows(self) -> None:
        """回收已过期的登录窗口，连占位账号一起删掉。"""
        now = time.monotonic()
        expired = [key for key, row in self.bridged.items() if row["expires"] <= now]
        for key in expired:
            self.bridged.pop(key, None)
            await self._discard_placeholder(key)

    async def plugin_accounts(self, provider):
        """该渠道在插件（channel-pack）里的账号，映射成页面用的行。

        12 家的账号由插件持有：登录、凭据、刷新、限流都在那边。网关不复制凭据，
        只把「有哪些账号」如实报给页面——否则用户在插件里登录完了，这里还是 0/0。

        **占位记录只有「确实还在登录中」的才算数。** `account.create` 会先往插件
        账号表里写一条「昵称等于 id」的行，登录完成后才补上凭据。登录窗口中途丢了
        （浏览器被关、进程重启）时，这条记录会永久留着，形状与「正在登录」完全
        相同，于是页面上多出一个点不动的「等待授权」账号。

        判据因此不是「昵称等于 id」，而是「凭据是否存在」：
          - 有凭据 → 真账号，正常显示；
          - 没凭据且本次会话正在等它 → 等待授权；
          - 没凭据且没人等 → 遗留空壳，不显示并顺手删掉。
        最后一种以前靠「`account_id not in self.bridged`」判断，进程一重启就失效
        （新的 bridged 是空的，旧占位被当成新登录）。

        **还有限流/封禁标记。** 上游回 403 + `11140`（账号存在异常行为）时，
        pack.js 会给这个账号写一条 30 分钟的 `modelRateLimits` 冷却，期间它不参与
        选号。配额看板原先完全不看这张表，于是「测试失败、对话也不通」的账号在
        看板上照样显示「正常」。这里把未过期的标记翻成 `cooldown` 状态与剩余秒数，
        看板就能显示「冷却 N 分钟」。
        """
        try:
            value = await self.bridge.call("account.list", {"provider": provider})
        except BridgeError:
            return []
        entries = [entry for entry in (value or {}).get("accounts") or []
                   if isinstance(entry, dict) and entry.get("id")]
        placeholders = [entry["id"] for entry in entries
                        if (entry.get("nickname") or entry["id"]) == entry["id"]]
        # `present[id]` 三态：True 有凭据、False 确定没有、缺失则是「问不到」。
        # 问不到时退回旧判据（本次会话在等的那些），不再一律当成占位——那会把
        # 真账号也藏掉。
        present: dict[str, bool] = {}
        store_unreadable = False
        if placeholders:
            try:
                answer = await self.bridge.call("credential.presence", {
                    "provider": provider, "accountIds": placeholders})
                present = {k: v for k, v in (answer or {}).get("present", {}).items()
                           if isinstance(v, bool)}
            except Exception:  # noqa: BLE001 - 可选增强，任何失败都退回旧判据
                present = {}
                store_unreadable = True
        if store_unreadable and placeholders:
            # 凭据库读不出来时，占位账号既不能删（可能是真账号）也不能用
            # （没人能保证它有凭据）。这种「说不清」的状态要暂停调用并把原因
            # 写清楚，而不是闷着继续打——否则用户只会看到一串莫名失败。
            guard = getattr(self, "protection", None)
            if guard is not None:
                await guard.pause(reason="credential_store_unavailable")
        now_ms = int(time.time() * 1000)
        out, stale = [], []
        for entry in entries:
            account_id = entry["id"]
            nickname = entry.get("nickname") or account_id
            # 昵称等于 id 是「占位记录」的**候选**；有凭据就说明它其实是真账号
            # （登录完成了但没拿到昵称），不能当空壳删。
            candidate = nickname == account_id
            has_credential = present.get(account_id)
            # 只有**明确**知道凭据不存在（False）才算空壳。`None`（问不到）不是
            # 证据——把「未知」当成「没有」会把仍然有效的账号直接删掉，用户的
            # 登录就没了。这是不可逆操作，判据必须偏向保留。
            pending = candidate and has_credential is not False and account_id in self.bridged
            if candidate and has_credential is False and not pending:
                stale.append(account_id)
                continue
            enabled = entry.get("enabled") is not False
            # 上游 403 + 11140 时 pack.js 会给账号写一条 30 分钟冷却，期间不参与
            # 选号。看板原先不看这张表，被限制的账号照样显示「正常」。
            limits = {} if pending else live_rate_limits(entry, now_ms)
            cooling = bool(limits) and enabled
            if pending or not enabled:
                status = "disabled"
            elif cooling:
                status = "cooldown"
            else:
                status = "active"
            out.append({
                "id": account_id, "provider": provider,
                "driver": "channel-pack",
                "nickname": f"{nickname} · 等待授权" if pending else nickname,
                "enabled": enabled and not pending,
                "status": status,
                "cooldown_remaining_seconds":
                    max(0, round((min(limits.values()) - now_ms) / 1000)) if cooling else 0,
                "rate_limited_models": sorted(limits),
                "expiresAt": int(entry.get("expiresAt") or 0),
                "models": {}, "disabled_models": [],
                "in_flight": 0, "fail_count": 0, "max_concurrency": 1,
                "refreshable": bool(entry.get("refreshable")),
                # 页面据此把「插件里的账号」和「本机导入的上游」区分开。
                "source": "plugin",
                "pending": pending,
            })
        # 遗留空壳就地清掉：留着只会越堆越多，而且它们是本次会话不认识的记录。
        for account_id in stale:
            await self._discard_placeholder(account_id)
        return out

    async def plugin_models(self, provider, *, probed: list[str] | None = None):
        """该渠道可用的模型：取自插件网关的 `/v1/models`，按 `provider/` 前缀过滤。

        模型 id 必须与插件网关一致（`codearts/GLM-5.2`），否则对话时转发不过去。

        插件网关只暴露**当前启用**的模型。某个渠道全部模型被关掉时（用户可以在
        插件里关），这里会拿到空列表，页面就显示「模型 —」。那种情况下退回插件的
        `model.list`，让用户至少看到有哪些模型、可以去插件里开启。
        """
        prefix = provider + "/"
        if probed is not None:
            models = probed
        else:
            try:
                models = await self.plugin.probe()
            except PluginGatewayError:
                models = []
        listed = [{"id": model, "name": model[len(prefix):], "pool": None, "disabled": False}
                  for model in models if model.startswith(prefix)]
        if listed:
            return listed
        # 回退：插件自己的模型表（id 不带前缀，这里补上以保持对外一致）。
        try:
            value = await self.bridge.call("model.list", {"provider": provider})
        except BridgeError:
            return []
        out = []
        for row in (value or {}).get("models") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            out.append({"id": prefix + row["id"],
                        "name": row.get("name") or row["id"],
                        "pool": None,
                        "disabled": True,   # 插件网关没暴露它，对话会失败
                        "native_id": row["id"]})
        return out

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
            statuses = {
                channel: {
                    "accounts": {"total": len(group), "enabled": sum(r["status"] == "active" for r in group)},
                    "models": {"total": len(models), "disabled": sum(m["disabled"] for m in models)},
                    "closed": bool(group) and all(r["status"] == "disabled" for r in group),
                    "capabilities": channel_capabilities(channel),
                }
                for channel in CHANNELS
                for group in [[r for r in rows if r["provider"] == channel]]
                for models in [model_rows(group)]
            }
            # 12 家的账号在插件里，不在本机 accounts.json。不合并就会显示 0/0，
            # 而用户在插件里明明已经登录过。
            probed_models: list[str] | None = None
            for channel in CHANNELS:
                if channel in LOCAL_CHANNELS:
                    continue
                plugin_rows = await self.plugin_accounts(channel)
                if not plugin_rows:
                    continue
                base = statuses[channel]
                total = base["accounts"]["total"] + len(plugin_rows)
                enabled = base["accounts"]["enabled"] + sum(r["status"] == "active" for r in plugin_rows)
                statuses[channel]["accounts"] = {"total": total, "enabled": enabled}
                if not base["models"]["total"]:
                    if probed_models is None:
                        try:
                            probed_models = await self.plugin.probe()
                        except PluginGatewayError:
                            probed_models = []
                    models = await self.plugin_models(channel, probed=probed_models)
                    if models:
                        statuses[channel]["models"] = {"total": len(models),
                                                       "disabled": 0}
            return {"statuses": statuses}
        if method == "account.list":
            merged = [{k: v for k, v in r.items() if k not in {"models", "disabled_models", "model_details"}}
                      for r in selected]
            # 插件里的账号排在前面：那才是用户刚登录过、期待看到的东西。
            if provider is not None and provider not in LOCAL_CHANNELS:
                seen = {row["id"] for row in merged}
                merged = [r for r in await self.plugin_accounts(provider) if r["id"] not in seen] + merged
            return {"accounts": merged}
        if method == "model.list":
            models = model_rows(selected)
            if provider is not None and provider not in LOCAL_CHANNELS:
                aliases = {m["id"] for m in models}
                models = models + [m for m in await self.plugin_models(provider)
                                   if m["id"] not in aliases]
            return {"models": models}
        if method == "credits.status":
            # 签到状态归插件：各家的 `checked_in` 字段形状不同，pack.js 已经
            # 按渠道解析过。没有状态的渠道（qoder 系、raccoon、zcode）由 pack.js
            # 回 `unsupported provider`，这里当「没有可展示的状态」处理，不报错。
            if provider is None or provider in LOCAL_CHANNELS:
                return {"accounts": []}
            try:
                value = await self.bridge.call("credits.status", {"provider": provider})
            except BridgeError:
                return {"accounts": []}
            return value or {"accounts": []}
        if method == "credits.balances":
            if provider == "gemini_api":
                # 官方 API Key 没有积分包、没有剩余额度接口（配额按 RPM/TPM 在
                # 上游滑动窗口里算）。回空表而不是伪造 0：页面据此不画进度条。
                return {"accounts": [], "windowDays": 0}
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
            # 12 家的余额由插件查询：token 在 `~/.dsh/.credentials.yaml`，各家的
            # 余额端点、积分包结构、失败原因都在 pack.js 里。
            #
            # 以前这里一律返回「本机网关没有余额接口」，于是页面上每个插件账号
            # 都显示「未返回余额数据」——而用户的积分明明是可以查到的。网关不
            # 该替插件回答它其实答得出来的问题。
            force = bool(payload.get("force"))
            if not force and provider in self._balance_cache:
                cached_at, cached_reply = self._balance_cache[provider]
                if time.monotonic() - cached_at < 45.0:
                    return cached_reply
            try:
                value = await self.bridge.call("credits.balances", {
                    "provider": provider, **({"force": True} if force else {})}, attempts=1)
            except BridgeError as exc:
                reason = f"余额查询失败：{exc}"
                return {"accounts": [{"accountId": row["id"], "balance": None, "error": reason}
                                     for row in selected]}
            rows_by_id = {row["id"]: row for row in selected}
            # 插件账号不在 `selected`（那是本机 accounts.json 的行）里，所以
            # 白名单只能取「本机该渠道的账号 ∪ 插件该渠道的账号」。只用前者过滤
            # 会把插件的每一行都丢掉，返回一个看起来成功、实际全空的列表。
            plugin_rows = {row["id"]: row for row in await self.plugin_accounts(provider)}
            known = {**plugin_rows, **rows_by_id}
            merged = []
            for entry in (value or {}).get("accounts") or []:
                if not isinstance(entry, dict) or not entry.get("accountId"):
                    continue
                account_id = entry["accountId"]
                # 插件对「登录中」的占位账号也会回一行；页面上没有它就不显示。
                if account_id not in known:
                    continue
                merged.append({**entry, "provider": provider,
                               "nickname": entry.get("nickname") or known[account_id]["nickname"]})
            reply = {"accounts": merged, "windowDays": (value or {}).get("windowDays")}
            if merged:
                self._balance_cache[provider] = (time.monotonic(), reply)
            return reply
        if method == "credits.claimAll":
            # antigravity 的配额由本机 OAuth 目录读取，没有「签到」这个动作；
            # 桥会把 provider 映射成 pack.js 的 `gemini`，那边确实有领取实现，
            # 但用的不是本机这份凭据。转发过去只会打到一个无关账号上。
            if provider is None or provider == "antigravity":
                raise ValueError(unsupported(provider, "积分领取",
                                             "本机按 OAuth 目录读取配额，没有签到接口"))
            if provider == "gemini_api":
                raise ValueError(unsupported(provider, "积分领取",
                                             "官方 API Key 按 RPM/TPM 限流，没有签到积分"))
            # 领取/签到同样交给插件：预检、幂等处理、设备指纹轮换都在那边。
            # 没有实现的渠道由 pack.js 自己给出具体原因（例如「后端没有签到
            # 接口」），比网关编一句「尚未接入」准确。
            try:
                value = await self.bridge.call("credits.claimAll", {"provider": provider})
            except BridgeError as exc:
                raise ValueError(str(exc)) from None
            self._balance_cache.pop(provider, None)
            return value or {"results": [], "summary": {}}
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
            if provider == "gemini_api":
                # 官方 API Key 没有可跳转的登录页（Google 不会为一个 API Key
                # 发起浏览器授权）。这里返回导入窗口而不是一句「不支持」：
                # 「添加账号」按钮对每个渠道都存在，点下去必须到得了能填密钥的地方。
                self.pending = {k: v for k, v in self.pending.items()
                                if v["expires"] > time.monotonic()}
                if len(self.pending) >= 32:
                    raise ValueError("待添加窗口过多，请稍后重试")
                id_ = secrets.token_urlsafe(24)
                self.pending[id_] = {"provider": provider, "expires": time.monotonic() + 1800}
                return {"accountId": id_, "loginUrl": base + "/connect?request=" + id_}
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
            await self._reap_expired_windows()
            return {"accountId": value["accountId"], "loginUrl": value["loginUrl"]}
        if method == "account.importWindow":
            # 显式的导入窗口。原先 12 家的 account.create 直接返回这个页面，
            # 现在它们走厂商登录，导入仍要可达——用户手上已有的兼容上游配置
            # 不该因为上游登录能力补齐而失去入口。
            #
            # `gemini_api` 走的就是这个窗口：官方 API Key 没有登录页可跳，
            # 用户手动粘贴密钥才是唯一诚实的入口。antigravity 不行——它的凭据
            # 必须由本进程的 OAuth 流程生成（PKCE + 本地回调），手填一个
            # access_token 进来既不能续期也不对 project。
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
                    # 过期的占位账号必须一起删掉：`account.create` 会先写一条
                    # 「昵称等于 id」的空壳，登录没完成时它不会自己消失。放任不管，
                    # 账号表会堆成上百条，用户看到的是一片无法使用的假账号。
                    await self._discard_placeholder(account_id)
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
            # 插件里的账号不在本机 accounts.json 里。以前这里一律走本机表，
            # 于是删一个插件账号会报「账号不存在」——账号明明就在页面上。
            if await self._delete_from_plugin(account_id):
                return {"ok": True}
            def remove(raw):
                find_account(raw)
                raw["accounts"] = [a for a in raw["accounts"] if a["id"] != account_id]
            await self.commit(remove)
            return {"ok": True}
        if method == "account.update":
            patch = payload.get("patch", {})
            if not isinstance(patch, dict) or type(patch.get("enabled")) is not bool or set(patch) != {"enabled"}:
                raise ValueError("仅支持启用或停用账号")
            owner = next((r for r in rows if r["id"] == account_id), None)
            if owner is None and await self._plugin_owner(account_id):
                # 页面调用 account.update 时不带 provider，`rows` 里只有本机账号，
                # 所以插件账号在这里必然查不到。以前那就直接落到 find_account 抛
                # 「账号不存在」——调度开关因此点不动。
                await self._update_in_plugin(account_id, patch["enabled"])
                return {"ok": True}
            await self.commit(lambda raw: find_account(raw).update(
                status="active" if patch["enabled"] else "disabled"))
            return {"ok": True}
        if method == "account.refresh":
            target = next((r for r in rows if r["id"] == account_id), None)
            if target is None:
                plugin_provider = await self._plugin_owner(account_id)
                if plugin_provider is None:
                    raise ValueError("账号不存在")
                # 令牌续期是插件自己的事；但「刷新」按钮按下去必须有真实动作，
                # 否则用户看到的是一句成功的空操作。交给插件的 account.refresh。
                try:
                    await self.bridge.call("account.refresh", {"accountId": account_id}, attempts=1)
                except BridgeError as exc:
                    raise ValueError(f"刷新失败：{exc}") from None
                return {"ok": True}
            if target["driver"] == "antigravity":
                await self.refresh_oauth_account(account_id)
            elif target["driver"] == "gemini":
                # API Key 没有令牌可续，但「刷新」仍有真实动作：重新拉官方模型目录，
                # 把账号可用的模型表对齐上游。密钥被撤销时这里会明确报错并停用。
                await self.refresh_gemini_account(account_id)
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
                # 插件账号的探活是插件自己的 `account.test`（它知道选哪个模型、
                # 走哪条出站链路）。这里转发而不是假装本机测过了。
                provider_of_account = await self._plugin_owner(account_id)
                if provider_of_account is None:
                    raise ValueError("账号不存在")
                try:
                    value = await self.bridge.call(
                        "account.test", {"accountId": account_id}, attempts=1)
                except BridgeError as exc:
                    return {"ok": False, "error": str(exc)}
                if not isinstance(value, dict):
                    return {"ok": False, "error": "插件未返回测试结果"}
                if value.get("ok") is False:
                    # 插件把「账号被服务端限制」和「渠道校验没过」糊成同一句，
                    # 还会在只有 1 个账号时谎称「已逐个换号重试」。转发前翻译。
                    siblings = [row for row in
                                await self._plugin_channel_accounts(provider_of_account)
                                if row.get("id") != account_id]
                    now_ms = int(time.time() * 1000)
                    usable = sum(1 for row in siblings
                                 if row.get("enabled") is not False
                                 and not live_rate_limits(row, now_ms))
                    return explain_plugin_probe_failure(
                        value, account_id=account_id, provider=provider_of_account,
                        nickname=str(value.get("nickname") or ""),
                        total=len(siblings) + 1, usable=usable + 1)
                return value
            aliases = [m for m in target["models"] if m not in target["disabled_models"]]
            if not aliases:
                return {"ok": False, "error": "该账号的模型已全部关闭"}
            if target["driver"] == "gemini":
                # 官方 API Key 没有「配额目录」可查，探活就是真发一次最小请求。
                # 挑模型不能写死版本号：Google 会下线旧模型（`no longer available
                # to new users`），写死 `gemini-2.5-flash` 等于让每个新用户第一次
                # 探活就吃 404。按「便宜且通用」的**族**排序，用正则匹配当前账号
                # 实际拥有的模型。
                probe_model = self._pick_probe_model(aliases, (
                    r"^gemini_api/gemini-\d+(?:\.\d+)?-flash-lite$",
                    r"^gemini_api/gemini-\d+(?:\.\d+)?-flash$",
                    r"^gemini_api/gemini-flash-lite-latest$",
                    r"^gemini_api/gemini-flash-latest$",
                )) or aliases[0]
            else:
                # 优先选用轻量通用的标准探活模型（如 gemini-2.5-flash / flash-lite 等），避免 chat_ 开头的内部补全端点报错
                probe_model = aliases[0]
                for cand in ("antigravity/gemini-2.5-flash", "antigravity/gemini-3-flash",
                             "antigravity/gemini-2.5-flash-lite", "antigravity/gemini-3.8-flash-low"):
                    if cand in aliases:
                        probe_model = cand
                        break
            lifetime = AsyncExitStack()
            start = time.monotonic()
            try:
                lease = await self.pool.acquire(probe_model, {r["id"] for r in rows if r["id"] != account_id}, lifetime)
                try:
                    with anyio.fail_after(self.pool.settings.first_output_timeout_seconds):
                        await self.upstream.send_upstream_request(
                            lease.account, [{"role": "user", "content": "Reply OK."}], False,
                            model=probe_model, options={"max_tokens": 8}, lifetime=lifetime,
                        )
                except (UpstreamFailure, TimeoutError) as exc:
                    issue = exc if isinstance(exc, UpstreamFailure) else UpstreamFailure("test_timeout")
                    await lease.fail(issue)
                    return {"ok": False, "error": issue.reason}
                await lease.succeed()
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
        return guard_rejection(request)
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
    raw_key = getattr(request.app.state, "api_key", b"")
    api_key = raw_key.decode("ascii", errors="ignore") if isinstance(raw_key, bytes) else str(raw_key)
    return JSONResponse({"ok": True, "demo": manager.demo, "persisted": not manager.demo,
                         "api_base": str(request.base_url).rstrip("/") + "/v1",
                         "api_key": api_key,
                         "accounts": snapshot,
                         "models": request.app.state.pool.models, "model_details": model_details},
                        headers={"Cache-Control": "no-store"})


@router.get("/ui/quota")
async def quota_snapshot(request: Request):
    if not authorized(request):
        return guard_rejection(request)
    manager = request.app.state.web
    raw_key = getattr(request.app.state, "api_key", b"")
    api_key = raw_key.decode("ascii", errors="ignore") if isinstance(raw_key, bytes) else str(raw_key)
    api_base = str(request.base_url).rstrip("/") + "/v1"
    result = []
    for row in await manager.rows():
        if row["driver"] == "gemini":
            # 官方 API Key 账号必须出现在号池列表里 —— 否则用户在渠道页加完
            # 密钥，回到号池一个账号都看不到。但没有配额目录可报，所以只给
            # 模型数，不给百分比：给个 0% 会被读成「额度用完了」。
            result.append({"id": row["id"], "provider": "gemini_api", "driver": "gemini",
                           "name": row["nickname"], "status": row["status"],
                           "models": [], "model_count": len(row["models"]),
                           "updated_at": 0, "error": ""})
            continue
        if row["driver"] != "antigravity":
            # 其他本机驱动没有配额目录。不给它造一行 `models: []` 的空条目：
            # 页面的「最近重置」「Gemini 池」会把它当成读数缺失的账号，
            # 看起来像查询失败。
            continue
        try:
            value = await manager.ag.load(row["id"])
            models = [{"id": id_, "name": meta["name"], "remaining": meta.get("remaining"),
                       "reset_time": meta.get("reset_time", ""),
                       # reset_at 是不随区域设置变化的 epoch 秒；前端倒计时只用它，
                       # 本地化文本 reset_time 仅用于显示。
                       "reset_at": meta.get("reset_at"),
                       "pool": meta.get("pool", model_pool(meta["name"], id_))}
                      for id_, meta in value.get("catalog", {}).items()]
            result.append({"id": row["id"], "name": value.get("email", row["nickname"]),
                           "status": row["status"], "models": models,
                           "updated_at": value.get("catalog_at"), "error": value.get("catalog_error", "")})
        except OAuthFailure as exc:
            result.append({"id": row["id"], "name": row["nickname"], "status": "reauth",
                           "models": [], "error": str(exc)})
    return JSONResponse({"ok": True, "api_base": api_base, "api_key": api_key, "accounts": result},
                        headers={"Cache-Control": "no-store"})


@router.post("/ui/quota/refresh")
async def quota_refresh(body: RPCInput, request: Request):
    if not authorized(request):
        return guard_rejection(request)
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


@router.get("/ui/protection")
async def protection_status(request: Request):
    if not authorized(request):
        return guard_rejection(request)
    guard = getattr(request.app.state.web, "protection", None)
    if guard is None:
        return failure("调用保护暂不可用", 503)
    return JSONResponse(await guard.snapshot(), headers={"Cache-Control": "no-store"})


@router.post("/ui/protection")
async def protection_action(body: RPCInput, request: Request):
    if not authorized(request):
        return guard_rejection(request)
    guard = getattr(request.app.state.web, "protection", None)
    if guard is None:
        return failure("调用保护暂不可用", 503)
    if body.method == "protection.pause":
        await guard.pause()
    elif body.method == "protection.resume":
        if body.payload.get("acknowledge") is not True:
            return failure("请先检查暂停原因并确认恢复")
        try:
            await guard.resume()
        except ProtectionBlocked:
            return failure("无法保存恢复状态，请检查本机文件权限", 503)
    else:
        return failure("未知的调用保护操作")
    return JSONResponse(await guard.snapshot(), headers={"Cache-Control": "no-store"})


@router.post("/ui/rpc")
async def rpc(body: RPCInput, request: Request):
    if not authorized(request):
        return guard_rejection(request)
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
    """读添加窗口的元信息。

    这个窗口是 `window.open` 出来的独立标签页，它**不能假设自己有会话**：
    桌面壳的 Cookie 与用户浏览器不共享，窗口被拖到别的浏览器打开时那条
    `/ui/session` 换来的 Cookie 根本不存在。于是它一进来就被 401 挡住，页面只
    显示「添加窗口不可用」，用户看到的却是「请先连接本机网关」——他明明刚从
    控制台点了按钮。

    这里的凭据是 `request_id` 本身：32 字符随机、单次保存即失效、TTL 有限、
    只对这一个 pending 条目有效。它由本机控制台交给本机页面，不是可以猜的。
    """
    manager = request.app.state.web
    pending = manager.pending.get(request_id)
    if pending is not None and pending["expires"] > time.monotonic():
        return {"ok": True, "provider": pending["provider"], "demo": manager.demo}
    if not authorized(request):
        return guard_rejection(request)
    return failure("添加窗口已过期", 404)


@router.post("/ui/import")
async def import_account(body: ImportInput, request: Request):
    manager = request.app.state.web
    pending_now = manager.pending.get(body.request_id)
    # 与 GET 同理：这次性 request_id 是窗口自身的凭据。但它只对「这个窗口」
    # 有效——别的路径（账号列表、改配置）仍然要求会话/Bearer，所以这里不是
    # 把整个 /ui 打开，只是让「粘一把 Key 进来」这一件事成立。
    #
    # 仍然要求同源：别的站点替用户 POST 一个它猜不到的 request_id 本来就不可能，
    # 但多加一道同源检查不花成本，也挡住「本机别的前端」顺手改号池。
    if pending_now is None and not authorized(request):
        return guard_rejection(request)
    if not same_origin(request):
        return failure("请求来源不是本机页面，已拒绝", 403)
    draft = copy.deepcopy(body.account)
    # 驱动按渠道决定，不由页面填写：页面只提交「填了什么」，不提交「走哪个实现」。
    # gemini_api 的凭据是官方 API Key，必须落到 `driver="gemini"`；写成 openai
    # 会把它当成一条 OpenAI 兼容上游去 POST，地址和鉴权头全都不对。
    #
    # 这些校验必须在**占位窗口还开着**的时候做完再往下走：以前写在锁外、
    # 又在 `pending["saving"] = True` 之后，抛错就直接变成 500，而且那个窗口
    # 会永久停在 saving 态（再提交只会得到 409）。
    if manager.pending.get(body.request_id, {}).get("provider") == "gemini_api":
        draft["driver"] = "gemini"
        # 官方端点由驱动按需推导，页面填的空串不能存成 `endpoint: ""`
        # （校验会把它当成「给了个不是 URL 的东西」）。
        if not draft.get("endpoint"):
            draft.pop("endpoint", None)
        if draft.get("cookie") or draft.get("cookie_env"):
            return failure("Gemini API Key 渠道不收 Cookie，请改用 token 或 token_env")
        if not draft.get("token") and not draft.get("token_env"):
            return failure("Gemini API Key 渠道需要填 API Key")
    async with manager.lock:
        pending = manager.pending.get(body.request_id)
        if pending is None or pending["expires"] <= time.monotonic() or pending.get("saved") or pending.get("saving"):
            return failure("添加窗口已完成或已过期", 409)
        provider = pending["provider"]
        draft["channel"] = provider
        if provider != "gemini_api":
            draft["driver"] = "demo" if manager.demo else "openai"
        pending["saving"] = True
    account = draft
    if provider == "gemini_api":
        # 密钥保存前先走与号池相同的代理选择规则拉官方模型目录：表单只填 Key，
        # `models` 来自 Google 当前实际公布的 generateContent 清单。
        # 目录查失败就保留添加窗口以便重试，不能把猜测的模型写成已可用。
        from gemini_driver import API_BASE, usable_models
        from config import Account
        from upstream_client import _proxy_for
        import httpx
        try:
            probe = Account.model_validate({**account, "models": {"gemini_api/_probe": "gemini-2.5-flash"}})
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                          proxy=_proxy_for(probe),
                                          timeout=httpx.Timeout(30, connect=8, pool=4, write=20)) as client:
                catalog, exhausted = await usable_models(client, probe.token.get_secret_value(),
                                                         base=probe.endpoint or API_BASE)
            account["models"] = {f"gemini_api/{name}": name
                                 for name in list(catalog) + list(exhausted)}
            account["disabled_models"] = sorted(f"gemini_api/{name}" for name in exhausted)
        except (ValueError, ValidationError, UpstreamFailure, httpx.HTTPError, TimeoutError) as exc:
            pending.pop("saving", None)
            if isinstance(exc, UpstreamFailure):
                detail = "API Key 验证失败" if exc.reauth else f"官方模型目录查询失败：{exc.reason}"
            elif isinstance(exc, (httpx.HTTPError, TimeoutError)):
                detail = "官方模型目录连接失败，请检查网络后重试"
            else:
                detail = "密钥或接口基址格式有误"
            return failure(detail)
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
    # 回传识别到的模型数：API Key 模式下模型清单是本机查出来的，用户看不到
    # 自己填了什么，保存后得给一句「识别到 N 个可用模型」，否则不知道成没成。
    return {"ok": True, "models": len(account.get("models") or {})}
