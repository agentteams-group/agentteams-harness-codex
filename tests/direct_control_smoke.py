"""Actual App Server: native steering preserves tools; Stop cancels them.

The Responses endpoint is a deterministic fixture; tools run in this disposable container.
"""
import json
from contextlib import nullcontext
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import sys
from http.server import ThreadingHTTPServer
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from unittest.mock import Mock, patch

from agentteams_codex.bridge import Bridge, Journal
from native_model_fixture import Model
from managed_direct_fixture import Controller, initialize
from agentteams_codex.managed_matrix import ManagedMatrix
from agentteams_codex.config import read_runtime

action = sys.argv[1] if len(sys.argv) > 1 else 'steer'
assert action in {'stop', 'steer'}
role = sys.argv[2] if len(sys.argv) > 2 else 'worker'
assert role in {'worker', 'leader', 'manager'}


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
    controller = ThreadingHTTPServer(('127.0.0.1', 0), Controller)
    initialize(controller, root / 'storage')
    threading.Thread(target=controller.serve_forever, daemon=True).start()
    token = root / 'token'
    token.write_text('fixture')
    os.environ.update(AGENTTEAMS_CONTROLLER_URL=f'http://127.0.0.1:{controller.server_port}',
        AGENTTEAMS_PROCESS_ID='fixture-process', AGENTTEAMS_AUTH_TOKEN_FILE=str(token),
        AGENTTEAMS_AGENT_ROLE=role, AGENTTEAMS_LOG_ROLE=role, AGENTTEAMS_LOG_RUNTIME='codex',
        AGENTTEAMS_WORKER_NAME='agent',
        AGENTTEAMS_WORKER_MATRIX_TOKEN='fixture', AGENTTEAMS_WORKER_GATEWAY_KEY='fixture',
        AGENTTEAMS_MATRIX_DOMAIN='test', AGENTTEAMS_STORAGE_PREFIX=str(root / 'storage'))
    os.environ.update(CODEX_HOME=str(home), CODEX_GATEWAY_KEY='local-test-only', CODEX_SANDBOX_MODE='danger-full-access')
    (home / 'config.toml').write_text(f'model = "test-model"\nmodel_provider = "local"\n[model_providers.local]\nname = "fixture"\nbase_url = "http://127.0.0.1:{server.server_port}"\nenv_key = "CODEX_GATEWAY_KEY"\nwire_api = "responses"\n')
    bridge = Bridge.__new__(Bridge)
    bridge.name, bridge.user_id = 'agent', '@agent:test'
    bridge.home, bridge.workspace = home, workspace
    bridge.plugin = Path(os.environ.get('AGENTTEAMS_PLUGIN_DIR', '/opt/agentteams/plugins/teamharness'))
    bridge.runtime_path = root / 'runtime.yaml'
    bridge.runtime_path.write_text(json.dumps({'apiVersion': 'agentteams.io/v1beta1', 'kind': 'MemberRuntimeConfig',
        'member': {'runtime': 'codex', 'runtimeName': 'agent', 'role': role,
            'matrixUserId': '@agent:test', 'personalRoomId': '!dm:test'},
        'storage': {'memberPrefix': 'agents/agent'},
        'matrix': {'transport': {'homeserver': 'http://matrix.invalid', 'dm': {'policy': 'open'}}},
        'desired': {'model': {'model': 'test-model', 'gatewayUrl': f'http://127.0.0.1:{server.server_port}'}}}))
    bridge.config, bridge.agent_ids, bridge.rooms = read_runtime(bridge.runtime_path), set(), {'!dm:test': 'personal'}
    bridge.journal = Journal(root / 'journal.json')
    bridge.manager_receipts = None
    bridge.controls, bridge.stop, bridge.pending_requests, bridge.active = queue.Queue(), threading.Event(), {}, None
    bridge.matrix = Mock()
    def matrix_request(method, path, *args):
        if '/context/' in path:
            event_id = unquote(urlsplit(path).path.rsplit('/', 1)[-1])
            return {'event': {'event_id': event_id}, 'events_before': []}
        if '/state/m.room.encryption/' in path:
            raise HTTPError(path, 404, 'Absent', {}, BytesIO(b'{"errcode":"M_NOT_FOUND"}'))
        if path.endswith('/account/whoami'):
            return {'user_id': '@agent:test', 'device_id': 'fixture-device'}
        return {'joined': {'@agent:test': {}, '@human:test': {}}}
    bridge.matrix.request.side_effect = matrix_request
    bridge.matrix.send_content.return_value = '$response'
    bridge.managed = ManagedMatrix(bridge, lambda *_args: []) if role != 'manager' else None
    if bridge.managed:
        bridge.managed.joined = {'!dm:test'}
    bridge.matrix.send_text.return_value = '$response'
    event = {'event_id': '$original', 'sender': '@agent:test' if role == 'manager' else '@human:test', 'room_id': '!dm:test', 'body': 'ORIGINAL_TASK', 'kind': 'text'}
    if bridge.managed:
        event['shared_key'] = bridge.managed.matrix.accept(event['room_id'], event['event_id'],
            {'sender': event['sender'], 'content': {'msgtype': 'm.text', 'body': event['body']}}, 'message')
    bridge.journal.ingest([event], 'cursor')
    errors = []
    def run():
        try:
            bridge.process('$original', bridge.journal.data['events']['$original'])
        except Exception as error:
            errors.append(error)
    try:
        # This native control fixture has no Task inputs. Actual Manager input
        # discovery is covered separately through the Dashboard/Controller.
        def prepared_manager(_plugin, _role, source):
            return {'role': 'manager', 'sourceEvent': source['event_id'],
                    'sourceRoom': source['room_id'], 'sources': []}, '', None
        prepared = patch('agentteams_codex.bridge.prepare_turn', side_effect=prepared_manager
        ) if role == 'manager' else nullcontext()
        with patch('agentteams_codex.bridge.prepare', return_value=('test-model', 'Use tools for the request.')), prepared:
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
            control['sender'] = '@human:test'
            if bridge.managed:
                control['shared_key'] = bridge.managed.matrix.accept(control['room_id'], control['event_id'],
                    {'sender': control['sender'], 'content': {'msgtype': 'm.text', 'body': control['body']}}, 'message')
                assert bridge.managed.control(control)
            else:
                assert bridge.handle_control(control)
            if action == 'steer':
                deadline = time.monotonic() + 10
                while bridge.journal.data['events']['$supplement']['status'] != 'completed' and time.monotonic() < deadline:
                    time.sleep(0.025)
                assert bridge.journal.data['events']['$supplement']['status'] == 'completed', ('native steer was not accepted', bridge.matrix.send_text.call_args_list, errors)
                assert bridge.active['event']['event_id'] == '$original'
                os.kill(int(Model.pidfile.read_text()), 0)
                assert not Model.marker.exists()
                release.touch()
            thread.join(15)
            assert not thread.is_alive(), 'native turn did not settle'
            assert not errors, errors
            if bridge.managed:
                assert controller.claims == ['$original', '$supplement'], controller.claims
                assert controller.saved_sessions, 'No actual native session checkpoint was published'
                execution = controller.inputs[event['shared_key']]['execution']
                cp = execution['checkpoint']
                if action == 'stop':
                    assert execution['state'] == 'cancelled' and not cp.get('result'), execution
                    assert bridge.journal.data['events']['$original']['status'] == 'completed'
                else:
                    assert cp['delivery'] == 'delivered', cp
            else:
                assert not controller.claims, 'Manager must not fabricate a Worker execution'
            pid = int(Model.pidfile.read_text())
            try:
                os.kill(pid, 0)
                stat = Path(f'/proc/{pid}/stat')
                assert stat.exists() and stat.read_text().split()[2] == 'Z', 'native exec survived interrupt'
            except ProcessLookupError:
                pass
            statuses = [c.args[1]['com.agentteams.execution']['status'] for c in bridge.matrix.send_content.call_args_list if 'com.agentteams.execution' in c.args[1]]
            if action == 'steer':
                assert Model.marker.exists(), 'steer cancelled the native tool'
                assert statuses == ['running', 'completed'], statuses
                assert bridge.journal.data['events']['$supplement']['status'] == 'completed'
                assert bridge.journal.data['events']['$original']['turn_id']
                assert any('ORIGINAL_TASK' in json.dumps(r) and 'SUPPLEMENT_HANGZHOU' in json.dumps(r) for r in Model.requests)
                print(f'PASS: {role} native Codex steer kept the turn and tool alive, and the next inference received the supplement')
            else:
                assert not Model.marker.exists()
                assert statuses == ['running', 'stopping', 'stopped'], statuses
            if action == 'stop':
                assert '$supplement' not in bridge.journal.data['events']
                followup = {**event, 'event_id': '$followup', 'body': 'SUPPLEMENT_HANGZHOU'}
                followup['sender'] = '@human:test'
                if bridge.managed:
                    followup['shared_key'] = bridge.managed.matrix.accept(followup['room_id'], followup['event_id'],
                        {'sender': followup['sender'], 'content': {'msgtype': 'm.text', 'body': followup['body']}}, 'message')
                bridge.journal.ingest([followup], 'next')
                record = bridge.journal.data['events']['$followup']
                assert record['status'] == 'queued', record
                bridge.process('$followup', record)
                assert record['status'] == 'completed', record
                print('PASS: native Codex Stop cancelled the tool and the same thread accepted a later message')
    finally:
        release.touch()
        bridge.stop.set()
        server.shutdown()
        controller.shutdown()
