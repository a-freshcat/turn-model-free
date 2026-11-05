"""同一份 OAuth 凭据必须恒定从同一个出口 IP 出网。

这里锁的是一个真实存在过的缺陷：对话请求走账号级 `proxy`，而令牌刷新、
项目探测、模型目录查询只认全局 `GATEWAY_UPSTREAM_PROXY`。于是同一个 Google
账号的访问令牌在一个 IP 上刷新、在另一个 IP 上使用 —— 这正是「一个账号多处
使用」最直接的信号，比任何请求频率特征都更容易被识别。

约定：账号级 proxy 优先，其次全局，都没有则直连。四类请求共用这套规则。
"""

import asyncio

import httpx
import pytest

from antigravity_auth import AntigravityAuth


@pytest.fixture()
def auth(tmp_path):
    instance = AntigravityAuth(tmp_path)
    yield instance
    asyncio.run(instance.aclose())


def _mounts(auth, proxy=None):
    client = auth.http(proxy)
    return dict(client._mounts or {})


def test_account_proxy_is_used_when_no_global(monkeypatch, auth):
    monkeypatch.delenv("GATEWAY_UPSTREAM_PROXY", raising=False)
    assert auth.resolve_proxy("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert len(_mounts(auth, "http://127.0.0.1:7890")) == 1


def test_account_proxy_beats_global(monkeypatch, auth):
    monkeypatch.setenv("GATEWAY_UPSTREAM_PROXY", "http://global:1111")
    assert auth.resolve_proxy("http://acct:2222") == "http://acct:2222"


def test_global_is_fallback(monkeypatch, auth):
    monkeypatch.setenv("GATEWAY_UPSTREAM_PROXY", "http://global:1111")
    assert auth.resolve_proxy(None) == "http://global:1111"


def test_no_proxy_anywhere_is_direct(monkeypatch, auth):
    monkeypatch.delenv("GATEWAY_UPSTREAM_PROXY", raising=False)
    assert auth.resolve_proxy(None) is None
    assert _mounts(auth) == {}


def test_different_proxies_get_different_pools(monkeypatch, auth):
    """两个账号配不同出口时，不能共用一个连接池 —— 否则出口会互相污染。"""
    monkeypatch.delenv("GATEWAY_UPSTREAM_PROXY", raising=False)
    first = auth.http("http://127.0.0.1:7890")
    second = auth.http("http://127.0.0.1:7891")
    assert first is not second
    assert auth.http("http://127.0.0.1:7890") is first, "同一代理应复用连接池"
    assert len(auth.clients) == 2


def test_credential_passes_proxy_to_token_refresh(monkeypatch, auth):
    """刷新令牌的 POST 必须带上账号代理，这是本组测试的核心断言。"""
    seen = {}

    async def fake_json_request(method, url, *, token=None, proxy=None, **kwargs):
        seen.update(method=method, url=url, proxy=proxy)
        return {"access_token": "fresh", "refresh_token": "rot", "expires_in": 3600}

    monkeypatch.setattr(auth, "json_request", fake_json_request)
    auth.store.save("ag-" + "a" * 24, {
        "access_token": "old", "refresh_token": "rot", "expires_at": 0,
        "subject": "s", "email": "e@example.test", "status": "authorized",
    })
    asyncio.run(auth.credential("ag-" + "a" * 24, proxy="http://127.0.0.1:7890"))
    assert seen["proxy"] == "http://127.0.0.1:7890"
    assert seen["url"].endswith("/token")


def test_discover_passes_proxy_to_both_calls(monkeypatch, auth):
    """项目探测 + 模型目录两个请求都必须走同一出口，不能只改一个。"""
    proxies, urls = [], []

    async def fake_json_request(method, url, *, token=None, proxy=None, **kwargs):
        proxies.append(proxy)
        urls.append(url)
        if url.endswith(":loadCodeAssist"):
            return {"cloudaicompanionProject": "proj-1"}
        return {"models": {"m1": {"displayName": "M1"}}}

    monkeypatch.setattr(auth, "json_request", fake_json_request)
    credential = {"access_token": "t", "status": "authorized"}
    asyncio.run(auth.discover(credential, proxy="http://127.0.0.1:7890"))
    assert len(proxies) == 2, urls
    assert set(proxies) == {"http://127.0.0.1:7890"}
    assert credential["project_id"] == "proj-1"
    assert credential["catalog"]["m1"]["name"] == "M1"


def test_injected_client_still_wins(monkeypatch, auth):
    """测试/宿主注入的 client 优先，代表「所有出口都用这个 transport」。"""
    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                                trust_env=False)
    monkeypatch.setattr(auth, "client", injected)
    assert auth.http("http://127.0.0.1:7890") is injected
    asyncio.run(injected.aclose())


def test_refresh_catalog_threads_proxy_through(monkeypatch, auth):
    calls = []

    async def fake_credential(id_, *, force=False, proxy=None):
        calls.append(("credential", proxy))
        return {"access_token": "t", "status": "authorized"}

    async def fake_discover(credential, proxy=None):
        calls.append(("discover", proxy))
        credential["catalog"] = {}

    monkeypatch.setattr(auth, "credential", fake_credential)
    monkeypatch.setattr(auth, "discover", fake_discover)
    auth.store.save("ag-" + "b" * 24, {
        "access_token": "t", "refresh_token": "r", "expires_at": 9e9,
        "subject": "s", "email": "e@example.test", "status": "authorized",
    })
    asyncio.run(auth.refresh_catalog("ag-" + "b" * 24, proxy="http://127.0.0.1:7890"))
    assert [name for name, _ in calls] == ["credential", "discover"]
    assert {proxy for _, proxy in calls} == {"http://127.0.0.1:7890"}
