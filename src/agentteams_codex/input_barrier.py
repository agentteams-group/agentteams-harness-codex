"""Private transport between a held native Task tool and its owning turn.

The fixed SDK's durable active-input API must acknowledge the exact turn after
native history and rollout flush. A queued inject/steer acknowledgement is not
sufficient. This transport never exposes a model-callable tool or HTTP API.
"""
import json
import os
from pathlib import Path
import socket
import tempfile

ENV = 'AGENTTEAMS_CODEX_INPUT_BARRIER'


def checkpoint(inputs):
    path = os.environ.get(ENV)
    if not path:
        raise RuntimeError('Native durable input barrier is unavailable')
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(90)
        connection.connect(path)
        connection.sendall(json.dumps({'inputs': inputs}, ensure_ascii=False).encode() + b'\n')
        response = json.loads(connection.makefile('rb').readline())
    if response.get('persisted') is not True:
        raise RuntimeError('Native refreshed User inputs require durable checkpoint verification')


class InputBarrier:
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='at-codex-input-')
        self.path = str(Path(self.directory.name) / 'input.sock')
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(self.path)
        self.socket.listen(1)
        self.socket.setblocking(False)
        return self

    def serve(self, client, thread, turn, persist):
        try:
            connection, _ = self.socket.accept()
        except BlockingIOError:
            return
        with connection:
            connection.settimeout(5)
            try:
                request = json.loads(connection.makefile('rb').readline())
                inputs = request['inputs']
                if not inputs or any(not isinstance(source.get('body'), str)
                                     or not source.get('eventId', '').startswith('$') for source in inputs):
                    raise ValueError('Native User input identities are unavailable')
                items = [{'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': f"[Message {source['eventId']}]\n{source['body']}"}]}
                    for source in inputs]
                result = client.request('thread/inject_items', {'threadId': thread, 'items': items,
                                                              'persistToActiveTurn': turn})
                if result.get('persistedToActiveTurn') != turn:
                    raise RuntimeError('SDK lacks the required durable active-input API')
                persist(include_workspace=False)
                response = {'persisted': True}
            except Exception:
                # Do not export source content, API payload or credentials.
                response = {'persisted': False}
            connection.sendall(json.dumps(response).encode() + b'\n')

    def __exit__(self, *_):
        self.socket.close()
        self.directory.cleanup()
