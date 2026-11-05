"""未接入的能力必须显式报错，不能返回「空成功」。

背景：本机网关只实现了 13 家渠道里的一个子集，但前端沿用原插件的静态能力表。
两边不一致时，页面会渲染出点下去必然失败的按钮，而失败原因被写成
「未返回余额数据」——和「接口 502」完全同形。这组测试锁住三点：
  1. `provider.status` 如实申报每个渠道实现了什么；
  2. 没实现的写操作报「尚未接入」，而不是默默成功；
  3. 没实现的读操作给出的原因能落到页面上，不是空数组。
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
from web_api import CHANNELS, WebManager, channel_capabilities
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
            # 没有任何渠道在这一侧接入签到/领积分。
            self.assertFalse(caps["claim"], channel)
            self.assertFalse(caps["checkin"], channel)
            self.assertFalse(caps["creditStatus"], channel)
            # 只有 antigravity 有真实配额目录与可刷新的上游。
            expected = channel == "antigravity"
            self.assertEqual(caps["balances"], expected, channel)
            self.assertEqual(caps["refresh"], expected, channel)

    async def test_capabilities_are_consistent_with_the_actual_rpc_behaviour(self):
        """申报为 False 的能力，对应 RPC 必须真的拒绝；申报为 True 的必须真的可读。"""
        caps = channel_capabilities("buddy")
        self.assertFalse(caps["claim"])
        self.assertFalse(caps["balances"])

        claim = await self.rpc("credits.claimAll", {"provider": "buddy"})
        self.assertEqual(claim.status_code, 400)
        self.assertIn("尚未接入本机网关", claim.json()["error"]["message"])

        checkin = await self.rpc("usage.autoCheckin", {"provider": "buddy", "enabled": True})
        self.assertEqual(checkin.status_code, 400)
        self.assertIn("尚未接入本机网关", checkin.json()["error"]["message"])

        # 读操作不报错，但明确说明「没有这个引擎」，页面据此不渲染开关。
        read = await self.rpc("usage.autoCheckin", {})
        self.assertEqual(read.status_code, 200)
        self.assertIsNone(read.json()["value"]["autoCheckin"])

    async def test_balances_explain_themselves_instead_of_returning_empty(self):
        """空数组在页面上长成「未返回余额数据」，和接口故障无法区分。"""
        response = await self.rpc("credits.balances", {"provider": "buddy"})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["value"]["accounts"]
        self.assertEqual([row["accountId"] for row in rows], ["primary"])
        self.assertIsNone(rows[0]["balance"])
        self.assertIn("尚未接入本机网关", rows[0]["error"])
        self.assertIn("兼容上游", rows[0]["error"])

    async def test_claim_all_is_refused_on_every_channel(self):
        statuses = (await self.rpc("provider.status", {})).json()["value"]["statuses"]
        for channel in CHANNELS:
            self.assertFalse(statuses[channel]["capabilities"]["claim"], channel)
            response = await self.rpc("credits.claimAll", {"provider": channel})
            self.assertEqual(response.status_code, 400, channel)
            self.assertIn("尚未接入本机网关", response.json()["error"]["message"])

    async def test_clear_dead_names_the_real_reason(self):
        response = await self.rpc("model.clearDead", {"provider": "buddy", "modelId": "chat"})
        self.assertEqual(response.status_code, 400)
        message = response.json()["error"]["message"]
        self.assertIn("下架模型恢复", message)
        self.assertNotIn("该操作", message)

    async def test_capability_error_names_the_channel_and_the_feature(self):
        response = await self.rpc("credits.claimAll", {"provider": "qoder"})
        message = response.json()["error"]["message"]
        self.assertIn("qoder", message)
        self.assertIn("积分领取", message)
        # 提示必须告诉用户替代路径，否则「没实现」等于死路。
        self.assertIn("兼容上游", message)

    async def test_frontend_defers_to_declared_capabilities(self):
        """页面必须读取声明表，否则它照静态表画出的按钮永远点不通。"""
        runtime = (await self.client.get("/assets/channel-runtime.js")).text
        self.assertIn("const declared = status?.capabilities", runtime)
        self.assertIn("can('balances')", runtime)
        self.assertIn("can('claim')", runtime)
        self.assertIn("status.capabilities.creditStatus !== true", runtime)
        # 静态表本身保留：它是「插件全部实现时」的样子，作为回落值。
        self.assertIn("const CHANNEL_CREDITS = {", runtime)

    async def test_frontend_explains_absent_balance_instead_of_hiding_it(self):
        """没有余额接口时要说明原因。

        隐藏按钮是对的，但只隐藏会让卡片上「账号 1/1」旁边空无一物，用户以为
        余额还在加载。所以对有账号却没余额接口的渠道，卡片上必须有一句说明。
        """
        runtime = (await self.client.get("/assets/channel-runtime.js")).text
        self.assertIn("accountsCount > 0 && !can('balances')", runtime)
        self.assertIn("t('chan.balance.absent')", runtime)

        page = (await self.client.get("/assets/app.mjs")).text
        self.assertIn("'chan.balance.absent'", page)
        # 文案必须交代「账号可用、配额在上游」，不能只说「未接入」就结束。
        self.assertIn("本机网关未接入", page)
        self.assertIn("上游", page)


if __name__ == "__main__":
    unittest.main()
