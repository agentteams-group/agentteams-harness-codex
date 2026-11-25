"""Private native admission state; all Task recovery decisions belong to Core."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
from contextlib import contextmanager

from .task_protocol import common

ENV = 'AGENTTEAMS_CODEX_NATIVE_SCOPE'
COMMAND_ENV = 'CODEX_NATIVE_ADMISSION_COMMAND'
REGISTRATION = {'origin': 'agentteams.common.task_native',
                'entrypoint': '/opt/agentteams/plugins/teamharness/mcp/common/task_native.py'}


@contextmanager
def locked(path):
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Scope:
    def __init__(self, home, plugin, context, recovery=None):
        self.home = home
        directory = home / 'agentteams-native'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / (hashlib.sha256(context['sourceEvent'].encode()).hexdigest() + '.json')
        with locked(self.path):
            prior = json.loads(self.path.read_text()) if self.path.exists() else {}
            self.frame = {'context': context, 'plugin': str(plugin), 'recovery': recovery,
                          'registrations': {'mcp__teamharness__task': REGISTRATION},
                          'observed': prior.get('observed', {}),
                          'blocked': prior.get('blocked', False)}
            if prior.get('context', {}).get('sourceEvent') == context['sourceEvent'] and prior.get('complete'):
                self.frame['complete'] = prior['complete']
                self.frame['dispatch_receipts'] = prior.get('dispatch_receipts', [])
            write(self.path, self.frame)

    def presentation(self):
        complete = json.loads(self.path.read_text()).get('complete')
        return complete.get('displayText', '') if complete else None

    def require_admitted(self):
        # The pinned SDK reports unexpected task errors as TurnComplete. Use
        # only the private framework callback outcome, never the model text.
        if json.loads(self.path.read_text()).get('blocked'):
            raise RuntimeError('Native framework admission blocked: authoritative verification required')

    def update(self, context):
        with locked(self.path):
            frame = json.loads(self.path.read_text())
            frame['context'] = context
            write(self.path, frame)


def load():
    path = os.getenv(ENV)
    if not path:
        return None
    return json.loads(Path(path).read_text())


def admit(frame):
    from task_operation import admit as admit_operation
    from task_recovery_rpc import execute
    if frame.get('complete'):
        raise RuntimeError('The native Task phase is complete')
    recovery = frame.get('recovery')
    if recovery:
        if recovery['kind'] == 'task':
            return execute({'action': 'admit_tools', 'identity': recovery['identity'], 'device_delivery': True})
        current = execute({'action': 'matrix.message.current', 'identity': recovery['identity'],
                           'runtime': 'codex', 'device_delivery': True})
        admit_operation(current['execution'].get('checkpoint'))
    result = common(frame['plugin'], 'task_native').invoke(
        {'operation': 'canonical_admit', 'context': frame['context']})
    if result.get('ok') is not True:
        raise RuntimeError('Canonical Task outcome requires authoritative verification')
    return result


def publish_context(context):
    path = os.getenv(ENV)
    if not path:
        return
    path = Path(path)
    with locked(path):
        frame = load()
        if frame['context']['sourceEvent'] != context['sourceEvent']:
            raise RuntimeError('Native context belongs to another admission')
        frame['context'] = context
        write(path, frame)


def phase_complete(outcome, context):
    """Only the fixed Task factory calls this with a validated common receipt."""
    if outcome.get('phaseComplete') is not True:
        return
    path = os.getenv(ENV)
    if not path:
        return
    path = Path(path)
    with locked(path):
        frame = json.loads(path.read_text())
        if frame['context']['sourceEvent'] != context['sourceEvent']:
            raise RuntimeError('Native Task receipt belongs to another admission')
        frame['complete'] = {'receiptId': outcome['receiptId'], 'fact': outcome['fact']}
        if isinstance(outcome.get('displayText'), str):
            frame['complete']['displayText'] = outcome['displayText']
        frame['dispatch_receipts'] = outcome.get('extraReceipts', [])
        write(path, frame)


def mark_blocked(blocked):
    path = Path(os.environ[ENV])
    with locked(path):
        frame = json.loads(path.read_text())
        frame['blocked'] = blocked
        write(path, frame)


def main():
    from .native_input import checkpoint_rows_present, saved_message
    request = json.load(sys.stdin)
    path = Path(os.environ[ENV])
    frame = load()
    home = Path(os.environ['CODEX_HOME'])
    if request['mode'] == 'ack':
        observed = {}
        for receipt in request['receipts']:
            item = receipt['item']
            digest = saved_message(home, request['threadId'], item['role'], item['content'][0]['text'])
            if not digest:
                raise RuntimeError('Native framework receipt row is not durable')
            observed[receipt['receiptId']] = {'threadId': request['threadId'], 'rows': [digest]}
        with locked(path):
            frame = load()
            frame['observed'].update(observed)
            write(path, frame)
        # The SDK has flushed the actual receipt items. Persist this same native
        # state before allowing the next model request, including process restore.
        from .bridge import snapshot_codex, mc
        archive = home.parent / 'runtime/codex-state.tar.gz'
        snapshot_codex(home, archive)
        prefix = os.getenv('AGENTTEAMS_STORAGE_PREFIX')
        if prefix:
            mc('cp', str(archive), f"{prefix}/agents/{os.environ['AGENTTEAMS_WORKER_NAME']}/runtime/{archive.name}")
        mark_blocked(False)
        print(json.dumps({'ok': True}))
        return
    complete = frame.get('complete')
    if complete:
        result = {'ok': True, 'receipts': [complete, *frame.get('dispatch_receipts', [])]}
        if request['mode'] == 'prepare':
            result['complete'] = complete['fact']
        else:
            raise RuntimeError('The native Task phase is complete')
    else:
        result = admit(frame)
    result = {'ok': True, 'receipts': [receipt for receipt in result.get('receipts', [])
              if not checkpoint_rows_present(home, frame['observed'].get(receipt['receiptId'], {}).get('threadId'),
                  frame['observed'].get(receipt['receiptId'], {}).get('rows'))],
              **({'complete': result['complete']} if result.get('complete') else {})}
    mark_blocked(False)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        mark_blocked(True)
        # This callback carries metadata only; never print API bodies or source
        # content into telemetry/model context when admission fails.
        print(json.dumps({'ok': False, 'error': 'native_admission_requires_verification'}))
        sys.exit(1)
