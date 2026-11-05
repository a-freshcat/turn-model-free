import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class StandaloneSkillTests(unittest.TestCase):
    def test_stdlib_client_calls_images_and_saves_actual_format(self):
        seen = []
        raw = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path,self.headers['Authorization'],
                    json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                payload = json.dumps({'created':1,'data':[{'b64_json':base64.b64encode(raw).decode()}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self,*args):
                pass
        with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server, tempfile.TemporaryDirectory() as temp:
            worker = threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            try:
                directory = Path(temp)
                prompt = directory/'prompt.txt'
                prompt.write_text('A synthetic image',encoding='utf-8')
                env = dict(os.environ,GATEWAY_BASE_URL=f'http://127.0.0.1:{server.server_port}/v1',
                    GATEWAY_API_KEY='fixture-only',GATEWAY_IMAGE_MODEL='image-model')
                script = Path(__file__).resolve().parents[2]/'skills/gateway-imagegen/scripts/generate.py'
                result = subprocess.run([sys.executable,str(script),'--prompt-file',str(prompt),
                    '--output',str(directory/'output')],env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                saved = json.loads(result.stdout)
                self.assertEqual(Path(saved['path']).read_bytes(),raw)
                self.assertTrue(saved['path'].endswith('.png'))
                self.assertNotIn('fixture-only',result.stdout+result.stderr)
                self.assertEqual(seen[0][0],'/v1/images/generations')
                self.assertEqual(seen[0][2]['response_format'],'b64_json')
                self.assertEqual(seen[0][2]['model'],'image-model')
            finally:
                server.shutdown()
                worker.join(timeout=3)
