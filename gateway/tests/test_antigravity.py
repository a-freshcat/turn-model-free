"""OAuth callback, persistence, refresh and native transport with fake Google HTTP."""

import asyncio
import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from account_manager import AccountManager
from antigravity_auth import OAuthCallbackRedaction
from config import Settings
from credential_store import CredentialStore
from main import app
from upstream_client import UpstreamClient, encode_event
from web_api import WebManager
from test_gateway import Frames


CATALOG = {
    "gemini-4-argon-high": {"displayName": "Gemini 4 Argon (High)", "quotaInfo": {"remainingFraction": .72}},
    "claude-opus-high": {"displayName": "Claude Opus 5.5 (High)", "quotaInfo": {"remainingFraction": .60}},
    "gpt-oss": {"displayName": "GPT-OSS 120B (Medium)", "quotaInfo": {"remainingFraction": .60}},
    "gemini-flash": {"displayName": "Gemini 3.8 Flash (Medium)", "quotaInfo": {"remainingFraction": .9961}},
    "tab_internal": {"displayName": "tab_flash_lite_preview", "quotaInfo": {"remainingFraction": .9961}},
}


class AntigravityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "accounts.json"
        self.path.write_text('{"accounts":[]}', encoding="utf-8")
        self.pool = AccountManager(Settings(accounts=[]))
        self.calls, self.refreshes, self.generates = [], 0, 0
        self.fail_catalog = False
        self.invalid_grant = False
        self.first_401 = False
        self.truncate = False
        self.native_bodies = []
        self.upstream = UpstreamClient(lambda account: httpx.MockTransport(self.google))
        self.manager = WebManager(self.pool, self.path, self.upstream)
        # An empty pool is not a mock pool in this test.
        self.manager.demo = False
        self.manager.ag.client = httpx.AsyncClient(transport=httpx.MockTransport(self.google), trust_env=False)
        app.state.pool, app.state.upstream, app.state.web = self.pool, self.upstream, self.manager
        app.state.api_key = b"oauth-test-key"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000")
        response = await self.client.post("/ui/session", json={"key": "oauth-test-key"},
                                          headers={"Origin": "http://localhost:8000"})
        self.assertEqual(response.status_code, 200)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()
        self.temp.cleanup()

    async def google(self, request):
        path = request.url.path
        self.calls.append(path)
        if path == "/token":
            data = parse_qs(request.content.decode())
            if data["grant_type"] == ["refresh_token"]:
                self.refreshes += 1
                if self.invalid_grant:
                    return httpx.Response(400, json={"error": "invalid_grant",
                                                    "error_description": "fixture-sensitive-token"})
                return httpx.Response(200, json={"access_token": "fixture-refreshed",
                                                "refresh_token": "fixture-rotated", "expires_in": 3600})
            self.assertEqual(data["redirect_uri"], ["http://localhost:8000/oauth-callback"])
            self.assertEqual(base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"][0].encode()).digest()).rstrip(b"=").decode(),
                             self.query["code_challenge"][0])
            return httpx.Response(200, json={"access_token": "fixture-access",
                                            "refresh_token": "fixture-refresh", "expires_in": 3600})
        if path == "/oauth2/v2/userinfo":
            return httpx.Response(200, json={"id": "fixture-subject", "email": "fixture@example.test"})
        if path.endswith(":loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": {"id": "fixture-project"}})
        if path.endswith(":fetchAvailableModels"):
            return httpx.Response(503 if self.fail_catalog else 200,
                                  json={"error": "fixture-sensitive-token"} if self.fail_catalog else {"models": CATALOG})
        if path.endswith(":generateContent") or path.endswith(":streamGenerateContent"):
            self.generates += 1
            if self.first_401 and self.generates == 1:
                return httpx.Response(401, json={"error": {"code": 401}})
            self.native_bodies.append(json.loads(request.content))
            candidate = {"index": 0, "content": {"role": "model", "parts": [{"text": "Hello"}]},
                         "finishReason": "STOP"}
            meta = {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}
            if path.endswith(":generateContent"):
                return httpx.Response(200, json={"response": {"candidates": [candidate], "usageMetadata": meta}})
            first = {"response": {"candidates": [{"index": 0, "content": {"parts": [{"text": "Hel"}]}}]}}
            last = {"response": {"candidates": [{**candidate, "content": {"parts": [{"text": "lo"}]}}],
                                 "usageMetadata": meta}}
            frames = [encode_event(first)] + ([] if self.truncate else [encode_event(last)])
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Frames(frames))
        raise AssertionError("Unexpected Google path: " + path)

    async def rpc(self, method, payload):
        return await self.client.post("/ui/rpc", json={"method": method, "payload": payload},
                                       headers={"Origin": "http://localhost:8000"})

    async def begin(self):
        response = await self.rpc("account.create", {"provider": "antigravity"})
        self.assertEqual(response.status_code, 200, response.text)
        self.pending = response.json()["value"]["accountId"]
        url = urlsplit(response.json()["value"]["loginUrl"])
        self.query = parse_qs(url.query)
        return self.query["state"][0]

    async def authorize(self):
        state = await self.begin()
        response = await self.client.get("/oauth-callback", params={"state": state, "code": "fixture-code"})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    async def test_callback_persists_encrypted_credentials_and_discovers_models(self):
        response = await self.authorize()
        self.assertNotIn("fixture-access", response.text)
        self.assertNotIn("fixture-refresh", response.text)
        self.assertEqual(len(self.pool.states), 1)
        account = self.pool.states[0].account
        self.assertEqual(account.driver, "antigravity")
        self.assertEqual(len(account.models), 5)
        disk = self.path.read_text()
        self.assertNotIn("fixture-access", disk)
        self.assertNotIn("fixture-refresh", disk)
        sealed = (Path(self.temp.name) / ".gateway-auth" / (account.id + ".json")).read_bytes()
        self.assertNotIn(b"fixture-access", sealed)
        value = self.manager.ag.store.load(account.id)
        self.assertEqual(value["project_id"], "fixture-project")
        poll = (await self.rpc("login.poll", {"accountId": self.pending})).json()["value"]
        self.assertTrue(poll["success"])
        public = (await self.rpc("account.list", {"provider": "antigravity"})).text
        self.assertNotIn("fixture-access", public)
        self.assertNotIn("refresh_token", public)

    async def test_state_is_single_use_and_invalid_state_performs_no_http(self):
        response = await self.client.get("/oauth-callback", params={"state": "wrong", "code": "do-not-echo"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("do-not-echo", response.text)
        self.assertEqual(self.calls, [])
        await self.authorize()
        count = len(self.calls)
        response = await self.client.get("/oauth-callback", params={"state": self.query["state"][0], "code": "replay"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.calls), count)

    async def test_declined_authorization_does_not_add_an_account(self):
        state = await self.begin()
        response = await self.client.get("/oauth-callback", params={"state": state, "error": "access_denied"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pool.states, [])
        self.assertEqual(self.calls, [])
        self.assertFalse((await self.rpc("login.poll", {"accountId": self.pending})).json()["value"]["success"])

    async def test_catalog_failure_keeps_real_authorization_without_fake_models(self):
        self.fail_catalog = True
        response = await self.authorize()
        self.assertIn("暂未就绪", response.text)
        self.assertNotIn("fixture-sensitive-token", response.text)
        self.assertEqual(self.pool.states[0].account.models, {})
        self.assertTrue((await self.rpc("login.poll", {"accountId": self.pending})).json()["value"]["success"])

    async def test_refresh_is_singleflight_and_rotated_token_is_saved(self):
        await self.authorize()
        id_ = self.pool.states[0].account.id
        record = self.manager.ag.store.load(id_)
        record["expires_at"] = 0
        self.manager.ag.store.save(id_, record)
        await asyncio.gather(*(self.manager.ag.credential(id_) for _ in range(8)))
        self.assertEqual(self.refreshes, 1)
        self.assertEqual(self.manager.ag.store.load(id_)["refresh_token"], "fixture-rotated")

    async def test_three_pool_quota_api_has_no_tokens_and_no_cross_pool_average(self):
        await self.authorize()
        response = await self.client.get("/ui/quota")
        data = response.json()["accounts"][0]["models"]
        by_id = {row["id"]: row for row in data}
        self.assertEqual(by_id["gemini-4-argon-high"]["pool"], "argon")
        self.assertEqual(by_id["claude-opus-high"]["pool"], "claude_gpt")
        self.assertEqual(by_id["gpt-oss"]["pool"], "claude_gpt")
        self.assertEqual(by_id["tab_internal"]["pool"], "gemini_shared")
        self.assertEqual(by_id["gemini-flash"]["remaining"], 99.61)
        self.assertNotIn("access_token", response.text)
        self.assertNotIn("refresh_token", response.text)
        page = await self.client.get("/quota")
        self.assertEqual(page.status_code, 200)
        # 配额看板已并入控制台；旧地址保留为别名，内容必须是同一份控制台。
        self.assertIn("console.mjs", page.text)
        models = (await self.rpc("model.list", {"provider": "antigravity"})).json()["value"]["models"]
        self.assertTrue(any(row["name"] == "Gemini 4 Argon (High)" for row in models))

    async def test_native_json_stream_and_401_refresh(self):
        await self.authorize()
        self.first_401 = True
        body = {"model": "antigravity/gemini-4-argon-high", "messages": [{"role": "user", "content": "Hi"}]}
        response = await self.client.post("/v1/chat/completions", json=body,
                                          headers={"Authorization": "Bearer oauth-test-key"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(self.refreshes, 1)
        self.assertEqual(self.native_bodies[0]["request"]["contents"][0]["parts"], [{"text": "Hi"}])
        response = await self.client.post("/v1/chat/completions",
            json={**body, "stream": True, "stream_options": {"include_usage": True}},
            headers={"Authorization": "Bearer oauth-test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('"content":"Hel"', response.text)
        self.assertIn('"content":"lo"', response.text)
        self.assertIn('"total_tokens":5', response.text)
        self.assertTrue(response.content.endswith(b"data: [DONE]\n\n"))

    async def test_native_truncation_is_an_error_without_replay(self):
        await self.authorize()
        self.truncate = True
        response = await self.client.post("/v1/chat/completions",
            json={"model": "antigravity/gemini-4-argon-high", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            headers={"Authorization": "Bearer oauth-test-key"})
        self.assertIn("upstream_stream_interrupted", response.text)
        self.assertNotIn("[DONE]", response.text)
        self.assertEqual(self.generates, 1)

    async def test_invalid_grant_marks_reauthentication_without_leaking_tokens(self):
        await self.authorize()
        account = self.pool.states[0].account
        record = self.manager.ag.store.load(account.id)
        record["expires_at"] = 0
        self.manager.ag.store.save(account.id, record)
        self.invalid_grant = True
        response = await self.client.post("/v1/chat/completions",
            json={"model": "antigravity/gemini-4-argon-high", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": "Bearer oauth-test-key"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.pool.states[0].status, "disabled")
        self.assertNotIn("fixture-sensitive-token", response.text)
        row = (await self.rpc("account.list", {"provider": "antigravity"})).json()["value"]["accounts"][0]
        self.assertFalse(row["refreshable"])
        self.assertEqual(row["authStatus"], "reauth")

    async def test_first_real_login_promotes_demo_without_overwriting_example(self):
        raw = {"accounts": [{"id": "demo", "driver": "demo", "models": {"demo-chat": "fixture"}}]}
        demo_path = Path(self.temp.name) / "accounts.demo.json"
        demo_path.write_text(json.dumps(raw), encoding="utf-8")
        self.pool = AccountManager(Settings.model_validate(raw))
        self.manager.pool, self.manager.path, self.manager.raw = self.pool, demo_path, raw
        self.manager.demo = True
        app.state.pool = self.pool
        before = demo_path.read_bytes()
        await self.authorize()
        self.assertFalse(self.manager.demo)
        self.assertEqual(self.manager.path, self.path)
        self.assertEqual(demo_path.read_bytes(), before)
        restored = Settings.model_validate_json(self.path.read_text())
        self.assertEqual([a.driver for a in restored.accounts], ["antigravity"])

    async def test_tool_history_preserves_names_and_real_thought_signature(self):
        from antigravity_driver import native_request
        request = native_request([
            {"role": "user", "content": "Call tool"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"SG"}'},
                "extra_content": {"google": {"thought_signature": "fixture-signature"}}}]},
            {"role": "tool", "tool_call_id": "call1", "content": "sunny"},
        ], {})
        call = request["contents"][1]["parts"][0]
        self.assertEqual(call["functionCall"]["args"], {"city": "SG"})
        self.assertEqual(call["thoughtSignature"], "fixture-signature")
        self.assertEqual(request["contents"][2]["parts"][0]["functionResponse"]["name"], "weather")

    async def test_invalid_request_does_not_cool_account(self):
        await self.authorize()
        response = await self.client.post("/v1/chat/completions",
            json={"model": "antigravity/gemini-4-argon-high", "messages": [
                {"role": "tool", "tool_call_id": "missing", "content": "result"}]},
            headers={"Authorization": "Bearer oauth-test-key"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pool.states[0].fail_count, 0)
        self.assertEqual(self.generates, 0)

    async def test_callback_query_is_removed_before_access_logging(self):
        observed = {}
        async def inner(scope, receive, send):
            observed.update(scope)
        scope = {"type": "http", "path": "/oauth-callback",
                 "query_string": b"state=safe-state&code=fixture-secret-code"}
        await OAuthCallbackRedaction(inner)(scope, None, None)
        self.assertEqual(observed["query_string"], b"")
        self.assertEqual(observed["state"]["oauth_callback_params"]["code"], "fixture-secret-code")


if __name__ == "__main__":
    unittest.main()
