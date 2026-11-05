"""Exercise slow native requests through the real route, pool, and SSE parser."""
import json
import unittest
from types import SimpleNamespace

import anyio
import httpx

from account_manager import AccountManager
from config import Account, Settings
from main import app
from upstream_client import UpstreamClient


class DelayedBody(httpx.AsyncByteStream):
    def __init__(self, payload, delay):
        self.payload, self.delay, self.closed = payload, delay, False

    async def __aiter__(self):
        await anyio.sleep(self.delay)
        yield self.payload

    async def aclose(self):
        self.closed = True


class TimeoutDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = AccountManager(Settings(accounts=[Account(
            id='test-native', channel='antigravity', driver='antigravity',
            credential_id='ag-'+'a'*24, models={'test-model':'native-model'})],
            first_output_timeout_seconds=.02, stream_timeout_seconds=.3))
        self.bodies, self.requests = [], []
        self.upstream = UpstreamClient(lambda account: httpx.MockTransport(self.handler))
        async def credential(*args, **kwargs):
            return {'access_token':'fixture', 'project_id':'fixture'}
        self.upstream.antigravity = SimpleNamespace(
            resolve_proxy=lambda value: None, credential=credential, user_agent='fixture')
        app.state.pool, app.state.upstream, app.state.api_key = self.pool, self.upstream, b'fixture'
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url='http://test', headers={'Authorization':'Bearer fixture'})
        self.payload = {'model':'test-model', 'messages':[
            {'role':'user','content':'Load an image skill'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'old','type':'function',
                'function':{'name':'load_skill','arguments':'{"name":"imagegen"}'}}]},
            {'role':'tool','tool_call_id':'old','content':'Synthetic skill result.'}]}
        self.parts = [{'text':'OK'}]

    async def handler(self, request):
        self.requests.append(request)
        value = {'response':{'candidates':[{'index':0,'content':{
            'role':'model','parts':self.parts},'finishReason':'STOP'}]}}
        streaming = 'streamGenerateContent' in request.url.path
        data = json.dumps(value).encode()
        if streaming:
            data = b'data: '+data+b'\n\n'
        body = DelayedBody(data, .06)
        self.bodies.append(body)
        return httpx.Response(200, headers={'Content-Type':
            'text/event-stream' if streaming else 'application/json'}, stream=body)

    async def asyncTearDown(self):
        await self.client.aclose()
        # The fake authentication object owns no sockets.
        self.upstream.antigravity = None
        await self.upstream.aclose()

    async def test_first_output_timeout_reports_504_not_an_empty_pool(self):
        reply = await self.client.post('/v1/chat/completions', json={**self.payload,'stream':True})
        self.assertEqual(reply.status_code, 504, reply.text)
        self.assertEqual(reply.json()['error']['code'], 'upstream_timeout')
        self.assertTrue(self.bodies[0].closed)
        self.assertEqual(self.pool.states[0].in_flight, 0)

    async def test_nonstream_uses_completion_budget_not_first_output_budget(self):
        reply = await self.client.post('/v1/chat/completions', json=self.payload)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()['choices'][0]['message']['content'], 'OK')
        self.assertEqual(self.pool.states[0].fail_count, 0)
        # Outer route deadlines bound native reads; a second 60s timer must not preempt them.
        self.assertIsNone(self.requests[0].extensions['timeout']['read'])

    async def test_unsupported_image_output_does_not_cool_account(self):
        self.pool.settings.first_output_timeout_seconds = .2
        self.parts = [{'inlineData':{'mimeType':'image/png','data':'fixture'}}]
        reply = await self.client.post('/v1/chat/completions', json={**self.payload,'stream':True})
        self.assertEqual(reply.status_code, 400, reply.text)
        self.assertEqual(self.pool.states[0].fail_count, 0)
        self.assertEqual(self.pool.states[0].status, 'active')

    async def test_unsupported_image_after_text_is_sse_error_without_cooling(self):
        self.pool.settings.first_output_timeout_seconds = .2
        async def late_image(request):
            frames = []
            for parts, finish in (([{'text':'Starting'}], None),
                    ([{'inlineData':{'mimeType':'image/png','data':'fixture'}}], 'STOP')):
                candidate = {'index':0,'content':{'role':'model','parts':parts}}
                if finish:
                    candidate['finishReason'] = finish
                frames.append('data: '+json.dumps({'response':{'candidates':[candidate]}})+'\n\n')
            return httpx.Response(200,headers={'Content-Type':'text/event-stream'},
                stream=DelayedBody(''.join(frames).encode(),.001))
        self.upstream.transport_factory = lambda account:httpx.MockTransport(late_image)
        reply = await self.client.post('/v1/chat/completions',json={**self.payload,'stream':True})
        self.assertEqual(reply.status_code,200)
        self.assertIn('unsupported_output',reply.text)
        self.assertNotIn('[DONE]',reply.text)
        self.assertEqual(self.pool.states[0].fail_count,0)
        self.assertEqual(self.pool.states[0].in_flight,0)

    async def test_completion_timeout_has_its_own_reason_and_short_backoff(self):
        self.pool.settings.stream_timeout_seconds = .02
        reply = await self.client.post('/v1/chat/completions',json=self.payload)
        self.assertEqual(reply.status_code,504,reply.text)
        self.assertEqual(self.pool.states[0].last_failure_reason,'completion_timeout')
        snapshot = (await self.pool.snapshot())[0]
        self.assertLessEqual(snapshot['cooldown_remaining_seconds'],20)
