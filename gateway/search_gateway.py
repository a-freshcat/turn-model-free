"""Grounded Google Search behind native account leases, separate from chat history."""
from contextlib import AsyncExitStack
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from account_manager import InvalidRequest, PoolUnavailable, UpstreamFailure
from antigravity_auth import API_BASE, OAuthFailure
from antigravity_driver import check_before_credential_refresh, native_http_error, unwrap
from protection import ProtectionBlocked
from upstream_client import MAX_ERROR, MAX_JSON, _body, _json_object, _retry_after

router = APIRouter()


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    query: str = Field(min_length=1,max_length=2048)
    model: str | None = Field(default=None,max_length=256)
    max_results: int = Field(default=8,ge=1,le=20,alias='maxResults')


class UngroundedSearch(Exception):
    pass


def search_model(native):
    return native.startswith('gemini-') and not any(s in native.lower() for s in ('image','argon'))


def extract_result(value, limit):
    """Only real grounding chunks count as sources; model prose is not a source list."""
    texts, sources, queries = [], [], []
    by_url = {}
    for candidate in value.get('candidates') or []:
        if not isinstance(candidate,dict):
            continue
        indexed = {}
        for part in (candidate.get('content') or {}).get('parts') or []:
            if not isinstance(part,dict):
                continue
            if isinstance(part.get('text'),str) and not part.get('thought'):
                texts.append(part['text'])
        meta = candidate.get('groundingMetadata') or candidate.get('grounding_metadata') or {}
        chunks = meta.get('groundingChunks') or meta.get('grounding_chunks') or []
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk,dict):
                continue
            web = chunk.get('web') or {}
            if not isinstance(web,dict):
                continue
            uri = web.get('uri')
            if not isinstance(uri,str) or len(uri)>8192:
                continue
            try:
                parsed = urlsplit(uri)
                valid = parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if not valid:
                continue
            existing = by_url.get(uri)
            if existing is None:
                existing = {'title':str(web.get('title') or parsed.hostname)[:512],'url':uri}
                sources.append(existing)
                by_url[uri] = existing
            indexed[index] = existing
        for support in meta.get('groundingSupports') or meta.get('grounding_supports') or []:
            if not isinstance(support,dict):
                continue
            text = (support.get('segment') or {}).get('text')
            if not isinstance(text,str):
                continue
            for index in support.get('groundingChunkIndices') or support.get('grounding_chunk_indices') or []:
                if index in indexed:
                    source = indexed[index]
                    source['snippet'] = (source.get('snippet','')+' '+text).strip()[:2000]
        for query in meta.get('webSearchQueries') or meta.get('web_search_queries') or []:
            if isinstance(query,str) and query not in queries:
                queries.append(query[:2048])
    if not sources:
        raise UngroundedSearch()
    return {'content':'\n'.join(texts)[:100000], 'sources':sources[:limit],
            'queries':queries[:20], 'truncated':len(sources)>limit}


async def generate_search(upstream, lease, lifetime, body, model):
    account = lease.account
    guard = getattr(upstream,'protection',None)
    if guard is not None:
        await guard.before_send()
    auth = upstream.antigravity
    proxy = auth.resolve_proxy(account.proxy)
    try:
        credential = await auth.credential(account.credential_id,proxy=proxy)
        if not credential.get('project_id'):
            credential = await auth.refresh_catalog(account.credential_id,proxy=proxy)
    except OAuthFailure as exc:
        raise UpstreamFailure(exc.reason,reauth=exc.reauth,retry_after=exc.retry_after) from None
    envelope = {'model':account.models[model], 'project':credential['project_id'],
        'userAgent':'antigravity','requestType':'agent','requestId':'agent-'+str(uuid4()),
        'request':{'contents':[{'role':'user','parts':[{'text':
            'Search the web for the following query. Give a concise factual summary grounded in actual web sources, with dates where relevant.\nQuery: '+body.query}]}],
            'tools':[{'googleSearch':{}}]}}
    client = upstream._client(account)
    try:
        for attempt in range(2):
            request = client.build_request('POST',API_BASE+'/v1internal:generateContent',
                headers={'Authorization':'Bearer '+credential['access_token'],'User-Agent':auth.user_agent,
                    'Accept':'application/json','Accept-Encoding':'identity'},json=envelope,
                timeout=httpx.Timeout(None,connect=5,write=15,pool=2))
            response = await client.send(request,stream=True)
            lifetime.push_async_callback(response.aclose)
            if response.status_code!=401 or attempt:
                break
            await check_before_credential_refresh(response)
            await response.aclose()
            try:
                credential = await auth.credential(account.credential_id,force=True,proxy=proxy)
            except OAuthFailure as exc:
                raise UpstreamFailure(exc.reason,reauth=exc.reauth,retry_after=exc.retry_after) from None
        if not response.is_success:
            raise native_http_error(response.status_code,await _body(response,MAX_ERROR,prefix=True),
                                    _retry_after(response.headers.get('retry-after')))
        value = unwrap(_json_object(await _body(response,MAX_JSON)), _retry_after(response.headers.get("retry-after")))
    except httpx.HTTPError:
        raise UpstreamFailure('antigravity_transport') from None
    return {**extract_result(value,body.max_results),'model':account.models[model]}


@router.post('/v1/search',dependencies=[Depends(HTTPBearer(auto_error=False))])
async def search(body: SearchRequest,request: Request):
    result = None
    async with anyio.create_task_group() as group:
        async def disconnected():
            while True:
                if await request.is_disconnected():
                    group.cancel_scope.cancel()
                    return
                await anyio.sleep(.25)
        group.start_soon(disconnected)
        result = await search_response(body,request)
        group.cancel_scope.cancel()
    return result or JSONResponse({'error':{'code':'client_disconnected'}},status_code=499)


async def search_response(body,request):
    from main import auth_error,error_response
    denied = auth_error(request)
    if denied is not None:
        return denied
    if not body.query.strip():
        return error_response(400,'Search query cannot be blank.','invalid_request','invalid_request_error')
    pool,upstream = request.app.state.pool,request.app.state.upstream
    if upstream.antigravity is None:
        return error_response(503,'Log in to an Antigravity account first.','search_unavailable')
    choices = {alias:native for s in pool.states if s.account.driver=='antigravity'
        for alias,native in s.account.models.items() if search_model(native)
        and alias not in s.account.disabled_models}
    ordered = sorted(choices,key=lambda alias:(choices[alias]!='gemini-2.5-flash',
        'flash' not in choices[alias], 'high' in choices[alias],alias))
    model = body.model or (ordered[0] if ordered else None)
    if model not in choices:
        return error_response(404,'No configured search-capable Gemini model matches this request.',
                              'model_not_found','invalid_request_error','model')
    lifetime = AsyncExitStack()
    try:
        async def attempt(lease,current):
            return lease,await generate_search(upstream,lease,current,body,model)
        lease,result = await pool.failover(model,attempt,lifetime,
            timeout_seconds=pool.settings.stream_timeout_seconds,timeout_reason='completion_timeout',
            excluded_accounts={s.account.id for s in pool.states
                if s.account.driver!='antigravity' or not search_model(s.account.models.get(model,''))})
        await lease.succeed()
        return JSONResponse(result,headers={'Cache-Control':'no-store'})
    except ProtectionBlocked as exc:
        return exc.response()
    except InvalidRequest as exc:
        return error_response(400,str(exc),'search_failed','invalid_request_error')
    except UngroundedSearch:
        return error_response(502,'The upstream returned no grounded web sources; search success is not confirmed.',
                              'search_ungrounded')
    except PoolUnavailable as exc:
        timeout = exc.failure_reason=='completion_timeout'
        return error_response(504 if timeout else 503,
            'Search timed out.' if timeout else 'No search account is currently available.',
            'upstream_timeout' if timeout else 'account_pool_unavailable',
            headers={'Retry-After':str(exc.retry_after)} if exc.retry_after else None)
    finally:
        with anyio.CancelScope(shield=True):
            await lifetime.aclose()
