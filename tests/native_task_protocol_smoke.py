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

from agentteams_codex.client import Client, CodexError
from agentteams_codex.config import prepare
from agentteams_codex.task_protocol import CONTEXT_ENV, prepare_turn, common
from agentteams_codex.task_protocol import native_task_metadata
from agentteams_codex.native_telemetry import trace_attributes
from agentteams_codex.input_barrier import InputBarrier, ENV as BARRIER_ENV
from agentteams_codex.native_input import saved_input, rollout_path, checkpoint_rows_present
from agentteams_codex.mcp_approvals import automatic_response

REFRESH = os.getenv("CODEX_NATIVE_REFRESH_FIXTURE") == "true"
APPROVAL = os.getenv("CODEX_NATIVE_APPROVAL_FIXTURE") == "true"
LATE_BODY = "OWN_PRIVATE_LATE_INPUT: amend your original request before registering it."


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
    compacting = False

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
            if REFRESH and len(self.requests) == 2:
                assert any(item.get('role') == 'user' and LATE_BODY in json.dumps(item) for item in request.get('input', [])), 'Late input is not a native User message'
            if not REFRESH and len(self.requests) == 2:
                assert 'effect' in json.dumps(request.get('input', [])), 'Task rejection was not returned to the model'
            arguments = {'action': 'create', 'leader': 'leader', 'title': '' if not REFRESH and len(self.requests) == 1 else 'Own native fixture'}
            if REFRESH and len(self.requests) == 2:
                arguments.update(inputId=type(self).original_ref, relatedInputIds=[type(self).related_ref])
            item = {'id': 'fc_task_' + str(len(self.requests)), 'type': 'function_call', 'call_id': 'call_task_' + str(len(self.requests)),
                    'namespace': 'mcp__teamharness', 'name': tools[0]['name'], 'arguments': json.dumps(arguments)}
        else:
            assert 'Task system receipt' in json.dumps(request.get('input', [])), 'Framework Task receipt is missing from native model input'
            item = {'id': 'msg_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': LATE_BODY + '\nTask system receipt: main registered.' if self.compacting else 'TASK_NATIVE_OK', 'annotations': []}]}
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
        script = ('#!' + sys.executable + '\nimport json,os,time\n'
            'path=' + repr(str(calls)) + '\n'
            'count=len(open(path).readlines()) if os.path.exists(path) else 0\n'
            'with open(path,"a") as target: target.write(json.dumps({"event":os.environ["AGENTTEAMS_ORIGIN_EVENT_ID"]})+"\\n")\n')
        if REFRESH:
            script += ('if count==0:\n print(json.dumps({"ok":True,"outcome":"input_changed","noTaskRegistered":True,"sourceEventId":os.environ["AGENTTEAMS_ORIGIN_EVENT_ID"],"inputs":[{"eventId":"$late-native","roomId":"!own:test","userId":"@own:test","body":' + repr(LATE_BODY) + '}]}))\nelse:\n time.sleep(.3)\n ')
        script += ('print(json.dumps({"sourceEventId":os.environ["AGENTTEAMS_ORIGIN_EVENT_ID"],"task":{"id":"main","kind":"main","status":"planned"},"notification":{"status":"queued","eventId":"notice","recipient":"leader"}}))\n')
        executable.write_text(script)
        executable.chmod(0o700)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        collector = ThreadingHTTPServer(('127.0.0.1', 0), Collector)
        threading.Thread(target=collector.serve_forever, daemon=True).start()
        os.environ.update(AGENTTEAMS_OTEL_ENABLED='true', AGENTTEAMS_OTEL_CAPTURE_CONTENT='false', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=f'http://127.0.0.1:{collector.server_port}/v1/traces', OTEL_EXPORTER_OTLP_TRACES_HEADERS='')
        config = {'member': {'role': 'manager', 'runtime': 'codex', 'runtimeName': 'fixture-manager'},
                  'desired': {'model': {'model': 'test-model', 'gatewayUrl': f'http://127.0.0.1:{server.server_port}'}}}
        plugin = source / 'plugins/teamharness'
        event = {'event_id': '$own-native', 'room_id': '!own:test', 'sender': '@own:test'}
        context, fact, _ = prepare_turn(plugin, 'manager', event)
        Model.original_ref = context['sources'][0]['inputId']
        Model.related_ref = common(plugin, 'task_receipt').input_reference('!own:test', '$late-native')
        model, instructions = prepare(config, home, workspace, plugin, trace_attributes=trace_attributes(config,event,context))
        if APPROVAL:
            native_config = home / 'config.toml'
            native_config.write_text(native_config.read_text().replace(
                'default_tools_approval_mode = "approve"', 'default_tools_approval_mode = "prompt"'))
        env = {**os.environ, 'HOME': str(root), 'CODEX_HOME': str(home), 'CODEX_GATEWAY_KEY': 'own-fixture',
               'PATH': str(binary) + os.pathsep + os.environ['PATH'],
               'AGENTTEAMS_PLUGIN_DIR': str(plugin), CONTEXT_ENV: json.dumps(context),
               'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
        env.pop('HTTP_PROXY', None)
        env.pop('HTTPS_PROXY', None)
        try:
            with InputBarrier() as barrier, Client(workspace, command=json.loads(os.environ['CODEX_NATIVE_COMMAND']), env={**env, BARRIER_ENV: barrier.path},
                        trace_context={'traceparent': '00-123456789012345678901234567890ab-12345678901234ab-01'}) as client:
                thread = client.open_thread(None, workspace, model, instructions + '\n' + fact)['id']
                turn = client.start(thread, 'Use your task tool to register the own fixture request.')
                if REFRESH:
                    invalid = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'OWN_INVALID_TURN_INPUT'}]}
                    try:
                        client.request('thread/inject_items', {'threadId': thread, 'items': [invalid], 'persistToActiveTurn': 'wrong-turn'})
                    except CodexError:
                        pass
                    else:
                        raise AssertionError('SDK admitted User input for another native turn')
                    invalid['role'] = 'assistant'
                    try:
                        client.request('thread/inject_items', {'threadId': thread, 'items': [invalid], 'persistToActiveTurn': turn['id']})
                    except CodexError:
                        pass
                    else:
                        raise AssertionError('SDK admitted a forged assistant message')
                completed = []
                approvals = []
                checkpoints = []
                frames = []
                def persist(frame):
                    digest = saved_input(home, thread, '[Message $late-native]\n' + LATE_BODY)
                    assert digest, 'Durable SDK ACK preceded native rollout User persistence'
                    assert len(Model.requests) == 1, 'Model advanced before the held ToolResult was checkpointed'
                    with rollout_path(home, thread).open('rb') as native_file:
                        os.fsync(native_file.fileno())
                    checkpoints.append(digest)
                    frames.append(frame)
                deadline = time.monotonic() + 90
                while True:
                    assert time.monotonic() < deadline, 'Native Task fixture timed out'
                    barrier.serve(client, thread, turn['id'], persist, context=context, home=home)
                    try:
                        notification = client.next_event(.1)
                    except TimeoutError:
                        continue
                    if 'id' in notification and notification.get('method'):
                        if APPROVAL and notification['method'] == 'mcpServer/elicitation/request':
                            params = notification['params']
                            assert params['_meta']['codex_approval_kind'] == 'mcp_tool_call'
                            assert params['serverName'] == 'teamharness'
                            server_policy = {'mode': 'ask', 'tools': '*'}
                            policy = {'desired': {'mcpServers': [{'name': 'teamharness', 'approvals': server_policy}]}}
                            assert automatic_response(policy, params, thread, turn['id']) is None
                            server_policy['mode'] = 'allow'
                            response = automatic_response(policy, params, thread, turn['id'])
                            assert response == {'action': 'accept', 'content': None}, params
                            approvals.append(notification['id'])
                            client.send({'id': notification['id'], 'result': response})
                            continue
                        raise AssertionError('Native Task unexpectedly requested approval/input: ' + notification['method'])
                    if notification.get('method') == 'item/completed':
                        completed.append(notification['params']['item'])
                    if notification.get('method') == 'turn/completed':
                        assert notification['params']['turn']['status'] == 'completed', notification
                        break
                if APPROVAL:
                    assert len(approvals) == 2, approvals
            if REFRESH:
                assert checkpoint_rows_present(home, thread, frames[0]['rows'])
                assert frames[0]['sourceEvent'] == '$own-native'
                assert all('body' not in source for source in frames[0]['sources'])
                prepare(config, home, workspace, plugin, trace_attributes=trace_attributes(config, event, context))
                with Client(workspace, command=json.loads(os.environ['CODEX_NATIVE_COMMAND']),
                            env={**env, CONTEXT_ENV: json.dumps(context)},
                            trace_context={'traceparent': '00-123456789012345678901234567890ab-12345678901234ab-01'}) as resumed:
                    assert resumed.open_thread(thread, workspace, model, instructions)['id'] == thread
                    probe = resumed.start(thread, 'Verify your restored native context without registering another task.')
                    while True:
                        message = resumed.next_event(30)
                        if message.get('method') == 'turn/completed' and message['params']['turn']['id'] == probe['id']:
                            assert message['params']['turn']['status'] == 'completed'
                            break
                    assert any(item.get('role') == 'user' and LATE_BODY in json.dumps(item) for item in Model.requests[-1]['input'])
                    Model.compacting = True
                    resumed.request('thread/compact/start', {'threadId': thread})
                    compact_deadline = time.monotonic() + 30
                    while True:
                        assert time.monotonic() < compact_deadline, 'Native compaction did not complete'
                        message = resumed.next_event(30)
                        if (message.get('method') == 'thread/compacted'
                                or message.get('method') == 'item/completed' and message.get('params', {}).get('item', {}).get('type') == 'contextCompaction'):
                            break
                    Model.compacting = False
                    post_compact = resumed.start(thread, 'Verify context after native compaction; do not call tools.')
                    while True:
                        message = resumed.next_event(30)
                        if message.get('method') == 'turn/completed' and message['params']['turn']['id'] == post_compact['id']:
                            assert message['params']['turn']['status'] == 'completed'
                            break
                    assert LATE_BODY in json.dumps(Model.requests[-1]['input']), 'Native compaction lost the verified late input'
            assert [json.loads(line) for line in calls.read_text().splitlines()] == [{'event': '$own-native'}] * (2 if REFRESH else 1)
            assert len(checkpoints) == int(REFRESH)
            assert any(item.get('type') == 'mcpToolCall' and item.get('status') == 'completed' for item in completed)
            assert REFRESH or any(native_task_metadata(item).get('effect') == 'none' for item in completed), 'Native SDK dropped the fixed Task effect classification'
            assert any(item.get('text') == 'TASK_NATIVE_OK' for item in completed)
            deadline = time.monotonic() + 10
            while not Collector.payloads and time.monotonic() < deadline:
                time.sleep(0.1)
            assert Collector.payloads, 'Native SDK did not export OTLP traces'
            spans = [span for batch in Collector.payloads for resource in batch.get('resourceSpans', [])
                     for scope in resource.get('scopeSpans', []) for span in scope.get('spans', [])]
            evidence = os.getenv('CODEX_NATIVE_TRACE_EVIDENCE')
            if evidence:
                Path(evidence).write_text(json.dumps(Collector.payloads, indent=2) + '\n')
            assert spans
            raw = json.dumps(Collector.payloads)
            for marker in ('Use your task tool to register the own fixture request.',
                           'Own native fixture', 'TASK_NATIVE_OK', 'sourceEventId',
                           'Task system receipt', 'gatewayKey', LATE_BODY):
                assert marker not in raw, 'Native SDK exported synthetic content: ' + marker
            forbidden = {'arguments', 'output', 'input', 'prompt', 'body', 'payload',
                         'tool.arguments', 'tool.result', 'http.request.body', 'http.response.body'}
            attributes = [attribute for span in spans for attribute in span.get('attributes', [])]
            attributes += [attribute for span in spans for event in span.get('events', [])
                           for attribute in event.get('attributes', [])]
            assert not forbidden.intersection(attribute['key'] for attribute in attributes)
            assert any(attribute['key'] == 'gen_ai.usage.input_tokens' for attribute in attributes)
            assert any(span['name'] == 'mcp.tools.call' for span in spans)
            # Both are native task execution boundaries. The long-lived outer turn
            # can remain open until session cleanup; its child run ends with the task.
            native_turns = [span for span in spans if span['name'] in {'session_task.turn', 'session_task.run'}]
            assert native_turns
            assert all(span['traceId'] == '123456789012345678901234567890ab' for span in native_turns), 'SDK lost the trusted incoming trace carrier'
            if REFRESH:
                generations = [span for span in spans if any(attribute['key'] == 'langfuse.observation.type'
                    and attribute['value'].get('stringValue') == 'generation' for attribute in span.get('attributes', []))]
                assert len(generations) == 5, 'Missing unique real model generation boundaries: ' + str(len(generations))
                sources = [json.loads(next(attribute['value']['stringValue'] for attribute in span['attributes']
                    if attribute['key'] == 'agentteams.input.event_ids')) for span in sorted(generations, key=lambda item: int(item['startTimeUnixNano']))]
                assert sources == [['$own-native']] + [['$own-native', '$late-native']] * 4, sources
                tools = [span for span in spans if span['name'] == 'tool.execute']
                assert len(tools) == 2
                duration = lambda span: int(span['endTimeUnixNano']) - int(span['startTimeUnixNano'])
                assert max(map(duration, tools)) > 300_000_000
                assert max(map(duration, generations)) < max(map(duration, tools))
            evidence = os.getenv('CODEX_NATIVE_TRACE_EVIDENCE')
            if evidence:
                Path(evidence).write_text(json.dumps(Collector.payloads, indent=2) + '\n')
            print(json.dumps({'nativeAppServerAndMCP': True, 'sourceBound': True, 'taskAuthorizationPrompt': APPROVAL,
                              'nativeMcpApprovals': len(approvals),
                              'frameworkReceiptInNativeToolContext': True, 'modelRequests': len(Model.requests),
                              'definitiveNoEffectMetadataPreserved': None if REFRESH else True,
                              'durableInputRefreshBeforeModel': bool(checkpoints), 'nativeOtlpSpans': len(spans), 'nativeSpanNames': sorted({span['name'] for span in spans}),
                              'productionModelOrTaskCalls': False, 'productionNativePrivateStateRead': False, 'syntheticNativeUserRowsVerified': bool(checkpoints)}))
        finally:
            server.shutdown()
            collector.shutdown()


if __name__ == '__main__':
    main()
