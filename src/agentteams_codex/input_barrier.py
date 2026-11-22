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


def checkpoint(inputs, context):
    path = os.environ.get(ENV)
    if not path:
        raise RuntimeError('Native durable input barrier is unavailable')
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(90)
        connection.connect(path)
        connection.sendall(json.dumps({'inputs': inputs, 'sources': context['sources'], 'sourceEvent': context['sourceEvent']}, ensure_ascii=False).encode() + b'\n')
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

    def serve(self, client, thread, turn, persist, *, context, home):
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
                if request['sourceEvent'] != context['sourceEvent']:
                    raise ValueError('Native input checkpoint belongs to another admission')
                sources = request['sources']
                if not isinstance(sources, list) or any(not isinstance(source, dict) or 'body' in source for source in sources):
                    raise ValueError('Native source checkpoint must be body-free')
                event_ids = [source['eventId'] for source in sources]
                if (not isinstance(event_ids, list) or any(not isinstance(event, str) or not event.startswith('$') for event in event_ids)
                        or not {source['eventId'] for source in inputs}.issubset(event_ids)):
                    raise ValueError('Native User source metadata is unavailable')
                items = [{'type': 'message' , 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': f"[Message {source['eventId']}]\n{source['body']}"}]}
                    for source in inputs]
                result = client.request('thread/inject_items', {'threadId': thread, 'items': items,
                                                              'persistToActiveTurn': turn, 'spanAttributes': {
                                                                  'agentteams.input.event_ids': json.dumps(event_ids, separators=(',', ':'))}})
                if result.get('persistedToActiveTurn') != turn:
                    raise RuntimeError('SDK lacks the required durable active-input API')
                from .native_input import saved_input
                digests = [saved_input(home, thread, item['content'][0]['text']) for item in items]
                if not all(digests):
                    raise RuntimeError('Native User row checkpoint is unavailable')
                frame = {'sourceEvent': context['sourceEvent'], 'sources': sources,
                         'inputRefreshPending': False, 'threadId': thread, 'rows': digests}
                persist(frame)
                context.update(sources=sources, inputRefreshPending=False)
                response = {'persisted': True}
            except Exception:
                # Do not export source content, API payload or credentials.
                response = {'persisted': False}
            connection.sendall(json.dumps(response).encode() + b'\n')

    def __exit__(self, *_):
        self.socket.close()
        self.directory.cleanup()
