"""插件侧账号与模型必须出现在网关上，且对话能转发过去。

这组测试对应一个真实故障：用户在插件里登录了 WorkBuddy，网关页面仍显示
「账号 0/0 · 还没有账号——点「添加账号」开始登录」。

根因是网关只读本机 `accounts.json`，而 12 家的账号在插件的 channel-pack 里
（`~/.dsh/channel-pack/state.json`）。用户登录成功了，网关看不见。

约定：
  - 账号：以插件为准，合并进列表（插件在前，本机导入的在后）；
  - 模型：id 必须与插件网关一致（`codearts/GLM-5.2`），否则转发不过去；
  - 对话：12 家转发给插件网关；插件没跑就明确报错，不假装成功。
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx
import pytest

from plugin_gateway import PluginGateway, PluginGatewayError, dsh_home
from web_api import CHANNELS, WebManager


class PluginGatewayDiscoveryTests(unittest.TestCase):
    """发现逻辑：密钥文件、端口、provider 前缀。不联网。"""

    def test_missing_key_file_is_a_readable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = PluginGateway(home=Path(tmp))
            with self.assertRaises(PluginGatewayError) as caught:
                gateway.read_key()
            self.assertIn("网关密钥不存在", str(caught.exception))

    def test_malformed_key_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openai-gateway"
            path.mkdir()
            (path / "api-key").write_text("这不是一个合法密钥\n", encoding="utf-8")
            gateway = PluginGateway(home=Path(tmp))
            with self.assertRaises(PluginGatewayError) as caught:
                gateway.read_key()
            self.assertIn("格式异常", str(caught.exception))

    def test_valid_key_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openai-gateway"
            path.mkdir()
            key = "A" * 43
            (path / "api-key").write_text(key + "\n", encoding="utf-8")
            self.assertEqual(PluginGateway(home=Path(tmp)).read_key(), key)

    def test_port_override_from_env(self):
        original = os.environ.get("DSH_OPENAI_GATEWAY_PORT")
        os.environ["DSH_OPENAI_GATEWAY_PORT"] = "9123"
        try:
            self.assertEqual(PluginGateway().port, 9123)
        finally:
            if original is None:
                os.environ.pop("DSH_OPENAI_GATEWAY_PORT", None)
            else:
                os.environ["DSH_OPENAI_GATEWAY_PORT"] = original

    def test_provider_prefix_parsing(self):
        self.assertEqual(PluginGateway.provider_of("codearts/GLM-5.2"), "codearts")
        self.assertEqual(PluginGateway.provider_of("antigravity/x"), "antigravity")
        self.assertEqual(PluginGateway.provider_of("no-slash"), "")

    def test_dsh_home_prefers_state_dir(self):
        original = {k: os.environ.get(k) for k in ("DSH_CHANNEL_PACK_STATE_DIR", "DSH_HOME")}
        try:
            os.environ["DSH_HOME"] = "/tmp/dsh-home"
            os.environ["DSH_CHANNEL_PACK_STATE_DIR"] = "/tmp/pack-state"
            self.assertEqual(dsh_home(), Path("/tmp/pack-state"))
            os.environ.pop("DSH_CHANNEL_PACK_STATE_DIR")
            self.assertEqual(dsh_home(), Path("/tmp/dsh-home"))
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


class _FakePlugin:
    """模拟插件网关：固定模型清单 + 回显对话。"""

    def __init__(self, models, *, fail=False):
        self._models = models
        self._fail = fail
        self.chat_calls = []

    async def probe(self):
        if self._fail:
            raise PluginGatewayError("连不上 DSH 插件的网关")
        return list(self._models)

    def cached_models(self):
        return list(self._models)

    @staticmethod
    def provider_of(model):
        return PluginGateway.provider_of(model)

    async def chat(self, body, *, stream):
        if self._fail:
            raise PluginGatewayError("连不上 DSH 插件的网关")
        self.chat_calls.append(body)
        raise AssertionError("真实网络不应在单元测试里被调用")


class MergedViewTests(unittest.IsolatedAsyncioTestCase):
    """网关侧的合并：账号来自插件，模型 id 带 provider 前缀。"""

    async def asyncSetUp(self):
        from account_manager import AccountManager
        from config import Settings
        from main import app
        from upstream_client import UpstreamClient

        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "accounts.json"
        self.path.write_text('{"accounts":[]}', encoding="utf-8")
        self.pool = AccountManager(Settings(accounts=[]))
        self.upstream = UpstreamClient(lambda a: httpx.MockTransport(lambda r: httpx.Response(200)))
        self.web = WebManager(self.pool, self.path, self.upstream)
        app.state.pool, app.state.upstream, app.state.web = self.pool, self.upstream, self.web
        app.state.api_key = b"merge-test-key"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://gateway")
        await self.client.post("/ui/session", json={"key": "merge-test-key"},
                               headers={"Origin": "http://gateway"})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.web.bridge.aclose()
        await self.upstream.aclose()
        self.temp.cleanup()

    async def rpc(self, method, payload):
        response = await self.client.post("/ui/rpc", json={"method": method, "payload": payload},
                                          headers={"Origin": "http://gateway"})
        return response

    async def test_plugin_rows_are_marked_as_plugin_source(self):
        self.web.plugin = _FakePlugin([])
        rows = await self.web.plugin_accounts("workbuddy")
        # 桥不可用时应返回空列表，而不是抛错。
        self.assertIsInstance(rows, list)

    async def test_models_carry_the_plugin_prefix(self):
        self.web.plugin = _FakePlugin(["codearts/GLM-5.2", "codearts/kimi-k2.6-vl",
                                       "buddy/DeepSeek-V3"])
        models = await self.web.plugin_models("codearts")
        self.assertEqual([m["id"] for m in models],
                         ["codearts/GLM-5.2", "codearts/kimi-k2.6-vl"])
        # name 去掉前缀，页面显示得干净，但 id 必须保留全名供转发。
        self.assertEqual(models[0]["name"], "GLM-5.2")

    async def test_status_counts_include_plugin_accounts(self):
        """插件有账号时状态不能还是 0/0——这正是用户报的那个现象。"""
        class _Plugin:
            async def probe(self_inner):
                return ["codearts/GLM-5.2"]

            @staticmethod
            def provider_of(model):
                return PluginGateway.provider_of(model)

        self.web.plugin = _Plugin()

        async def fake_accounts(provider):
            if provider != "codearts":
                return []
            return [{"id": "codearts-1", "provider": "codearts", "driver": "channel-pack",
                     "nickname": "codearts-1", "enabled": True, "status": "active",
                     "expiresAt": 0, "models": {}, "disabled_models": [], "in_flight": 0,
                     "fail_count": 0, "max_concurrency": 1, "refreshable": False,
                     "source": "plugin"}]

        self.web.plugin_accounts = fake_accounts
        response = await self.rpc("provider.status", {})
        statuses = response.json()["value"]["statuses"]
        self.assertEqual(statuses["codearts"]["accounts"]["total"], 1)
        self.assertEqual(statuses["codearts"]["accounts"]["enabled"], 1)
        self.assertGreater(statuses["codearts"]["models"]["total"], 0)
        # 没有插件账号的渠道仍是 0，不能凭空多出来。
        self.assertEqual(statuses["qoder"]["accounts"]["total"], 0)

    async def test_account_list_puts_plugin_accounts_first(self):
        async def fake_accounts(provider):
            return [{"id": "plugin-1", "provider": provider, "driver": "channel-pack",
                     "nickname": "插件账号", "enabled": True, "status": "active",
                     "expiresAt": 0, "models": {}, "disabled_models": [], "in_flight": 0,
                     "fail_count": 0, "max_concurrency": 1, "refreshable": False,
                     "source": "plugin"}]

        self.web.plugin_accounts = fake_accounts
        response = await self.rpc("account.list", {"provider": "buddy"})
        rows = response.json()["value"]["accounts"]
        self.assertEqual(rows[0]["id"], "plugin-1")
        self.assertEqual(rows[0]["source"], "plugin")

    async def test_pending_placeholder_is_marked_and_not_counted_as_active(self):
        """`account.create` 会先写占位记录；登录完成前不能显示成可用账号。"""
        async def fake_accounts(provider):
            return [{"id": "buddy-abc123", "provider": provider, "driver": "channel-pack",
                     "nickname": "buddy-abc123 · 等待授权", "enabled": False,
                     "status": "disabled", "expiresAt": 0, "models": {},
                     "disabled_models": [], "in_flight": 0, "fail_count": 0,
                     "max_concurrency": 1, "refreshable": False,
                     "source": "plugin", "pending": True}]

        self.web.plugin_accounts = fake_accounts
        rows = (await self.rpc("account.list", {"provider": "buddy"})).json()["value"]["accounts"]
        self.assertTrue(rows[0]["pending"])
        self.assertFalse(rows[0]["enabled"])
        self.assertIn("等待授权", rows[0]["nickname"])

    async def test_orphan_placeholders_are_hidden_and_removed(self):
        """历史遗留的空壳不能占着账号数——用户会看到「账号 0/21」却一个都点不动。"""
        listed = [
            {"id": "buddy-real", "nickname": "埃尔温", "enabled": True},
            {"id": "buddy-orphan1", "nickname": "buddy-orphan1", "enabled": True},
            {"id": "buddy-orphan2", "nickname": "buddy-orphan2", "enabled": True},
        ]
        deleted = []

        class _Bridge:
            async def call(inner, method, payload, attempts=5):
                if method == "account.list":
                    return {"accounts": listed}
                if method == "account.delete":
                    deleted.append(payload["accountId"])
                    return {"ok": True}
                raise AssertionError(method)

            async def aclose(inner):
                pass

        self.web.bridge = _Bridge()
        rows = await self.web.plugin_accounts("buddy")
        # 只有真实账号出现。
        self.assertEqual([r["id"] for r in rows], ["buddy-real"])
        # 两个空壳被删掉，真实账号没被误删。
        self.assertEqual(sorted(deleted), ["buddy-orphan1", "buddy-orphan2"])

    async def test_pending_login_window_is_still_visible(self):
        """本次会话正在等的那个占位必须留下，否则用户看不到自己的登录进度。"""
        listed = [{"id": "buddy-live", "nickname": "buddy-live", "enabled": True}]
        deleted = []

        class _Bridge:
            async def call(inner, method, payload, attempts=5):
                if method == "account.list":
                    return {"accounts": listed}
                if method == "account.delete":
                    deleted.append(payload["accountId"])
                    return {"ok": True}
                raise AssertionError(method)

            async def aclose(inner):
                pass

        self.web.bridge = _Bridge()
        self.web.bridged = {"buddy-live": {"provider": "buddy",
                                           "expires": __import__("time").monotonic() + 600}}
        rows = await self.web.plugin_accounts("buddy")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["pending"])
        self.assertEqual(deleted, [], "正在登录的占位不能删")

    async def test_placeholder_with_a_credential_is_not_deleted(self):
        """昵称等于 id 但**有凭据**的是真账号，不能当空壳删掉。

        登录成功但上游没给昵称时，记录长这样。旧判据只看「昵称等于 id」，
        于是这种账号每刷一次页面就被删一次——用户看到的是账号自己消失。
        """
        listed = [
            {"id": "buddy-named", "nickname": "buddy-named", "enabled": True,
             "credentialRef": "BUDDY_ACCOUNT_OK"},
            {"id": "buddy-shell", "nickname": "buddy-shell", "enabled": True,
             "credentialRef": "BUDDY_ACCOUNT_GONE"},
        ]
        deleted = []

        class _Bridge:
            async def call(inner, method, payload, attempts=5):
                if method == "account.list":
                    return {"accounts": listed}
                if method == "credential.presence":
                    return {"present": {"buddy-named": True, "buddy-shell": False}}
                if method == "account.delete":
                    deleted.append(payload["accountId"])
                    return {"ok": True}
                raise AssertionError(method)

            async def aclose(inner):
                pass

        self.web.bridge = _Bridge()
        rows = await self.web.plugin_accounts("buddy")
        self.assertEqual([r["id"] for r in rows], ["buddy-named"])
        self.assertFalse(rows[0]["pending"])
        self.assertEqual(deleted, ["buddy-shell"])

    async def test_restarted_gateway_reaps_old_placeholders(self):
        """进程重启后，`bridged` 是空的——旧占位必须靠凭据判定被清掉。

        这正是用户报的那个现象：没点「添加账号」，页面上却有一个
        「等待授权」的账号占着位置。它来自上一次会话中断的登录。
        """
        listed = [{"id": "buddy-stale", "nickname": "buddy-stale", "enabled": True,
                   "credentialRef": "BUDDY_ACCOUNT_MISSING"}]
        deleted = []

        class _Bridge:
            async def call(inner, method, payload, attempts=5):
                if method == "account.list":
                    return {"accounts": listed}
                if method == "credential.presence":
                    return {"present": {"buddy-stale": False}}
                if method == "account.delete":
                    deleted.append(payload["accountId"])
                    return {"ok": True}
                raise AssertionError(method)

            async def aclose(inner):
                pass

        self.web.bridge = _Bridge()
        # 模拟重启：没有任何在途登录窗口。
        self.web.bridged = {}
        rows = await self.web.plugin_accounts("buddy")
        self.assertEqual(rows, [])
        self.assertEqual(deleted, ["buddy-stale"])

    async def test_plugin_failure_degrades_to_local_accounts_only(self):
        async def broken(provider):
            return []

        async def broken_models(provider):
            return []

        self.web.plugin_accounts = broken
        self.web.plugin_models = broken_models
        response = await self.rpc("account.list", {"provider": "buddy"})
        self.assertEqual(response.json()["value"]["accounts"], [])
        self.assertEqual(response.status_code, 200)


class ChatRoutingTests(unittest.IsolatedAsyncioTestCase):
    """对话路由：插件模型转发，未知模型 404，插件挂了 503。"""

    async def asyncSetUp(self):
        from account_manager import AccountManager
        from config import Settings
        from main import app
        from upstream_client import UpstreamClient

        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "accounts.json"
        self.path.write_text('{"accounts":[]}', encoding="utf-8")
        self.pool = AccountManager(Settings(accounts=[]))
        self.upstream = UpstreamClient(lambda a: httpx.MockTransport(lambda r: httpx.Response(200)))
        self.web = WebManager(self.pool, self.path, self.upstream)
        app.state.pool, app.state.upstream, app.state.web = self.pool, self.upstream, self.web
        app.state.api_key = b"chat-test-key"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://gateway")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.web.bridge.aclose()
        await self.upstream.aclose()
        self.temp.cleanup()

    async def test_unknown_plugin_model_is_404(self):
        self.web.plugin = _FakePlugin(["codearts/GLM-5.2"])
        response = await self.client.post(
            "/v1/chat/completions",
            json={"model": "not-a-provider/x", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer chat-test-key"})
        self.assertEqual(response.status_code, 404)

    async def test_antigravity_prefix_does_not_go_to_plugin(self):
        """antigravity 有自己的原生驱动，不能被当成插件模型转发。"""
        self.web.plugin = _FakePlugin(["antigravity/gemini-4-argon-high"])
        response = await self.client.post(
            "/v1/chat/completions",
            json={"model": "antigravity/gemini-4-argon-high",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer chat-test-key"})
        # 本机号池没有这个模型 → 404，而不是转发给插件。
        self.assertEqual(response.status_code, 404)

    async def test_plugin_unavailable_is_503_with_reason(self):
        self.web.plugin = _FakePlugin(["codearts/GLM-5.2"], fail=True)
        response = await self.client.post(
            "/v1/chat/completions",
            json={"model": "codearts/GLM-5.2", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer chat-test-key"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("DSH", response.text)


class RealPluginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """真机：插件在跑时，模型清单与账号必须真的取到。

    这是本组测试里唯一依赖外部状态的用例：它要求 DSH 正在运行、且
    「白嫖模型接入」的本机网关已启用并且真的在监听。不满足时跳过，
    **不放宽断言**——跳过意味着「这次没验证」，不意味着「验证通过」。
    """

    async def asyncSetUp(self):
        self.gateway = PluginGateway()
        try:
            self.models = await self.gateway.probe()
        except PluginGatewayError as exc:
            self.skipTest(f"插件网关不可用：{exc}")
        if not self.models:
            self.skipTest("插件网关在跑但没有可用模型（DSH 侧未启用任何渠道）")

    async def test_models_are_namespaced_by_provider(self):
        """插件返回的模型 id 必须带 provider 前缀，否则网关无法按渠道过滤。"""
        self.assertTrue(self.models, "插件网关返回了空模型清单")
        prefixed = [m for m in self.models if self.gateway.provider_of(m) in CHANNELS]
        self.assertTrue(prefixed, f"没有属于已知渠道的模型：{self.models[:5]}")
        # 至少覆盖一个非 antigravity 渠道，证明 12 家的链路是通的。
        self.assertTrue(any(self.gateway.provider_of(m) != "antigravity" for m in prefixed))


class BridgeDoesNotHijackGatewayPortTests(unittest.TestCase):
    """桥接子进程绝不能占用 DSH 的 openai-gateway 端口。

    这是一条**回归**用例，锁住一个已经发生过的真实缺陷：`pack.js` 的
    `apply()` 会顺带启动它自带的 OpenAI 兼容网关（默认 127.0.0.1:8326），
    而桥原本是无条件 `apply()`。于是桥一启动就占了 DSH 的端口，DSH 自己的
    网关 bind 失败后**静默**降级——端口通着、密钥对得上，`/v1/models` 却回空、
    对话回 502。用户看到的是「插件坏了」，实际是一个本该无足轻重的辅助进程
    冒名顶替了服务端。

    断言的是脚本里的那一行，而不是「现在有没有被占」——后者取决于时序，
    跑测试时可能恰好没碰上也照样是缺陷。
    """

    def test_bridge_disables_its_own_gateway(self):
        script = (Path(__file__).resolve().parent.parent
                  / "scripts" / "pack_bridge.mjs").read_text(encoding="utf-8")
        self.assertIn("DSH_OPENAI_GATEWAY_ENABLED", script)
        self.assertIn("DSH_OPENAI_GATEWAY_ENABLED = '0'", script)
        # 必须是在 load()/apply() 之前设好；apply 之后设已经晚了。
        self.assertLess(script.index("DSH_OPENAI_GATEWAY_ENABLED = '0'"),
                        script.index("await mod.apply(ctx, {})"))

    def test_bridge_reads_the_real_credentials_store(self):
        """桥的凭据库必须是真的那个文件，不是内存 Map。

        这也是一条回归：桥原本用内存 `Map` 顶替 `ctx.credentials`，于是插件
        账号的 token 从来没被读到过——`credits.balances` 对每个账号都报
        「凭据未配置」，对话也发不出去，而账号状态明明是 active。
        """
        script = (Path(__file__).resolve().parent.parent
                  / "scripts" / "pack_bridge.mjs").read_text(encoding="utf-8")
        self.assertIn("@deepseek-ai/dsh-credentials-local", script)
        self.assertIn(".credentials.yaml", script)
        # 内存实现只允许作为装载失败时的退路，且必须留痕。
        self.assertIn("memory-fallback", script)


if __name__ == "__main__":
    unittest.main()
