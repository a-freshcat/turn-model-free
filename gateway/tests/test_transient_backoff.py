import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path

from account_manager import AccountManager, UpstreamFailure
from config import Account, Settings
from protection import DispatchProtection


class TransientBackoffTests(unittest.IsolatedAsyncioTestCase):
    def pool(self, **kwargs):
        return AccountManager(Settings(accounts=[Account(id='one',models={'chat':'model'},
            endpoint='https://example.com/v1/chat/completions')],**kwargs))

    async def apply_failure(self, pool, reason, **kwargs):
        async with AsyncExitStack() as life:
            lease=await pool.acquire('chat',set(),life)
            await lease.fail(UpstreamFailure(reason,**kwargs))
        return (await pool.snapshot())[0]

    async def test_first_transport_failure_waits_twenty_seconds_not_ten_minutes(self):
        for reason in ('upstream_transport','antigravity_transport','antigravity_stream_transport',
                       'first_output_timeout','stream_timeout','antigravity_http_503'):
            state=await self.apply_failure(self.pool(),reason)
            self.assertLessEqual(state['cooldown_remaining_seconds'],20)
            self.assertGreaterEqual(state['cooldown_remaining_seconds'],19)
            self.assertEqual(state['status'],'cooldown')

    async def test_backoff_increases_is_capped_and_success_resets_it(self):
        pool=self.pool()
        for seconds in (20,40,80,120,120):
            state=await self.apply_failure(pool,'antigravity_transport')
            self.assertEqual(state['cooldown_remaining_seconds'],seconds)
            pool.states[0].cooldown_until=0
        async with AsyncExitStack() as life:
            lease=await pool.acquire('chat',set(),life)
            await lease.succeed()
        state=await self.apply_failure(pool,'antigravity_transport')
        self.assertEqual(state['cooldown_remaining_seconds'],20)

    async def test_upstream_retry_after_is_not_shortened(self):
        state=await self.apply_failure(self.pool(),'antigravity_http_503',retry_after=900)
        self.assertGreaterEqual(state['cooldown_remaining_seconds'],899)

    async def test_explicitly_short_legacy_configuration_still_applies(self):
        state=await self.apply_failure(self.pool(cooldown_seconds=3),'upstream_transport')
        self.assertLessEqual(state['cooldown_remaining_seconds'],3)

    async def test_repeated_transport_errors_do_not_become_a_security_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            guard=DispatchProtection(Path(directory)/'state.json')
            for _ in range(6):
                await guard.observe_failure('antigravity_transport')
            state=await guard.snapshot()
            self.assertFalse(state['paused'])
            self.assertEqual(state['recent_network_failure_count'],6)
            await guard.observe_failure('antigravity_http_403')
            self.assertTrue((await guard.snapshot())['paused'])

    async def test_network_failure_during_token_refresh_is_not_invalid_authorization(self):
        import httpx
        from antigravity_auth import AntigravityAuth, OAuthFailure
        from antigravity_driver import send_native
        with tempfile.TemporaryDirectory() as directory:
            auth=AntigravityAuth(Path(directory))
            auth.client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError('fixture'))))
            with self.assertRaises(OAuthFailure) as failure:
                await auth.json_request('POST','https://oauth.example/token')
            self.assertEqual(failure.exception.reason,'antigravity_auth_transport')
            self.assertFalse(failure.exception.reauth)
            account=Account(id='ag',channel='antigravity',driver='antigravity',
                credential_id='ag-'+'a'*24,models={'chat':'native'})
            async def credential(*args,**kwargs):raise failure.exception
            auth.credential=credential
            async with AsyncExitStack() as life, httpx.AsyncClient() as client:
                with self.assertRaises(UpstreamFailure) as converted:
                    await send_native(auth,client,account,[{'role':'user','content':'hello'}],False,
                        model='chat',options={},lifetime=life)
                self.assertEqual(converted.exception.reason,'antigravity_auth_transport')
            await auth.aclose()

    async def test_temporary_auth_service_failure_preserves_retry_after(self):
        import httpx
        from antigravity_auth import AntigravityAuth, OAuthFailure
        with tempfile.TemporaryDirectory() as directory:
            auth=AntigravityAuth(Path(directory))
            auth.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(503,text='temporary proxy error',headers={'Retry-After':'900'})))
            try:
                with self.assertRaises(OAuthFailure) as caught:
                    await auth.json_request('POST','https://oauth.example/token')
                self.assertEqual(caught.exception.reason,'antigravity_http_503')
                self.assertEqual(caught.exception.retry_after,900)
            finally:
                await auth.aclose()

    async def test_real_route_returns_short_retry_after_then_resets_after_success(self):
        import json
        import httpx
        from main import app
        from upstream_client import UpstreamClient
        from web_api import WebManager
        from test_gateway import completion
        with tempfile.TemporaryDirectory() as directory:
            pool=self.pool()
            path=Path(directory)/'accounts.json'
            path.write_text(pool.settings.model_dump_json(),encoding='utf-8')
            calls=[]
            def reply(request):
                calls.append(request.url.path)
                if len(calls)==1:
                    raise httpx.ReadTimeout('temporary network failure')
                return httpx.Response(200,json=completion())
            upstream=UpstreamClient(lambda account:httpx.MockTransport(reply))
            web=WebManager(pool,path,upstream)
            app.state.pool,app.state.upstream,app.state.web=pool,upstream,web
            app.state.api_key=b'backoff-test'
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                        headers={'Authorization':'Bearer backoff-test'}) as client:
                    body={'model':'chat','messages':[{'role':'user','content':'hello'}]}
                    error=await client.post('/v1/chat/completions',json=body)
                    self.assertEqual(error.status_code,503)
                    self.assertLessEqual(int(error.headers['Retry-After']),20)
                    self.assertEqual(error.json()['error']['code'],'account_pool_unavailable')
                    self.assertFalse(web.protection.paused)
                    await client.post('/v1/chat/completions',json=body)
                    self.assertEqual(len(calls),1)
                    pool.states[0].cooldown_until=0
                    self.assertEqual((await client.post('/v1/chat/completions',json=body)).status_code,200)
                    self.assertEqual(pool.states[0].transient_streak,0)
            finally:
                await upstream.aclose()
