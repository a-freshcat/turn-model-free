"""备用网关兜底路径的守护测试。

用户报的 503 `plugin_gateway_unavailable`（`转发到 DSH 插件网关失败（ConnectError）`）
根因不是号池坏了，而是对话要经过插件自己的 OpenAI 网关（默认 8326，归 DSH
桌面端所有）；DSH 没开时那个端口是空的。桥能装载同一份 pack.js 在备用端口
（18326）提供同样能力，这条兜底必须一直有效。

同时守住两条底线：
  1. 桥**绝不**去碰 8326（抢端口会让 DSH 自己 bind 失败并静默降级）；
  2. 连不上时的报错要能照着做，而不是只丢一个异常类名。
"""

import re
import unittest
from pathlib import Path

from plugin_gateway import DEFAULT_PORT, PluginGateway, PluginGatewayError

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "pack_bridge.mjs"
GATEWAY_SCRIPT = ROOT / "scripts" / "pack_gateway.mjs"


class FakeBridge:
    """只实现 `gateway.ensure`，用来验证 Python 侧怎么消费它的回复。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def call(self, method, payload, attempts=3):
        self.calls.append((method, payload))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class EnsureGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_switches_endpoint_to_fallback_port(self):
        bridge = FakeBridge({
            "running": True,
            "address": {"host": "127.0.0.1", "port": 18326},
            "apiKey": "kn_test_key_value_for_fallback",
        })
        gateway = PluginGateway(bridge=bridge)
        self.assertEqual(gateway.port, DEFAULT_PORT)
        self.assertTrue(await gateway._ensure_bridge_gateway())
        self.assertEqual(gateway.port, 18326)
        self.assertEqual(gateway.base_url, "http://127.0.0.1:18326")
        # 备用网关的密钥也跟着切过去了，否则会 401。
        self.assertEqual(gateway.read_key(), "kn_test_key_value_for_fallback")

    async def test_ensure_reports_failure_without_touching_endpoint(self):
        """桥起不来时必须原地不动，让调用方继续报原来的错，而不是指向空端口。"""
        gateway = PluginGateway(bridge=FakeBridge({"running": False}))
        self.assertFalse(await gateway._ensure_bridge_gateway())
        self.assertEqual(gateway.port, DEFAULT_PORT)

    async def test_ensure_swallows_bridge_errors(self):
        """老版本桥没有 gateway.ensure，会抛 unknown method —— 不能因此炸掉整个请求。"""
        gateway = PluginGateway(bridge=FakeBridge(RuntimeError("unknown method: gateway.ensure")))
        self.assertFalse(await gateway._ensure_bridge_gateway())

    async def test_no_bridge_means_no_ensure(self):
        gateway = PluginGateway()
        self.assertFalse(await gateway._ensure_bridge_gateway())

    async def test_missing_running_flag_is_not_success(self):
        for reply in ({}, {"running": True}, {"running": True, "address": {}},
                      {"running": True, "address": {"port": 0}}):
            gateway = PluginGateway(bridge=FakeBridge(reply))
            self.assertFalse(await gateway._ensure_bridge_gateway(), reply)


class ConnectHelpTests(unittest.TestCase):
    def test_help_names_the_actual_cause_and_a_next_step(self):
        """`ConnectError` 本身说明不了问题，报错必须说清「8326 没起」并给出动作。"""
        gateway = PluginGateway()
        help_text = gateway._connect_help()
        self.assertIn("8326", help_text)
        self.assertIn("DSH", help_text)
        # 必须给可执行的动作，而不是让人自己猜。
        self.assertTrue(any(word in help_text for word in ("请打开", "停用", "重启")))

    def test_help_mentions_restart_when_on_fallback_port(self):
        gateway = PluginGateway(port=18326)
        self.assertIn("重启", gateway._connect_help())


class BridgeScriptInvariants(unittest.TestCase):
    """静态检查两个 Node 脚本，守住几条容易回退的约束。"""

    @classmethod
    def setUpClass(cls):
        cls.bridge = BRIDGE.read_text(encoding="utf-8")
        cls.gateway = GATEWAY_SCRIPT.read_text(encoding="utf-8")

    def test_bridge_never_binds_dsh_port(self):
        """8326 是 DSH 的。桥一旦真的去 bind 它，DSH 会静默降级成 502。

        只检查**代码行**：注释里提到 8326 是在解释「为什么不能碰」，那是有价值
        的说明，不该被这条断言误伤。`//` 与块注释都要排掉。
        """
        code = re.sub(r"/\*.*?\*/", "", self.bridge, flags=re.S)
        code_lines = [
            line for line in code.splitlines()
            if line.strip() and not line.lstrip().startswith('//')
        ]
        offenders = [line for line in code_lines if '8326' in line]
        self.assertEqual(offenders, [], f"桥的代码里出现了 DSH 的端口 8326：{offenders}")
        self.assertIn("18326", self.bridge)

    def test_bridge_still_disables_its_own_gateway_by_default(self):
        """桥自己不该常驻网关（只用 RPC）；备用网关是独立进程、独立端口。"""
        self.assertIn("DSH_OPENAI_GATEWAY_ENABLED", self.bridge)
        self.assertIn("= '0'", self.bridge)

    def test_ensure_handler_exists(self):
        self.assertIn("'gateway.ensure'", self.bridge)

    def test_gateway_waits_for_port_instead_of_sleeping(self):
        """等端口可连而不是固定 sleep：实测首启要 3~8 秒，睡短了必然误判。"""
        self.assertIn("isPortOpen", self.bridge)
        self.assertIn("for (let i = 0; i < 300", self.bridge)

    def test_gateway_child_is_not_killed_by_stdin_close(self):
        """曾经的坑：子进程以 stdin 关闭为退出条件，而桥给的是 'ignore'，
        于是网关刚启动就退出，表现为「ensure 永远报未就绪、18326 一直 closed」。"""
        self.assertNotIn("process.stdin.on('close'", self.gateway)
        self.assertNotIn("process.stdin.on('end'", self.gateway)

    def test_child_is_reaped_with_the_bridge(self):
        """桥退出要带走网关子进程，否则孤儿会占着 18326，
        下一次 ensure 会误判「已在运行」而连到一个将死的监听。"""
        self.assertIn("killGatewayChild", self.bridge)
        self.assertIn("process.on('exit', killGatewayChild)", self.bridge)

    def test_gateway_uses_real_llm_runtime(self):
        """空壳 `ctx.llm` 会让 pack.js 的 registerAdapter 直接抛错，适配器一个都
        注册不上 —— 端口绑了也发不出对话。"""
        self.assertIn("dsh-llm", self.gateway)
        self.assertIn("ctx.plugin", self.gateway)


if __name__ == "__main__":
    unittest.main()
