"""Safety invariants against the pinned source. All credentials/processes are fake."""
import asyncio
import json
import os
import subprocess
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from account_manager import AccountManager,UpstreamFailure
from config import Account,Settings
from main import app
from protection import DispatchProtection,ProtectionBlocked


def run(coro):
    return asyncio.run(coro)


def test_auth_rejects_before_reading_large_body():
    async def check():
        consumed=0
        async def chunks():
            nonlocal consumed
            for _ in range(256):
                consumed+=8192
                yield b' '*8192
        app.state.api_key=b'audit-fixture'
        app.state.pool=None
        app.state.web=SimpleNamespace(pool=None,protection=None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            reply=await client.post('/v1/chat/completions',content=chunks(),
                headers={'Content-Type':'application/json'})
        assert reply.status_code==401
        assert consumed==0, f'Unauthenticated body bytes consumed: {consumed}'
    run(check())


def test_three_revoked_gemini_keys_do_not_pause_healthy_fourth_key():
    async def check():
        with tempfile.TemporaryDirectory() as temp:
            accounts=[Account(id='fixture-'+str(i),channel='gemini_api',driver='gemini',
                token='fixture-key',models={'chat':'gemini-test'},jitter_ms=0) for i in range(4)]
            pool=AccountManager(Settings(accounts=accounts))
            guard=DispatchProtection(Path(temp)/'guard.json')
            pool.protection=guard
            reached=[]
            async def attempt(lease,current):
                reached.append(lease.account.id)
                if lease.account.id!='fixture-3':
                    raise UpstreamFailure('gemini_key_invalid',reauth=True)
                return 'healthy'
            result=None
            async with AsyncExitStack() as lifetime:
                try:
                    result=await pool.failover('chat',attempt,lifetime)
                except ProtectionBlocked:
                    pass
            assert result=='healthy', f'healthy account not reached; paused={guard.paused}; attempts={len(reached)}'
    run(check())


def test_enable_runtime_disabled_account_really_enables_it():
    async def check():
        from upstream_client import UpstreamClient
        from web_api import WebManager
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'accounts.json'
            raw={'accounts':[{'id':'fixture','endpoint':'http://127.0.0.1:1/chat',
                'models':{'chat':'fixture'},'token':'fixture-key','jitter_ms':0}]}
            path.write_text(json.dumps(raw),encoding='utf-8')
            pool=AccountManager(Settings.model_validate(raw))
            upstream=UpstreamClient(lambda account:None)
            web=WebManager(pool,path,upstream)
            try:
                async with AsyncExitStack() as lifetime:
                    lease=await pool.acquire('chat',set(),lifetime)
                    await lease.fail(UpstreamFailure('http_401',reauth=True))
                await web.rpc('account.update', {'accountId': 'fixture', 'patch': {'enabled': True}}, 'http://test')
                assert pool.states[0].status=='active', 'Explicit enable left runtime status disabled'
            finally:
                await web.bridge.aclose()
                await upstream.aclose()
    run(check())


def test_independent_model_requests_do_not_pin_argon_to_one_account():
    async def check():
        pool=AccountManager(Settings(accounts=[Account(id=name,driver='demo',
            models={'argon':'gemini-4-argon-high','search':'gemini-2.5-flash'},jitter_ms=0)
            for name in ['A','B']]))
        argon=[]
        for _ in range(4):
            for model in ['argon','search']:
                async with AsyncExitStack() as lifetime:
                    lease=await pool.acquire(model,set(),lifetime)
                    if model=='argon':argon.append(lease.account.id)
        assert len(set(argon))==2, f'Argon used only {argon}'
    run(check())


def test_takeover_does_not_kill_unidentified_python_service():
    import launch_web
    with patch.object(launch_web,'_check_health',return_value=True), \
         patch.object(launch_web,'_port_owner_info',return_value=('999999','python.exe')), \
         patch.object(launch_web,'_port_available',return_value=True), \
         patch.object(launch_web.time,'sleep'), \
         patch.object(launch_web.subprocess,'run') as command:
        launch_web._try_takeover_previous_gateway(18298)
        assert not command.called, 'Unidentified service was targeted by forced termination'


def test_exe_failure_report_redacts_gateway_key():
    root=Path(__file__).resolve().parents[2]
    csc=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not csc.is_file():pytest.skip('No local .NET Framework compiler')
    with tempfile.TemporaryDirectory() as temp:
        target=Path(temp)
        fake=target/'.venv/Scripts/pythonw.exe'
        fake.parent.mkdir(parents=True)
        (target/'launch_desktop.py').write_text('# fixture only',encoding='utf-8')
        code=target/'fake.cs'
        code.write_text('''using System; class Fake { static int Main(string[] args) {
            if(args.Length>0 && args[0]=="-c") return 0;
            Console.WriteLine("Gateway Key: AUDIT_FIXTURE_GATEWAY_KEY_20261108");
            Console.Error.WriteLine("Synthetic startup failure"); return 1; }}''',encoding='utf-8')
        for output,source,extra in [(fake,code,[]),(target/'TurnModelFree.exe',
                root/'launcher/TurnModelFreeLauncher.cs',['/target:winexe','/reference:System.Windows.Forms.dll',
                                                       '/reference:System.Drawing.dll'])]:
            compilation=subprocess.run([str(csc),'/nologo','/codepage:65001','/out:'+str(output),*extra,str(source)],
                capture_output=True,timeout=30)
            assert compilation.returncode==0
        report=target/'failure.txt'
        env=dict(os.environ,TMF_LAUNCHER_NO_DIALOG='1',TMF_LAUNCHER_REPORT=str(report))
        env.pop('TMF_LAUNCHER_TRACE',None)
        subprocess.run([str(target/'TurnModelFree.exe')],env=env,capture_output=True,timeout=30)
        message=report.read_text(encoding='utf-8')
        assert 'AUDIT_FIXTURE_GATEWAY_KEY_20261108' not in message, 'Gateway key exposed in failure-report text'


def test_ui_catalog_contains_models_exposed_by_api():
    async def check():
        pool=AccountManager(Settings(accounts=[Account(id='fixture',driver='demo',
            models={'local/chat':'native'},jitter_ms=0)]))
        async def rows():
            return [{'driver':'demo','models':{'local/chat':'native'}}]
        async def probe():
            return ['codearts/fixture-model']
        app.state.pool=pool
        app.state.api_key=b'audit-fixture'
        app.state.web=SimpleNamespace(pool=pool,protection=None,sessions={},demo=True,
            rows=rows,plugin=SimpleNamespace(probe=probe))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                headers={'Authorization':'Bearer audit-fixture'}) as client:
            api=(await client.get('/v1/models')).json()
            state=(await client.get('/ui/state')).json()
        expected={item['id'] for item in api['data']}
        assert expected <= set(state['models']), f'UI catalog is missing {expected-set(state["models"])}'
    run(check())


def test_three_antigravity_pool_cursors_are_independent_and_aliases_share_pool():
    async def check():
        models = {'argon-high':'gemini-4-argon-high', 'argon-low':'gemini-4-argon-low',
                  'claude':'claude-sonnet', 'flash':'gemini-flash'}
        pool=AccountManager(Settings(accounts=[Account(id=name, channel='antigravity', driver='antigravity',
            credential_id='ag-'+('a' if name=='A' else 'b')*24, models=models, jitter_ms=0) for name in ['A','B']]))
        seen={key:[] for key in ('argon','claude','flash')}
        for i in range(4):
            for key,alias in [('argon','argon-high' if i%2==0 else 'argon-low'),('claude','claude'),('flash','flash')]:
                async with AsyncExitStack() as lifetime:
                    lease=await pool.acquire(alias,set(),lifetime)
                    seen[key].append(lease.account.id)
        assert all(len(set(values))==2 and all(a!=b for a,b in zip(values, values[1:])) for values in seen.values()), seen
        assert len(pool.cursors)==3
    run(check())


def test_metadata_commit_keeps_runtime_cooldown():
    async def check():
        from upstream_client import UpstreamClient
        from web_api import WebManager
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'accounts.json'
            raw={'accounts':[{'id':'fixture','driver':'demo','models':{'chat':'fixture'},'jitter_ms':0}]}
            path.write_text(json.dumps(raw),encoding='utf-8')
            pool=AccountManager(Settings.model_validate(raw))
            upstream=UpstreamClient(lambda account:None)
            web=WebManager(pool,path,upstream)
            try:
                async with AsyncExitStack() as lifetime:
                    lease=await pool.acquire('chat',set(),lifetime)
                    await lease.fail(UpstreamFailure('network_error'))
                original=pool.states[0].cooldown_until
                await web.commit(lambda data:data['accounts'][0].update(nickname='renamed'))
                assert pool.states[0].status=='cooldown'
                assert pool.states[0].cooldown_until==original
            finally:
                await web.bridge.aclose()
                await upstream.aclose()
    run(check())


def test_redirected_desktop_banner_never_emits_key():
    import io
    from contextlib import redirect_stdout
    from launch_desktop import _print_startup_banner
    output=io.StringIO()
    with redirect_stdout(output):
        _print_startup_banner(8199,'SYNTHETIC-SECRET-DO-NOT-LOG')
    assert 'SYNTHETIC-SECRET-DO-NOT-LOG' not in output.getvalue()


def test_model_catalog_deduplicates_and_caches_polling():
    async def check():
        from web_api import available_models
        from plugin_gateway import PluginGatewayError
        pool=SimpleNamespace(models=['antigravity/local'])
        calls=[]
        async def probe():
            calls.append(1)
            return ['codearts/chat','codearts/chat','antigravity/local','trae/retired','unknown/hidden']
        plugin=SimpleNamespace(probe=probe)
        expected=['antigravity/local','codearts/chat']
        assert await available_models(pool,plugin)==expected
        assert await available_models(pool,plugin)==expected
        assert len(calls)==1
        async def unavailable():
            raise PluginGatewayError('fixture unavailable')
        plugin.probe=unavailable
        assert await available_models(pool,plugin,refresh=True)==expected
    run(check())
