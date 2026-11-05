import base64
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import httpx

from account_manager import AccountManager
from config import Account, Settings
from main import app
from upstream_client import UpstreamClient

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = patch('image_gateway.IMAGE_DIR',Path(self.temp.name))
        self.store.start()
        self.calls = []
        self.mode = 'ok'
        self.started = anyio.Event()
        self.pool = AccountManager(Settings(accounts=[Account(id='fixture',driver='antigravity',
            channel='antigravity',credential_id='ag-'+'a'*24,
            models={'image-model':'gemini-fixture-image','chat-model':'gemini-fixture'})],
            stream_timeout_seconds=.2))
        self.upstream = UpstreamClient(lambda account:httpx.MockTransport(self.response))
        async def credential(*args, **kwargs):
            return {'project_id':'fixture','access_token':'fixture-secret'}
        self.upstream.antigravity = SimpleNamespace(credential=credential,
            resolve_proxy=lambda value:None,user_agent='fixture')
        app.state.pool,app.state.upstream,app.state.api_key = self.pool,self.upstream,b'fixture'
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://host:9000',
            headers={'Authorization':'Bearer fixture'})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.upstream.antigravity = None
        await self.upstream.aclose()
        self.store.stop()
        self.temp.cleanup()

    async def response(self, request):
        self.started.set()
        self.calls.append(json.loads(request.content))
        self.assertEqual(sum(s.in_flight for s in self.pool.states),1)
        if self.mode == 'timeout':
            await anyio.sleep_forever()
        if self.mode == 'bad':
            return httpx.Response(400,json={'error':{'status':'INVALID_ARGUMENT','message':'private secret'}})
        if self.mode == 'limit':
            return httpx.Response(429,headers={'Retry-After':'2'})
        parts = [{'inlineData':{'mimeType':'image/png','data':base64.b64encode(PNG).decode()}}]
        if self.mode == 'text':
            parts = [{'text':'private text must not be echoed'}]
        if self.mode == 'invalid':
            parts[0]['inlineData']['data'] = 'not base64'
        return httpx.Response(200,json={'response':{'candidates':[{'content':{'parts':parts}}]}})

    async def post(self, **extra):
        return await self.client.post('/v1/images/generations',json={'prompt':'A test icon',**extra})

    async def test_base64_schema_mapping_and_no_disk_write(self):
        reply = await self.post(response_format='b64_json',size='1536x1024')
        self.assertEqual(reply.status_code,200,reply.text)
        self.assertEqual(base64.b64decode(reply.json()['data'][0]['b64_json']),PNG)
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])
        self.assertEqual(self.calls[0]['request']['generationConfig'],
            {'responseModalities':['TEXT','IMAGE'],'imageConfig':{'aspectRatio':'3:2'}})
        self.assertEqual(self.pool.states[0].in_flight,0)

    async def test_signed_url_uses_request_host_and_can_be_opened(self):
        reply = await self.post()
        self.assertEqual(reply.status_code,200,reply.text)
        url = reply.json()['data'][0]['url']
        self.assertTrue(url.startswith('http://host:9000/v1/images/files/'))
        self.assertNotIn('fixture',url)
        image = await self.client.get(url,headers={'Authorization':''})
        self.assertEqual(image.content,PNG)
        self.assertEqual(image.headers['content-type'],'image/png')
        self.assertEqual((await self.client.get(url.split('?')[0])).status_code,403)
        self.assertEqual((await self.client.get(url.replace('signature=','signature=x'))).status_code,403)

    async def test_caller_cannot_select_file_path_or_unsupported_model(self):
        self.assertEqual((await self.post(output_path='outside.png')).status_code,400)
        self.assertEqual((await self.post(model='chat-model')).status_code,404)
        self.assertEqual((await self.post(n=2)).status_code,400)
        self.assertEqual(self.calls,[])

    async def test_bad_parameter_or_missing_image_does_not_cool(self):
        for mode in ['bad','text','invalid']:
            self.mode = mode
            reply = await self.post()
            self.assertEqual(reply.status_code,400,reply.text)
            self.assertNotIn('private',reply.text)
            self.assertEqual(self.pool.states[0].fail_count,0)
            self.assertEqual(self.pool.states[0].in_flight,0)

    async def test_timeout_releases_lease_and_is_transient(self):
        self.mode = 'timeout'
        self.pool.settings.stream_timeout_seconds = .01
        reply = await self.post()
        self.assertEqual(reply.status_code,504,reply.text)
        self.assertEqual(self.pool.states[0].in_flight,0)
        self.assertEqual(self.pool.states[0].last_failure_reason,'completion_timeout')

    async def test_limit_is_not_silently_ignored(self):
        self.mode = 'limit'
        reply = await self.post()
        self.assertEqual(reply.status_code,503)
        self.assertEqual(self.pool.states[0].last_failure_reason,'antigravity_http_429')
        self.assertEqual(self.pool.states[0].in_flight,0)

    async def test_auth_and_disabled_model(self):
        reply = await self.client.post('/v1/images/generations',json={'prompt':'test'},
            headers={'Authorization':'Bearer invalid'})
        self.assertEqual(reply.status_code,401)
        self.pool.states[0].account.disabled_models = ['image-model']
        self.assertEqual((await self.post()).status_code,404)
        self.assertEqual(self.calls,[])

    async def test_cancellation_releases_account_without_penalty(self):
        self.mode = 'timeout'
        task = asyncio.create_task(self.post())
        await self.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.pool.states[0].in_flight,0)
        self.assertEqual(self.pool.states[0].fail_count,0)

    async def test_paused_dispatch_returns_protection_error_without_network(self):
        from protection import ProtectionBlocked
        async def before_send():
            raise ProtectionBlocked()
        self.upstream.protection = SimpleNamespace(before_send=before_send)
        reply = await self.post()
        self.assertEqual(reply.status_code,503,reply.text)
        self.assertEqual(reply.json()['error']['code'],'dispatch_paused')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.pool.states[0].in_flight,0)

    async def test_same_alias_on_non_native_account_is_excluded(self):
        other = AccountManager(Settings(accounts=[Account(id='other',
            endpoint='http://127.0.0.1:9001/chat',models={'image-model':'text-only'})]))
        self.pool.states.insert(0,other.states[0])
        reply = await self.post(response_format='b64_json')
        self.assertEqual(reply.status_code,200,reply.text)
        self.assertEqual(self.calls[0]['model'],'gemini-fixture-image')
        self.assertEqual(self.pool.states[0].fail_count,0)
