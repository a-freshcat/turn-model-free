"""未接入的能力必须显式报错，不能返回「空成功」。

背景：本机网关把 13 家渠道里的 12 家转发给插件的 `channel-pack`，只自己实现
Antigravity。前端沿用原插件的静态能力表，两边不一致时页面会渲染出点下去必然
失败的按钮，而失败原因被写成「未返回余额数据」——和「接口 502」完全同形。
这组测试锁住三点：
  1. `provider.status` 如实申报每个渠道实现了什么；
  2. 能力声明与 RPC 的真实行为一致（申报 False 必须真的拒绝）；
  3. 读操作给出的原因能落到页面上，不是空数组。
"""

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from account_manager import AccountManager
from config import Settings
from demo_upstream import transport_for
from main import app
from upstream_client import UpstreamClient
from web_api import (CHANNELS, _PLUGIN_CLAIM, _PLUGIN_CREDIT_STATUS, WebManager,
                     channel_capabilities)
from test_gateway import completion


class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "accounts.json"
        self.raw = {"accounts": [{
            "id": "primary", "channel": "buddy", "nickname": "Local account",
            "endpoint": "http://127.0.0.1:9999/v1/chat/completions",
            "models": {"chat": "upstream-chat"}, "token": "upstream-test-only",
        }]}
        self.path.write_text(json.dumps(self.raw), encoding="utf-8")
        self.pool = AccountManager(Settings.model_validate(self.raw))
        self.upstream = UpstreamClient(lambda account: httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion())))
        self.web = WebManager(self.pool, self.path, self.upstream)
        app.state.pool, app.state.upstream, app.state.web = self.pool, self.upstream, self.web
        app.state.api_key = b"cap-test-key"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://gateway")
        response = await self.client.post("/ui/session", json={"key": "cap-test-key"},
                                          headers={"Origin": "http://gateway"})
        self.assertEqual(response.status_code, 200)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()
        self.temp.cleanup()

    async def rpc(self, method, payload):
        return await self.client.post("/ui/rpc", json={"method": method, "payload": payload},
                                       headers={"Origin": "http://gateway"})

    async def test_status_declares_capabilities_for_every_channel(self):
        statuses = (await self.rpc("provider.status", {})).json()["value"]["statuses"]
        self.assertEqual(set(statuses), set(CHANNELS))
        for channel, row in statuses.items():
            self.assertIn("capabilities", row, channel)
            caps = row["capabilities"]
            # 没有后台签到引擎：自动签到是插件的调度，不在本机网关里。
            self.assertFalse(caps["checkin"], channel)
            # 12 家的余额由插件查询（token 在 ~/.dsh/.credentials.yaml），
            # antigravity 则读本机 OAuth 目录。两侧都有余额能力。
            self.assertTrue(caps["balances"], channel)
            self.assertTrue(caps["refresh"], channel)
            # 领取与签到状态必须以 pack.js 的分支清单为准，不能凭印象声明：
            # 声明了但那边没实现，按钮点下去就是一句「操作失败」。
            self.assertEqual(caps["claim"], channel in _PLUGIN_CLAIM, channel)
            self.assertEqual(caps["creditStatus"], channel in _PLUGIN_CREDIT_STATUS, channel)

    async def test_capabilities_are_consistent_with_the_actual_rpc_behaviour(self):
        """申报为 False 的能力，对应 RPC 必须真的拒绝。"""
        caps = channel_capabilities("workbuddy")
        self.assertFalse(caps["claim"])          # pack.js 明确回「没有签到接口」
        self.assertTrue(caps["balances"])        # 但余额查得到

        claim = await self.rpc("credits.claimAll", {"provider": "workbuddy"})
        self.assertEqual(claim.status_code, 400)
        self.assertIn("没有签到接口", claim.json()["error"]["message"])

        checkin = await self.rpc("usage.autoCheckin", {"provider": "workbuddy", "enabled": True})
        self.assertEqual(checkin.status_code, 400)
        self.assertIn("尚未接入本机网关", checkin.json()["error"]["message"])

        # 读操作不报错，但明确说明「没有这个引擎」，页面据此不渲染开关。
        read = await self.rpc("usage.autoCheckin", {})
        self.assertEqual(read.status_code, 200)
        self.assertIsNone(read.json()["value"]["autoCheckin"])

    async def test_antigravity_claim_is_refused_with_its_own_reason(self):
        """antigravity 的配额按 OAuth 目录读，没有签到这个概念。"""
        response = await self.rpc("credits.claimAll", {"provider": "antigravity"})
        self.assertEqual(response.status_code, 400)
        message = response.json()["error"]["message"]
        self.assertIn("antigravity", message)
        self.assertIn("OAuth", message)

    async def test_balances_forward_to_plugin_instead_of_returning_empty(self):
        """余额必须真的转发给插件，而不是编一句「没有余额接口」。

        以前这里对 12 家一律返回「本机网关没有余额接口」，于是页面上每个插件
        账号都显示「未返回余额数据」——而积分其实查得到。判据是「插件的
        `credits.balances` 被调用过」，而不是「返回了非空列表」：测试环境里
        没有真插件账号，非空与否取决于外部状态。
        """
        seen = []

        async def fake_call(method, payload=None, *, attempts=5):
            seen.append((method, payload))
            if method == "account.list":
                return {"accounts": []}
            if method == "credits.balances":
                return {"accounts": [], "windowDays": 15}
            raise AssertionError(f"unexpected bridge call: {method}")

        self.web.bridge.call = fake_call
        response = await self.rpc("credits.balances", {"provider": "buddy"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("credits.balances", [m for m, _ in seen])

    async def test_clear_dead_names_the_real_reason(self):
        response = await self.rpc("model.clearDead", {"provider": "buddy", "modelId": "chat"})
        self.assertEqual(response.status_code, 400)
        message = response.json()["error"]["message"]
        self.assertIn("下架模型恢复", message)
        self.assertNotIn("该操作", message)

    async def test_capability_error_names_the_channel_and_the_feature(self):
        """没实现的能力，报错必须说清是哪个渠道的哪个功能。"""
        response = await self.rpc("model.clearDead", {"provider": "qoder"})
        message = response.json()["error"]["message"]
        self.assertIn("qoder", message)
        self.assertIn("下架模型恢复", message)

    async def test_frontend_defers_to_declared_capabilities(self):
        """页面必须读取声明表，否则它照静态表画出的按钮永远点不通。"""
        runtime = (await self.client.get("/assets/channel-runtime.js")).text
        self.assertIn("const declared = status?.capabilities", runtime)
        self.assertIn("can('balances')", runtime)
        self.assertIn("can('claim')", runtime)
        self.assertIn("status.capabilities.creditStatus !== true", runtime)
        # 静态表本身保留：它是「插件全部实现时」的样子，作为回落值。
        self.assertIn("const CHANNEL_CREDITS = {", runtime)

    async def test_quota_board_shows_plugin_credits(self):
        """配额看板必须把插件积分读出来，不能只显示并发负载。

        「看不到积分」是用户直接报过的问题：看板只画 antigravity 的三个池，
        插件账号那一列是个空的并发条。判据是看板真的去要了 `credits.balances`。
        """
        page = (await self.client.get("/assets/quota.mjs")).text
        self.assertIn("credits.balances", page)
        self.assertIn("balance.packages", page)
        self.assertIn("balanceError", page)


    async def test_quota_board_labels_the_two_tencent_channels_correctly(self):
        """`buddy` 是国内版、`workbuddy` 是国际版，不能写反。

        用户报障：「在配额界面中 codebuddy 会被显示为国际版，workbuddy 反倒是
        变成了国内版」。原先看板就是这么写的：`workbuddy: 'WorkBuddy 国内版'`、
        `buddy: 'CodeBuddy 国际版'`，两家对调。以插件自己的注册表为准
        （`web/channel-runtime.js` 的 CHANNELS 表：buddy → copilot.tencent.com，
        workbuddy → workbuddy.ai）。
        """
        page = (await self.client.get("/assets/quota.mjs")).text
        labels = page.split("const CHANNEL_HINTS", 1)[0]
        self.assertIn("buddy: 'WorkBuddy 国内版'", labels)
        self.assertIn("workbuddy: 'WorkBuddy 国际版'", labels)
        # 反向写法必须彻底消失，否则又是对调。
        self.assertNotIn("workbuddy: 'WorkBuddy 国内版'", labels)
        self.assertNotIn("buddy: 'CodeBuddy 国际版'", labels)
        # 标签只差一个字，域名提示是唯一能一眼分清的依据。
        self.assertIn("copilot.tencent.com", page)
        self.assertIn("workbuddy.ai", page)


class LauncherPortTests(unittest.TestCase):
    """本机监听地址不能落在 8000。

    8000 是本机另一个 Web 插件面板的位置。撞上去时 `serve()` bind 失败退出，
    用户看到的只是一句「Cannot start the loopback listener」——但 8000 上跑着的
    其实是别的服务，很容易被当成这个面板自己起坏了。这里锁住默认值与启动脚本
    里写的那个数字，避免下次改动又把它挪回去。
    """

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_default_port_is_not_shared_with_other_panels(self):
        source = (self.root / "launch_web.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_PORT = 8199", source)
        self.assertIn("default=DEFAULT_PORT", source)
        self.assertNotIn("default=8000", source)

    def test_launcher_cmd_passes_the_port_explicitly(self):
        """地址要能在命令里一眼看到，而不是埋在工作目录的某个默认值里。"""
        script = (self.root / "launch-web.cmd").read_text(encoding="utf-8")
        self.assertIn('set "GATEWAY_PORT=8199"', script)
        self.assertIn("--port %GATEWAY_PORT%", script)

    def test_cmd_files_are_pure_ascii(self):
        """`.cmd` 里一个非 ASCII 字节都不能有。

        这是用户直接踩到的坑：`cmd.exe` 按系统 OEM 代码页（中文 Windows 是
        936/GBK）读取批处理文件，UTF-8 编码的中文注释被当成 GBK 解码后变成
        乱码，还会把 `rem` 行拆散、和下一行粘起来当命令执行——

            '屾挒涓婂幓鍙細寰楀埌涓€鍙?rem' 不是内部或外部命令

        脚本仍然会往下跑到真正的原因（端口被占），于是屏幕上「乱码命令报错」
        和「端口错误」混在一起，看起来像是脚本自己坏了。
        中文说明写在 README/CHANGELOG 里，`.cmd` 只放 ASCII。
        """
        candidates = [self.root / "launch-web.cmd", self.root.parent / "launch-web.cmd"]
        checked = 0
        for path in candidates:
            if not path.is_file():
                continue
            checked += 1
            raw = path.read_bytes()
            offenders = [(index, byte) for index, byte in enumerate(raw) if byte > 127]
            self.assertEqual(offenders[:5], [], f"{path} 含非 ASCII 字节（cmd.exe 会按 GBK 解码）")
        self.assertGreater(checked, 0, "没有找到任何 launch-web.cmd")

    def test_port_in_use_names_the_port_and_the_owner(self):
        """端口被占时必须说清哪条端口、谁占着。

        只说 `try another --port` 等于让用户自己猜。实测最难分辨的情形是上一次
        启动的窗口还开着：端口在听、页面也能打开，看起来像起成功了。
        """
        module = self.root / "launch_web.py"
        source = module.read_text(encoding="utf-8")
        self.assertIn("class PortInUse", source)
        self.assertIn("def port_in_use_message", source)
        self.assertIn("10048", source)
        # 探端口要早于「Mode / 密钥 / 请保持窗口打开」那些看起来成功的输出。
        self.assertLess(source.index("_port_available(args.port)"),
                        source.index('print("Mode: "'))
        # 用一条真实被占的端口验证文案本身。
        import socket
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        busy = probe.getsockname()[1]
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_launch_web_probe", module)
            module_obj = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module_obj)
            message = module_obj.port_in_use_message(busy)
            self.assertIn(str(busy), message)
            self.assertIn("launch-web.cmd --port", message)
            # 占用者应该是这个测试进程自己：能说出 PID 才算真的查了。
            self.assertIn("PID", message)
        finally:
            probe.close()

    def test_run_ps1_agrees_with_the_launcher(self):
        script = (self.root / "run.ps1").read_text(encoding="utf-8")
        self.assertIn("[int]$Port = 8199", script)

    def test_root_entry_points_at_the_gateway_launcher(self):
        """根目录的 `launch-web.cmd` 是双击入口，必须继续转发，不能自己再写一份端口。

        两份端口各自漂移是这类脚本最容易出的问题：改了一处、另一处还是旧的。

        这个文件只存在于发布仓库（`free-m-clone/` 根），源目录没有它。不存在时
        跳过——「这次没验证」，不是「验证通过」。
        """
        entry = self.root.parent / "launch-web.cmd"
        if not entry.is_file():
            self.skipTest("根目录没有 launch-web.cmd（源目录形态）")
        script = entry.read_text(encoding="utf-8")
        self.assertIn(r"gateway\launch-web.cmd", script)
        self.assertIn("8199", script)
        # 不该自己调用 python —— 那就成了第二份启动逻辑。
        self.assertNotIn("launch_web.py", script)


if __name__ == "__main__":
    unittest.main()
