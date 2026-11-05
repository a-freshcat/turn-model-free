"""每进程一个号池。锁只保护状态，网络 I/O 永远在锁之外。"""

import logging
import math
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

import anyio

from config import Account, Settings, Status

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
    def __init__(self, *, retry_after: int | None = None, budget_exhausted: bool = False):
        self.retry_after = retry_after
        self.budget_exhausted = budget_exhausted


@dataclass
class _State:
    account: Account
    status: Status
    fail_count: int
    cooldown_until: float = 0
    in_flight: int = 0


class Lease:
    """租约从发送前持有到响应完成、报错或客户端断开。"""

    def __init__(self, pool: "AccountManager", state: _State):
        self.pool, self.state = pool, state
        self.released = False

    @property
    def account(self) -> Account:
        return self.state.account

    async def fail(self, failure: UpstreamFailure) -> None:
        async with self.pool.condition:
            state = self.state
            state.fail_count += 1
            state.status = "disabled" if failure.reauth else "cooldown"
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + max(self.pool.settings.cooldown_seconds, failure.retry_after),
            )
            LOG.warning("account=%s outcome=%s fail_count=%d",
                        state.account.id, failure.reason, state.fail_count)
            self.pool.condition.notify_all()

    async def release(self) -> None:
        async with self.pool.condition:
            if not self.released:
                self.released = True
                self.state.in_flight -= 1
                self.pool.condition.notify_all()


class AccountManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.condition = anyio.Condition()
        self.cursor = 0
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
                available = {
                    s.account.id for s in eligible if s.status == "active"
                    and s.in_flight < s.account.max_concurrency
                }
                for offset in range(len(self.states)):
                    index = (self.cursor + offset) % len(self.states)
                    state = self.states[index]
                    if state.account.id in available:
                        self.cursor = (index + 1) % len(self.states)
                        state.in_flight += 1
                        lease = Lease(self, state)
                        lifetime.push_async_callback(lease.release)
                        return lease

                remaining = deadline - time.monotonic()
                busy = any(s.status == "active" for s in eligible)
                if not busy or remaining <= 0:
                    # 冷却时间可计算；槽位释放时间不可预测。
                    cooling = [s.cooldown_until for s in self._eligible(model, set())
                               if s.status == "cooldown"]
                    active = any(s.status == "active" for s in self._eligible(model, set()))
                    retry = max(1, math.ceil(min(cooling) - time.monotonic())) if cooling and not active else None
                    raise PoolUnavailable(retry_after=retry)
                # 忙时等待一次释放，且不会错过等待期间刚好到期的冷却。
                cooling = [s.cooldown_until - time.monotonic() for s in eligible
                           if s.status == "cooldown"]
                with anyio.move_on_after(min(remaining, *cooling) if cooling else remaining):
                    await self.condition.wait()

    async def failover(
        self, model: str, attempt: Callable[[Lease, AsyncExitStack], Awaitable[T]],
        lifetime: AsyncExitStack,
    ) -> T:
        tried: set[str] = set()
        for _ in range(self.settings.max_retries + 1):
            current = AsyncExitStack()
            try:
                lease = await self.acquire(model, tried, current)
                tried.add(lease.account.id)
                try:
                    with anyio.fail_after(self.settings.first_output_timeout_seconds):
                        result = await attempt(lease, current)
                except (UpstreamFailure, TimeoutError) as exc:
                    await lease.fail(exc if isinstance(exc, UpstreamFailure)
                                     else UpstreamFailure("first_output_timeout"))
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
                raise PoolUnavailable(budget_exhausted=True)
            cooling = [s.cooldown_until for s in self._eligible(model, set())
                       if s.status == "cooldown"]
            retry = max(1, math.ceil(min(cooling) - time.monotonic())) if cooling else None
            raise PoolUnavailable(retry_after=retry)
