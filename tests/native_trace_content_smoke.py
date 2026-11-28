"""Inspect content exposed by the pinned native SDK against a local fixture."""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from agentteams_codex.client import Client
from agentteams_codex.native_telemetry import configuration
from native_model_fixture import Model
from native_task_protocol_smoke import Collector


def main():
    with tempfile.TemporaryDirectory(dir=Path.home()) as tmp, ExitStack() as stack:
        root = Path(tmp)
        home, work = root / '.codex', root / 'work'
        home.mkdir(); work.mkdir()
        Model.tool_command = 'printf "TRACE_TOOL_OUTPUT fixture-secret-key-123456"'
        Model.reasoning_summary = 'TRACE_REASONING_SUMMARY'
        capture = os.getenv("AGENTTEAMS_OTEL_CAPTURE_CONTENT", "true")
        Model.sent_tool = False
        Model.requests = []
        Collector.payloads = []
        model, collector = [stack.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), handler)) for handler in (Model, Collector)]
        for server in (model, collector):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.shutdown)
        os.environ.update(AGENTTEAMS_OTEL_ENABLED='true', AGENTTEAMS_OTEL_CAPTURE_CONTENT=capture,
                          CODEX_SANDBOX_MODE='danger-full-access',
                          OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=f'http://127.0.0.1:{collector.server_port}/v1/traces',
                          OTEL_EXPORTER_OTLP_TRACES_HEADERS='')
        config = f'model = "fixture"\nmodel_provider = "fixture"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n[model_providers.fixture]\nname = "fixture"\nbase_url = "http://127.0.0.1:{model.server_port}"\nenv_key = "CODEX_GATEWAY_KEY"\nwire_api = "responses"\n'
        (home / 'config.toml').write_text(config + '\n' + '\n'.join(configuration()))
        env = {**os.environ, 'HOME': str(root), 'CODEX_HOME': str(home), 'CODEX_GATEWAY_KEY': 'fixture-key', 'TRACE_FIXTURE_API_KEY': 'fixture-secret-key-123456', 'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
        for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
            env.pop(key, None)
        with Client(work, env=env) as client:
            thread = client.open_thread(None, work, 'fixture', 'Run the local fixture command')['id']
            client.start(thread, 'TRACE_USER_INPUT')
            while True:
                event = client.next_event(20)
                if event.get('method') == 'turn/completed': break
        deadline = time.monotonic() + 10
        while not Collector.payloads and time.monotonic() < deadline: time.sleep(.05)
        spans = [s for batch in Collector.payloads for resource in batch.get('resourceSpans', []) for scope in resource.get('scopeSpans', []) for s in scope.get('spans', [])]
        def attrs(span):
            return {item['key']: next(iter(item['value'].values())) for item in span.get('attributes', [])}
        generations = [attrs(s) for s in spans if s['name'] == 'gen_ai.chat']
        tools = [attrs(s) for s in spans if s['name'] == 'tool.execute']
        assert len(generations) == 2 and tools
        assert all(s['name'] != 'auth' for s in spans), 'Internal TRACE-level spans must not flood task telemetry'
        assert all(s['name'] in {'gen_ai.chat', 'tool.execute'} for s in spans), (
            'Internal SDK HTTP/config spans must not become separate task traces',
            sorted({s['name'] for s in spans}))
        if capture == 'true':
            assert any('TRACE_USER_INPUT' in a.get('langfuse.observation.input', '') for a in generations)
            assert any('TRACE_REASONING_SUMMARY' in a.get('langfuse.observation.output', '') for a in generations)
            assert 'TRACE_TOOL_OUTPUT' in tools[0]['langfuse.observation.input']
            assert 'TRACE_TOOL_OUTPUT' in tools[0]['langfuse.observation.output'], tools[0]['langfuse.observation.output'].replace('fixture-secret-key-123456', '[REDACTED]')
            assert 'fixture-secret-key-123456' not in json.dumps(Collector.payloads)
            assert '[REDACTED]' in tools[0]['langfuse.observation.output']
        else:
            assert all('langfuse.observation.input' not in a and 'langfuse.observation.output' not in a for a in [*generations, *tools])
        print(json.dumps({'contentCapture': capture, 'nativeGenerations': len(generations), 'nativeTools': len(tools), 'returnedReasoningAndToolIO': capture == 'true', 'passed': True}))
        assert spans


if __name__ == '__main__': main()
