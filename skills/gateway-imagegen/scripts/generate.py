"""Standalone image gateway client. Python 3.10+, standard library only."""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Gateway redirects are not followed; check the configured endpoint.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt-file', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--base-url', default=os.environ.get('GATEWAY_BASE_URL'))
    parser.add_argument('--api-key-env', default='GATEWAY_API_KEY')
    parser.add_argument('--model', default=os.environ.get('GATEWAY_IMAGE_MODEL'))
    parser.add_argument('--size', choices=['auto','1024x1024','1536x1024','1024x1536'],default='auto')
    parser.add_argument('--overwrite',action='store_true')
    args = parser.parse_args()
    key = os.environ.get(args.api_key_env,'').strip()
    if not key or not args.base_url:
        parser.error('Set GATEWAY_BASE_URL and the selected API key environment variable first.')
    url = urllib.parse.urlsplit(args.base_url)
    local = url.hostname in {'localhost','127.0.0.1','::1'}
    if (url.scheme != 'https' and not (local and url.scheme == 'http') or
            not url.hostname or url.username or url.password or url.query or url.fragment):
        parser.error('Use HTTPS for a remote gateway, or loopback HTTP; no URL credentials/query.')
    base = args.base_url.rstrip('/')
    endpoint = base+('/images/generations' if base.endswith('/v1') else '/v1/images/generations')
    payload = {'prompt':args.prompt_file.read_text(encoding='utf-8-sig'),
        'size':args.size,'response_format':'b64_json','n':1}
    if args.model:
        payload['model'] = args.model
    request = urllib.request.Request(endpoint,data=json.dumps(payload).encode(),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=320) as response:
        body = response.read(32*1024*1024+1)
    if len(body)>32*1024*1024:
        raise ValueError('Image response exceeds 32 MiB.')
    raw = base64.b64decode(json.loads(body)['data'][0]['b64_json'],validate=True)
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        suffix = '.png'
    elif raw.startswith(b'\xff\xd8\xff'):
        suffix = '.jpg'
    elif raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        suffix = '.webp'
    else:
        raise ValueError('Gateway did not return a supported image.')
    output = args.output.with_suffix(suffix).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('wb' if args.overwrite else 'xb') as file:
        file.write(raw)
    print(json.dumps({'path':str(output),'bytes':len(raw)},ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as exc:
        print(f'Gateway HTTP {exc.code}; check credentials, model, quota and Retry-After before retrying.',file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, KeyError, IndexError) as exc:
        print('Image generation failed ('+type(exc).__name__+'); no success was confirmed.',file=sys.stderr)
        raise SystemExit(1)
