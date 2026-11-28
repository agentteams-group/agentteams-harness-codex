"""Real SDK admission/receipt checkpoint against an isolated canonical HTTP ledger.

This is a native boundary fixture, not a real-task certification run.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agentteams_codex.admission import Scope, ENV, COMMAND_ENV
from agentteams_codex.client import Client
from agentteams_codex.config import prepare
from agentteams_codex.task_protocol import CONTEXT_ENV
from agentteams_codex.native_input import saved_message
from native_task_protocol_smoke import Collector


class Ledger(BaseHTTPRequestHandler):
    blocked = False
    calls = 0
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path == '/api/v1/tasks/native-operations' and payload['mode'] == 'admit'
        type(self).calls += 1
        result = {'mode': 'needs_verification', 'authority': {'state': 'unknown'}} if self.blocked else {
            'mode': 'admitted', 'action': 'create', 'sourceEventId': '$native-origin',
            'result': {'sourceEventId': '$native-origin',
                       'task': {'id': 'native-main', 'kind': 'main', 'status': 'planned'},
                       'notification': {'eventId': 'own-notice', 'status': 'queued', 'recipient': 'leader'}}}
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(result).encode())
    def log_message(self, *_):
        pass


class Model(BaseHTTPRequestHandler):
    requests = []
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).requests.append(request)
        assert any(item.get('role') == 'developer' and 'Task system receipt' in json.dumps(item)
                   for item in request['input']), 'Canonical receipt missing from actual model context'
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        item = {'id': 'own-result', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'own completed response', 'annotations': []}]}
        response = {'id': 'own-response', 'object': 'response', 'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
        for kind, values in [('response.created', {'response': {**response, 'status': 'in_progress'}}),
                             ('response.output_item.added', {'output_index': 0, 'item': item}),
                             ('response.output_item.done', {'output_index': 0, 'item': item}),
                             ('response.completed', {'response': response})]:
            self.wfile.write(('event: '+kind+'\ndata: '+json.dumps({'type': kind, **values})+'\n\n').encode())
        self.wfile.flush()
    def log_message(self, *_):
        pass


def completed(client, turn):
    while True:
        event = client.next_event(30)
        if event.get('method') == 'turn/completed' and event['params']['turn']['id'] == turn['id']:
            return event['params']['turn']['status']


def main():
    servers = [ThreadingHTTPServer(('127.0.0.1', 0), handler) for handler in (Ledger, Model, Collector)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='own-native-admission-') as directory:
            root = Path(directory); home = root/'.codex'; workspace = root/'workspace'
            (root/'runtime').mkdir()
            source = Path(os.environ['AT_SOURCE']).resolve(); plugin = source/'plugins/teamharness'
            context = {'role': 'manager', 'sourceEvent': '$native-origin', 'sources': [{
                'eventId': '$native-origin', 'roomId': '!own:test', 'userId': '@own:test'}],
                'retained': '原始任务记录' * 20000}
            scope = Scope(home, plugin, context)
            config = {'member': {'role': 'manager', 'runtime': 'codex'}, 'desired': {'model': {
                'model': 'own-model', 'gatewayUrl': f'http://127.0.0.1:{servers[1].server_port}'}}}
            os.environ.update(AGENTTEAMS_OTEL_ENABLED='true',
                OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=f'http://127.0.0.1:{servers[2].server_port}/v1/traces',
                OTEL_EXPORTER_OTLP_TRACES_HEADERS='')
            model, instructions = prepare(config, home, workspace, plugin,
                trace_attributes={'agentteams.event.id': '$native-origin', 'agentteams.room.id': '!own:test'})
            env = {**os.environ, 'HOME': str(root), 'CODEX_HOME': str(home), 'CODEX_GATEWAY_KEY': 'own-fixture',
                   'AGENTTEAMS_AUTH_TOKEN': 'own-fixture', 'AGENTTEAMS_PLUGIN_DIR': str(plugin),
                   'AGENTTEAMS_CONTROLLER_URL': f'http://127.0.0.1:{servers[0].server_port}',
                   ENV: str(scope.path), COMMAND_ENV: json.dumps([sys.executable, '-m', 'agentteams_codex.admission']),
                   'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
            for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', CONTEXT_ENV):
                env.pop(key, None)
            command = json.loads(os.environ['CODEX_NATIVE_COMMAND'])
            with Client(workspace, command=command, env=env,
                        trace_context={'traceparent': '00-123456789012345678901234567890ab-12345678901234ab-01'}) as client:
                thread = client.open_thread(None, workspace, model, instructions)['id']
                assert completed(client, client.start(thread, 'Own request')) == 'completed'
                scope.require_admitted()
                observed = json.loads(scope.path.read_text())['observed']
                assert len(observed) == 1
                assert (root/'runtime/codex-state.tar.gz').exists()
                receipt = next(item for item in Model.requests[0]['input'] if item.get('role') == 'developer'
                               and 'Task system receipt' in json.dumps(item))
                assert saved_message(home, thread, 'developer', receipt['content'][0]['text'])
                Ledger.blocked = True
                completed(client, client.start(thread, 'Must not enter model while outcome unknown'))
                try:
                    scope.require_admitted()
                except RuntimeError as error:
                    assert 'admission blocked' in str(error)
                else:
                    raise AssertionError('Native framework refusal was reported as success')
                assert len(Model.requests) == 1, 'Unknown canonical outcome reached actual generation'
            with Client(workspace, command=command, env=env,
                        trace_context={'traceparent': '00-123456789012345678901234567890ab-12345678901234ab-01'}) as restored:
                restored.open_thread(thread, workspace, model, instructions)
                completed(restored, restored.start(thread, 'Restart must retain the canonical fence'))
                try:
                    scope.require_admitted()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('Restart lost the native framework blocked outcome')
                assert len(Model.requests) == 1
            deadline = time.monotonic() + 10
            while not Collector.payloads and time.monotonic() < deadline:
                time.sleep(0.1)
            spans = [span for batch in Collector.payloads for resource in batch.get('resourceSpans', [])
                     for scope_spans in resource.get('scopeSpans', []) for span in scope_spans.get('spans', [])]
            generations = [span for span in spans if span['name'] == 'gen_ai.chat']
            assert len(generations) == 1, 'Blocked native turns entered another actual generation'
            assert generations[0]['traceId'] == '123456789012345678901234567890ab'
            assert 'own completed response' not in json.dumps(Collector.payloads)
            evidence = os.getenv('CODEX_NATIVE_TRACE_EVIDENCE')
            if evidence:
                Path(evidence).write_text(json.dumps(Collector.payloads, indent=2)+'\n')
            print(json.dumps({'nativeGenerationSpans': len(generations), 'trustedTraceBound': True, 'nativeModelRequests': len(Model.requests), 'canonicalHTTPChecks': Ledger.calls,
                              'durableFrameworkReceipt': True, 'blockedOutcomeFromPrivateCallback': True, 'unknownBlocksModelAndRestart': True,
                              'realTaskCertification': False}))
    finally:
        for server in servers:
            server.shutdown()


if __name__ == '__main__':
    main()
