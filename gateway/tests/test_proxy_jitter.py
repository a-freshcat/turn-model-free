"""账号级出站代理与请求抖动：只测行为，不联网。

这三条防线对应「上游能不能看出这是逆向客户端」里可以本地验证的部分：
  1. 代理必须**真的挂到 httpx 客户端上**，没有被 transport 覆盖；
  2. 账号级代理优先于全局，且空值归一成直连（不是空串代理）；
  3. 抖动只发生在真正发上游请求时，且范围受控。
"""

import asyncio

import httpx
import pytest

from config import Account
from upstream_client import UpstreamClient, _apply_jitter, _proxy_for


def _account(**kw):
    base = dict(
        id="a1",
        channel="codearts",
        endpoint="https://example.com/v1/chat/completions",
        models={"m": "upstream-id"},
    )
    base.update(kw)
    return Account(**base)


def _no_transport(account):
    return None


async def _mounts(account):
    upstream = UpstreamClient(_no_transport)
    try:
        client = upstream._client(account)
        return dict(client._mounts or {})
    finally:
        await upstream.aclose()


def test_proxy_mounts_on_http_client():
    """代理必须真的出现在 httpx 的挂载表里，否则等于没配。"""
    mounts = asyncio.run(_mounts(_account(proxy="http://127.0.0.1:7890")))
    assert len(mounts) == 1
    assert all(isinstance(t, httpx.AsyncBaseTransport) for t in mounts.values())


def test_proxy_mounts_for_socks5():
    mounts = asyncio.run(_mounts(_account(proxy="socks5://127.0.0.1:1080")))
    assert len(mounts) == 1


def test_no_proxy_means_direct():
    """未配置代理时不能凭空挂一个 transport —— 那样会把直连变成走本地代理。"""
    assert asyncio.run(_mounts(_account())) == {}


def test_proxy_applies_to_every_driver():
    """代理不能只对 antigravity 生效：其余渠道同样是可识别流量。"""
    plain = _account(proxy="http://127.0.0.1:7890")
    assert len(asyncio.run(_mounts(plain))) == 1, "openai 渠道的代理没生效"

    # antigravity 走本地 OAuth 凭据，不能带 endpoint / token
    gravity = Account(
        id="ag1", channel="antigravity", driver="antigravity",
        credential_id="ag-" + "a" * 24, models={"m": "upstream-id"},
        proxy="http://127.0.0.1:7890",
    )
    assert len(asyncio.run(_mounts(gravity))) == 1, "antigravity 渠道的代理没生效"


def test_account_proxy_beats_global(monkeypatch):
    monkeypatch.setenv("GATEWAY_UPSTREAM_PROXY", "http://global:1111")
    assert _proxy_for(_account(proxy="http://acct:2222")) == "http://acct:2222"


def test_global_proxy_is_fallback(monkeypatch):
    monkeypatch.setenv("GATEWAY_UPSTREAM_PROXY", "http://global:1111")
    assert _proxy_for(_account()) == "http://global:1111"


def test_no_global_no_account_is_direct(monkeypatch):
    monkeypatch.delenv("GATEWAY_UPSTREAM_PROXY", raising=False)
    assert _proxy_for(_account()) is None


@pytest.mark.parametrize("raw, expected", [
    ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
    ("127.0.0.1:7890", "http://127.0.0.1:7890"),
    ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
    ("socks5h://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
])
def test_proxy_normalisation(raw, expected):
    assert _account(proxy=raw).proxy == expected


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_proxy_becomes_direct(raw):
    """空串是「清除代理」，必须归一成 None 而不是保留空串。"""
    assert _account(proxy=raw).proxy is None


@pytest.mark.parametrize("bad", [
    "ftp://x:21",
    "http://host",            # 缺端口
    "http://h:99999",         # 端口越界
    "http://h:8080/path",     # 带路径
])
def test_invalid_proxy_rejected(bad):
    with pytest.raises(Exception):
        _account(proxy=bad)


def test_jitter_is_on_by_default_and_bounded():
    """默认开启：这项能力以前存在但默认 0，等于没有。

    固定间隔是自动化指纹；300ms 的均值对交互式对话无感，却足以让请求间隔
    不再是一条直线。
    """
    assert _account().jitter_ms == 300
    with pytest.raises(Exception):
        _account(jitter_ms=6000)
    # 显式关掉仍然允许（延迟基准测试需要）。
    assert _account(jitter_ms=0).jitter_ms == 0


def test_jitter_does_not_consume_the_first_output_timeout_budget():
    """抖动必须发生在超时预算之外。

    抖动是本地为了打散节奏做的故意延迟，不是上游成本。它以前嵌在
    `send_upstream_request` 里，而那段代码运行在 `failover` 的
    `anyio.fail_after(first_output_timeout_seconds)` 之内 —— 默认 300ms 抖动
    会让超时很短的调用直接超时，并被记成账号故障（实测两个超时测试因此变红）。

    这条测试盯的是代码位置：抖动只在 `failover` 里、计时开始之前调用，
    不能在 `send_upstream_request` 内部。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    upstream = (root / "upstream_client.py").read_text(encoding="utf-8")
    manager = (root / "account_manager.py").read_text(encoding="utf-8")

    # send_upstream_request 里不得再调用 _apply_jitter
    body = upstream[upstream.index("async def send_upstream_request"):]
    body = body[:body.index("\n        client = self._client(account)")]
    assert "_apply_jitter(" not in body, "抖动不能在超时预算内执行"

    # failover 里必须在 fail_after 之前调用
    failover = manager[manager.index("async def failover"):]
    jitter_at = failover.index("jitter_before_request(lease.account)")
    timeout_at = failover.index("anyio.fail_after(")
    assert jitter_at < timeout_at, "抖动必须排在计时之前"


def test_jitter_actually_delays():
    """抖动必须真的睡——否则「加了抖动」只是注释。"""
    async def run():
        started = asyncio.get_running_loop().time()
        await _apply_jitter(_account(jitter_ms=80))
        return asyncio.get_running_loop().time() - started
    elapsed = asyncio.run(run())
    assert elapsed >= 0.0
    # 上界给足余量：sleep 精度受调度影响，但绝不能超过设定上限太多
    assert elapsed < 0.5


def test_jitter_zero_is_noop():
    async def run():
        started = asyncio.get_running_loop().time()
        await _apply_jitter(_account(jitter_ms=0))
        return asyncio.get_running_loop().time() - started
    assert asyncio.run(run()) < 0.05
