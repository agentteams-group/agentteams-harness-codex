"""Native deferred history injection fixture; not a real-task certification run."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

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
                'model': 'fixture', 'gatewayUrl': f'http://127.0.0.1:{server.server_port}'}}}
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
                print(json.dumps({'nativeHistoryInjection': True, 'queueRestore': True, 'nativeArchiveSaved': True,
                                  'nativeModelRequests': Gateway.calls, 'realTaskCertification': False}))
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
