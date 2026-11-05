import json
import tempfile
import unittest
from pathlib import Path

import httpx
from account_manager import AccountManager
from config import Settings
from main import app
from upstream_client import UpstreamClient
from web_api import WebManager


class NativeBadRequestTests(unittest.IsolatedAsyncioTestCase):
    def test_known_restriction_codes_are_not_downgraded_to_input_errors(self):
        from antigravity_driver import native_http_error
        from account_manager import UpstreamFailure
        for code,reason in ((11128,'channel_11128'),(11140,'account_restricted')):
            error=native_http_error(400,json.dumps({'code':code}).encode(),90)
            self.assertIsInstance(error,UpstreamFailure)
            self.assertEqual(error.reason,reason)
            self.assertEqual(error.retry_after,90)

    async def test_sdk_dropping_signature_metadata_still_continues_native_tool_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'accounts.json'
            raw={'accounts':[{'id':'ag-test','channel':'antigravity','driver':'antigravity',
                'credential_id':'ag-'+'b'*24,'models':{'antigravity/gemini-test':'gemini-test'}}]}
            path.write_text(json.dumps(raw),encoding='utf-8')
            pool=AccountManager(Settings.model_validate(raw))
            requests=[]
            def response(request):
                wire=json.loads(request.content)['request']
                requests.append(wire)
                if len(requests)==1:
                    parts=[{'functionCall':{'id':'native-call','name':'load_skill','args':{'name':'imagegen'}},
                            'thoughtSignature':'real-upstream-signature'}]
                else:
                    call=wire['contents'][1]['parts'][0]
                    self.assertEqual(call['thoughtSignature'],'real-upstream-signature')
                    self.assertIn('functionResponse',wire['contents'][2]['parts'][0])
                    parts=[{'text':'OK'}]
                return httpx.Response(200,json={'response':{'candidates':[{'index':0,
                    'content':{'role':'model','parts':parts},'finishReason':'STOP'}]}})
            upstream=UpstreamClient(lambda account:httpx.MockTransport(response))
            manager=WebManager(pool,path,upstream)
            async def credential(*args,**kwargs):return {'project_id':'fixture','access_token':'fixture-secret'}
            manager.ag.credential=credential
            app.state.pool,app.state.upstream,app.state.web=pool,upstream,manager
            app.state.api_key=b'tool-history-test'
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                        headers={'Authorization':'Bearer tool-history-test'}) as client:
                    messages=[{'role':'user','content':'Load the imagegen skill'}]
                    first=await client.post('/v1/chat/completions',json={
                        'model':'antigravity/gemini-test','messages':messages})
                    self.assertEqual(first.status_code,200)
                    assistant=first.json()['choices'][0]['message']
                    assistant['tool_calls'][0].pop('extra_content')
                    messages.extend([assistant,{'role':'tool','tool_call_id':'native-call',
                        'content':'<skill_content name="imagegen">synthetic skill text</skill_content>'}])
                    second=await client.post('/v1/chat/completions',json={
                        'model':'antigravity/gemini-test','messages':messages})
                    self.assertEqual(second.status_code,200,second.text)
                    self.assertEqual(second.json()['choices'][0]['message']['content'],'OK')
                    self.assertEqual(pool.states[0].fail_count,0)
            finally:
                await upstream.aclose()

    async def test_validation_error_is_400_without_cooling_or_secret_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'accounts.json'
            raw={'accounts':[{'id':'ag-test','channel':'antigravity','driver':'antigravity',
                'credential_id':'ag-'+'a'*24,'models':{'antigravity/gemini-test':'gemini-test'}}]}
            path.write_text(json.dumps(raw),encoding='utf-8')
            pool=AccountManager(Settings.model_validate(raw))
            def response(request):
                return httpx.Response(400,json={'error':{'code':400,'status':'INVALID_ARGUMENT',
                    'message':'private prompt token fixture must not be exposed',
                    'details':[{'@type':'type.googleapis.com/google.rpc.BadRequest',
                        'fieldViolations':[{'field':'request.generation_config.max_output_tokens',
                                             'description':'fixture-private-value'}]}]}})
            upstream=UpstreamClient(lambda account:httpx.MockTransport(response))
            manager=WebManager(pool,path,upstream)
            async def credential(*args,**kwargs):return {'project_id':'fixture-project','access_token':'fixture-secret'}
            manager.ag.credential=credential
            app.state.pool,app.state.upstream,app.state.web=pool,upstream,manager
            app.state.api_key=b'bad-request-test'
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                        headers={'Authorization':'Bearer bad-request-test'}) as client:
                    reply=await client.post('/v1/chat/completions',json={'model':'antigravity/gemini-test',
                        'messages':[{'role':'user','content':'test'}]})
                self.assertEqual(reply.status_code,400,reply.text)
                self.assertIn('INVALID_ARGUMENT',reply.text)
                self.assertIn('max_output_tokens',reply.text)
                self.assertNotIn('fixture-private-value',reply.text)
                self.assertNotIn('private prompt',reply.text)
                self.assertEqual(pool.states[0].fail_count,0)
                self.assertEqual(pool.states[0].status,'active')
                self.assertFalse(manager.protection.paused)
            finally:
                await upstream.aclose()
