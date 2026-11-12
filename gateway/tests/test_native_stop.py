"""Offline regression tests for stopping on explicit upstream rejection."""
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from account_manager import UpstreamFailure
from antigravity_driver import send_native
from protection import DispatchProtection, ProtectionBlocked
from web_api import WebManager


@pytest.mark.parametrize("response,stream,expected", [
    (httpx.Response(401, json={"error": {"status": "PERMISSION_DENIED"}}),
     False, "antigravity_http_403"),
    (httpx.Response(200, json={"response": {"error": {"status": "PERMISSION_DENIED"}}}),
     False, "antigravity_http_403"),
    (httpx.Response(200, headers={"content-type": "text/event-stream", "retry-after": "30"},
                    content='event: error\ndata: {"error":{"status":"RESOURCE_EXHAUSTED"}}\n\n'),
     True, "antigravity_http_429"),
])
def test_native_stop_uses_original_error(response, stream, expected):
    import asyncio

    async def run():
        auth = SimpleNamespace(
            resolve_proxy=lambda value: value,
            credential=AsyncMock(return_value={"project_id": "fixture", "access_token": "fake"}),
        )
        account = SimpleNamespace(proxy=None, credential_id="fixture", models={"chat": "fixture"},
                                  device_headers={}, user_agent="fixture")
        transport = httpx.MockTransport(lambda request: response)
        async with httpx.AsyncClient(transport=transport) as client, AsyncExitStack() as lifetime:
            with pytest.raises(UpstreamFailure) as exc:
                await send_native(auth, client, account, [{"role": "user", "content": "hello"}],
                                  stream, model="chat", options={}, lifetime=lifetime)
        assert exc.value.reason == expected
        assert auth.credential.await_count == 1
        if stream:
            assert exc.value.retry_after == 30

    asyncio.run(run())


def test_paused_management_stops_before_account_lookup(tmp_path: Path):
    import asyncio

    async def run():
        guard = DispatchProtection(tmp_path / "guard.json")
        await guard.pause()
        manager = object.__new__(WebManager)
        manager.protection = guard
        manager.rows = AsyncMock()
        for method in ("account.test", "account.refresh"):
            with pytest.raises(ProtectionBlocked):
                await manager.rpc(method, {"accountId": "fixture"}, "http://localhost")
        manager.rows.assert_not_awaited()
        with pytest.raises(ProtectionBlocked):
            await manager.refresh_oauth_account("fixture")

    asyncio.run(run())
