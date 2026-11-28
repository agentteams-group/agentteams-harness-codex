"""Native deferred history injection fixture; not a real-task certification run."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import selectors
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agentteams_codex.bridge import Bridge, Journal
from agentteams_codex.client import Client
from agentteams_codex.config import prepare
from agentteams_codex.native_input import saved_message
from agentteams_codex.task_protocol import CONTEXT_ENV


class Gateway(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        Gateway.calls += 1
        self.send_error(500, 'History injection must not enter the model')

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='native-handoff-history-') as directory:
            root = Path(directory)
            home, workspace = root / '.codex', root / 'work'
            home.mkdir(); workspace.mkdir()
            plugin = Path('/opt/agentteams/plugins/teamharness')
            config = {'member': {'role': 'worker', 'runtime': 'codex'}, 'desired': {'model': {
                'model': 'fixture', 'gatewayKey': 'fixture', 'gatewayUrl': f'http://127.0.0.1:{server.server_port}'}}}
            env = {**os.environ, 'HOME': str(root), 'CODEX_HOME': str(home), 'CODEX_GATEWAY_KEY': 'fixture',
                   'AGENTTEAMS_PLUGIN_DIR': str(plugin), CONTEXT_ENV: json.dumps({'role': 'worker', 'sourceEvent': '$fixture', 'sources': []})}
            env.pop('AGENTTEAMS_STORAGE_PREFIX', None)
            with patch.dict(os.environ, env, clear=True):
                model, instructions = prepare(config, home, workspace, plugin)
                with Client(workspace, env=env) as client:
                    thread = client.open_thread(None, workspace, model, instructions)['id']
                    client.request('thread/inject_items', {'threadId': thread, 'items': [
                        {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Fixture original input'}]}]})
                    for _ in range(40):
                        if saved_message(home, thread, 'user', 'Fixture original input'):
                            break
                        time.sleep(.05)
                bridge = Bridge.__new__(Bridge)
                bridge.home, bridge.workspace, bridge.plugin = home, workspace, plugin
                bridge.config, bridge.user_id = config, '@fixture:test'
                bridge.runtime_path = root / 'runtime' / 'runtime.yaml'
                bridge.runtime_path.parent.mkdir()
                bridge.journal = Journal(root / 'journal.json')
                bridge.journal.data['handoff_context'] = {'$fixture': {
                    'thread_id': thread, 'scope_file': str(root / 'expired-private-scope'), 'observed': {},
                    'outgoing': {'task': {'id': 'node', 'submissionId': 'submission'},
                                 'notification': {'status': 'queued', 'eventId': 'outgoing'}}}}
                bridge.journal.save()
                fact = '[Task handoff receipt]\n{"eventId":"outgoing","matrixEventId":"$sent","status":"sent"}\nAlready notified. No action or further reasoning is needed.'
                native = SimpleNamespace(controller_json=lambda *_: None,
                    dispatched_receipts=lambda *_args, **_kw: [{'receiptId': json.dumps(['dispatch', 'outgoing', 'matrix:$sent']), 'fact': fact}])
                # Restore the queue as another process would, without resuming work.
                bridge.journal = Journal(bridge.journal.path)
                with patch('agentteams_codex.bridge.common', return_value=native):
                    bridge.flush_handoff_context()
                assert saved_message(home, thread, 'developer', fact), 'Native row absent after real SDK injection'
                assert not bridge.journal.data['handoff_context'], 'Confirmed injection was not acknowledged durably'
                assert Gateway.calls == 0, 'History injection started a model request'
                assert (bridge.runtime_path.parent / 'codex-state.tar.gz').exists()
                # A late assignment attaches to an already completed native
                # turn. No admission environment survives from that turn.
                os.environ.pop(CONTEXT_ENV, None)
                work = {'kind': 'task', 'task_id': 'node', 'team_id': 'fixture'}
                task = {'task_id': 'node', 'room_id': '!fixture:test', 'execution': {
                    'process_id': 'fixture-process', 'state': 'active', 'generation': 1}}
                matrix = SimpleNamespace(
                    native_input=lambda key: {'input': {'intent': 'task_notification', 'work': work,
                        'room_id': '!fixture:test', 'event_id': '$late', 'input': {'sender': '@leader:test'}}},
                    recovery=SimpleNamespace(process='fixture-process', current=lambda *args: task))
                late_text = 'Actual late assignment source, attached without another model turn.'
                def attach(client, native_thread, event):
                    client.request('thread/inject_items', {'threadId': native_thread, 'items': [
                        {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': late_text}]}]})
                bridge.managed = SimpleNamespace(matrix=matrix, classify=lambda *args: ('task_notification', 'node'),
                                                  commit_for_turn=attach)
                bridge.journal.data['events']['recovery:fixture/node:1'] = {'status': 'completed'}
                with patch('agentteams_codex.bridge.read_runtime', return_value=config), \
                        patch('agentteams_codex.bridge.task_thread', return_value=(thread, {})), \
                        patch('agentteams_codex.bridge.rollout_digest', return_value=None):
                    assert bridge.attach_late_assignment({'shared_key': 'late', 'event_id': '$late', 'room_id': '!fixture:test'})
                for _ in range(40):
                    if saved_message(home, thread, 'user', late_text):
                        break
                    time.sleep(.05)
                assert saved_message(home, thread, 'user', late_text), 'Actual late source absent in native journal'
                assert Gateway.calls == 0, 'Late assignment invoked a model'
                missing_scope = root / 'not-yet-attached.json'
                stdio_env = {**os.environ, 'AGENTTEAMS_CODEX_NATIVE_SCOPE': str(missing_scope)}
                stdio_env.pop(CONTEXT_ENV, None)
                with subprocess.Popen([sys.executable, '-m', 'agentteams_codex.task_protocol'],
                        env=stdio_env, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True) as child:
                    def request(identity, method):
                        child.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': identity, 'method': method}) + '\n')
                        child.stdin.flush()
                        with selectors.DefaultSelector() as selector:
                            selector.register(child.stdout, selectors.EVENT_READ)
                            assert selector.select(10), 'stdio response unavailable'
                        response = json.loads(child.stdout.readline())
                        assert response['id'] == identity
                        return response
                    assert 'result' in request(1, 'initialize')
                    assert request(2, 'tools/list')['error']['code'] == -32000
                    assert child.poll() is None, 'Missing context exited the Task server'
                    missing_scope.write_text(json.dumps({'context': {'role': 'worker', 'sourceEvent': '$reattached', 'sources': []}}))
                    assert request(3, 'tools/list')['result']['tools'][0]['name'] == 'task'
                    child.stdin.close()
                    assert child.wait(timeout=10) == 0
                print(json.dumps({'nativeHistoryInjection': True, 'queueRestore': True, 'nativeArchiveSaved': True,
                                  'lateAssignmentWithoutInheritedContext': True, 'stdioContextFailureRecoveredInSameProcess': True, 'nativeModelRequests': Gateway.calls, 'realTaskCertification': False}))
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
