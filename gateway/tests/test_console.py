"""控制台自身的契约测试。

只测「页面会不会撒谎」这一类事：状态徽章必须跟随运行态，配额没有读数时必须
显示未知，重置倒计时必须用后端归一化的 epoch 而不是本地化文本。
"""

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from account_manager import AccountManager
from antigravity_auth import _reset_epoch
from config import Settings
from main import app
from upstream_client import UpstreamClient
from web_api import WebManager


class ConsolePageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "accounts.json"
        raw = {"accounts": [{
            "id": "console-a", "channel": "antigravity", "driver": "antigravity",
            "credential_id": "ag-" + "c" * 24,
            "models": {"antigravity/gemini-test": "gemini-test"},
        }]}
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.pool = AccountManager(Settings.model_validate(raw))
        self.upstream = UpstreamClient(lambda account: httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})))
        self.web = WebManager(self.pool, self.path, self.upstream)
        app.state.pool, app.state.upstream, app.state.web = self.pool, self.upstream, self.web
        app.state.api_key = b"console-test-key"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://gateway")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()
        self.temp.cleanup()

    async def test_root_serves_own_console_not_the_copied_cards_page(self):
        """根路径必须是自己写的控制台，页面上不能有写死的示例数据。"""
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/assets/console.mjs", response.text)
        self.assertIn("/assets/console.css", response.text)
        # 旧通道卡页面的特征物不该再出现在首页。
        self.assertNotIn("channel-runtime.js", response.text)
        self.assertNotIn("白嫖模型接入", response.text)
        # 首页不该内联任何账号或百分比示例。
        self.assertNotIn("yuanyuanflashcat", response.text)
        self.assertNotIn("35.4%", response.text)

    async def test_quota_alias_points_at_the_same_console(self):
        """`/quota` 保留为别名，免得收藏夹失效，但内容必须是同一份控制台。"""
        quota = await self.client.get("/quota")
        root = await self.client.get("/")
        self.assertEqual(quota.status_code, 200)
        self.assertIn("/assets/console.mjs", quota.text)
        self.assertEqual(quota.text, root.text)

    async def test_import_window_is_its_own_page(self):
        """`/connect` 只承载自有兼容上游的填表，不再复用渠道卡页面。"""
        response = await self.client.get("/connect")
        self.assertEqual(response.status_code, 200)
        self.assertIn("导入已有上游", response.text)
        self.assertNotIn("/assets/console.mjs", response.text)

    async def test_console_assets_are_local_and_typed(self):
        for asset, needle in (
            ("/assets/console.css", "--paper"),
            ("/assets/console.mjs", "renderResetStrip"),
        ):
            response = await self.client.get(asset)
            self.assertEqual(response.status_code, 200, asset)
            self.assertIn(needle, response.text)
        module = await self.client.get("/assets/console.mjs")
        self.assertTrue(module.headers["content-type"].startswith("text/javascript"))

    async def test_console_never_hardcodes_account_state(self):
        """页面里不能出现写死的「正常 / 冷却」判定。

        旧看板把示例数据与真实读数混排，用户无法分辨哪一格是真的；这条断言
        防止新的控制台又退回那种写法。
        """
        module = (await self.client.get("/assets/console.mjs")).text
        self.assertIn("function stateOf", module)
        # 状态必须由 status 字段推出，而不是常量。
        self.assertIn("account.status", module)
        # 没有读数时显示「未知」，不能写 0 或「正常」。
        self.assertIn("'未知'", module)
        # 重置倒计时只认后端归一化的 epoch。
        self.assertIn("reset_at", module)

    async def test_reset_countdown_uses_epoch_not_localized_text(self):
        module = (await self.client.get("/assets/console.mjs")).text
        self.assertIn("item.reset_at", module)
        # 不允许退回自己 parse 本地化文本（月/日顺序会错）。
        self.assertNotIn("new Date(item.reset_time)", module)

    async def test_console_exchanges_the_desktop_bootstrap_token(self):
        """桌面端用 `#bootstrap=` 打开本页时必须静默换会话。

        重写控制台时漏了这条，导致双击 exe 开出来的窗口照样弹「粘贴网关密钥」，
        用户以为快捷方式坏了。旧界面（app.mjs）认这个 hash，控制台必须一样认。
        """
        module = (await self.client.get("/assets/console.mjs")).text
        self.assertIn("bootstrap", module)
        self.assertIn("exchangeBootstrap", module)
        # 换完必须把令牌从地址栏抹掉：刷新一次就失效，也不该留在截图里。
        self.assertIn("history.replaceState", module)

    async def test_console_keeps_hot_and_cold_reads_apart(self):
        """热数据（本机）与冷数据（账号/积分/模型）必须分开计时。

        旧实现每 15 秒把所有渠道的积分重查一遍：6 个渠道约 14 次调用 / 15 秒，
        其中 `credits.balances` 对每个账号都是一次真实上游请求。这条断言守住
        「热数据一轮只有一个计数周期、且不再整轮 load」。
        """
        module = (await self.client.get("/assets/console.mjs")).text
        self.assertNotIn("setInterval(() => { if (connected) void load() }, 15_000)", module)
        self.assertIn("HOT_MS", module)
        self.assertIn("COLD_MS", module)
        self.assertIn("CREDITS_MS", module)
        # 窗口不可见就停表：最小化一夜的窗口不该继续每分钟打上游。
        self.assertIn("visibilitychange", module)
        self.assertIn("document.hidden", module)


class ResetEpochTests(unittest.TestCase):
    def test_iso_strings_are_parsed_directly(self):
        self.assertIsNotNone(_reset_epoch("2026-11-06T10:33:19Z"))
        self.assertIsNotNone(_reset_epoch("2026-11-06T10:33:19+08:00"))

    def test_localized_text_prefers_the_future_interpretation(self):
        """`6/11/2026` 可能是 6 月 11 日也可能是 11 月 6 日。

        重置时间必然在未来，所以只要有一个解释在未来就必须选它——按美式读成
        6 月 11 日会得到「还有几个月」，按日/月读成 11 月 6 日才是真的。
        """
        epoch = _reset_epoch("12/11/2026 上午 9:36:12")
        self.assertIsNotNone(epoch)
        # 12/11 在未来 -> 必须是 11 月 12 日而不是 12 月 11 日
        from datetime import datetime
        when = datetime.fromtimestamp(epoch)
        self.assertEqual((when.month, when.day), (11, 12))

    def test_afternoon_meridiem_maps_to_pm(self):
        from datetime import datetime
        epoch = _reset_epoch("12/11/2026 下午 3:32:41")
        self.assertIsNotNone(epoch)
        self.assertEqual(datetime.fromtimestamp(epoch).hour, 15)

        epoch = _reset_epoch("12/11/2026 上午 9:36:12")
        self.assertIsNotNone(epoch)
        self.assertEqual(datetime.fromtimestamp(epoch).hour, 9)

    def test_noon_and_midnight_are_handled(self):
        from datetime import datetime
        self.assertEqual(datetime.fromtimestamp(_reset_epoch("12/11/2026 下午 12:00:00")).hour, 12)
        self.assertEqual(datetime.fromtimestamp(_reset_epoch("12/11/2026 上午 12:00:00")).hour, 0)

    def test_unparseable_input_returns_none_rather_than_a_guess(self):
        """认不出就返回 None：错的重置时间比没有更糟。"""
        for raw in ("", None, "not a date", "2026-13-45T99:99:99Z", 12345):
            self.assertIsNone(_reset_epoch(raw), raw)


class SharedGuardTests(unittest.TestCase):
    def test_web_manager_reuses_the_pool_guard(self):
        """保护实例必须与号池同一个。

        否则「上游拒绝 -> 暂停」只写进一个没人读的对象：页面看不到暂停，
        号池也照旧换下一个号继续打，被限制的账号会一路拖累整个号池。
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            raw = {"accounts": [{
                "id": "guard-a", "models": {"chat": "upstream"},
                "endpoint": "https://example.com/v1/chat/completions",
            }]}
            path.write_text(json.dumps(raw), encoding="utf-8")
            pool = AccountManager(Settings.model_validate(raw))
            upstream = UpstreamClient(lambda account: httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True})))
            manager = WebManager(pool, path, upstream)
            self.assertIs(manager.protection, pool.protection)
            self.assertIs(upstream.protection, pool.protection)
            upstream_coro = upstream.aclose()
            try:
                import anyio
                anyio.run(lambda: upstream_coro)
            except RuntimeError:
                pass


if __name__ == "__main__":
    unittest.main()