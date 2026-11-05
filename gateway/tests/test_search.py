import asyncio
import json
import unittest
from types import SimpleNamespace

import anyio
import httpx

from account_manager import AccountManager
from config import Account,Settings
from main import app
from upstream_client import UpstreamClient


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[]
        self.mode='ok'
        self.started=anyio.Event()
        self.pool=AccountManager(Settings(accounts=[Account(id='search-fixture',driver='antigravity',
            channel='antigravity',credential_id='ag-'+'a'*24,
            models={'search-model':'gemini-2.5-flash','image-model':'gemini-fixture-image'})]))
        self.upstream=UpstreamClient(lambda account:httpx.MockTransport(self.response))
        async def credential(*args,**kwargs):
            return {'project_id':'fixture','access_token':'fixture-secret'}
        self.upstream.antigravity=SimpleNamespace(credential=credential,resolve_proxy=lambda p:None,user_agent='fixture')
        app.state.pool,app.state.upstream,app.state.api_key=self.pool,self.upstream,b'fixture'
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
            headers={'Authorization':'Bearer fixture'})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.upstream.antigravity=None
        await self.upstream.aclose()

    async def response(self,request):
        self.started.set()
        self.calls.append(json.loads(request.content))
        self.assertEqual(self.pool.states[0].in_flight,1)
        if self.mode=='wait':
            await anyio.sleep_forever()
        if self.mode=='bad':
            return httpx.Response(400,json={'error':{'status':'INVALID_ARGUMENT','message':'private prompt'}})
        if self.mode=='limit':
            return httpx.Response(429,headers={'Retry-After':'60'})
        meta={'groundingChunks':[{'web':{'uri':'https://example.com/a','title':'Source A'}},
            {'web':{'uri':'javascript:alert(1)','title':'invalid'}},
            {'web':{'uri':'https://example.com/b','title':'Source B'}}],
            'webSearchQueries':['fixture query'],'groundingSupports':[{'segment':{'text':'Supported fact'},
                'groundingChunkIndices':[0]}]}
        if self.mode=='ungrounded':
            meta={}
        return httpx.Response(200,json={'response':{'candidates':[{'content':{
            'parts':[{'text':'Factual summary'}]},'groundingMetadata':meta}]}})

    async def post(self,**extra):
        return await self.client.post('/v1/search',json={'query':'fixture query',**extra})

    async def test_grounding_sources_request_and_lease(self):
        reply=await self.post(maxResults=1)
        self.assertEqual(reply.status_code,200,reply.text)
        result=reply.json()
        self.assertEqual(result['sources'],[{'url':'https://example.com/a','title':'Source A','snippet':'Supported fact'}])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['queries'],['fixture query'])
        self.assertEqual(self.calls[0]['request']['tools'],[{'googleSearch':{}}])
        self.assertEqual(self.pool.states[0].in_flight,0)

    async def test_no_sources_is_failure_without_cooling(self):
        self.mode='ungrounded'
        reply=await self.post()
        self.assertEqual(reply.status_code,502)
        self.assertEqual(reply.json()['error']['code'],'search_ungrounded')
        self.assertEqual(self.pool.states[0].fail_count,0)

    async def test_bad_parameter_does_not_cool_or_echo_private_text(self):
        self.mode='bad'
        reply=await self.post()
        self.assertEqual(reply.status_code,400)
        self.assertNotIn('private prompt',reply.text)
        self.assertEqual(self.pool.states[0].fail_count,0)

    async def test_auth_validation_and_model_switch(self):
        reply=await self.client.post('/v1/search',json={'query':'x'},headers={'Authorization':'Bearer wrong'})
        self.assertEqual(reply.status_code,401)
        self.assertEqual((await self.post(query='  ')).status_code,400)
        self.assertEqual((await self.post(model='image-model')).status_code,404)
        self.assertEqual((await self.post(max_results=21)).status_code,400)
        self.assertEqual(self.calls,[])

    async def test_timeout_and_cancel_release_lease(self):
        self.mode='wait'
        self.pool.settings.stream_timeout_seconds=.01
        reply=await self.post()
        self.assertEqual(reply.status_code,504)
        self.assertEqual(self.pool.states[0].in_flight,0)

    async def test_cancellation_does_not_penalize_account(self):
        self.mode='wait'
        task=asyncio.create_task(self.post())
        await self.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.pool.states[0].in_flight,0)
        self.assertEqual(self.pool.states[0].fail_count,0)

    async def test_real_rate_limit_keeps_account_protection(self):
        self.mode='limit'
        reply=await self.post()
        self.assertEqual(reply.status_code,503)
        self.assertEqual(self.pool.states[0].last_failure_reason,'antigravity_http_429')
