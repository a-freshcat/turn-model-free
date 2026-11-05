"""12 家渠道的真实登录：网关必须转发给插件的 channel-pack，而不是伪造。

这组测试守三件事：
  1. `account.create` 对 12 家返回**厂商域名**的登录地址，不是本地导入页；
  2. 桥接进程按行分隔 JSON 收发，请求/响应按 id 对齐，串行不乱序；
  3. 桥接不可用时给出可读错误，而不是静默退回导入页。

跑真实 Node 子进程的部分会标 `skipif`：没有 node、没有安装插件时跳过，
但**不会**退化成「假装通过」。
"""

import asyncio
import json
import shutil
import unittest
from pathlib import Path

import pytest

from bridge_client import BridgeError, PackBridge
from web_api import CHANNELS

BRIDGED = tuple(c for c in CHANNELS if c != "antigravity")

# 每家登录页应落在的域名。用后缀匹配：厂商会换 CDN，但不会换主域。
EXPECTED_HOSTS = {
    "codearts": "huaweicloud.com",
    "buddy": "tencent.com",
    "workbuddy": "workbuddy.ai",
    "lobsterai": "youdao.com",
    "qoder": "qoder.com",
    "qodercn": "qoder.cn",
    "trae": "trae.cn",
    "cline": "cline.bot",
    "loomy": "127.0.0.1",       # 插件内置二维码页
    "raccoon": "127.0.0.1",     # 插件内置登录页
    "minimax": "minimax.cn",
    "zcode": "bigmodel.cn",     # bigmodel 时；zai 时是 chat.z.ai
}


def _pack_available() -> bool:
    home = PackBridge.DEFAULT_PACK_HOME
    return shutil.which("node") is not None and (Path(home) / "vendor/channel-pack/pack.js").is_file()


needs_pack = pytest.mark.skipif(not _pack_available(),
                                reason="需要 node 与已安装的 dsh-our-free-model 插件")


class BridgeProtocolTests(unittest.IsolatedAsyncioTestCase):
    """不依赖 pack.js：用假子进程验证收发与对齐。"""

    async def test_start_reports_channels(self):
        bridge = PackBridge()
        try:
            hello = await bridge.start()
            self.assertTrue(hello.get("ready"))
            self.assertEqual(set(hello["channels"]), set(CHANNELS))
        finally:
            await bridge.aclose()

    async def test_missing_script_is_a_readable_error(self):
        bridge = PackBridge(script=Path("/nonexistent/pack_bridge.mjs"))
        with self.assertRaises(BridgeError) as caught:
            await bridge.start()
        self.assertIn("登录桥", str(caught.exception))

    async def test_unknown_node_binary_is_a_readable_error(self):
        bridge = PackBridge(node="definitely-not-a-node-binary")
        with self.assertRaises(BridgeError) as caught:
            await bridge.start()
        self.assertIn("无法启动登录桥", str(caught.exception))


@needs_pack
class RealBridgeTests(unittest.IsolatedAsyncioTestCase):
    """真跑 Node 子进程，覆盖 12 家的 account.create。"""

    async def asyncSetUp(self):
        self.bridge = PackBridge()
        await self.bridge.start()

    async def asyncTearDown(self):
        await self.bridge.aclose()

    async def test_every_bridged_channel_returns_a_vendor_login_url(self):
        for provider in BRIDGED:
            with self.subTest(provider=provider):
                value = await self.bridge.call("account.create", {"provider": provider})
                self.assertIsInstance(value, dict)
                url = value.get("loginUrl", "")
                self.assertTrue(url.startswith("http://") or url.startswith("https://"),
                                f"{provider} 没有返回登录地址：{url!r}")
                host = url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
                expected = EXPECTED_HOSTS[provider]
                self.assertTrue(host == expected or host.endswith("." + expected),
                                f"{provider} 落到了 {host}，期望 {expected}")
                # 关键断言：不是网关自己的导入页。
                self.assertNotIn("/connect?request=", url)
                self.assertTrue(value.get("accountId"), f"{provider} 缺少 accountId")

    async def test_flaky_vendor_endpoint_eventually_succeeds(self):
        """厂商端点会抖，但重试之后必须能成功——这才是用户看到的行为。

        实测（2026-11-05，本机网络）：`account.minimax.cn/oauth2/device/code`
        约 1/3 的请求 SSL EOF 或读超时，耗时 2.8s 到 22s。这里不要求单次必成，
        要求的是「在重试预算内必成」。
        """
        failures = 0
        for attempt in range(6):
            try:
                value = await self.bridge.call("account.create", {"provider": "minimax"})
            except BridgeError:
                failures += 1
                continue
            self.assertIn("account.minimax.cn", value["loginUrl"])
            self.assertTrue(value.get("accountId"))
            return
        self.fail(f"连续 6 次都失败（{failures} 次网络错误），MiniMax 登录不可用")

    async def test_account_ids_are_unique_per_call(self):
        first = await self.bridge.call("account.create", {"provider": "buddy"})
        second = await self.bridge.call("account.create", {"provider": "buddy"})
        self.assertNotEqual(first["accountId"], second["accountId"])

    async def test_unknown_provider_is_refused(self):
        with self.assertRaises(BridgeError):
            await self.bridge.call("account.create", {"provider": "not-a-channel"})

    async def test_requests_stay_aligned_under_concurrency(self):
        """并发发出时响应必须各归各——错位会把 A 的 URL 交给 B。"""
        results = await asyncio.gather(*(
            self.bridge.call("account.create", {"provider": "buddy"}) for _ in range(4)
        ))
        ids = [r["accountId"] for r in results]
        self.assertEqual(len(set(ids)), 4, ids)
        for row in results:
            self.assertIn("copilot.tencent.com", row["loginUrl"])


class WebApiBridgeWiringTests(unittest.IsolatedAsyncioTestCase):
    """网关侧：12 家不落到导入页，antigravity 仍走自己的 OAuth。"""

    async def asyncSetUp(self):
        import tempfile
        from account_manager import AccountManager
        from config import Settings
        from main import app
        from upstream_client import UpstreamClient
        from web_api import WebManager

        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "accounts.json"
        path.write_text('{"accounts":[]}', encoding="utf-8")
        self.pool = AccountManager(Settings(accounts=[]))
        import httpx
        self.upstream = UpstreamClient(lambda a: httpx.MockTransport(lambda r: httpx.Response(200)))
        self.web = WebManager(self.pool, path, self.upstream)
        app.state.pool, app.state.upstream, app.state.web = self.pool, self.upstream, self.web
        app.state.api_key = b"bridge-test-key"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://gateway")
        response = await self.client.post("/ui/session", json={"key": "bridge-test-key"},
                                         headers={"Origin": "http://gateway"})
        self.assertEqual(response.status_code, 200)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.web.bridge.aclose()
        await self.upstream.aclose()
        self.temp.cleanup()

    async def rpc(self, method, payload):
        return await self.client.post("/ui/rpc", json={"method": method, "payload": payload},
                                      headers={"Origin": "http://gateway"})

    @needs_pack
    async def test_bridged_channel_returns_vendor_url_not_import_page(self):
        response = await self.rpc("account.create", {"provider": "buddy"})
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()["value"]["loginUrl"]
        self.assertIn("copilot.tencent.com", url)
        self.assertNotIn("/connect?request=", url)

    @needs_pack
    async def test_bridge_failure_is_reported_not_silently_downgraded(self):
        """桥挂了不能偷偷退回导入页——那样用户会以为自己点的还是登录。"""
        await self.web.bridge.aclose()
        self.web.bridge = PackBridge(node="definitely-not-a-node-binary")
        response = await self.rpc("account.create", {"provider": "buddy"})
        self.assertEqual(response.status_code, 400)
        message = response.json()["error"]["message"]
        self.assertIn("无法启动", message)
        self.assertIn("buddy", message)

    async def test_antigravity_does_not_use_the_bridge(self):
        """antigravity 的凭据由本进程使用，必须留在 Python 侧。"""
        response = await self.rpc("account.create", {"provider": "antigravity"})
        self.assertEqual(response.status_code, 200)
        url = response.json()["value"]["loginUrl"]
        self.assertIn("accounts.google.com", url)
        self.assertEqual(self.web.bridged, {}, "antigravity 不应写进桥接表")

    async def test_import_window_still_reachable(self):
        response = await self.rpc("account.importWindow", {"provider": "qoder"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("/connect?request=", response.json()["value"]["loginUrl"])

    async def test_import_window_refuses_antigravity(self):
        response = await self.rpc("account.importWindow", {"provider": "antigravity"})
        self.assertEqual(response.status_code, 400)

    async def test_login_dialog_text_matches_what_the_button_actually_does(self):
        """12 家弹窗不能说「填写已有配置」——它们打开的是厂商登录页。"""
        runtime = (await self.client.get("/assets/channel-runtime.js")).text
        # 三档文案并存，按渠道与窗口类型选择。
        self.assertIn("const importing =", runtime)
        self.assertIn("t('chan.oauth.", runtime)
        self.assertIn("t('chan.import.", runtime)
        self.assertIn("importing ? t('chan.import.title')", runtime)

        page = (await self.client.get("/assets/app.mjs")).text
        # 厂商登录档必须说要「在厂商页面完成登录」，不能是「填写配置」。
        self.assertIn("'chan.login.step1': '在打开的页面完成该厂商的登录", page)
        self.assertIn("'chan.login.open': '打开登录页'", page)
        # 导入档仍在，且文案描述的是填表。
        self.assertIn("'chan.import.step1': '在打开的窗口中填写已有账号与模型配置。", page)


if __name__ == "__main__":
    unittest.main()
