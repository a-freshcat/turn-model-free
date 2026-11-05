"""离线集成测试：真实经过 ASGI 路由、号池与 HTTP 驱动，只有外网被替换。"""

import asyncio
import copy
import json
import time
import unittest
from contextlib import AsyncExitStack
from types import SimpleNamespace

import anyio
import httpx
from pydantic import ValidationError

from account_manager import AccountManager, PoolUnavailable, UpstreamFailure
from config import Account, Settings
from main import ChatRequest, ChatResponse, app
from tencent_guard import scrub_messages
from upstream_client import DONE, UpstreamClient, _is_gate_body, encode_event


META = {"id": "chatcmpl-fixture", "created": 1234, "model": "internal"}
GATE = {"error": {"code": 11128, "message": "Unapproved channel"}}


def completion(text="ok"):
    return {**META, "object": "chat.completion", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}


def chunk(delta, finish=None):
    return {**META, "object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": delta, "finish_reason": finish}]}


class Frames(httpx.AsyncByteStream):
    def __init__(self, frames, *, block=False):
        self.items, self.block, self.closed = frames, block, False

    async def __aiter__(self):
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item
        if self.block:
            await anyio.sleep_forever()

    async def aclose(self):
        self.closed = True


def sse(frames, *, block=False):
    stream = Frames(frames, block=block)
    return httpx.Response(200, headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=stream)


def config(count=2, **options):
    return Settings(accounts=[
        Account(id=f"a{i}", endpoint="http://127.0.0.1:9999/v1/chat/completions",
                models={"chat": "internal"}, token=f"fixture-{i}") for i in range(count)
    ], **options)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.responses = []
        self.clients = []
        self.payload = {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        if hasattr(self, "upstream"):
            await self.upstream.aclose()

    def gateway(self, handler, settings=None):
        self.pool = AccountManager(settings or config())

        def factory(account):
            async def handle(request):
                self.calls.append((account.id, json.loads(request.content), dict(request.headers)))
                response = await handler(account, request)
                self.responses.append(response)
                return response
            return httpx.MockTransport(handle)

        self.upstream = UpstreamClient(factory)
        app.state.pool, app.state.upstream, app.state.api_key = self.pool, self.upstream, b"test-key"
        async def plugin_models():
            return []
        app.state.web = SimpleNamespace(plugin=SimpleNamespace(probe=plugin_models))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway",
            headers={"Authorization": "Bearer test-key"},
        )
        self.clients.append(client)
        return client

    async def test_http_gate_failover(self):
        async def handler(account, request):
            return httpx.Response(400, json=GATE) if account.id == "a0" else httpx.Response(200, json=completion())
        client = self.gateway(handler)
        response = await client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "chat")
        self.assertEqual([c[0] for c in self.calls], ["a0", "a1"])
        self.assertEqual(self.pool.states[0].status, "cooldown")
        self.assertEqual(self.pool.states[0].fail_count, 1)
        self.assertTrue(all(s.in_flight == 0 for s in self.pool.states))
        self.assertTrue(all(r.is_closed for r in self.responses))

    async def test_json_200_gate_and_retry_after(self):
        async def handler(account, request):
            return httpx.Response(200, json=GATE, headers={"Retry-After": "1200"})
        client = self.gateway(handler, config(1))
        response = await client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertGreaterEqual(int(response.headers["retry-after"]), 1199)
        self.assertNotIn("Unapproved", response.text)

    async def test_nonstream_round_robin_and_forward_fields(self):
        async def handler(account, request):
            return httpx.Response(200, json=completion())
        client = self.gateway(handler)
        self.payload.update(tools=[{"type": "function", "function": {"name": "x"}}],
                            temperature=0.2, response_format={"type": "json_object"})
        self.payload["messages"] = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call1", "content": "42"},
        ]
        for _ in range(3):
            self.assertEqual((await client.post("/v1/chat/completions", json=self.payload)).status_code, 200)
        self.assertEqual([c[0] for c in self.calls], ["a0", "a1", "a0"])
        forwarded = self.calls[0][1]
        self.assertEqual(forwarded["messages"], self.payload["messages"])
        self.assertEqual(forwarded["tools"], self.payload["tools"])
        self.assertEqual(forwarded["response_format"], {"type": "json_object"})
        self.assertEqual(forwarded["model"], "internal")
        self.assertEqual(self.calls[0][2]["authorization"], "Bearer fixture-0")

    async def test_sse_role_then_gate_is_not_committed(self):
        async def handler(account, request):
            if account.id == "a0":
                return sse([encode_event(chunk({"role": "assistant"})), encode_event(GATE)])
            return sse([encode_event(chunk({"role": "assistant"})),
                        encode_event(chunk({"content": "yes"})),
                        encode_event(chunk({}, "stop")), DONE])
        client = self.gateway(handler)
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertEqual(response.text.count('"role":"assistant"'), 1)
        self.assertIn('"content":"yes"', response.text)
        self.assertIn('"model":"chat"', response.text)
        self.assertTrue(response.content.endswith(DONE))
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(r.is_closed for r in self.responses))

    async def test_sse_late_failure_does_not_replay(self):
        async def handler(account, request):
            return sse([encode_event(chunk({"content": "partial"})), encode_event(GATE)])
        client = self.gateway(handler)
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertEqual(response.status_code, 200)
        self.assertIn("partial", response.text)
        self.assertIn("upstream_stream_interrupted", response.text)
        self.assertNotIn("[DONE]", response.text)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.pool.states[0].status, "cooldown")
        self.assertEqual(self.pool.states[0].in_flight, 0)

    async def test_sse_tool_usage_and_fragmented_crlf_unicode(self):
        tool = {"tool_calls": [{"index": 0, "id": "x", "type": "function",
                               "function": {"name": "say", "arguments": '{"text":"你好"}'}}]}
        usage = {**META, "object": "chat.completion.chunk", "choices": [], "usage": {"total_tokens": 5}}
        raw = (": ping\r\n\r\ndata: " + json.dumps(chunk(tool), ensure_ascii=False) +
               "\r\n\r\ndata: " + json.dumps(chunk({}, "tool_calls")) +
               "\r\n\r\ndata: " + json.dumps(usage) + "\r\n\r\ndata: [DONE]\r\n\r\n").encode()

        async def handler(account, request):
            return sse([raw[i:i+1] for i in range(len(raw))])
        client = self.gateway(handler)
        response = await client.post("/v1/chat/completions", json={
            **self.payload, "stream": True, "stream_options": {"include_usage": True}})
        events = [json.loads(l[6:]) for l in response.text.splitlines() if l.startswith("data: {")]
        self.assertEqual(events[0]["choices"][0]["delta"], tool)
        self.assertEqual(events[-1]["usage"]["total_tokens"], 5)
        self.assertEqual(events[-1]["choices"], [])
        self.assertTrue(response.content.endswith(DONE))
        self.assertEqual(self.calls[0][1]["stream_options"], {"include_usage": True})

    async def test_sse_multiline_bom_and_cr_only(self):
        value = json.dumps(chunk({"content": "fine"})).replace(', "choices"', ',\n"choices"')
        raw = b"\xef\xbb\xbf" + b"".join(b"data: " + l.encode() + b"\r" for l in value.splitlines())
        raw += b"\r" + encode_event(chunk({}, "stop")) + b"data: [DONE]\r\r"

        async def handler(account, request):
            return sse([raw])
        client = self.gateway(handler)
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertIn("fine", response.text)
        self.assertTrue(response.content.endswith(DONE))

    async def test_missing_done_detected_after_output(self):
        async def handler(account, request):
            return sse([encode_event(chunk({"content": "partial"}))])
        client = self.gateway(handler)
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertIn("upstream_stream_interrupted", response.text)
        self.assertEqual(len(self.calls), 1)

    async def test_done_without_finish_reason_is_late_error(self):
        async def handler(account, request):
            return sse([encode_event(chunk({"content": "partial"})), DONE])
        client = self.gateway(handler, config(1))
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertIn("upstream_stream_interrupted", response.text)
        self.assertNotIn("[DONE]", response.text)

    async def test_sse_eof_before_output_and_http_500_failover(self):
        async def handler(account, request):
            if account.id == "a0":
                return sse([b": ping\n\n"])
            if account.id == "a1":
                return httpx.Response(500, text="error")
            return sse([encode_event(chunk({"content": "ok"})),
                        encode_event(chunk({}, "stop")), DONE])
        client = self.gateway(handler, config(3))
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(response.content.endswith(DONE))

    async def test_transport_timeout_and_invalid_shape_failover(self):
        async def handler(account, request):
            if account.id == "a0":
                raise httpx.ReadTimeout("fixture")
            if account.id == "a1":
                return httpx.Response(200, json={"choices": "invalid"})
            return httpx.Response(200, json=completion())
        client = self.gateway(handler, config(3))
        response = await client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([s.in_flight for s in self.pool.states], [0, 0, 0])

    async def test_all_unavailable_503(self):
        async def handler(account, request):
            return httpx.Response(403, text="denied")
        client = self.gateway(handler)
        response = await client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.calls), 2)
        self.assertGreaterEqual(int(response.headers["retry-after"]), 599)
        response = await client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.calls), 2)

    async def test_all_disabled_is_503_and_monitor_hides_credentials(self):
        async def handler(account, request):
            raise AssertionError("disabled accounts cannot be used")
        settings = config(1)
        settings.accounts[0].status = "disabled"
        client = self.gateway(handler, settings)
        response = await client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 503)
        response = await client.get("/admin/accounts")
        self.assertEqual(response.json()["accounts"][0]["status"], "disabled")
        self.assertNotIn("fixture-0", response.text)
        self.assertNotIn("endpoint", response.text)
        self.assertEqual((await client.get("/admin/accounts",
                                           headers={"Authorization": ""})).status_code, 401)

    async def test_budget_exhausted_with_healthy_accounts_is_502(self):
        async def handler(account, request):
            return httpx.Response(500)
        client = self.gateway(handler, config(5))
        response = await client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.pool.states[-1].status, "active")

    async def test_first_output_timeout_closes_response(self):
        async def handler(account, request):
            return sse([], block=True)
        client = self.gateway(handler, config(1, first_output_timeout_seconds=0.01))
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(self.responses[0].is_closed)
        self.assertEqual(self.pool.states[0].in_flight, 0)

    async def test_auth_validation_models_and_no_network(self):
        async def handler(account, request):
            raise AssertionError("should not reach upstream")
        client = self.gateway(handler)
        for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}):
            self.assertEqual((await client.post("/v1/chat/completions", json=self.payload, headers=headers)).status_code, 401)
        response = await client.post("/v1/chat/completions", json={**self.payload, "model": "missing"})
        self.assertEqual(response.status_code, 404)
        for payload in ({}, {**self.payload, "stream": "true"},
                        {**self.payload, "messages": []},
                        {**self.payload, "stream_options": {"include_usage": True}}):
            self.assertEqual((await client.post("/v1/chat/completions", json=payload)).status_code, 400)
        response = await client.post("/v1/chat/completions", content='{"model":')
        self.assertEqual(response.status_code, 400)
        response = await client.get("/v1/models")
        self.assertEqual(response.json()["data"][0]["id"], "chat")
        self.assertEqual((await client.get("/healthz")).status_code, 200)
        self.assertEqual(self.calls, [])

    async def test_invalid_numeric_and_unicode_inputs_are_400(self):
        async def handler(account, request):
            raise AssertionError("invalid client JSON must not acquire an account")
        client = self.gateway(handler)
        for value in ("NaN", "Infinity", "1e999"):
            raw = json.dumps(self.payload)[:-1] + ', "temperature":' + value + '}'
            self.assertEqual((await client.post("/v1/chat/completions", content=raw)).status_code, 400)
        raw = '{"model":"chat","messages":[{"role":"user","content":"\\ud800"}]}'
        self.assertEqual((await client.post("/v1/chat/completions", content=raw)).status_code, 400)

    async def test_disconnect_during_stream_releases_lease_without_cooldown(self):
        async def handler(account, request):
            return sse([encode_event(chunk({"content": "first"}))], block=True)
        self.gateway(handler, config(1))
        response = ChatResponse(ChatRequest.model_validate({**self.payload, "stream": True}), self.pool, self.upstream)
        disconnected = anyio.Event()
        sent = []

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body":
                disconnected.set()

        with anyio.fail_after(1):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.assertEqual(self.pool.states[0].in_flight, 0)
        self.assertEqual(self.pool.states[0].status, "active")
        self.assertTrue(self.responses[0].is_closed)

    async def test_disconnect_while_waiting_for_first_frame(self):
        started = anyio.Event()
        async def handler(account, request):
            started.set()
            return sse([], block=True)
        self.gateway(handler, config(1))
        response = ChatResponse(ChatRequest.model_validate({**self.payload, "stream": True}), self.pool, self.upstream)
        async def receive():
            await started.wait()
            await anyio.sleep(0)
            return {"type": "http.disconnect"}
        async def send(message):
            self.fail("disconnect happened before response could start")
        with anyio.fail_after(1):
            await response({"type": "http"}, receive, send)
        self.assertEqual(self.pool.states[0].in_flight, 0)
        self.assertEqual(self.pool.states[0].fail_count, 0)

    async def test_concurrency_wait_and_cooldown_recovery(self):
        pool = AccountManager(config(1, pool_wait_seconds=0.1, cooldown_seconds=0.01))
        first, second = AsyncExitStack(), AsyncExitStack()
        lease = await pool.acquire("chat", set(), first)
        task = asyncio.create_task(pool.acquire("chat", set(), second))
        await anyio.sleep(0.01)
        self.assertFalse(task.done())
        self.assertEqual(pool.states[0].in_flight, 1)
        await first.aclose()
        next_lease = await task
        self.assertEqual(pool.states[0].in_flight, 1)
        await next_lease.fail(UpstreamFailure("fixture"))
        await second.aclose()
        await anyio.sleep(0.02)
        async with AsyncExitStack() as final:
            await pool.acquire("chat", set(), final)
            self.assertEqual(pool.states[0].status, "active")

    async def test_busy_pool_bounded_503_and_disabled_never_selected(self):
        settings = config(2, pool_wait_seconds=0.01)
        settings.accounts[1].status = "disabled"
        pool = AccountManager(settings)
        async with AsyncExitStack() as lifetime:
            await pool.acquire("chat", set(), lifetime)
            async with AsyncExitStack() as pending:
                with self.assertRaises(PoolUnavailable):
                    await pool.acquire("chat", set(), pending)
        self.assertEqual([s.in_flight for s in pool.states], [0, 0])
        self.assertEqual(pool.states[1].status, "disabled")

    async def test_cookie_isolation_and_no_redirects(self):
        async def handler(account, request):
            if "set" not in request.headers.get("Cookie", ""):
                return httpx.Response(200, json=completion(), headers={"Set-Cookie": f"set={account.id}; Path=/"})
            return httpx.Response(302, headers={"Location": "https://untrusted.example/"})
        client = self.gateway(handler)
        for _ in range(2):
            self.assertEqual((await client.post("/v1/chat/completions", json=self.payload)).status_code, 200)
        self.assertNotIn("cookie", self.calls[1][2])
        response = await client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[2][2]["cookie"], "set=a0")
        self.assertEqual(self.calls[3][2]["cookie"], "set=a1")

    async def test_sse_protocol_error_preamble_is_bounded(self):
        async def handler(account, request):
            return sse([encode_event(chunk({"role": "assistant"}))] * 65)
        client = self.gateway(handler, config(1))
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertEqual(response.status_code, 503)
        self.assertTrue(self.responses[0].is_closed)

    async def test_late_transport_error(self):
        async def handler(account, request):
            return sse([encode_event(chunk({"content": "ok"})), httpx.ReadError("broken")])
        client = self.gateway(handler, config(1))
        response = await client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertIn("upstream_stream_interrupted", response.text)
        self.assertNotIn("[DONE]", response.text)


class GuardAndConfigTests(unittest.TestCase):
    def test_structured_gate_does_not_match_normal_content(self):
        self.assertTrue(_is_gate_body('data: {"code": 11128}'))
        self.assertTrue(_is_gate_body('{"error":{"message":"来自未授权渠道"}}'))
        self.assertFalse(_is_gate_body(json.dumps(completion("unapproved channel, 11128"))))
        self.assertFalse(_is_gate_body('{"code":11140,"message":"request illegal"}'))

    def test_clean_prompts_and_tool_fields_unchanged(self):
        original = [
            {"role": "system", "content": "You are a helpful coding assistant.  Keep  spaces."},
            {"role": "user", "content": "DeepSeek Harness"},
            {"role": "tool", "tool_call_id": "x", "content": "DeepSeek Harness"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
        ]
        self.assertEqual(scrub_messages(original, 3), original)

    def test_guard_levels_paths_and_input_immutability(self):
        text = ('You are an AI agent powered by DeepSeek Harness.\n'
                '"C:\\work docs DeepSeek Harness\\my  file.txt" then DeepSeek Harness Web GUI')
        original = [{"role": "system", "content": text, "name": "keep"}]
        frozen = copy.deepcopy(original)
        for level in (1, 2, 3):
            content = scrub_messages(original, level)[0]["content"]
            self.assertIn('"C:\\work docs DeepSeek Harness\\my  file.txt"', content)
            self.assertIn("You are an AI coding agent.", content)
            self.assertIn("the Web GUI", content)
        self.assertEqual(original, frozen)

    def test_level3_fullwidth_and_level2_hint(self):
        self.assertEqual(scrub_messages([{"role": "system", "content": "ＤｅｅｐＳｅｅｋ Ｈａｒｎｅｓｓ"}], 3)[0]["content"], "DSH")
        text = "Tokens prefixed with @ are paths the user explicitly referenced."
        self.assertEqual(scrub_messages([{"role": "system", "content": text}], 1)[0]["content"], text)
        self.assertNotEqual(scrub_messages([{"role": "system", "content": text}], 2)[0]["content"], text)

    def test_header_url_and_pool_configuration(self):
        base = {"id": "x", "models": {"chat": "internal"}, "endpoint": "https://example.test/v1/chat/completions"}
        bad = [
            {**base, "device_headers": {"Authorization": "override"}},
            {**base, "device_headers": {"X-Device": "ok", "x-device": "other"}},
            {**base, "user_agent": "bad\r\nHeader: value"},
            {**base, "endpoint": "http://external.test/v1/chat/completions"},
            {**base, "endpoint": "https://u:secret@example.test/v1/chat/completions"},
            {**base, "token": " secret "},
        ]
        for value in bad:
            with self.assertRaises(ValidationError):
                Account.model_validate(value)
        account = Account.model_validate(base)
        with self.assertRaises(ValidationError):
            Settings(accounts=[account, account])
        with self.assertRaises(ValidationError):
            Settings(accounts=[account], cooldown_seconds=float("nan"))


if __name__ == "__main__":
    unittest.main()
