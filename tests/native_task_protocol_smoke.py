"""Real pinned App Server/MCP/CLI binding with an isolated Responses fixture.

No production model, Matrix, native state or Controller is used. Run with
AT_SOURCE pointing to the matching Core and CODEX_NATIVE_COMMAND containing a
JSON argv for the pinned Codex App Server executable.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import gzip
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agentteams_codex.client import Client
from agentteams_codex.config import prepare
from agentteams_codex.task_protocol import CONTEXT_ENV, prepare_turn
from agentteams_codex.task_protocol import native_task_metadata
from agentteams_codex.native_telemetry import trace_attributes


class Collector(BaseHTTPRequestHandler):
    payloads = []
    def do_POST(self):
        data = self.rfile.read(int(self.headers['Content-Length']))
        if self.headers.get('Content-Encoding') == 'gzip':
            data = gzip.decompress(data)
        type(self).payloads.append(json.loads(data))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{}')
    def log_message(self, *_):
        pass


class Model(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).requests.append(request)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()

        def send(kind, **values):
            self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **values}) + '\n\n').encode())
            self.wfile.flush()

        response = {'id': 'resp_' + str(len(self.requests)), 'object': 'response', 'status': 'in_progress', 'output': []}
        send('response.created', response=response)
        if len(self.requests) <= 2:
            groups = [item for item in request.get('tools', []) if item.get('name') == 'mcp__teamharness']
            tools = [item for group in groups for item in group.get('tools', []) if item.get('name') == 'task']
            assert len(tools) == 1, 'Native Task schema is missing'
            schema = tools[0].get('parameters') or tools[0].get('input_schema')
            assert '_nativeSource' not in schema['properties']
            if len(self.requests) == 2:
                assert 'effect' in json.dumps(request.get('input', [])), 'Task rejection was not returned to the model'
            item = {'id': 'fc_task_' + str(len(self.requests)), 'type': 'function_call', 'call_id': 'call_task_' + str(len(self.requests)),
                    'namespace': 'mcp__teamharness', 'name': tools[0]['name'], 'arguments': json.dumps({'action': 'create', 'leader': 'leader', 'title': '' if len(self.requests) == 1 else 'Own native fixture'})}
        else:
            assert 'Task system receipt' in json.dumps(request.get('input', [])), 'Framework Task receipt is missing from native model input'
            item = {'id': 'msg_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'TASK_NATIVE_OK', 'annotations': []}]}
        send('response.output_item.added', output_index=0, item=item)
        send('response.output_item.done', output_index=0, item=item)
        send('response.completed', response={**response, 'status': 'completed', 'output': [item],
             'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}})

    def log_message(self, *_):
        pass


def main():
    with tempfile.TemporaryDirectory(prefix='own-codex-task-fixture-') as directory:
        root = Path(directory)
        source = Path(os.environ['AT_SOURCE']).resolve()
        workspace, home, binary = root / 'workspace', root / '.codex', root / 'bin'
        binary.mkdir()
        calls = root / 'cli-calls.jsonl'
        executable = binary / 'agt'
        executable.write_text('#!' + sys.executable + '\nimport json,os\n'
            'with open(' + repr(str(calls)) + ',"a") as target: target.write(json.dumps({"event":os.environ["AGENTTEAMS_ORIGIN_EVENT_ID"]})+"\\n")\n'
            'print(json.dumps({"sourceEventId":os.environ["AGENTTEAMS_ORIGIN_EVENT_ID"],"task":{"id":"main","kind":"main","status":"planned"},"notification":{"status":"queued","eventId":"notice","recipient":"leader"}}))\n')
        executable.chmod(0o700)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        collector = ThreadingHTTPServer(('127.0.0.1', 0), Collector)
        threading.Thread(target=collector.serve_forever, daemon=True).start()
        os.environ.update(AGENTTEAMS_OTEL_ENABLED='true', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=f'http://127.0.0.1:{collector.server_port}/v1/traces', OTEL_EXPORTER_OTLP_TRACES_HEADERS='')
        config = {'member': {'role': 'manager', 'runtime': 'codex', 'runtimeName': 'fixture-manager'},
                  'desired': {'model': {'model': 'test-model', 'gatewayUrl': f'http://127.0.0.1:{server.server_port}'}}}
        plugin = source / 'plugins/teamharness'
        event = {'event_id': '$own-native', 'room_id': '!own:test', 'sender': '@own:test'}
        context, fact, _ = prepare_turn(plugin, 'manager', event)
        model, instructions = prepare(config, home, workspace, plugin, trace_attributes=trace_attributes(config,event,context))
        env = {**os.environ, 'HOME': str(root), 'CODEX_HOME': str(home), 'CODEX_GATEWAY_KEY': 'own-fixture',
               'PATH': str(binary) + os.pathsep + os.environ['PATH'],
               'AGENTTEAMS_PLUGIN_DIR': str(plugin), CONTEXT_ENV: json.dumps(context),
               'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
        env.pop('HTTP_PROXY', None)
        env.pop('HTTPS_PROXY', None)
        try:
            with Client(workspace, command=json.loads(os.environ['CODEX_NATIVE_COMMAND']), env=env) as client:
                thread = client.open_thread(None, workspace, model, instructions + '\n' + fact)['id']
                turn = client.start(thread, 'Use your task tool to register the own fixture request.')
                completed = []
                while True:
                    notification = client.next_event(45)
                    if 'id' in notification and notification.get('method'):
                        raise AssertionError('Native Task unexpectedly requested approval/input: ' + notification['method'])
                    if notification.get('method') == 'item/completed':
                        completed.append(notification['params']['item'])
                    if notification.get('method') == 'turn/completed':
                        assert notification['params']['turn']['status'] == 'completed', notification
                        break
            assert [json.loads(line) for line in calls.read_text().splitlines()] == [{'event': '$own-native'}]
            assert any(item.get('type') == 'mcpToolCall' and item.get('status') == 'completed' for item in completed)
            assert any(native_task_metadata(item).get('effect') == 'none' for item in completed), 'Native SDK dropped the fixed Task effect classification'
            assert any(item.get('text') == 'TASK_NATIVE_OK' for item in completed)
            deadline = time.monotonic() + 10
            while not Collector.payloads and time.monotonic() < deadline:
                time.sleep(0.1)
            assert Collector.payloads, 'Native SDK did not export OTLP traces'
            spans = [span for batch in Collector.payloads for resource in batch.get('resourceSpans', [])
                     for scope in resource.get('scopeSpans', []) for span in scope.get('spans', [])]
            assert spans
            raw = json.dumps(Collector.payloads)
            for marker in ('Use your task tool to register the own fixture request.',
                           'Own native fixture', 'TASK_NATIVE_OK', 'sourceEventId',
                           'Task system receipt', 'gatewayKey'):
                assert marker not in raw, 'Native SDK exported synthetic content: ' + marker
            forbidden = {'arguments', 'output', 'input', 'prompt', 'body', 'payload',
                         'tool.arguments', 'tool.result', 'http.request.body', 'http.response.body'}
            attributes = [attribute for span in spans for attribute in span.get('attributes', [])]
            attributes += [attribute for span in spans for event in span.get('events', [])
                           for attribute in event.get('attributes', [])]
            assert not forbidden.intersection(attribute['key'] for attribute in attributes)
            assert any(attribute['key'] == 'gen_ai.usage.input_tokens' for attribute in attributes)
            assert any(span['name'] == 'mcp.tools.call' for span in spans)
            evidence = os.getenv('CODEX_NATIVE_TRACE_EVIDENCE')
            if evidence:
                Path(evidence).write_text(json.dumps(Collector.payloads, indent=2) + '\n')
            print(json.dumps({'nativeAppServerAndMCP': True, 'sourceBound': True, 'taskAuthorizationPrompt': False,
                              'frameworkReceiptInNativeToolContext': True, 'modelRequests': len(Model.requests),
                              'definitiveNoEffectMetadataPreserved': True,
                              'nativeOtlpSpans': len(spans), 'nativeSpanNames': sorted({span['name'] for span in spans}),
                              'productionModelOrTaskCalls': False, 'nativePrivateStateRead': False}))
        finally:
            server.shutdown()
            collector.shutdown()


if __name__ == '__main__':
    main()
