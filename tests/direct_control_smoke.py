"""Actual App Server: native steering preserves tools; Stop cancels them.

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
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from agentteams_codex.bridge import Bridge, Journal
from native_model_fixture import Model

action = sys.argv[1] if len(sys.argv) > 1 else 'steer'
assert action in {'stop', 'steer'}


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    home = root / '.codex'
    home.mkdir()
    workspace = root / 'workspace'
    workspace.mkdir()
    Model.pidfile, Model.marker = workspace / 'pid', workspace / 'must-not-run'
    release = workspace / 'release-tool'
    Model.tool_command = f'echo $$ > {Model.pidfile}; while [ ! -f {release} ]; do sleep 0.1; done; touch {Model.marker}'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ.update(CODEX_HOME=str(home), CODEX_GATEWAY_KEY='local-test-only', CODEX_SANDBOX_MODE='danger-full-access')
    (home / 'config.toml').write_text(f'model = "test-model"\nmodel_provider = "local"\n[model_providers.local]\nname = "fixture"\nbase_url = "http://127.0.0.1:{server.server_port}"\nenv_key = "CODEX_GATEWAY_KEY"\nwire_api = "responses"\n')
    bridge = Bridge.__new__(Bridge)
    bridge.name, bridge.user_id = 'agent', '@agent:test'
    bridge.home, bridge.workspace = home, workspace
    bridge.plugin = Path(os.environ.get('AT_SOURCE', '/opt/agentteams/transport')) / 'plugins/teamharness'
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
            if action == 'steer':
                deadline = time.monotonic() + 10
                while bridge.journal.data['events']['$supplement']['status'] != 'completed' and time.monotonic() < deadline:
                    time.sleep(0.025)
                assert bridge.journal.data['events']['$supplement']['status'] == 'completed', 'native steer was not accepted'
                assert bridge.active['event']['event_id'] == '$original'
                os.kill(int(Model.pidfile.read_text()), 0)
                assert not Model.marker.exists()
                release.touch()
            thread.join(15)
            assert not thread.is_alive(), 'native turn did not settle'
            assert not errors, errors
            pid = int(Model.pidfile.read_text())
            try:
                os.kill(pid, 0)
                stat = Path(f'/proc/{pid}/stat')
                assert stat.exists() and stat.read_text().split()[2] == 'Z', 'native exec survived interrupt'
            except ProcessLookupError:
                pass
            statuses = [c.args[1]['com.agentteams.execution']['status'] for c in bridge.matrix.send_content.call_args_list]
            if action == 'steer':
                assert Model.marker.exists(), 'steer cancelled the native tool'
                assert statuses == ['running', 'completed'], statuses
                assert bridge.journal.data['events']['$supplement']['status'] == 'completed'
                assert bridge.journal.data['events']['$original']['turn_id']
                assert any('ORIGINAL_TASK' in json.dumps(r) and 'SUPPLEMENT_HANGZHOU' in json.dumps(r) for r in Model.requests)
                print('PASS: native Codex steer kept the turn and tool alive, and the next inference received the supplement')
            else:
                assert not Model.marker.exists()
                assert statuses == ['running', 'stopping', 'stopped'], statuses
            if action == 'stop':
                assert '$supplement' not in bridge.journal.data['events']
                bridge.journal.ingest([{**event, 'event_id': '$supplement', 'body': 'SUPPLEMENT_HANGZHOU'}], 'next')
                record = bridge.journal.data['events']['$supplement']
                assert record['status'] == 'queued', record
                bridge.process('$supplement', record)
                assert record['status'] == 'completed', record
                print('PASS: native Codex Stop cancelled the tool and the same thread accepted a later message')
    finally:
        bridge.stop.set()
        server.shutdown()
