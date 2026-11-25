"""Harness health wire tests. Only local HTTP fixtures; never real model credentials."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
adapter_path = ROOT / "health_adapter.py"
if not adapter_path.is_file():
    adapter_path = Path("/opt/agentteams/health_adapter.py")
spec = importlib.util.spec_from_file_location("health_adapter", adapter_path)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class Wire(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.server.requests.append((self.path, dict(self.headers), json.load(io.BytesIO(self.rfile.read(int(self.headers['Content-Length']))))))
        self.send_response(self.server.code)
        self.send_header('Content-Type', 'application/json')
        if self.server.code == 302:
            self.send_header('Location', self.server.base + '/private')
        self.end_headers()
        self.wfile.write(json.dumps(self.server.body).encode())


class HealthContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.temp)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Wire)
        self.server.base = 'http://127.0.0.1:' + str(self.server.server_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.server.code, self.server.requests = 200, []
        self.server.body = ({'type':'message','role':'assistant','content':[{'type':'text','text':'ok'}]} if adapter.WIRE == 'messages' else
            {'status':'completed','output':[{'content':[{'type':'output_text','text':'ok'}]}]} if adapter.WIRE == 'responses' else
            {'choices':[{'message':{'content':'ok'}}]})
        (self.home / 'runtime').mkdir()
        doc = {'desired':{'model':{'model':'selected-model','gatewayUrl':self.server.base + '/tenant'}},
               'credentials':{'gatewayKeyEnv':'TEST_MEMBER_KEY'}}
        (self.home / 'runtime/runtime.yaml').write_text(json.dumps(doc))
        self.native = self.home / 'native.json'
        self.native.write_text(json.dumps({'agents':{'defaults':{'model':{'primary':'private/selected-model'}}},
            'models':{'providers':{'private':{'baseUrl':self.server.base+'/tenant','apiKey':'fixture-key','api':'openai-completions'}}}}))
        self.enterContext(patch.dict(os.environ, {'HOME':self.temp,'AGENTTEAMS_WORKER_HOME':self.temp,
            'TEAMHARNESS_RUNTIME_CONFIG':str(self.home / 'runtime/runtime.yaml'),'OPENCLAW_CONFIG_PATH':str(self.native),
            'TEST_MEMBER_KEY':'fixture-key','CODEX_AUTH_MODE':'gateway','QODER_MODEL_ROUTE':'gateway'}))

    def test_effective_binding_and_native_wire_are_used_without_agent_execution(self):
        result = adapter.check(adapter.configuration())
        self.assertEqual(result, {'status':'ok','reason':'ok'})
        self.assertEqual(len(self.server.requests), 1)
        path, headers, payload = self.server.requests[0]
        suffix = {'chat':'/chat/completions','responses':'/responses','messages':'/messages'}[adapter.WIRE]
        self.assertEqual(path, '/tenant/v1' + suffix)
        self.assertEqual(headers.get('Authorization'), 'Bearer fixture-key')
        self.assertEqual(payload['model'], 'selected-model')
        self.assertNotIn('tools', payload)
        if adapter.WIRE == 'messages':
            self.assertEqual(headers.get('Anthropic-Version'), '2023-06-01')

    def test_errors_and_redirects_never_leak_or_become_success(self):
        for code, body, reason in [(401,{'error':'fixture-key'},'authentication'), (429,{},'rate_limit_or_quota'),
                (503,{},'upstream_unavailable'),(302,{},'configuration'),(200,{'error':{'message':'quota fixture-key'}},'billing_or_quota'),
                (200,{},'invalid_response')]:
            with self.subTest(code=code, reason=reason):
                self.server.code,self.server.body,self.server.requests = code,body,[]
                result=adapter.check(adapter.configuration())
                self.assertEqual(result,{'status':'failed','reason':reason})
                self.assertEqual(len(self.server.requests),1)
                self.assertNotIn('fixture-key',json.dumps(result))

    def test_configuration_identity_changes_without_returning_credentials(self):
        output=io.StringIO()
        with patch('sys.argv',['health','--configuration']),contextlib.redirect_stdout(output):
            adapter.main()
        digest=output.getvalue().strip()
        self.assertEqual(len(digest),64)
        self.assertNotIn('fixture-key',digest)
        self.assertEqual(self.server.requests,[])

    def test_readiness_is_not_model_inference(self):
        logs=self.home/'logs';logs.mkdir();(logs/'status.json').write_text(json.dumps({'boot_id':'current','state':'ready','evidence':'matrix_sync'}))
        response=Mock();response.status=200
        with patch.dict(os.environ,AGENTTEAMS_LOG_DIR=str(logs),AGENTTEAMS_BOOT_ID='current'), \
                patch.object(adapter.subprocess,'run',return_value=Mock(returncode=0,stdout=b'{"ok":true}')), \
                patch.object(adapter.urllib.request,'urlopen') as local:
            local.return_value.__enter__.return_value=response
            result=adapter.readiness()
        self.assertEqual(result['version'],1)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(self.server.requests,[])

    def test_missing_configuration_is_unknown(self):
        (self.home/'runtime/runtime.yaml').unlink();self.native.unlink()
        output=io.StringIO()
        with patch('sys.argv',['health','--json']),contextlib.redirect_stdout(output):
            adapter.main()
        self.assertEqual(json.loads(output.getvalue()),{'status':'unknown','reason':'configuration'})
        self.assertEqual(self.server.requests,[])


if __name__ == '__main__':
    unittest.main()
