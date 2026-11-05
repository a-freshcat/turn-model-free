"""Exercise the original UI bridge against the real pool, including persistence."""

import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

import httpx

from account_manager import AccountManager
from config import Settings
from demo_upstream import transport_for
from main import app
from upstream_client import UpstreamClient
from web_api import WebManager
from test_gateway import completion


class WebUITests(unittest.IsolatedAsyncioTestCase):
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
        app.state.api_key = b"ui-test-key"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()
        self.temp.cleanup()

    async def login(self):
        response = await self.client.post("/ui/session", json={"key": "ui-test-key"},
                                          headers={"Origin": "http://gateway"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])

    async def rpc(self, method, payload):
        return await self.client.post("/ui/rpc", json={"method": method, "payload": payload},
                                       headers={"Origin": "http://gateway"})

    async def test_root_is_original_ui_and_assets_are_local(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("白嫖模型接入", response.text)
        self.assertIn("/assets/channel-runtime.js", response.text)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        asset = await self.client.get("/assets/vendor/free-m-clone/dist/demo.css")
        self.assertEqual(asset.status_code, 200)
        self.assertIn(".ofm_chan", asset.text)
        module = await self.client.get("/assets/app.mjs")
        self.assertTrue(module.headers["content-type"].startswith("text/javascript"))
        self.assertEqual((await self.client.get("/assets/../accounts.json")).status_code, 404)

    async def test_antigravity_add_starts_google_authorization(self):
        await self.login()
        runtime = await self.client.get("/assets/channel-runtime.js")
        self.assertIn("id: 'antigravity', name: 'Antigravity'", runtime.text)
        self.assertNotIn("id: 'gemini', name: 'Gemini'", runtime.text)
        response = await self.rpc("account.create", {"provider": "antigravity"})
        self.assertEqual(response.status_code, 200, response.json())
        link = urlsplit(response.json()["value"]["loginUrl"])
        self.assertEqual(link.hostname, "accounts.google.com")
        query = parse_qs(link.query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertTrue(query["state"][0])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        status = (await self.rpc("provider.status", {})).json()["value"]["statuses"]
        self.assertEqual(status["antigravity"]["accounts"]["total"], 0)

    async def test_cookie_auth_csrf_and_secret_redaction(self):
        self.assertEqual((await self.client.get("/ui/state")).status_code, 401)
        self.assertEqual((await self.client.post("/ui/session", json={"key": "ui-test-key"},
                                                headers={"Origin": "https://other.test"})).status_code, 403)
        await self.login()
        for origin in ("https://other.test", None):
            response = await self.client.post("/ui/rpc",
                json={"method": "account.delete", "payload": {"accountId": "primary"}},
                headers={} if origin is None else {"Origin": origin})
            self.assertEqual(response.status_code, 401)
        response = await self.rpc("account.list", {"provider": "buddy"})
        self.assertEqual(response.json()["value"]["accounts"][0]["id"], "primary")
        self.assertNotIn("upstream-test-only", response.text)
        self.assertNotIn("endpoint", response.text)
        self.assertNotIn("token", response.text.lower())
        self.assertEqual((await self.client.get("/v1/models")).status_code, 401)

    async def test_bootstrap_is_single_use(self):
        with patch.dict(os.environ, {"GATEWAY_UI_BOOTSTRAP": "nonce-test-only"}):
            self.web = WebManager(self.pool, self.path, self.upstream)
            app.state.web = self.web
        headers = {"Origin": "http://gateway"}
        response = await self.client.post("/ui/session", json={"bootstrap": "nonce-test-only"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        response = await self.client.post("/ui/session", json={"bootstrap": "nonce-test-only"}, headers=headers)
        self.assertEqual(response.status_code, 401)

    async def test_account_toggle_and_model_switch_change_live_pool_and_disk(self):
        await self.login()
        response = await self.rpc("account.update", {"accountId": "primary", "patch": {"enabled": False}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pool.states[0].status, "disabled")
        self.assertEqual(json.loads(self.path.read_text())["accounts"][0]["status"], "disabled")
        await self.rpc("account.update", {"accountId": "primary", "patch": {"enabled": True}})
        response = await self.rpc("model.setDisabled", {"provider": "buddy", "modelId": "chat", "disabled": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pool.models, [])
        response = await self.rpc("model.setAllDisabled", {"provider": "buddy", "disabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pool.models, ["chat"])
        self.assertEqual(self.pool.states[0].status, "active")
        self.assertEqual(json.loads(self.path.read_text())["accounts"][0]["token"], "upstream-test-only")

    async def test_import_poll_delete_and_duplicate_submission(self):
        await self.login()
        # 12 家的 account.create 现在走厂商登录（见 test_bridge_login.py）；
        # 导入仍然可达，但走显式入口。
        response = await self.rpc("account.importWindow", {"provider": "qoder"})
        pending = response.json()["value"]
        self.assertTrue(pending["loginUrl"].startswith("http://gateway/connect?request="))
        account = {"id": "secondary", "endpoint": "https://provider.example/v1/chat/completions",
                   "models": {"chat2": "other-model"}, "token": "second-test-only"}
        response = await self.client.post("/ui/import",
            json={"request_id": pending["accountId"], "account": account},
            headers={"Origin": "http://gateway"})
        self.assertEqual(response.status_code, 200)
        response = await self.rpc("login.poll", {"accountId": pending["accountId"]})
        self.assertTrue(response.json()["value"]["success"])
        self.assertEqual(self.pool.states[1].account.channel, "qoder")
        response = await self.client.post("/ui/import",
            json={"request_id": pending["accountId"], "account": {**account, "id": "third"}},
            headers={"Origin": "http://gateway"})
        self.assertEqual(response.status_code, 409)
        response = await self.rpc("account.delete", {"accountId": "secondary"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.pool.states), 1)
        response = await self.rpc("account.delete", {"accountId": "primary"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pool.states, [])
        self.assertEqual(Settings.model_validate_json(self.path.read_text()).accounts, [])

    async def test_delete_in_flight_is_rejected_without_mutating_file(self):
        await self.login()
        before = self.path.read_bytes()
        async with AsyncExitStack() as lifetime:
            await self.pool.acquire("chat", set(), lifetime)
            response = await self.rpc("account.delete", {"accountId": "primary"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.pool.states[0].in_flight, 0)

    async def test_failed_disk_commit_does_not_change_live_state(self):
        await self.login()
        with patch.object(self.web, "_write", side_effect=OSError):
            response = await self.rpc("account.update", {"accountId": "primary", "patch": {"enabled": False}})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.pool.states[0].status, "active")

    async def test_account_test_and_ui_chat_use_upstream(self):
        await self.login()
        response = await self.rpc("account.test", {"accountId": "primary"})
        self.assertTrue(response.json()["value"]["ok"])
        response = await self.client.post("/ui/chat",
            json={"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
            headers={"Origin": "http://gateway"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "chat.completion")
        self.assertEqual(self.pool.states[0].in_flight, 0)

    async def test_invalid_import_does_not_expose_submitted_secret(self):
        await self.login()
        pending = (await self.rpc("account.importWindow", {"provider": "buddy"})).json()["value"]["accountId"]
        response = await self.client.post("/ui/import", json={
            "request_id": pending, "account": {"id": "bad", "endpoint": "not-a-url",
                                              "models": {"chat": "x"}, "token": "secret-must-not-echo"}},
            headers={"Origin": "http://gateway"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("secret-must-not-echo", response.text)
        self.assertEqual(len(self.pool.states), 1)


if __name__ == "__main__":
    unittest.main()
