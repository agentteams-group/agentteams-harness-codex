"""Synthetic Controller transport for actual ManagedMatrix admission, never model identity."""
import copy
import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from task_recovery import Recovery
from worker_matrix_state import matrix_event_key


class Controller(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, value, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def view(self, key):
        item = self.server.inputs[key]
        execution = item['execution']
        cp = execution.get('checkpoint') or {}
        mode = ('verify' if (cp.get('operation') or {}).get('state') in {'started', 'unknown'}
                else 'deliver' if cp.get('delivery') == 'pending' else 'resume')
        view = {'runtime': 'codex', 'instance_id': 'fixture-instance', 'etag': str(item['version']),
                'input': item, 'execution': execution, 'recovery': mode}
        if mode == 'deliver':
            view['saved_result'] = (self.server.storage / cp['result']['key']).read_text()
        return view

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == '/api/v1/runtime/matrix':
            return self.reply(self.server.snapshot)
        if path.path == '/api/v1/runtime/sessions':
            key = parse_qs(path.query)['event_key'][0]
            item = self.server.inputs[key]
            cp = item['execution'].get('checkpoint')
            return self.reply({'runtime': 'codex', 'instance_id': 'fixture-instance',
                'generation': item['execution']['generation'], 'checkpoint_version': (cp or {}).get('version', 0),
                'checkpoint_digest': Recovery.digest(cp), 'config_generation': 1, 'etag': '',
                'cache': None, 'compatible': False, 'work': {'kind': 'message',
                'room_id': item['room_id'], 'event_id': item['event_id']}})
        prefix = '/api/v1/runtime/matrix/events/'
        if path.path.startswith(prefix):
            parts = path.path[len(prefix):].split('/')
            key = parts[0]
            if key not in self.server.inputs:
                return self.reply({'error': 'not found'}, 404)
            item = self.server.inputs[key]
            if parts[-1] == 'execution':
                return self.reply(self.view(key))
            if parts[-1] == 'native':
                return self.reply({'runtime': 'codex', 'instance_id': 'fixture-instance',
                    'etag': str(item['version']), 'current': True, 'input': item})
            return self.reply(item)
        return self.reply({'error': 'unknown fixture route'}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.headers['Authorization'] == 'Bearer fixture'
        assert self.headers['X-AgentTeams-Process-ID'] == 'fixture-process'
        assert body['instance_id'] == 'fixture-instance'
        if self.path == '/api/v1/runtime/matrix':
            snapshot = self.server.snapshot
            if body['action'] == 'start':
                snapshot['state']['active_start'] = {'runtime': 'codex', 'instance_id': 'fixture-instance', 'process_id': 'fixture-process'}
            elif body['action'] == 'ingest':
                for source in body['inputs']:
                    key = matrix_event_key(source['room_id'], source['event_id'])
                    if key not in self.server.inputs:
                        self.server.inputs[key] = {**source, 'member_prefix': 'agents/agent', 'version': 0,
                            'execution': {'runtime': 'codex', 'instance_id': 'fixture-instance',
                                'process_id': '', 'generation': 0, 'state': 'pending', 'checkpoint': None}}
                        snapshot['state']['pending'].append(key)
            else:
                raise AssertionError(body)
            snapshot['etag'] = str(int(snapshot['etag']) + 1)
            return self.reply(snapshot)
        parts = self.path.split('/')
        key, operation = parts[-2:]
        item = self.server.inputs[key]
        if self.headers.get('If-Match') != str(item['version']):
            return self.reply({'error': 'stale fixture write'}, 412)
        execution = item['execution']
        if operation == 'native':
            item['native_commit'] = {'runtime': 'codex', 'start': copy.deepcopy(self.server.snapshot['state']['active_start']),
                'native_ref': body['native_ref'], 'native_digest': body['native_digest']}
        else:
            assert operation == 'execution'
            if body['action'] == 'claim':
                if execution['state'] == 'pending':
                    execution.update(state='active', process_id='fixture-process', generation=1)
                self.server.claims.append(item['event_id'])
            else:
                assert body['generation'] == execution['generation'] and body['process_id'] == execution['process_id']
                if body['action'] == 'cancel':
                    execution['state'] = 'cancelled'
                else:
                    assert body['action'] in {'checkpoint', 'release'}
                    execution['checkpoint'] = body['checkpoint']
        item['version'] += 1
        if operation == 'native':
            return self.do_GET()
        return self.reply(self.view(key))

    def do_PUT(self):
        assert self.path.startswith('/api/v1/runtime/sessions?')
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.saved_sessions.append(body)
        self.reply(body)


def initialize(server, storage):
    server.storage = Path(storage)
    server.inputs, server.claims, server.saved_sessions = {}, [], []
    server.snapshot = {'runtime': 'codex', 'instance_id': 'fixture-instance', 'etag': '0',
        'state': {'active_start': {}, 'pending': [], 'rooms': {}, 'consumers': {}}}
