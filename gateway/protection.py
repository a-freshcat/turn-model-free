"""Conservative local dispatch controls; no identity spoofing or detection bypass.

Only fixed categories and HTTP status are recorded. User prompts, upstream error
bodies, credentials, account names and URLs never enter the diagnostic snapshot.
"""
import json
import math
import os
import re
import tempfile
import time
from collections import deque
from contextvars import ContextVar
from pathlib import Path

import anyio
from failure_policy import is_transient_failure
from starlette.responses import JSONResponse

CURRENT_REQUEST = ContextVar('gateway_protection_request', default=None)
MESSAGES = {
    'manual': '已手动暂停模型调用。',
    'upstream_rejected': '上游拒绝或要求重新授权，已暂停，避免重复请求。',
    'channel_verification': '上游渠道校验未通过，已暂停，避免继续重试或换号。',
    'rate_limited': '上游返回限流，已暂停，避免继续消耗请求。',
    'repeated_failures': '短时间内多次出现上游故障，已暂停模型调用。',
    'state_unreadable': '保护状态文件无法读取，已暂停，请先检查文件。',
    'credential_store_unavailable': '无法确认持久凭据状态，已暂停调用并保留账号，请先检查凭据库。',
}


class ProtectionBlocked(Exception):
    def __init__(self, code='dispatch_paused', status=503, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after
        self.message = ('本机调用频率或并发已达上限，请稍后再试。' if status == 429
                        else '模型调用已暂停。请在管理页复制诊断信息，发给 AI 或维护者排查后再恢复。')
        super().__init__(self.message)

    def response(self):
        return JSONResponse({'error': {'message': self.message, 'type': 'server_error',
            'code': self.code, 'param': None}}, status_code=self.status,
            headers={'Retry-After': str(self.retry_after)} if self.retry_after else {})


def _limit(name, default, ceiling):
    try:
        return max(1, min(ceiling, int(os.environ.get(name, default))))
    except ValueError:
        return default


class DispatchProtection:
    def __init__(self, path: Path, *, concurrency=None, per_minute=None):
        self.path = path
        self.concurrency = concurrency or _limit('GATEWAY_MAX_ACTIVE_CHATS', 2, 32)
        self.per_minute = per_minute or _limit('GATEWAY_MAX_DISPATCH_PER_MINUTE', 20, 600)
        self.lock = anyio.Lock()
        self.active = {}
        self.starts, self.failures = deque(), deque()
        self.network_failures = deque()
        self.last_issue_at = 0
        self.paused, self.reason, self.occurred_at, self.http_status = False, '', 0, None
        self.persisted = True
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or type(state.get('paused')) is not bool:
                raise ValueError('invalid state')
            self.paused = state['paused']
            self.reason = state.get('reason') if state.get('reason') in MESSAGES else 'state_unreadable'
            when = state.get('occurred_at')
            self.occurred_at = when if type(when) in (int, float) and math.isfinite(when) else 0
            status = state.get('http_status')
            self.http_status = status if type(status) is int and 100 <= status <= 599 else None
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError):
            self.paused, self.reason, self.occurred_at = True, 'state_unreadable', time.time()
            self.persisted = False

    def check(self):
        if self.paused:
            raise ProtectionBlocked()

    async def admit(self):
        async with self.lock:
            self.check()
            if len(self.active) >= self.concurrency:
                raise ProtectionBlocked('gateway_concurrency_limited', 429, 1)
            token, event = object(), anyio.Event()
            self.active[token] = event
            return token, event

    async def release(self, token):
        async with self.lock:
            self.active.pop(token, None)

    async def before_send(self):
        """Count each local failover attempt and each plugin dispatch, not UI polling."""
        async with self.lock:
            self.check()
            now = time.monotonic()
            while self.starts and self.starts[0] <= now - 60:
                self.starts.popleft()
            if len(self.starts) >= self.per_minute:
                raise ProtectionBlocked('gateway_rate_limited', 429,
                    max(1, math.ceil(self.starts[0] + 60 - now)))
            self.starts.append(now)

    def _save(self):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.protection-', suffix='.tmp', dir=self.path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'paused': self.paused, 'reason': self.reason,
                           'occurred_at': self.occurred_at, 'http_status': self.http_status}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self.persisted = True
        except OSError:
            self.persisted = False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    async def pause(self, reason='manual', status=None):
        async with self.lock:
            if not self.paused:
                self.paused, self.reason, self.occurred_at = True, reason if reason in MESSAGES else 'manual', time.time()
                self.http_status = status if type(status) is int and 100 <= status <= 599 else None
                self._save()
            current = CURRENT_REQUEST.get()
            for token, event in self.active.items():
                if token is not current:
                    event.set()

    async def resume(self):
        async with self.lock:
            # Resume never changes account cooldown/disabled flags, tokens or quotas.
            self.paused, self.reason, self.http_status = False, '', None
            self.occurred_at = time.time()
            self.failures.clear()
            self.network_failures.clear()
            self.last_issue_at = 0
            self._save()
            if not self.persisted:
                self.paused, self.reason = True, 'state_unreadable'
                raise ProtectionBlocked()

    async def observe_failure(self, reason):
        if is_transient_failure(reason):
            async with self.lock:
                self.network_failures.append(time.monotonic())
                self.last_issue_at = time.time()
            return  # Ordinary network faults never latch an account-security pause.
        # Native drivers namespace HTTP failures; keep the same stop policy.
        if reason.startswith('antigravity_http_'):
            reason = reason.removeprefix('antigravity_')
        if reason in ('http_401', 'http_403', 'account_restricted', 'antigravity_authorization'):
            await self.pause('upstream_rejected', int(reason[5:]) if reason.startswith('http_') else None)
        elif reason in ('http_429',):
            await self.pause('rate_limited', 429)
        elif reason == 'channel_11128':
            await self.pause('channel_verification')
        else:
            async with self.lock:
                now = time.monotonic()
                while self.failures and self.failures[0] <= now - 60:
                    self.failures.popleft()
                self.failures.append(now)
                self.last_issue_at = time.time()
                stop = len(self.failures) >= 3
            if stop:
                await self.pause('repeated_failures')

    async def snapshot(self):
        async with self.lock:
            while self.failures and self.failures[0] <= time.monotonic() - 60:
                self.failures.popleft()
            while self.network_failures and self.network_failures[0] <= time.monotonic() - 60:
                self.network_failures.popleft()
            return {'paused': self.paused, 'reason': self.reason,
                'message': MESSAGES.get(self.reason, '模型调用保护已启用。'),
                'occurred_at': self.occurred_at, 'http_status': self.http_status,
                'active_requests': len(self.active), 'max_active_requests': self.concurrency,
                'max_dispatch_per_minute': self.per_minute, 'persisted': self.persisted,
                'recent_failure_count': len(self.failures),
                'recent_network_failure_count': len(self.network_failures),
                'last_issue_at': self.last_issue_at if self.failures or self.network_failures else 0,
                'help': '暂停后请复制诊断信息，发给 AI 或维护者排查。请勿粘贴账号配置、Cookie 或令牌。'}


class ProtectionMiddleware:
    """Admission and cancellation cover both API/UI and both local/plugin routes."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        state = getattr(scope.get('app'), 'state', None)
        web = getattr(state, 'web', None)
        guard = getattr(web, 'protection', None)
        if getattr(web, 'pool', None) is not getattr(state, 'pool', None):
            guard = None
        if (scope['type'] != 'http' or scope.get('method') != 'POST'
                or scope.get('path') not in ('/v1/chat/completions', '/ui/chat') or guard is None):
            return await self.app(scope, receive, send)
        try:
            token, event = await guard.admit()
        except ProtectionBlocked as exc:
            return await exc.response()(scope, receive, send)
        context = CURRENT_REQUEST.set(token)
        started = finished = streaming = interrupted = False

        async def relay(message):
            nonlocal started, finished, streaming
            if message['type'] == 'http.response.start':
                started = True
                streaming = any(k.lower() == b'content-type' and b'text/event-stream' in v
                                for k, v in message.get('headers', []))
            elif message['type'] == 'http.response.body' and not message.get('more_body', False):
                finished = True
            await send(message)

        try:
            async with anyio.create_task_group() as group:
                async def watch():
                    nonlocal interrupted
                    await event.wait()
                    interrupted = True
                    group.cancel_scope.cancel()
                group.start_soon(watch)
                try:
                    await self.app(scope, receive, relay)
                finally:
                    group.cancel_scope.cancel()
            if interrupted and not finished:
                if not started:
                    await ProtectionBlocked().response()(scope, receive, send)
                else:
                    body = b''
                    if streaming:
                        body = b'data: {"error":{"code":"dispatch_paused","message":"Dispatch paused; copy diagnostics from the dashboard for AI review."}}\n\n'
                    await send({'type':'http.response.body','body':body,'more_body':False})
        finally:
            CURRENT_REQUEST.reset(context)
            with anyio.CancelScope(shield=True):
                await guard.release(token)


async def observe_error_value(guard, raw):
    """Inspect explicit error objects only. A model mentioning a code is not an error."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return
    if not isinstance(value, dict) or 'choices' in value:
        return
    error = value.get('error')
    if not isinstance(error, dict):
        if value.get('code') not in (11128, '11128', 11140, '11140', 401, 403, 429):
            return
        error = value
    code = error.get('code')
    if type(code) not in (str, int):
        code = None
    reason = {11128:'channel_11128', '11128':'channel_11128',
              11140:'account_restricted', '11140':'account_restricted',
              401:'http_401',403:'http_403',429:'http_429',
              '401':'http_401','403':'http_403','429':'http_429'}.get(code, 'upstream_error')
    await guard.observe_failure(reason)


class SSEProtectionObserver:
    def __init__(self, guard):
        self.guard, self.pending = guard, bytearray()

    async def feed(self, chunk):
        self.pending.extend(chunk)
        while True:
            boundary = re.search(rb'\r\n\r\n|\n\n|\r\r', self.pending)
            if boundary is None:
                if len(self.pending) > 1024 * 1024:
                    self.pending.clear()
                    await self.guard.pause('repeated_failures')
                return
            event = bytes(self.pending[:boundary.start()])
            del self.pending[:boundary.end()]
            data = b'\n'.join(line[5:].lstrip(b' ') for line in event.splitlines()
                              if line.startswith(b'data:'))
            if data and data != b'[DONE]':
                await observe_error_value(self.guard, data)
