"""Gemini 官方 API Key 独立池：传输、错误分类、号池行为与页面契约。

这一条链路与 Antigravity 共用 Gemini 协议内核，但**故意不共用传输层**：
Antigravity 的 `send_native` 把 OAuth 刷新、project 探测、信封组装写死在一起。
测试因此只盯三件事：
  1. 请求真的走 `x-goog-api-key` 到 `/v1beta/models/<id>`，没有信封；
  2. 400 只罚请求、不罚账号；429 是短退避；密钥作废才停用账号；
  3. 页面拿到的字段能区分「本机密钥账号 / 本机 OAuth 账号 / 插件账号」。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from unittest.mock import patch

from account_manager import AccountManager
from config import Settings
from main import app
from upstream_client import UpstreamClient, encode_event
from web_api import CHANNELS, LOCAL_CHANNELS, WebManager, channel_capabilities
from test_gateway import Frames


MODELS = {"gemini_api/gemini-3.8-flash": "gemini-3.8-flash",
          "gemini_api/gemini-3.5-flash": "gemini-3.5-flash"}


def raw_pool(**overrides):
    account = {"id": "gem-key-1", "channel": "gemini_api", "driver": "gemini",
               "nickname": "key-one", "token": "AIzaSy-fixture-key", "models": dict(MODELS),
               "max_concurrency": 2}
    account.update(overrides)
    return {"accounts": [account]}


class GeminiApiKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "accounts.json"
        self.raw = raw_pool()
        self.path.write_text(json.dumps(self.raw), encoding="utf-8")
        self.bodies, self.paths, self.headers = [], [], []
        self.status, self.payload = 200, None
        self.stream_frames = None
        self.catalog = {"models": [
            {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash",
             "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]},
            {"name": "models/gemini-3.5-flash", "displayName": "Gemini 3.5 Flash",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "displayName": "Embedding",
             "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-3.1-flash-image", "displayName": "Image",
             "supportedGenerationMethods": ["generateContent"]},
        ]}
        # 官方目录里混着一堆「签名支持但用不了」的模型。真机上旧 flash 是 404
        # （对新用户下线）、pro 是免费额度 429、tts 是 400（只要音频输出）。
        # 测试也按这三种情形区分，否则「目录就代表可用」这个错误假设会再次混过去。
        self.unusable = {
            "gemini-2.5-flash": (404, "NOT_FOUND"),
            "gemini-2.5-pro": (429, "RESOURCE_EXHAUSTED"),
            "gemini-3.1-flash-image": (400, "INVALID_ARGUMENT"),
        }
        # 目录里有、但今天免费额度已用完的模型。它们必须被标出来，而不是
        # 悄悄消失——用户今天调不动，明天刷新就能回来。
        self.daily_exhausted = set()
        self.pool = AccountManager(Settings.model_validate(self.raw))
        self.upstream = UpstreamClient(lambda account: httpx.MockTransport(self.google))
        self.web = WebManager(self.pool, self.path, self.upstream)
        self.web.demo = False
        app.state.pool, app.state.upstream, app.state.web = self.pool, self.upstream, self.web
        app.state.api_key = b"gemini-test-key"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://gateway")
        response = await self.client.post("/ui/session", json={"key": "gemini-test-key"},
                                          headers={"Origin": "http://gateway"})
        self.assertEqual(response.status_code, 200)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()
        self.temp.cleanup()

    async def google(self, request):
        self.paths.append(request.url.path)
        self.headers.append(dict(request.headers))
        if request.url.path.endswith("/models"):
            return httpx.Response(200 if self.status == 200 else self.status,
                                  json=self.catalog if self.status == 200 else self.payload)
        self.bodies.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json=self.payload)
        # 复现官方真实行为：同一把钥匙下，不同模型的结果不同。旧版本 flash 已对
        # 新用户下线（404），pro 额度不足（429），只有这几个真能对话。没有这层
        # 区分，测试会假装「目录里每个模型都能用」——正是用户踩到的那一坑。
        name = request.url.path.rsplit("/models/", 1)[-1].split(":", 1)[0]
        if name in self.unusable:
            code, status = self.unusable[name]
            return httpx.Response(code, json={"error": {"code": code, "status": status,
                                                        "message": "fixture"}})
        if name in self.daily_exhausted:
            # 免费档每天每模型 20 次用尽时的真实形状：429 + QuotaFailure(PerDay)
            # + RetryInfo。这类模型不是「不存在」，是今天调不动。
            return httpx.Response(429, json={"error": {"code": 429,
                "status": "RESOURCE_EXHAUSTED", "message": "quota",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                     "violations": [{"description": "Generate content requests per day limit "
                                                    "exceeded on Free Tier.",
                                     "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                     "quotaValue": "20"}]},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                     "retryDelay": "61941s"}]}})
        if request.url.path.endswith(":generateContent"):
            # `self.payload` 是「上游该回什么」的覆写点；没设时必须是**一个能用的
            # 回复**，否则目录探测（它看 candidates 判可用）会把所有模型都当成
            # 不可用，得到一行误导性的 gemini_no_usable_models。
            return httpx.Response(200, json=self.payload or self.reply())
        frames = self.stream_frames if self.stream_frames is not None else [
            encode_event({"candidates": [{"index": 0, "content": {"parts": [{"text": "Hel"}]}}]}),
            encode_event({"candidates": [{"index": 0, "content": {"parts": [{"text": "lo"}]},
                                          "finishReason": "STOP"}],
                          "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2,
                                            "totalTokenCount": 5}}),
        ]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=Frames(frames))

    def reply(self, text="Hello"):
        return {"candidates": [{"index": 0, "content": {"role": "model",
                                                        "parts": [{"text": text}]},
                                "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2,
                                  "totalTokenCount": 5}}

    async def rpc(self, method, payload):
        return await self.client.post("/ui/rpc", json={"method": method, "payload": payload},
                                       headers={"Origin": "http://gateway"})

    async def chat(self, body, **kwargs):
        return await self.client.post("/v1/chat/completions", json=body,
                                      headers={"Authorization": "Bearer gemini-test-key"}, **kwargs)

    # ---- 传输 -------------------------------------------------------------

    async def test_request_uses_api_key_header_and_no_envelope(self):
        self.payload = self.reply()
        response = await self.chat({"model": "gemini_api/gemini-3.8-flash",
                                    "messages": [{"role": "user", "content": "Hi"}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.paths[-1], "/v1beta/models/gemini-3.8-flash:generateContent")
        self.assertEqual(self.headers[-1]["x-goog-api-key"], "AIzaSy-fixture-key")
        self.assertNotIn("authorization", self.headers[-1])
        body = self.bodies[-1]
        # 没有 v1internal 信封：直接就是 Gemini 线格式。
        self.assertEqual(body["contents"][0]["parts"], [{"text": "Hi"}])
        self.assertNotIn("request", body)
        self.assertNotIn("project", body)
        self.assertNotIn("sessionId", body)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "Hello")

    async def test_stream_is_rewritten_to_openai_sse_with_usage(self):
        response = await self.chat({"model": "gemini_api/gemini-3.8-flash",
                                    "messages": [{"role": "user", "content": "Hi"}],
                                    "stream": True,
                                    "stream_options": {"include_usage": True}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"content":"Hel"', response.text)
        self.assertIn('"content":"lo"', response.text)
        self.assertIn('"total_tokens":5', response.text)
        self.assertTrue(response.content.endswith(b"data: [DONE]\n\n"))
        self.assertEqual(self.paths[-1], "/v1beta/models/gemini-3.8-flash:streamGenerateContent")

    async def test_tool_signature_round_trips_through_the_shared_cache(self):
        """官方端点同样要求 functionCall 带 thoughtSignature，且会回一个真实的。

        这里验证「收到就存、下一次同一调用就带回去」：缓存若没接上，多轮工具
        调用在第二跳必然 400，表现为「模型能聊但不肯用工具」。
        """
        self.payload = {"candidates": [{"index": 0, "content": {"parts": [{
            "functionCall": {"name": "weather", "args": {"city": "SG"}},
            "thoughtSignature": "ZmFrZS1zaWduYXR1cmU="}]}, "finishReason": "STOP"}]}
        first = await self.chat({"model": "gemini_api/gemini-3.8-flash",
                                 "messages": [{"role": "user", "content": "weather?"}]})
        self.assertEqual(first.status_code, 200, first.text)
        call = first.json()["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["extra_content"]["google"]["thought_signature"],
                         "ZmFrZS1zaWduYXR1cmU=")

        # 下一轮：客户端把签名带回来（正常 OpenAI 客户端不回 extra_content，
        # 所以网关必须靠缓存借出）。
        self.payload = self.reply("sunny")
        second = await self.chat({"model": "gemini_api/gemini-3.8-flash", "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": call["id"], "type": "function",
                 "function": {"name": "weather", "arguments": '{"city":"SG"}'}}]},
            {"role": "tool", "tool_call_id": call["id"], "content": "sunny"},
        ]})
        self.assertEqual(second.status_code, 200, second.text)
        part = self.bodies[-1]["contents"][1]["parts"][0]
        self.assertEqual(part["thoughtSignature"], "ZmFrZS1zaWduYXR1cmU=")

    # ---- 错误分类 ---------------------------------------------------------

    async def test_bad_request_does_not_cool_the_account(self):
        self.status = 400
        self.payload = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                  "message": "invalid argument",
                                  "details": [{"fieldViolations": [
                                      {"field": "generation_config.max_output_tokens"}]}]}}
        response = await self.chat({"model": "gemini_api/gemini-3.8-flash",
                                    "messages": [{"role": "user", "content": "Hi"}]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("max_output_tokens", response.text)
        state = self.pool.states[0]
        self.assertEqual(state.status, "active")
        self.assertEqual(state.fail_count, 0)

    async def test_rate_limit_is_a_short_backoff_not_a_ten_minute_lock(self):
        self.status = 429
        self.payload = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                  "message": "quota"}}
        response = await self.chat({"model": "gemini_api/gemini-3.8-flash",
                                    "messages": [{"role": "user", "content": "Hi"}]})
        # v1.5.1：429 的退避期不再报成 503 「无可用账号」。503 会让客户端把它
        # 当成故障立刻重试，进一步加深限流；真实的限流应当回 429 + Retry-After，
        # 让客户端自己等。
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", {k.lower() for k in response.headers})
        state = self.pool.states[0]
        self.assertEqual(state.status, "cooldown")
        self.assertEqual(state.last_failure_reason, "gemini_http_429")
        self.assertIn(state.transient_streak, (1, 2))
        # 短退避：远小于整轮 10 分钟。
        self.assertLessEqual(state.cooldown_until - time.monotonic(), 25)

    async def test_revoked_key_disables_the_account_without_leaking_it(self):
        self.status = 401
        self.payload = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                  "message": "API key not valid. Please pass a valid API key."}}
        response = await self.chat({"model": "gemini_api/gemini-3.8-flash",
                                    "messages": [{"role": "user", "content": "Hi"}]})
        self.assertEqual(response.status_code, 503)
        state = self.pool.states[0]
        self.assertEqual(state.status, "disabled")
        self.assertEqual(state.last_failure_reason, "gemini_key_invalid")
        self.assertNotIn("AIzaSy-fixture-key", response.text)

    # ---- 号池与页面契约 ----------------------------------------------------

    async def test_channel_is_local_and_declares_no_credit_surface(self):
        self.assertIn("gemini_api", CHANNELS)
        self.assertIn("gemini_api", LOCAL_CHANNELS)
        caps = channel_capabilities("gemini_api")
        self.assertFalse(caps["plugin"], "API Key 渠道的凭据在本进程，不是插件渠道")
        self.assertEqual(caps["login"], "key")
        # 官方按 RPM/TPM 限流：既没有积分包，也没有签到。
        self.assertFalse(caps["balances"])
        self.assertFalse(caps["claim"])
        self.assertFalse(caps["creditStatus"])
        # 但「刷新」是真的会做事：重拉官方模型目录，顺带验一次密钥。
        self.assertTrue(caps["refresh"])

    async def test_balances_returns_empty_instead_of_fabricated_zero(self):
        reply = await self.rpc("credits.balances", {"provider": "gemini_api"})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["value"]["accounts"], [])

    async def test_claim_is_refused_with_its_own_reason(self):
        reply = await self.rpc("credits.claimAll", {"provider": "gemini_api"})
        self.assertEqual(reply.status_code, 400)
        message = reply.json()["error"]["message"]
        self.assertIn("gemini_api", message)
        self.assertIn("签到", message)

    async def test_quota_snapshot_lists_the_key_account_without_fake_percentages(self):
        reply = await self.client.get("/ui/quota")
        rows = reply.json()["accounts"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider"], "gemini_api")
        self.assertEqual(row["driver"], "gemini")
        self.assertEqual(row["model_count"], 2)
        self.assertEqual(row["models"], [], "不能编造配额行")
        self.assertNotIn("AIza", reply.text)

    async def test_state_row_says_api_key_instead_of_pretending_it_can_refresh(self):
        reply = await self.rpc("account.list", {"provider": "gemini_api"})
        row = reply.json()["value"]["accounts"][0]
        self.assertIsNone(row["refreshable"], "API Key 没有续期这回事")
        self.assertEqual(row["authStatus"], "api_key")
        self.assertEqual(row["expiresAt"], 0)

    async def test_refresh_pulls_the_official_catalog_and_filters_non_chat_models(self):
        reply = await self.rpc("account.refresh", {"accountId": "gem-key-1"})
        self.assertEqual(reply.status_code, 200, reply.text)
        # 刷新 = 读目录 + 逐个实测。两条都要真的发生过，否则「刷新」只是
        # 把目录原样抄一遍，用户照样会选到 429/404 的模型。
        self.assertIn("/v1beta/models", self.paths)
        self.assertTrue(any(path.endswith(":generateContent") for path in self.paths),
                        self.paths)
        stored = Settings.model_validate_json(self.path.read_text(encoding="utf-8-sig"))
        models = stored.accounts[0].models
        self.assertEqual(models, {"gemini_api/gemini-3.8-flash": "gemini-3.8-flash",
                                  "gemini_api/gemini-3.5-flash": "gemini-3.5-flash"})
        self.assertNotIn("gemini_api/text-embedding-004", models)
        # 目录里存在但实测用不了的，不能列出去：这是用户踩到的那个坑。
        self.assertNotIn("gemini_api/gemini-2.5-flash", models)
        self.assertNotIn("gemini_api/gemini-2.5-pro", models)
        self.assertNotIn("gemini_api/gemini-3.1-flash-image", models)

    async def test_refresh_with_a_revoked_key_disables_and_says_so(self):
        self.status = 400
        self.payload = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                  "message": "API key not valid"}}
        reply = await self.rpc("account.refresh", {"accountId": "gem-key-1"})
        self.assertEqual(reply.status_code, 400)
        self.assertIn("失效", reply.json()["error"]["message"])
        stored = Settings.model_validate_json(self.path.read_text(encoding="utf-8-sig"))
        self.assertEqual(stored.accounts[0].status, "disabled")

    async def test_probe_uses_a_flash_model_not_the_pro_model(self):
        self.payload = self.reply("OK")
        reply = await self.rpc("account.test", {"accountId": "gem-key-1"})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(reply.json()["value"]["ok"])
        self.assertEqual(self.paths[-1], "/v1beta/models/gemini-3.8-flash:generateContent")
        self.assertEqual(self.bodies[-1]["generationConfig"]["maxOutputTokens"], 8)

    async def test_import_window_accepts_the_key_channel_and_persists_driver(self):
        window = await self.rpc("account.create", {"provider": "gemini_api"})
        self.assertEqual(window.status_code, 200, window.text)
        request_id = window.json()["value"]["accountId"]
        self.assertIn("/connect?request=", window.json()["value"]["loginUrl"])
        # 新账号的目录查询也走实际 HTTP 客户端：用 MockTransport 隔离，不触碰
        # 真实 Google 端点或在线密钥。
        real_client = httpx.AsyncClient
        with patch("httpx.AsyncClient", side_effect=lambda **kw: real_client(
                **{**kw, "transport": httpx.MockTransport(self.google)})):
            saved = await self.client.post("/ui/import", json={
                "request_id": request_id,
                "account": {"id": "gem-key-2", "nickname": "second",
                            "models": {}, "token": "AIzaSy-second", "endpoint": "",
                            "max_concurrency": 1, "status": "active"},
            }, headers={"Origin": "http://gateway"})
        self.assertEqual(saved.status_code, 200, saved.text)
        stored = Settings.model_validate_json(self.path.read_text(encoding="utf-8-sig"))
        entry = next(a for a in stored.accounts if a.id == "gem-key-2")
        self.assertEqual(entry.driver, "gemini")
        self.assertEqual(entry.channel, "gemini_api")
        self.assertIsNone(entry.endpoint, "空串不能存成 endpoint")
        self.assertEqual(set(entry.models), {"gemini_api/gemini-3.8-flash", "gemini_api/gemini-3.5-flash"})
        self.assertNotIn("gemini_api/text-embedding-004", entry.models)
        self.assertNotIn("AIzaSy-second", saved.text)

    async def test_import_window_works_without_a_dashboard_session(self):
        """`window.open` 出来的标签页不共享桌面壳的会话 Cookie。

        之前这个窗口一进来就被 401 挡住，页面上写着「添加窗口不可用」，
        用户看到的却是「请先连接本机网关」——他刚从控制台点的按钮。
        这次性 request_id 本身就是这把钥匙的凭据：能读元信息、能保存这一把，
        但拿同一个 id 去动账号表仍然不行。
        """
        window = await self.rpc("account.create", {"provider": "gemini_api"})
        request_id = window.json()["value"]["accountId"]
        anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url="http://gateway")
        try:
            meta = await anonymous.get(f"/ui/import/{request_id}")
            self.assertEqual(meta.status_code, 200, meta.text)
            self.assertEqual(meta.json()["provider"], "gemini_api")
            # 同一个 id 换到需要会话的接口上，仍然必须是 401。
            state = await anonymous.get("/ui/state")
            self.assertEqual(state.status_code, 401)
            # 不存在的 id 也不能当成通行证。
            bogus = await anonymous.get("/ui/import/not-a-real-request")
            self.assertEqual(bogus.status_code, 401)

            real_client = httpx.AsyncClient
            with patch("httpx.AsyncClient", side_effect=lambda **kw: real_client(
                    **{**kw, "transport": httpx.MockTransport(self.google)})):
                saved = await anonymous.post("/ui/import", json={
                    "request_id": request_id,
                    "account": {"id": "gem-key-anon", "token": "AIzaSy-anon",
                                "models": {}, "max_concurrency": 1, "status": "active"},
                }, headers={"Origin": "http://gateway"})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["models"], 2, "保存后要告诉用户识别到几个模型")
        finally:
            await anonymous.aclose()

    async def test_daily_quota_exhaustion_marks_models_instead_of_dropping_them(self):
        """今天用完额度的模型要标出来，不能当成「不存在」丢掉。

        官方对两种 429 都回 `RESOURCE_EXHAUSTED`：每分钟超限等几十秒，每天
        超限要等十几小时（免费档每模型每天 20 次）。分不清就会把明天还能用的
        模型永久删掉。
        """
        self.daily_exhausted = {"gemini-3.5-flash"}
        reply = await self.rpc("account.refresh", {"accountId": "gem-key-1"})
        self.assertEqual(reply.status_code, 200, reply.text)
        stored = Settings.model_validate_json(self.path.read_text(encoding="utf-8-sig"))
        account = stored.accounts[0]
        self.assertIn("gemini_api/gemini-3.5-flash", account.models)
        self.assertIn("gemini_api/gemini-3.5-flash", account.disabled_models)
        # 真的可用的那个不能被误标。
        self.assertIn("gemini_api/gemini-3.8-flash", account.models)
        self.assertNotIn("gemini_api/gemini-3.8-flash", account.disabled_models)

    def test_per_minute_429_is_not_treated_as_daily_exhaustion(self):
        """每分钟超限只是「刚才打太快」，不能把模型标成今日报废。"""
        import json as _json
        from gemini_driver import quota_exhaustion
        per_minute = _json.dumps({"error": {"code": 429, "details": [
            {"violations": [{"description": "per minute limit exceeded",
                             "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                             "quotaValue": "10"}]}]}}).encode()
        self.assertIsNone(quota_exhaustion(per_minute))
        per_day = _json.dumps({"error": {"code": 429, "details": [
            {"violations": [{"description": "requests per day limit exceeded",
                             "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                             "quotaValue": "20"}]},
            {"retryDelay": "61941s"}]}}).encode()
        info = quota_exhaustion(per_day)
        self.assertIsNotNone(info)
        self.assertTrue(info["per_day"])
        self.assertGreater(info["retry_after"], 60000)

    async def test_import_post_from_another_site_is_refused(self):
        """request_id 不能变成跨站改号池的通行证。"""
        window = await self.rpc("account.create", {"provider": "gemini_api"})
        request_id = window.json()["value"]["accountId"]
        reply = await self.client.post("/ui/import", json={
            "request_id": request_id,
            "account": {"id": "gem-key-csrf", "token": "AIzaSy-csrf",
                        "models": {}, "max_concurrency": 1, "status": "active"},
        }, headers={"Origin": "http://evil.example"})
        self.assertEqual(reply.status_code, 403, reply.text)

    def test_candidates_skip_non_chat_families_and_put_likely_ones_first(self):
        """目录里能 `generateContent` 不等于能对话，更不等于免费档用得起。

        `_chat_candidates` 要同时做两件事：剔掉只出图像/音频/研究的模型族，
        并把最可能是主力对话模型的排到前面——探测有预算，先测谁很关键。
        """
        from gemini_driver import _chat_candidates
        catalog = {
            "gemini-2.5-flash-preview-tts": "",     # 只出音频
            "gemini-3.1-flash-image": "",           # 只出图片
            "lyria-3": "",                          # 音乐
            "deep-research-pro-preview-12-2025": "",  # 研究任务
            "gemini-2.5-pro": "",
            "gemini-pro-latest": "",
            "gemini-3.8-flash": "",
            "gemini-3.1-flash-lite": "",
        }
        names = _chat_candidates(catalog)
        for skipped in ("gemini-2.5-flash-preview-tts", "gemini-3.1-flash-image", "lyria-3",
                        "deep-research-pro-preview-12-2025"):
            self.assertNotIn(skipped, names)
        # lite 先测（免费额度最宽），pro 最后（基本 429）。
        self.assertLess(names.index("gemini-3.1-flash-lite"), names.index("gemini-3.8-flash"))
        self.assertLess(names.index("gemini-3.8-flash"), names.index("gemini-2.5-pro"))
        self.assertEqual(names[-1], "gemini-pro-latest")

    async def test_refresh_drops_models_the_key_cannot_actually_call(self):
        """目录里有、实测 404/429/400 的模型不能出现在账号里。

        真机证据：某把免费 Key 的目录有 47 个 `generateContent` 模型，实际能用
        的只有 7 到 9 个；其余是 `no longer available to new users`（404）、
        免费额度不足（429）或只要图像输出（400）。把它们列给用户，等于让他在
        客户端选一个必然失败的模型。
        """
        reply = await self.rpc("account.refresh", {"accountId": "gem-key-1"})
        self.assertEqual(reply.status_code, 200, reply.text)
        stored = Settings.model_validate_json(self.path.read_text(encoding="utf-8-sig"))
        models = set(stored.accounts[0].models)
        self.assertEqual(models, {"gemini_api/gemini-3.8-flash", "gemini_api/gemini-3.5-flash"})
        # 三个不可用的代表：下线、额度、非文本输出。
        self.assertNotIn("gemini_api/gemini-2.5-flash", models)
        self.assertNotIn("gemini_api/gemini-2.5-pro", models)
        self.assertNotIn("gemini_api/gemini-3.1-flash-image", models)

    async def test_import_refuses_cookies_on_the_key_channel(self):
        window = await self.rpc("account.create", {"provider": "gemini_api"})
        request_id = window.json()["value"]["accountId"]
        saved = await self.client.post("/ui/import", json={
            "request_id": request_id,
            "account": {"id": "gem-key-3", "models": {"gemini_api/x": "gemini-3.8-flash"},
                        "cookie": "SID=abc", "status": "active"},
        }, headers={"Origin": "http://gateway"})
        self.assertEqual(saved.status_code, 400)
        self.assertIn("Cookie", saved.json()["error"]["message"])

    async def test_disabled_models_stay_out_of_the_published_model_list(self):
        await self.rpc("model.setAllDisabled", {"provider": "gemini_api", "disabled": True})
        listed = (await self.client.get("/v1/models",
                    headers={"Authorization": "Bearer gemini-test-key"})).json()["data"]
        # 只看本机这一条渠道的命名空间：真机上插件网关可能在跑，会额外列出 12 家
        # 的模型，那不是这条测试要断言的东西。
        self.assertEqual([row["id"] for row in listed
                          if row["id"].startswith("gemini_api/")], [])
        response = await self.chat({"model": "gemini_api/gemini-3.8-flash",
                                    "messages": [{"role": "user", "content": "Hi"}]})
        # 本机渠道的模型被停用后不能退化成「转发给插件」——插件那边没有这条渠道。
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.bodies, [])