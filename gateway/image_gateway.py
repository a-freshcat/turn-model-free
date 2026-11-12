"""Native image generation with the same leases, credentials and protection as chat."""
import base64
import binascii
import hashlib
import hmac
import os
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal
from uuid import uuid4

import anyio
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from account_manager import InvalidRequest, PoolUnavailable, UpstreamFailure
from antigravity_auth import API_BASE, OAuthFailure
from antigravity_driver import check_before_credential_refresh, native_http_error, unwrap
from protection import ProtectionBlocked
from upstream_client import MAX_ERROR, _body, _json_object, _retry_after

router = APIRouter()
ROOT = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / '.gateway-images'
MAX_IMAGE = 20 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
TTL = 3600
RATIOS = {'auto':None, '1024x1024':'1:1', '1536x1024':'3:2', '1024x1536':'2:3'}


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    prompt: str = Field(min_length=1, max_length=16000)
    model: str | None = Field(default=None, max_length=256)
    n: Literal[1] = 1
    size: Literal['auto','1024x1024','1536x1024','1024x1536'] = 'auto'
    response_format: Literal['url','b64_json'] = 'url'
    user: str | None = Field(default=None, max_length=256)


def decode_image(part):
    inline = part.get('inlineData') or part.get('inline_data')
    if not isinstance(inline, dict):
        return None
    encoded = inline.get('data')
    mime = inline.get('mimeType') or inline.get('mime_type')
    if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE + 2)//3*4:
        raise InvalidRequest('The image output is missing or exceeds the 20 MiB limit.')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise InvalidRequest('The upstream returned invalid image encoding.') from None
    if not raw or len(raw) > MAX_IMAGE:
        raise InvalidRequest('The image output is empty or too large.')
    valid = ((mime == 'image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or
             (mime == 'image/jpeg' and raw.startswith(b'\xff\xd8\xff')) or
             (mime == 'image/webp' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'))
    if not valid:
        raise InvalidRequest('Unsupported image MIME type or image signature.')
    return raw, mime


async def generate(upstream, lease, lifetime, body, model):
    account = lease.account
    guard = getattr(upstream, 'protection', None)
    if guard is not None:
        await guard.before_send()
    auth = upstream.antigravity
    proxy = auth.resolve_proxy(account.proxy)
    try:
        credential = await auth.credential(account.credential_id, proxy=proxy)
        if not credential.get('project_id'):
            credential = await auth.refresh_catalog(account.credential_id, proxy=proxy)
    except OAuthFailure as exc:
        raise UpstreamFailure(exc.reason, reauth=exc.reauth, retry_after=exc.retry_after) from None
    generation = {'responseModalities':['TEXT','IMAGE']}
    if RATIOS[body.size]:
        generation['imageConfig'] = {'aspectRatio':RATIOS[body.size]}
    envelope = {'model':account.models[model], 'project':credential['project_id'],
        'userAgent':'antigravity', 'requestType':'agent', 'requestId':'agent-'+str(uuid4()),
        'request':{'contents':[{'role':'user','parts':[{'text':body.prompt}]}],
                   'generationConfig':generation}}
    client = upstream._client(account)
    try:
        for attempt in range(2):
            request = client.build_request('POST', API_BASE+'/v1internal:generateContent',
                headers={'Authorization':'Bearer '+credential['access_token'],
                    'User-Agent':auth.user_agent, 'Accept':'application/json', 'Accept-Encoding':'identity'},
                json=envelope, timeout=httpx.Timeout(None, connect=5, write=15, pool=2))
            response = await client.send(request, stream=True)
            lifetime.push_async_callback(response.aclose)
            if response.status_code != 401 or attempt:
                break
            await check_before_credential_refresh(response)
            await response.aclose()
            try:
                credential = await auth.credential(account.credential_id, force=True, proxy=proxy)
            except OAuthFailure as exc:
                raise UpstreamFailure(exc.reason, reauth=exc.reauth, retry_after=exc.retry_after) from None
        if not response.is_success:
            raise native_http_error(response.status_code,
                await _body(response, MAX_ERROR, prefix=True), _retry_after(response.headers.get('retry-after')))
        value = unwrap(_json_object(await _body(response, MAX_RESPONSE)), _retry_after(response.headers.get("retry-after")))
    except httpx.HTTPError:
        raise UpstreamFailure('antigravity_transport') from None
    for candidate in value.get('candidates') or []:
        for part in (candidate.get('content') or {}).get('parts') or []:
            if part.get('thought'):
                continue
            image = decode_image(part)
            if image is not None:
                return image
    raise InvalidRequest('The upstream returned no image. The prompt may be filtered or the model may not support generation.')


def sign_link(key, filename, expires):
    return hmac.new(key, f'{filename}:{expires}'.encode(), hashlib.sha256).hexdigest()


def persist(raw, mime):
    """Private bounded store; the caller never controls a filesystem path."""
    IMAGE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if IMAGE_DIR.is_symlink():
        raise OSError('Image directory cannot be a symlink')
    now = time.time()
    files = list(IMAGE_DIR.glob('*.*'))
    for path in files:
        if re.fullmatch(r'[a-f0-9]{32}\.(png|jpg|webp)', path.name) and now-path.stat().st_mtime > TTL:
            path.unlink(missing_ok=True)
    if sum(1 for _ in IMAGE_DIR.glob('*.*')) >= 128:
        raise OSError('Image store full')
    ext = {'image/png':'png','image/jpeg':'jpg','image/webp':'webp'}[mime]
    name = uuid4().hex+'.'+ext
    with (IMAGE_DIR/name).open('xb') as file:
        file.write(raw)
    if os.name != 'nt':
        (IMAGE_DIR/name).chmod(0o600)
    return name


@router.post('/v1/images/generations', dependencies=[Depends(HTTPBearer(auto_error=False))])
async def images(body: ImageRequest, request: Request):
    # FastAPI does not automatically cancel an async handler when the caller
    # disconnects. Keep polling separate from the already-consumed request body.
    result = None
    async with anyio.create_task_group() as group:
        async def disconnected():
            while True:
                if await request.is_disconnected():
                    group.cancel_scope.cancel()
                    return
                await anyio.sleep(.25)
        group.start_soon(disconnected)
        result = await image_response(body, request)
        group.cancel_scope.cancel()
    return result or JSONResponse({'error':{'code':'client_disconnected'}}, status_code=499)


async def image_response(body: ImageRequest, request: Request):
    from main import auth_error, error_response
    unauthorized = auth_error(request)
    if unauthorized is not None:
        return unauthorized
    pool, upstream = request.app.state.pool, request.app.state.upstream
    if upstream.antigravity is None:
        return error_response(503, 'Log in to an Antigravity account first.', 'image_generation_unavailable')
    available = sorted({alias for state in pool.states if state.account.driver == 'antigravity'
        for alias, native in state.account.models.items()
        if 'image' in native.lower() and alias not in state.account.disabled_models})
    model = body.model or (available[0] if available else None)
    if model not in available:
        return error_response(404, 'No configured image model matches this request.', 'model_not_found',
                              'invalid_request_error', 'model')
    lifetime = AsyncExitStack()
    try:
        async def attempt(lease, current):
            return lease, await generate(upstream, lease, current, body, model)
        lease, (raw, mime) = await pool.failover(model, attempt, lifetime,
            timeout_seconds=pool.settings.stream_timeout_seconds, timeout_reason='completion_timeout',
            excluded_accounts={state.account.id for state in pool.states
                if state.account.driver != 'antigravity' or
                   'image' not in state.account.models.get(model, '').lower()})
        await lease.succeed()
        if body.response_format == 'b64_json':
            data = {'b64_json':base64.b64encode(raw).decode('ascii')}
        else:
            name = await anyio.to_thread.run_sync(persist, raw, mime)
            expires = int(time.time())+TTL
            token = sign_link(request.app.state.api_key, name, expires)
            data = {'url':str(request.url_for('generated_image', filename=name))+
                          f'?expires={expires}&signature={token}'}
        return JSONResponse({'created':int(time.time()), 'data':[data]},
                            headers={'Cache-Control':'no-store'})
    except ProtectionBlocked as exc:
        return exc.response()
    except InvalidRequest as exc:
        return error_response(400, str(exc), 'image_generation_failed', 'invalid_request_error')
    except PoolUnavailable as exc:
        timeout = exc.failure_reason == 'completion_timeout'
        return error_response(504 if timeout else 503,
            'Image generation timed out.' if timeout else 'No image account is currently available.',
            'upstream_timeout' if timeout else 'account_pool_unavailable',
            headers={'Retry-After':str(exc.retry_after)} if exc.retry_after else None)
    except OSError:
        return error_response(503, 'The local image store is unavailable. Try b64_json.', 'image_storage_unavailable')
    finally:
        with anyio.CancelScope(shield=True):
            await lifetime.aclose()


@router.get('/v1/images/files/{filename}', name='generated_image', include_in_schema=False)
async def generated_image(filename: str, request: Request, expires: int = 0, signature: str = ''):
    from main import error_response
    now = int(time.time())
    if (not re.fullmatch(r'[a-f0-9]{32}\.(png|jpg|webp)', filename) or
            not now < expires <= now+TTL or not hmac.compare_digest(signature,
                sign_link(request.app.state.api_key, filename, expires))):
        return error_response(403, 'Image link is invalid or expired.', 'invalid_image_link')
    path = IMAGE_DIR/filename
    if not path.is_file() or path.is_symlink():
        return error_response(404, 'Image not found.', 'image_not_found')
    return FileResponse(path, headers={'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})
