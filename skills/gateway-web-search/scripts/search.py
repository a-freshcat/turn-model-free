"""Standalone gateway search client; Python 3.10+, standard library only."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise ValueError('Gateway redirects are not followed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query',required=True)
    parser.add_argument('--max-results',type=int,default=8,choices=range(1,21),metavar='1..20')
    parser.add_argument('--base-url')
    parser.add_argument('--model')
    parser.add_argument('--config',type=Path,default=Path(__file__).resolve().parents[1]/'config.json')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config.is_file() else {}
    base = (args.base_url or os.getenv('GATEWAY_SEARCH_BASE_URL') or config.get('base_url') or
            os.getenv('GATEWAY_BASE_URL') or '').rstrip('/')
    key = (os.getenv('GATEWAY_SEARCH_API_KEY') or config.get('api_key') or os.getenv('GATEWAY_API_KEY') or '').strip()
    model = args.model or os.getenv('GATEWAY_SEARCH_MODEL') or config.get('model')
    if not base or not key:
        parser.error('Configure the gateway URL and API key in environment variables or private config.json.')
    url = urllib.parse.urlsplit(base)
    local = url.hostname in {'localhost','127.0.0.1','::1'}
    if (not url.hostname or (url.scheme!='https' and not (url.scheme=='http' and local)) or
            url.username or url.password or url.query or url.fragment):
        parser.error('Use remote HTTPS or loopback HTTP, without URL credentials or query parameters.')
    payload = {'query':args.query,'max_results':args.max_results}
    if model:
        payload['model'] = model
    endpoint = base+('/search' if base.endswith('/v1') else '/v1/search')
    request = urllib.request.Request(endpoint,data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=320) as response:
        raw = response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:
        raise ValueError('Search response exceeds 2 MiB.')
    result = json.loads(raw)
    if not isinstance(result.get('sources'),list) or not result['sources']:
        raise ValueError('Search has no verified source links.')
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':
    try:
        main()
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get('Retry-After','')
        hint = f' Retry after {retry} seconds.' if retry.isdigit() else ''
        print(f'Gateway search HTTP {exc.code}.{hint} Check credentials, model, quota or server status.',file=sys.stderr)
        raise SystemExit(1)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print('Search failed ('+type(exc).__name__+'); no successful web search was confirmed.',file=sys.stderr)
        raise SystemExit(1)
