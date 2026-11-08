"""Exercise the ASGI boundary without live credentials or an upstream service."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from request_limits import RequestLimitsMiddleware


def exercise(path, chunks, *, key='fixture', length=None, method='POST'):
    async def check():
        calls, consumed, output = [], [], []
        async def app(scope, receive, send):
            calls.append(await receive() if method == 'POST' else None)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        events = iter(chunks)
        async def receive():
            event = next(events)
            consumed.append(event)
            return event
        async def send(event):
            output.append(event)
        headers = [(b'authorization', b'Bearer ' + key.encode())]
        if length is not None:
            headers.append((b'content-length', str(length).encode()))
        scope = {'type': 'http', 'path': path, 'method': method, 'headers': headers,
                 'app': SimpleNamespace(state=SimpleNamespace(api_key=b'fixture'))}
        await RequestLimitsMiddleware(app, max_bytes=16)(scope, receive, send)
        return calls, consumed, output
    return asyncio.run(check())


def chunk(data, more=False):
    return {'type': 'http.request', 'body': data, 'more_body': more}


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/models', '/admin/accounts'])
def test_bad_auth_does_not_read_body(path):
    calls, consumed, output = exercise(path, [], key='wrong')
    assert not calls and not consumed
    assert output[0]['status'] == 401
    assert (b'www-authenticate', b'Bearer') in output[0]['headers']


def test_content_length_rejected_before_read():
    calls, consumed, output = exercise('/v1/chat/completions', [], length=17)
    assert not calls and not consumed
    assert output[0]['status'] == 413


@pytest.mark.parametrize('length', ['abc', -1])
def test_invalid_length(length):
    calls, consumed, output = exercise('/ui/login', [], length=length)
    assert not calls and not consumed
    assert output[0]['status'] == 400


def test_chunked_body_cannot_bypass_limit():
    calls, consumed, output = exercise('/v1/chat/completions', [chunk(b'x'*10, True), chunk(b'y'*7)])
    assert not calls and len(consumed) == 2
    assert output[0]['status'] == 413


def test_under_limit_replayed_as_one_body():
    calls, consumed, output = exercise('/v1/chat/completions', [chunk(b'{', True), chunk(b'}')])
    assert calls == [chunk(b'{}')]
    assert output[0]['status'] == 200


def test_client_disconnect_is_not_dispatched():
    calls, consumed, output = exercise('/v1/chat/completions', [chunk(b'{', True), {'type': 'http.disconnect'}])
    assert not calls and not output


def test_signed_image_download_retains_its_own_authentication():
    calls, consumed, output = exercise('/v1/images/files/fixture', [], key='wrong', method='GET')
    assert output[0]['status'] == 200


def test_json_and_sse_routes_keep_working():
    from fastapi import FastAPI, Request
    from starlette.responses import StreamingResponse
    async def check():
        app = FastAPI()
        app.state.api_key = b'fixture'
        app.add_middleware(RequestLimitsMiddleware, max_bytes=1024)
        @app.post('/v1/test')
        async def endpoint(request: Request):
            payload = await request.json()
            if payload.get('stream'):
                async def events():
                    yield b'data: hello\n\ndata: [DONE]\n\n'
                return StreamingResponse(events(), media_type='text/event-stream')
            return {'ok': True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers={'Authorization': 'Bearer fixture'}) as client:
            assert (await client.post('/v1/test', json={})).json() == {'ok': True}
            reply = await client.post('/v1/test', json={'stream': True})
            assert reply.headers['content-type'].startswith('text/event-stream')
            assert 'data: [DONE]' in reply.text
    asyncio.run(check())
