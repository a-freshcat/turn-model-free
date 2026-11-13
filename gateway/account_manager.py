"""每进程一个号池。锁只保护状态，网络 I/O 永远在锁之外。"""

import logging
import math
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

import anyio

from config import Account, Settings, Status
from failure_policy import is_transient_failure

LOG = logging.getLogger("gateway.pool")
T = TypeVar("T")


class UpstreamFailure(Exception):
    """驱动提供结构化失败，避免把上游正文、凭据或 URL 写入日志。"""

    def __init__(self, reason: str, *, retry_after: float = 0, reauth: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after
        self.reauth = reauth


class InvalidRequest(Exception):
    """Caller input unsupported by a driver; never penalize an account."""


class PoolUnavailable(Exception):
    def __init__(self, *, retry_after: int | None = None, budget_exhausted: bool = False,
                 failure_reason: str | None = None, cooling_reason: str | None = None):
        self.retry_after = retry_after
        self.budget_exhausted = budget_exhausted
        # 「本次请求自己失败的原因」。只有真的打过上游才可能有值。
        self.failure_reason = failure_reason
        # 「账号正在冷却的原因」。本轮一次上游都没打，靠它区分
        # 「正在为限流退避」和「账号被停用」——否则两者都被报成 503。
        self.cooling_reason = cooling_reason

    @property
    def rate_limited(self) -> bool:
        """真正需要客户端等待的限流状态，无论原因是本轮还是上一轮留下的。"""
        return any(reason in _RATE_LIMIT_REASONS
                   for reason in (self.failure_reason, self.cooling_reason))


# HTTP 429 的各种落地名：native 驱动带 `antigravity_` 前缀，旧适配器不带。
_RATE_LIMIT_REASONS = frozenset({"antigravity_http_429", "gemini_http_429"})


@dataclass
class _State:
    account: Account
    status: Status
    fail_count: int
    cooldown_until: float = 0
    in_flight: int = 0
    last_failure_reason: str | None = None
    last_failure_at: float = 0
    transient_streak: int = 0
    last_transient_at: float = 0


class Lease:
    """租约从发送前持有到响应完成、报错或客户端断开。"""

    def __init__(self, pool: "AccountManager", state: _State):
        self.pool, self.state = pool, state
        self.released = False

    @property
    def account(self) -> Account:
        return self.state.account

    async def fail(self, failure: UpstreamFailure) -> None:
        guard = getattr(self.pool, 'protection', None)
        if guard is not None and self.account.driver != 'demo':
            await guard.observe_failure(failure.reason)
        async with self.pool.condition:
            state = self.state
            state.fail_count += 1
            state.last_failure_reason = (failure.reason if re.fullmatch(r'[a-z0-9_]{1,64}', failure.reason)
                                         else 'upstream_failure')
            state.last_failure_at = time.time()
            state.status = "disabled" if failure.reauth else "cooldown"
            delay = self.pool.settings.cooldown_seconds
            if is_transient_failure(failure.reason) and not failure.reauth:
                now = time.monotonic()
                state.transient_streak = min(8, state.transient_streak + 1) if now - state.last_transient_at < 300 else 1
                state.last_transient_at = now
                # A deliberately smaller legacy cooldown remains respected.
                delay = min(delay, self.pool.settings.network_cooldown_max_seconds,
                            self.pool.settings.network_cooldown_seconds * 2 ** (state.transient_streak - 1))
            else:
                state.transient_streak = 0
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + max(delay, failure.retry_after),
            )
            LOG.warning("account=%s outcome=%s fail_count=%d",
                        state.account.id, failure.reason, state.fail_count)
            self.pool.condition.notify_all()

    async def succeed(self) -> None:
        """Reset adaptive backoff only after a complete response, never on SSE preflight."""
        async with self.pool.condition:
            self.state.transient_streak = 0
            self.state.last_transient_at = 0

    async def release(self) -> None:
        async with self.pool.condition:
            if not self.released:
                self.released = True
                self.state.in_flight -= 1
                self.pool.condition.notify_all()


def _cooling_reason(states: list[_State]) -> str | None:
    """把「正在冷却」归因成一个可路由的原因。

    优先返回限流原因：如果这批冷却的账号里有任何一个是因为 429 退避，
    客户端唯一有意义的动作就是等，而不是把整个渠道判死。
    """
    reasons = [state.last_failure_reason for state in states if state.last_failure_reason]
    for reason in reasons:
        if reason in _RATE_LIMIT_REASONS:
            return reason
    return reasons[0] if reasons else None


def jitter_before_request(account: Account) -> Awaitable[None]:
    """请求前的随机延迟。

    延迟导入：`upstream_client` 反过来要 `config`，模块级导入会成环。
    """
    from upstream_client import _apply_jitter
    return _apply_jitter(account)


class AccountManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.condition = anyio.Condition()
        self.cursor = 0
        self.cursors = {}
        now = time.monotonic()
        self.states = [
            _State(a, a.status, a.fail_count,
                   now + settings.cooldown_seconds if a.status == "cooldown" else 0)
            for a in settings.accounts
        ]
    @property
    def models(self) -> list[str]:
        return sorted({model for state in self.states for model in state.account.models
                       if model not in state.account.disabled_models})

    async def snapshot(self) -> list[dict]:
        """只返回运行状态，不暴露 URL、凭据、设备头或消息。"""
        async with self.condition:
            self._eligible("", set())
            now = time.monotonic()
            return [
                {"id": s.account.id, "status": s.status, "fail_count": s.fail_count,
                 "last_failure_reason": s.last_failure_reason, "last_failure_at": s.last_failure_at,
                 "transient_failure_streak": s.transient_streak,
                 "in_flight": s.in_flight, "max_concurrency": s.account.max_concurrency,
                 "cooldown_remaining_seconds": max(0, math.ceil(s.cooldown_until - now))
                 if s.status == "cooldown" else 0}
                for s in self.states
            ]

    def _eligible(self, model: str, excluded: set[str]) -> list[_State]:
        now = time.monotonic()
        eligible = []
        for state in self.states:
            if state.status == "cooldown" and state.cooldown_until <= now:
                state.status = "active"
            if (state.status != "disabled" and model in state.account.models
                    and model not in state.account.disabled_models
                    and state.account.id not in excluded):
                eligible.append(state)
        return eligible

    async def acquire(self, model: str, excluded: set[str], lifetime: AsyncExitStack) -> Lease:
        deadline = time.monotonic() + self.settings.pool_wait_seconds
        async with self.condition:
            while True:
                eligible = self._eligible(model, excluded)
                # Argon, Claude/GPT and ordinary Gemini are independent quota pools.
                # Other drivers use per-channel/model rotation (e.g. official Gemini RPM).
                first = eligible[0].account if eligible else None
                if first is not None and first.driver == 'antigravity':
                    from antigravity_auth import model_pool
                    group = (first.channel, model_pool(model, first.models[model]))
                else:
                    group = (first.channel, first.driver, model) if first else ('unknown', model)
                start = self.cursors.get(group, self.cursor)
                available = {
                    s.account.id for s in eligible if s.status == "active"
                    and s.in_flight < s.account.max_concurrency
                }
                for offset in range(len(self.states)):
                    index = (start + offset) % len(self.states)
                    state = self.states[index]
                    if state.account.id in available:
                        self.cursor = (index + 1) % len(self.states)
                        self.cursors[group] = self.cursor
                        state.in_flight += 1
                        lease = Lease(self, state)
                        lifetime.push_async_callback(lease.release)
                        return lease

                remaining = deadline - time.monotonic()
                busy = any(s.status == "active" for s in eligible)
                # 全部账号都在冷却时，`busy` 是 False —— 原实现让这个分支立刻
                # 抛出，`pool_wait_seconds` 的预算一次都没被用过。实测：
                # pool_wait_seconds 取 1 / 10 / 30，全都在 0.00s 内失败。
                #
                # 代价落在客户端：pi-ai 从不设置 `providerRetryAfterMs`（整份
                # dsh-llm-pi-ai 里零次赋值），所以 dsh-llm-retry 只能用本地退避
                # 0.5+1+2+4+8 = 15.5s，而 429 的冷却窗默认 20s。15.5 < 20，五次
                # 重试全落在冷却里，一个本该「等一下就好」的状态变成整回合失败。
                #
                # 所以冷却期**必须**消耗预算去等：等到冷却结束就正常服务，等不到
                # 才报错。这与「忙」的等待不同 —— 冷却到期时刻是已知的。
                cooling_left = [s.cooldown_until - time.monotonic() for s in eligible
                                if s.status == "cooldown"]
                if remaining <= 0 or not (busy or cooling_left):
                    # 冷却时间可计算；槽位释放时间不可预测。
                    cooling_states = [s for s in self._eligible(model, set())
                                      if s.status == "cooldown"]
                    cooling = [s.cooldown_until for s in cooling_states]
                    active = any(s.status == "active" for s in self._eligible(model, set()))
                    retry = max(1, math.ceil(min(cooling) - time.monotonic())) if cooling and not active else None
                    # 带出冷却原因：账号因 429 退避时，这是一个「请稍后再试」而不是
                    # 「池里没有可用账号」。少这一个字段，调用方只能看到
                    # failure_reason=None，于是把限流报成 503。
                    raise PoolUnavailable(
                        retry_after=retry,
                        cooling_reason=_cooling_reason(cooling_states))
                # 忙时等一次释放；冷却中等它到期 —— 两者都不超过剩余预算。
                waits = [w for w in cooling_left if w > 0]
                if busy:
                    waits.append(remaining)
                with anyio.move_on_after(min(remaining, *waits) if waits else remaining):
                    await self.condition.wait()

    async def failover(
        self, model: str, attempt: Callable[[Lease, AsyncExitStack], Awaitable[T]],
        lifetime: AsyncExitStack, *, timeout_seconds: float | None = None,
        timeout_reason: str = "first_output_timeout",
        excluded_accounts: set[str] | None = None,
    ) -> T:
        tried: set[str] = set(excluded_accounts or ())
        last_failure = None
        for _ in range(self.settings.max_retries + 1):
            current = AsyncExitStack()
            try:
                try:
                    lease = await self.acquire(model, tried, current)
                except PoolUnavailable as exc:
                    # Keep the cause of this request's failed attempt. A following
                    # acquire against its cooling account must not erase it.
                    exc.failure_reason = last_failure.reason if last_failure else None
                    raise
                tried.add(lease.account.id)
                try:
                    # 抖动在这里、超时预算之外：它是本地为了打散请求节奏而做的
                    # 故意延迟，不是上游的成本。放进 `fail_after` 里就会吃掉
                    # 「等上游出第一个字」的预算，把抖动记成账号超时。
                    await jitter_before_request(lease.account)
                    with anyio.fail_after(timeout_seconds if timeout_seconds is not None
                                          else self.settings.first_output_timeout_seconds):
                        result = await attempt(lease, current)
                except (UpstreamFailure, TimeoutError) as exc:
                    last_failure = exc if isinstance(exc, UpstreamFailure) else UpstreamFailure(timeout_reason)
                    await lease.fail(last_failure)
                    guard = getattr(self, 'protection', None)
                    if guard is not None:
                        guard.check()
                else:
                    # 把成功尝试的连接与租约交给响应生命周期。
                    lifetime.push_async_callback(current.pop_all().aclose)
                    return result
            finally:
                with anyio.CancelScope(shield=True):
                    await current.aclose()

        async with self.condition:
            remaining = self._eligible(model, tried)
            if any(s.status == "active" for s in remaining):
                raise PoolUnavailable(budget_exhausted=True,
                                      failure_reason=last_failure.reason if last_failure else None)
            cooling_states = [s for s in self._eligible(model, set())
                              if s.status == "cooldown"]
            cooling = [s.cooldown_until for s in cooling_states]
            retry = max(1, math.ceil(min(cooling) - time.monotonic())) if cooling else None
            raise PoolUnavailable(retry_after=retry,
                                  failure_reason=last_failure.reason if last_failure else None,
                                  cooling_reason=_cooling_reason(cooling_states))
