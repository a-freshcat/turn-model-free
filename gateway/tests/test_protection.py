import asyncio
import json
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
from fastapi import FastAPI

from protection import (DispatchProtection, ProtectionBlocked, ProtectionMiddleware,
                        SSEProtectionObserver, observe_error_value)


class ProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'.gateway-protection.json'
        self.guard = DispatchProtection(self.path,concurrency=1,per_minute=2)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_pause_survives_restart_and_needs_explicit_resume(self):
        await self.guard.observe_failure('http_403')
        restarted=DispatchProtection(self.path)
        self.assertTrue(restarted.paused)
        with self.assertRaises(ProtectionBlocked):
            await restarted.before_send()
        await restarted.resume()
        self.assertFalse(DispatchProtection(self.path).paused)

    async def test_corrupt_state_fails_closed(self):
        self.path.write_text('bad state',encoding='utf-8')
        self.assertTrue(DispatchProtection(self.path).paused)

    async def test_native_http_status_prefix_uses_the_same_immediate_stop_policy(self):
        for status in (401,403,429):
            await self.guard.resume()
            await self.guard.observe_failure(f'antigravity_http_{status}')
            state=await self.guard.snapshot()
            self.assertTrue(state['paused'])
            self.assertEqual(state['http_status'],status)

    async def test_local_rate_and_concurrency_limits_do_not_reach_upstream(self):
        await self.guard.before_send()
        await self.guard.before_send()
        with self.assertRaises(ProtectionBlocked) as caught:
            await self.guard.before_send()
        self.assertEqual(caught.exception.status,429)
        token,_=await self.guard.admit()
        with self.assertRaises(ProtectionBlocked):
            await self.guard.admit()
        await self.guard.release(token)
        self.assertFalse(self.guard.paused)

    async def test_repeated_protocol_errors_trip_without_recording_body(self):
        for _ in range(3):
            await self.guard.observe_failure('invalid_completion_shape')
        state=await self.guard.snapshot()
        self.assertTrue(state['paused'])
        self.assertEqual(state['reason'],'repeated_failures')
        self.assertNotIn('token',json.dumps(state))

    async def test_structured_sse_error_is_seen_even_when_split_and_content_is_ignored(self):
        observer=SSEProtectionObserver(self.guard)
        await observer.feed(b'data: {"choices":[{"delta":{"content":"code 11140"}}]}\n\n')
        self.assertFalse(self.guard.paused)
        await observer.feed(b'data: {"error":{"code":111')
        await observer.feed(b'40,"message":"secret-not-recorded"}}\r\n\r\n')
        state=await self.guard.snapshot()
        self.assertTrue(state['paused'])
        self.assertNotIn('secret-not-recorded',json.dumps(state))
        await self.guard.resume()
        await observe_error_value(self.guard,b'{"error":{"code":[]}}')
        self.assertFalse(self.guard.paused)

    async def test_manual_pause_cancels_active_asgi_stream_and_closes_generator(self):
        from starlette.responses import StreamingResponse
        app=FastAPI()
        pool=object()
        app.state.pool=pool
        app.state.web=SimpleNamespace(pool=pool,protection=self.guard)
        app.add_middleware(ProtectionMiddleware)
        began,closed=anyio.Event(),anyio.Event()

        @app.post('/v1/chat/completions')
        async def chat():
            async def frames():
                try:
                    yield b'data: {"choices":[]}\n\n'
                    began.set()
                    await anyio.sleep_forever()
                finally:
                    closed.set()
            return StreamingResponse(frames(),media_type='text/event-stream')

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            pending=asyncio.create_task(client.post('/v1/chat/completions',json={}))
            with anyio.fail_after(3):
                await began.wait()
                await self.guard.pause()
                reply=await pending
                await closed.wait()
            self.assertIn('dispatch_paused',reply.text)
            self.assertNotIn('[DONE]',reply.text)
            self.assertEqual((await self.guard.snapshot())['active_requests'],0)
            self.assertEqual((await client.post('/v1/chat/completions',json={})).status_code,503)


class ProtectionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_response_cleanup_is_shielded_during_pause(self):
        from plugin_gateway import _OwnedResponse
        closed=[]
        class Resource:
            def __init__(self,name):self.name=name
            async def aclose(self):
                await anyio.lowlevel.checkpoint()
                closed.append(self.name)
        with anyio.CancelScope() as scope:
            scope.cancel()
            await _OwnedResponse(Resource('response'),Resource('client')).aclose()
        self.assertEqual(closed,['response','client'])

    async def test_plugin_connection_is_closed_if_cancelled_before_headers(self):
        from unittest.mock import patch
        from plugin_gateway import PluginGateway
        began,closed=anyio.Event(),anyio.Event()
        class Client:
            def build_request(self,*args,**kwargs):return object()
            async def send(self,*args,**kwargs):
                began.set()
                await anyio.sleep_forever()
            async def aclose(self):
                await anyio.lowlevel.checkpoint()
                closed.set()
        with patch('plugin_gateway.httpx.AsyncClient',return_value=Client()):
            async with anyio.create_task_group() as tasks:
                async def call():
                    await PluginGateway(key='test-only').chat({},stream=True)
                tasks.start_soon(call)
                await began.wait()
                tasks.cancel_scope.cancel()
        self.assertTrue(closed.is_set())

    async def test_upstream_rejection_stops_before_switching_accounts(self):
        from account_manager import AccountManager
        from config import Settings
        from main import app
        from upstream_client import UpstreamClient
        from web_api import WebManager
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'accounts.json'
            raw={'accounts':[{'id':f'a{i}','models':{'chat':'upstream'},
                 'endpoint':'https://example.com/v1/chat/completions'} for i in range(2)]}
            path.write_text(json.dumps(raw),encoding='utf-8')
            pool=AccountManager(Settings.model_validate(raw))
            calls=[]
            def transport(account):
                def reply(request):
                    calls.append(account.id)
                    return httpx.Response(403,json={'code':11140,'message':'secret-error-body'})
                return httpx.MockTransport(reply)
            upstream=UpstreamClient(transport)
            web=WebManager(pool,path,upstream)
            app.state.pool,app.state.upstream,app.state.web=pool,upstream,web
            app.state.api_key=b'test-protection-key'
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                  headers={'Authorization':'Bearer test-protection-key'}) as client:
                reply=await client.post('/v1/chat/completions',json={
                    'model':'chat','messages':[{'role':'user','content':'hello'}]})
                self.assertEqual(reply.status_code,503)
                self.assertEqual(calls,['a0'])
                self.assertNotIn('secret-error-body',reply.text)
                self.assertTrue(web.protection.paused)
                self.assertTrue(all(s.in_flight==0 for s in pool.states))
                state=(await pool.snapshot())[0]
                self.assertEqual(state['last_failure_reason'],'http_403')
                self.assertGreater(state['last_failure_at'],0)
                self.assertEqual((await client.get('/ui/protection',headers={'Authorization':''})).status_code,401)
                await client.post('/ui/session',json={'key':'test-protection-key'},headers={'Origin':'http://test'})
                self.assertEqual((await client.post('/ui/protection',json={
                    'method':'protection.resume','payload':{'acknowledge':True}},
                    headers={'Origin':'https://wrong.example'})).status_code,403)
                self.assertEqual((await client.post('/ui/protection',json={
                    'method':'protection.resume','payload':{}},headers={'Origin':'http://test'})).status_code,400)
                diag=(await client.get('/ui/protection')).json()
                self.assertIn('AI',diag['help'])
                self.assertNotIn('secret-error-body',json.dumps(diag))
            await upstream.aclose()
