import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path


class SearchSkillTests(unittest.TestCase):
    def test_private_config_client_is_independent_and_keeps_credentials_out_of_output(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path,self.headers.get('Authorization'),
                    json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                data=json.dumps({'content':'A fact','sources':[{'title':'Source',
                    'url':'https://example.com/official'}],'queries':['test']}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self,*args):
                pass
        with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server,tempfile.TemporaryDirectory() as temp:
            worker=threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            try:
                config=Path(temp)/'config.json'
                config.write_text(json.dumps({'base_url':f'http://127.0.0.1:{server.server_port}/v1',
                    'api_key':'fixture-private-key','model':'search-model'}),encoding='utf-8')
                env={k:v for k,v in os.environ.items() if not k.startswith('GATEWAY_')}
                script=Path(__file__).resolve().parents[2]/'skills/gateway-web-search/scripts/search.py'
                run=subprocess.run([sys.executable,str(script),'--config',str(config),'--query','test'],
                    env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(run.returncode,0,run.stderr)
                self.assertEqual(json.loads(run.stdout)['sources'][0]['url'],'https://example.com/official')
                self.assertNotIn('fixture-private-key',run.stdout+run.stderr)
                self.assertEqual(seen[0][0],'/v1/search')
                self.assertEqual(seen[0][1],'Bearer fixture-private-key')
                self.assertEqual(seen[0][2]['model'],'search-model')
            finally:
                server.shutdown()
                worker.join(timeout=3)
