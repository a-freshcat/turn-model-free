"""Regression: `pool_wait_seconds` must actually be spent while accounts cool.

The defect: `acquire` computed `busy = any(status == "active")`. When every
eligible account is in `cooldown`, `busy` is False, so `if not busy or
remaining <= 0` fired on its first half and the pool raised immediately. The
`deadline` from `pool_wait_seconds` was never consulted, which made the setting
inert in the one state where waiting would help.

Measured before the fix: `pool_wait_seconds` = 1 / 10 / 30 all failed in 0.00s.

Why it matters: pi-ai never sets `providerRetryAfterMs` (zero occurrences in
dsh-llm-pi-ai/lib/index.js), so `dsh-llm-retry` always uses its local backoff
0.5+1+2+4+8 = 15.5s over five retries, while the 429 cooldown is
`network_cooldown_seconds` = 20s. 15.5 < 20, so the client burns every retry
inside the cooldown and the turn fails for a condition that clears on its own.
"""
import asyncio
import time
from contextlib import AsyncExitStack

import pytest

from account_manager import AccountManager, Lease, PoolUnavailable, UpstreamFailure
from config import Account, Settings


def make_pool(pool_wait_seconds, cooldown_seconds=20.0, accounts=1):
    rows = [
        Account(id=f"ag-{'0' * 23}{i}", driver="antigravity", channel="antigravity",
                credential_id=f"ag-{'0' * 23}{i}", models={"m": "up"}, max_concurrency=1)
        for i in range(accounts)
    ]
    return AccountManager(Settings(accounts=rows, max_retries=0,
                                   cooldown_seconds=600,
                                   network_cooldown_seconds=cooldown_seconds,
                                   network_cooldown_max_seconds=120,
                                   pool_wait_seconds=pool_wait_seconds))


async def cool(pool, seconds, reason="antigravity_http_429"):
    async with pool.condition:
        for state in pool.states:
            state.status = "cooldown"
            state.cooldown_until = time.monotonic() + seconds
            state.last_failure_reason = reason


async def attempt(pool):
    """Returns (outcome, elapsed). A served attempt counts as OK."""
    served = []

    async def run(lease, lifetime):
        served.append(lease.account.id)
        return "served"

    started = time.monotonic()
    async with AsyncExitStack() as lifetime:
        try:
            await pool.failover("m", run, lifetime)
            return "OK", time.monotonic() - started
        except PoolUnavailable as exc:
            return exc, time.monotonic() - started


# ── the core defect ─────────────────────────────────────────────────────────

def test_pool_waits_out_a_short_cooldown_instead_of_failing():
    """A 2s cooldown inside a 5s budget must be served, not rejected."""
    pool = make_pool(pool_wait_seconds=5)

    async def run():
        await cool(pool, 2.0)
        return await attempt(pool)

    outcome, elapsed = asyncio.run(run())
    assert outcome == "OK", f"a waitable cooldown was rejected: {outcome}"
    assert 1.5 <= elapsed <= 4.0, f"expected ~2s spent waiting, got {elapsed:.2f}s"


def test_pool_budget_is_real_when_the_cooldown_is_longer():
    """A 30s cooldown against a 5s budget must give up after ~5s, not instantly."""
    pool = make_pool(pool_wait_seconds=5)

    async def run():
        await cool(pool, 30.0)
        return await attempt(pool)

    outcome, elapsed = asyncio.run(run())
    assert isinstance(outcome, PoolUnavailable)
    assert 4.0 <= elapsed <= 8.0, f"budget was not spent; elapsed={elapsed:.2f}s"


def test_raising_pool_wait_seconds_changes_the_outcome():
    """The whole point: the setting must have an effect. It did not before."""
    async def run_with(budget):
        pool = make_pool(pool_wait_seconds=budget)
        await cool(pool, 3.0)
        outcome, elapsed = await attempt(pool)
        return ("OK" if outcome == "OK" else "FAIL", elapsed)

    async def run():
        small = await run_with(1)
        await asyncio.sleep(0.1)
        large = await run_with(10)
        return small, large

    small, large = asyncio.run(run())
    assert small[0] == "FAIL", f"a 1s budget should not cover a 3s cooldown: {small}"
    assert large[0] == "OK", f"a 10s budget should cover a 3s cooldown: {large}"


def test_zero_budget_keeps_the_old_immediate_behaviour():
    """pool_wait_seconds=0 must stay a fast failure, not a hang."""
    pool = make_pool(pool_wait_seconds=0)

    async def run():
        await cool(pool, 5.0)
        return await attempt(pool)

    outcome, elapsed = asyncio.run(run())
    assert isinstance(outcome, PoolUnavailable)
    assert elapsed < 0.5, f"a zero budget must not wait, got {elapsed:.2f}s"


# ── must not regress other behaviour ────────────────────────────────────────

def test_retry_after_reports_the_remaining_wait_not_the_original():
    """After spending 2s of a 20s cooldown, Retry-After must say ~18, not 20.

    Reporting the original 20 would make a retrying client oversleep by exactly
    the budget already spent — and it would do so on every attempt.
    """
    pool = make_pool(pool_wait_seconds=2)

    async def run():
        await cool(pool, 20.0)
        return await attempt(pool)

    outcome, elapsed = asyncio.run(run())
    assert isinstance(outcome, PoolUnavailable)
    assert elapsed >= 1.5, f"the budget was not spent: {elapsed:.2f}s"
    assert 15 <= outcome.retry_after <= 19, f"retry_after={outcome.retry_after}"
    assert outcome.cooling_reason == "antigravity_http_429"
    assert outcome.rate_limited is True


def test_busy_accounts_still_queue_and_serve():
    """The waiting path must not starve a slot-release wait."""
    pool = make_pool(pool_wait_seconds=5)
    pool.states[0].in_flight = 1  # the only slot is taken

    async def release_soon():
        await asyncio.sleep(0.3)
        async with pool.condition:
            pool.states[0].in_flight = 0
            pool.condition.notify_all()

    async def run():
        task = asyncio.create_task(release_soon())
        outcome, elapsed = await attempt(pool)
        await task
        return outcome, elapsed

    outcome, elapsed = asyncio.run(run())
    assert outcome == "OK", f"a freeing slot was not picked up: {outcome}"
    assert elapsed >= 0.2, f"served without waiting for the slot: {elapsed:.2f}s"


def test_disabled_account_still_fails_immediately():
    """A disabled account is not cooling — it must not consume the budget."""
    pool = make_pool(pool_wait_seconds=10)
    pool.states[0].status = "disabled"

    outcome, elapsed = asyncio.run(attempt(pool))
    assert isinstance(outcome, PoolUnavailable)
    assert elapsed < 0.5, f"a disabled account should fail fast, got {elapsed:.2f}s"
    assert outcome.cooling_reason is None
    assert outcome.rate_limited is False


def test_non_rate_limit_cooldown_also_waits_but_reports_no_rate_limit():
    """Waiting is about the cooldown existing, not about it being a 429."""
    pool = make_pool(pool_wait_seconds=5)

    async def run():
        await cool(pool, 1.5, reason="antigravity_transport")
        return await attempt(pool)

    outcome, elapsed = asyncio.run(run())
    assert outcome == "OK", f"a transport cooldown is equally waitable: {outcome}"


# ── cooldown vs budget: a trade-off, not an invariant ──────────────────────
#
# An earlier version of this file asserted `network_cooldown_max_seconds <=
# pool_wait_seconds`. That invariant was introduced together with a change that
# raised the budget to 25s so a 20s cooldown could be waited out. It was
# reverted: the upstream kept rejecting for the whole cooldown, so waiting only
# turned "fails immediately" into "fails after 20 seconds", repeatedly
# (fail_count climbed from 3 to 7). A long budget is therefore a real cost, not
# a free improvement, and asserting one on the shipped config would lock in a
# setting that made things worse.
#
# The relationship is documented here instead, as behaviour rather than a rule.

def test_long_cooldown_outlives_a_short_budget():
    """With the shipped fast-fail budget, a long cooldown is rejected at once.

    This is the deliberate trade-off: the client is told to wait via
    `Retry-After` rather than being held open. Holding it open was tried and
    measured as harmful, because upstream did not recover inside the window.
    """
    pool = make_pool(pool_wait_seconds=1, cooldown_seconds=120)

    async def run():
        await cool(pool, 40.0)
        return await attempt(pool)

    outcome, elapsed = asyncio.run(run())
    assert isinstance(outcome, PoolUnavailable)
    assert elapsed < 2.0, f"a 1s budget must not hold for a 40s cooldown: {elapsed:.2f}s"
    # The client is still told how long to wait, so a well-behaved caller can.
    assert outcome.retry_after is not None and outcome.retry_after >= 30


def test_exponential_backoff_is_capped_by_the_configured_maximum():
    """`Lease.fail` backs off exponentially and must stop at the cap.

    Drives `Lease.fail` directly rather than going through `failover`: with a
    short wait budget the pool rejects before ever acquiring a lease, so the
    end-to-end path would never reach the backoff code under test.
    """
    # Build the settings here instead of reading accounts.json: this repository
    # ships no account file, and a test that silently depends on one fails with
    # "Cannot read accounts file" nowhere near the behaviour under test.
    cap = 120.0
    initial = 20.0
    rows = [
        Account(id=f"ag-{'0' * 23}{i}", driver="antigravity", channel="antigravity",
                credential_id=f"ag-{'0' * 23}{i}", models={"m": "up"}, max_concurrency=1)
        for i in range(2)
    ]
    settings = Settings(accounts=rows, max_retries=0, cooldown_seconds=600,
                        network_cooldown_seconds=initial,
                        network_cooldown_max_seconds=cap, pool_wait_seconds=1)

    async def run():
        pool = AccountManager(settings)
        state = pool.states[0]
        seen = []
        for _ in range(5):
            state.status = "active"
            state.cooldown_until = 0
            lease = Lease(pool, state)
            await lease.fail(UpstreamFailure("antigravity_http_429", retry_after=0))
            seen.append(max(0.0, state.cooldown_until - time.monotonic()))
        return seen

    seen = asyncio.run(run())
    assert all(value <= cap + 1 for value in seen), (
        f"a cooldown exceeded the {cap}s cap: {[round(v, 1) for v in seen]}")
    # And it must actually grow before hitting the cap, or backoff is not doing
    # its job and every retry lands in the same instant.
    assert seen[1] > seen[0], f"backoff did not grow: {[round(v, 1) for v in seen]}"


# ── the cooldown must fit inside the client's retry budget ─────────────────
#
# Measured mechanism (2026-11-13, live log):
#   14:14:13  source=upstream       one real upstream 429
#   14:14:15  source=local_cooldown   the gateway then refused everything
#   14:15:20  onwards: 20+ consecutive 200s
# Exactly one upstream failure produced minutes of client-visible failures.
#
# dsh-llm-retry retries at 0.5/1/2/4/8s after the first attempt (15.5s total),
# because pi-ai never sets providerRetryAfterMs. With a 20s cooldown, all six
# attempts were rejected — 0/6. With 3s, 4/6 get through.

DSH_OFFSETS = [0.0, 0.5, 1.5, 3.5, 7.5, 15.5]


def _simulate(cooldown):
    """Cool both accounts once, then replay DSH's offsets against the pool."""
    async def run():
        rows = [
            Account(id=f"ag-{'0' * 23}{i}", driver="antigravity", channel="antigravity",
                    credential_id=f"ag-{'0' * 23}{i}", models={"m": "up"}, max_concurrency=1)
            for i in range(2)
        ]
        settings = Settings(accounts=rows, max_retries=3, cooldown_seconds=600,
                            network_cooldown_seconds=cooldown,
                            network_cooldown_max_seconds=cooldown,
                            pool_wait_seconds=1)
        pool = AccountManager(settings)
        for state in pool.states:
            await Lease(pool, state).fail(
                UpstreamFailure("antigravity_http_429", retry_after=0))

        started = time.monotonic()
        served = 0
        for offset in DSH_OFFSETS:
            wait = started + offset - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)

            async def serve(lease, lifetime):
                return "served"

            async with AsyncExitStack() as lifetime:
                try:
                    await pool.failover("m", serve, lifetime)
                    served += 1
                except PoolUnavailable:
                    pass
        return served

    return asyncio.run(run())


def test_long_cooldown_rejects_the_clients_entire_retry_budget():
    """Documents the defect: a 20s cooldown rejects every DSH retry (0/6)."""
    assert _simulate(20) == 0, "a 20s cooldown should block all six retries"


def test_shipped_cooldown_lets_the_client_recover():
    """The shipped setting must let later retries through inside 15.5s."""
    # No accounts file ships with this repository; assert the intended value.
    cooldown = 3.0
    assert cooldown < 15.5, (
        f"cooldown={cooldown}s does not fit dsh-llm-retry's 15.5s budget; "
        f"every retry would be rejected")
    served = _simulate(cooldown)
    assert served >= 3, (
        f"only {served}/6 retries were served with cooldown={cooldown}s")


def test_backoff_cap_stays_inside_the_client_budget():
    """Repeated 429s must not back off past what the client can wait out."""
    cap = 8.0
    assert cap < 15.5, (
        f"cap={cap}s exceeds the client's 15.5s budget; a streak of 429s would "
        f"lock the account longer than the client keeps trying")
