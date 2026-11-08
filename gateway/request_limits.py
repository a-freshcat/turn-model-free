"""Authenticate API calls before body parsing and bound every buffered request."""
import hmac
import os

from starlette.responses import JSONResponse


class RequestLimitsMiddleware:
    def __init__(self, app, max_bytes=None):
        self.app = app
        try:
            configured = int(os.environ.get('GATEWAY_MAX_REQUEST_BYTES', 32 * 1024 * 1024))
        except ValueError:
            configured = 32 * 1024 * 1024
        self.max_bytes = max_bytes if max_bytes is not None else max(1024, min(configured, 128 * 1024 * 1024))

    async def reject(self, scope, receive, send, status, code, message):
        await JSONResponse({'error': {'message': message, 'type': 'invalid_request_error',
            'param': None, 'code': code}}, status_code=status, headers={'WWW-Authenticate': 'Bearer'} if status == 401 else None)(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        headers = dict(scope.get('headers', []))
        protected = ((path.startswith('/v1/') and not path.startswith('/v1/images/files/'))
                     or path.startswith('/admin/'))
        if protected:
            scheme, _, token = headers.get(b'authorization', b'').partition(b' ')
            expected = getattr(getattr(scope.get('app'), 'state', None), 'api_key', b'')
            if not expected or scheme.lower() != b'bearer' or not hmac.compare_digest(token, expected):
                return await self.reject(scope, receive, send, 401, 'invalid_api_key',
                                         'Invalid or missing gateway API key.')
        if scope.get('method') not in ('POST', 'PUT', 'PATCH'):
            return await self.app(scope, receive, send)
        try:
            length = int(headers.get(b'content-length', b'0'))
            if length < 0:
                raise ValueError()
        except ValueError:
            return await self.reject(scope, receive, send, 400, 'invalid_request', 'Invalid Content-Length.')
        if length > self.max_bytes:
            return await self.reject(scope, receive, send, 413, 'request_too_large', 'Request body exceeds the size limit.')
        body = bytearray()
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect':
                return
            chunk = event.get('body', b'')
            if len(body) + len(chunk) > self.max_bytes:
                return await self.reject(scope, receive, send, 413, 'request_too_large', 'Request body exceeds the size limit.')
            body.extend(chunk)
            if not event.get('more_body', False):
                break
        payload = bytes(body)
        body.clear()
        delivered = False
        async def replay():
            nonlocal delivered, payload
            if not delivered:
                delivered = True
                data, payload = payload, b''
                return {'type': 'http.request', 'body': data, 'more_body': False}
            return await receive()
        await self.app(scope, replay, send)
