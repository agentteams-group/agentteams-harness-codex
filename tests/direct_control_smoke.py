"""Actual packaged App Server, native exec cancellation and thread continuation.

The Responses endpoint is a deterministic fixture; tools run in this disposable container.
"""
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from agentteams_codex.bridge import Bridge, Journal

action = sys.argv[1] if len(sys.argv) > 1 else 'steer'
assert action in {'stop', 'steer'}


class Model(BaseHTTPRequestHandler):
    sent_tool = False
    requests = []

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).requests.append(request)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        def send(kind, **value):
            self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **value}) + '\n\n').encode())
            self.wfile.flush()
        rid = 'resp_' + str(len(self.requests))
        response = {'id': rid, 'object': 'response', 'status': 'in_progress', 'output': []}
        send('response.created', response=response)
        if not type(self).sent_tool:
            type(self).sent_tool = True
            names = [t.get('name') for t in request.get('tools', [])]
            assert 'exec_command' in names, names
            item = {'id': 'fc_control', 'type': 'function_call', 'call_id': 'call_control',
                'name': 'exec_command', 'arguments': json.dumps({'cmd': f'echo $$ > {self.pidfile}; sleep 30; touch {self.marker}', 'yield_time_ms': 10000})}
        else:
            item = {'id': 'msg_continued', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'CONTINUED_OK', 'annotations': []}]}
        send('response.output_item.added', output_index=0, item=item)
        send('response.output_item.done', output_index=0, item=item)
        send('response.completed', response={**response, 'status': 'completed', 'output': [item],
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}})

    def log_message(self, *_args):
        pass


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    home = root / '.codex'
    home.mkdir()
    workspace = root / 'workspace'
    workspace.mkdir()
    Model.pidfile, Model.marker = workspace / 'pid', workspace / 'must-not-run'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ.update(CODEX_HOME=str(home), CODEX_GATEWAY_KEY='local-test-only', CODEX_SANDBOX_MODE='danger-full-access')
    (home / 'config.toml').write_text(f'model = "test-model"\nmodel_provider = "local"\n[model_providers.local]\nname = "fixture"\nbase_url = "http://127.0.0.1:{server.server_port}"\nenv_key = "CODEX_GATEWAY_KEY"\nwire_api = "responses"\n')
    bridge = Bridge.__new__(Bridge)
    bridge.name, bridge.user_id = 'agent', '@agent:test'
    bridge.home, bridge.workspace, bridge.plugin = home, workspace, root
    bridge.runtime_path = root / 'runtime.yaml'
    bridge.runtime_path.write_text('member:\n  runtime: codex\n')
    bridge.config, bridge.agent_ids, bridge.rooms = {}, set(), {'!dm:test': 'personal'}
    bridge.journal = Journal(root / 'journal.json')
    bridge.controls, bridge.stop, bridge.pending_requests, bridge.active = queue.Queue(), threading.Event(), {}, None
    bridge.matrix = Mock()
    bridge.matrix.request.return_value = {'joined': {'@agent:test': {}, '@human:test': {}}}
    bridge.matrix.send_text.return_value = '$response'
    event = {'event_id': '$original', 'sender': '@human:test', 'room_id': '!dm:test', 'body': 'ORIGINAL_TASK', 'kind': 'text'}
    bridge.journal.ingest([event], 'cursor')
    errors = []
    def run():
        try:
            bridge.process('$original', bridge.journal.data['events']['$original'])
        except Exception as error:
            errors.append(error)
    try:
        with patch('agentteams_codex.bridge.prepare', return_value=('test-model', 'Use tools for the request.')):
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 30
            while not Model.pidfile.exists() and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert Model.pidfile.exists(), ('native exec never started', errors, Model.requests)
            deadline = time.monotonic() + 5
            while not bridge.journal.data['events']['$original'].get('input_checkpointed') and time.monotonic() < deadline:
                time.sleep(0.01)
            assert bridge.journal.data['events']['$original'].get('input_checkpointed'), 'Native input was not checkpointed while tools were running'
            control = {**event, 'event_id': '$supplement', 'body': 'SUPPLEMENT_HANGZHOU',
                'execution_control': {'action': action, 'agent_id': '@agent:test', 'run_id': '$original'}}
            assert bridge.handle_control(control)
            thread.join(15)
            assert not thread.is_alive(), 'interrupt did not settle'
            assert not errors, errors
            pid = int(Model.pidfile.read_text())
            try:
                os.kill(pid, 0)
                stat = Path(f'/proc/{pid}/stat')
                assert stat.exists() and stat.read_text().split()[2] == 'Z', 'native exec survived interrupt'
            except ProcessLookupError:
                pass
            assert not Model.marker.exists()
            statuses = [c.args[1]['com.agentteams.execution']['status'] for c in bridge.matrix.send_content.call_args_list]
            assert statuses == ['running', 'stopping', 'stopped'], statuses
            if action == 'stop':
                assert '$supplement' not in bridge.journal.data['events']
                bridge.journal.ingest([{**event, 'event_id': '$supplement', 'body': 'SUPPLEMENT_HANGZHOU'}], 'next')
            record = bridge.journal.data['events']['$supplement']
            assert record['status'] == 'queued', record
            bridge.process('$supplement', record)
            assert record['status'] == 'completed', record
            assert any('ORIGINAL_TASK' in json.dumps(r) and 'SUPPLEMENT_HANGZHOU' in json.dumps(r) for r in Model.requests)
            print('PASS: native Codex exec stopped, confirmed badge metadata, same thread continued with original and supplementary inputs')
    finally:
        bridge.stop.set()
        server.shutdown()
