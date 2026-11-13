"""Regression: a request arriving while an account is ALREADY cooling from a 429
must be reported as 429 + Retry-After, not as 503 `account_pool_unavailable`.

Why this is a distinct bug from "this request hit a 429":
`PoolUnavailable.failure_reason` only describes *this* request's own failed
attempt. A request that arrives after the account already entered 429 cooling
never reaches upstream, so `last_failure` is None and `failure_reason` is None.
The old `main.py` branch matched only `failure_reason in {429 names}`, so that
path fell through to the generic 503.

Evidence from the desktop log: 21 `status=503` lines for `antigravity` shapes
with `tools=86` against 4 `status=429`. The 503s are all this path.

Measured DSH behaviour makes the distinction matter: `dsh-llm-retry` maps 503
to `SERVER` and retries at 0.5s / 1s / 2s / 4s, which lands inside a 20s
cooldown, producing a retry storm for a plain rate limit.
"""
import asyncio
from contextlib import AsyncExitStack

import pytest

from account_manager import AccountManager, PoolUnavailable, UpstreamFailure
from config import Account, Settings

RATE_LIMIT = "antigravity_http_429"


def make_account(**overrides):
    defaults = dict(id="ag-0123456789abcdef01234567", driver="antigravity",
                    channel="antigravity", credential_id="ag-0123456789abcdef01234567",
                    models={"m": "up"}, max_concurrency=1)
    defaults.update(overrides)
    return Account(**defaults)


def make_pool(**setting_overrides):
    settings = dict(accounts=[make_account()], max_retries=0, cooldown_seconds=600,
                    network_cooldown_seconds=20, network_cooldown_max_seconds=120,
                    pool_wait_seconds=0.05)
    settings.update(setting_overrides)
    return AccountManager(Settings(**settings))


async def fail_with(pool, reason=RATE_LIMIT):
    async def attempt(lease, lifetime):
        raise UpstreamFailure(reason, retry_after=0)

    async with AsyncExitStack() as lifetime:
        try:
            await pool.failover("m", attempt, lifetime)
        except Exception as exc:  # noqa: BLE001
            return exc
    return None


async def next_request(pool):
    async def attempt(lease, lifetime):
        raise AssertionError("must not reach upstream while cooling")

    async with AsyncExitStack() as lifetime:
        try:
            await pool.failover("m", attempt, lifetime)
        except PoolUnavailable as exc:
            return exc
    return None


# ── the new field ───────────────────────────────────────────────────────────

def test_cooling_reason_is_carried_when_nothing_failed_this_request():
    """The core defect: failure_reason is None, so only cooling_reason can say why."""
    pool = make_pool()

    async def run():
        await fail_with(pool)
        return await next_request(pool)

    exc = asyncio.run(run())
    assert exc.failure_reason is None, "this request never reached upstream"
    assert exc.cooling_reason == RATE_LIMIT
    assert exc.rate_limited is True


def test_rate_limited_property_ignores_which_field_carried_the_cause():
    from account_manager import PoolUnavailable as P
    assert P(failure_reason=RATE_LIMIT).rate_limited is True
    assert P(cooling_reason=RATE_LIMIT).rate_limited is True
    assert P(failure_reason="antigravity_transport").rate_limited is False
    assert P().rate_limited is False


def test_gemini_429_is_also_recognised_in_either_field():
    from account_manager import PoolUnavailable as P
    assert P(cooling_reason="gemini_http_429").rate_limited is True
    assert P(failure_reason="gemini_http_429").rate_limited is True


def test_a_non_rate_limit_cooldown_is_not_reported_as_rate_limited():
    pool = make_pool()

    async def run():
        await fail_with(pool, reason="antigravity_transport")
        return await next_request(pool)

    exc = asyncio.run(run())
    assert exc.cooling_reason == "antigravity_transport"
    assert exc.rate_limited is False


def test_a_disabled_account_stays_a_pool_outage():
    """No cooling at all — this must keep reporting 503."""
    pool = make_pool()
    pool.states[0].status = "disabled"

    exc = asyncio.run(next_request(pool))
    assert exc.cooling_reason is None
    assert exc.rate_limited is False


def test_rate_limit_wins_over_an_earlier_unrelated_cooldown():
    """Two cooling accounts, one rate-limited: waiting is the only useful action."""
    other = make_account(id="ag-bbbbbbbbbbbbbbbbbbbbbbbb",
                         credential_id="ag-bbbbbbbbbbbbbbbbbbbbbbbb")
    pool = AccountManager(Settings(accounts=[make_account(), other], max_retries=0,
                                   cooldown_seconds=600, network_cooldown_seconds=20,
                                   network_cooldown_max_seconds=120, pool_wait_seconds=0.05))

    async def run():
        import time
        async with pool.condition:
            for state in pool.states:
                state.status = "cooldown"
                state.cooldown_until = time.monotonic() + 20
            pool.states[0].last_failure_reason = "antigravity_transport"
            pool.states[1].last_failure_reason = RATE_LIMIT
        return await next_request(pool)

    exc = asyncio.run(run())
    assert exc.cooling_reason == RATE_LIMIT
    assert exc.rate_limited is True


# ── HTTP surface ────────────────────────────────────────────────────────────

def _prime(pool):
    from main import app
    from types import SimpleNamespace
    app.state.pool = pool
    app.state.api_key = b"test-key"
    app.state.web = SimpleNamespace(pool=pool, protection=None, plugin=None)


def _post():
    from main import ChatRequest, ChatResponse
    import httpx as _httpx
    pool = __import__("main").app.state.pool
    body = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    captured = {}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {k.decode().lower(): v.decode()
                                   for k, v in message.get("headers", [])}

    async def receive():
        await asyncio.sleep(3600)

    response = ChatResponse(body, pool, _httpx.AsyncClient())
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        pass
    return response, captured, receive, send


def test_http_surface_is_429_with_retry_after_while_cooling():
    """End-to-end: the client must be told to wait, not told the pool is gone."""
    pool = make_pool()

    async def run():
        await fail_with(pool)
        _prime(pool)
        from main import ChatRequest, ChatResponse
        from types import SimpleNamespace
        body = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
        captured = {}

        async def send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = {k.decode().lower(): v.decode()
                                       for k, v in message.get("headers", [])}

        async def receive():
            await asyncio.sleep(3600)

        response = ChatResponse(body, pool, SimpleNamespace())
        try:
            await asyncio.wait_for(response({"type": "http", "method": "POST",
                                             "path": "/v1/chat/completions"},
                                            receive, send), timeout=10)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        return captured

    captured = asyncio.run(run())
    assert captured.get("status") == 429, captured
    assert "retry-after" in captured.get("headers", {}), captured
    assert int(captured["headers"]["retry-after"]) >= 1


def test_http_surface_for_disabled_account_is_still_503():
    pool = make_pool()
    pool.states[0].status = "disabled"

    async def run():
        _prime(pool)
        from main import ChatRequest, ChatResponse
        from types import SimpleNamespace
        body = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
        captured = {}

        async def send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]

        async def receive():
            await asyncio.sleep(3600)

        response = ChatResponse(body, pool, SimpleNamespace())
        try:
            await asyncio.wait_for(response({"type": "http", "method": "POST",
                                             "path": "/v1/chat/completions"},
                                            receive, send), timeout=10)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        return captured

    captured = asyncio.run(run())
    assert captured.get("status") == 503, captured
