"""Project native tool items into the shared Matrix tool-card protocol."""
import json
import time

from runtime_observability import redact


def run_id(event):
    task = event.get('recovery')
    if task:
        return f"codex:task:{task['team_id']}:{task['task_id']}:{task['generation']}"
    return f"codex:message:{event['room_id']}:{event['event_id']}"


def bounded(value):
    text = redact(json.dumps(value, ensure_ascii=False))
    return json.loads(text) if len(text) <= 4096 else {"preview": text[:4096], "truncated": True}


class ToolProgress:
    def __init__(self, matrix, event, run_id):
        self.matrix, self.event, self.run_id = matrix, event, run_id
        self.calls = {}

    def update(self, method, item):
        if (self.event.get('_task_protocol_meta') or {}).get('teamharness_coordination', {}).get('kind') == 'assistant_digest':
            return
        kind, identity = item.get('type'), item.get('id')
        if identity and method == 'item/completed' and (kind == 'reasoning' or (kind == 'agentMessage' and item.get('phase') == 'commentary')):
            text = '\n\n'.join(item.get('summary') or []) if kind == 'reasoning' else item.get('text', '')
            if text:
                content = {'msgtype': 'm.notice', 'body': redact(text)[:4096],
                    'org.agentteams.progress_only': True, 'm.mentions': {'user_ids': []},
                    'org.agentteams.run': {'version': '1', 'run_id': self.run_id,
                        'blocks': [{'type': 'thinking', 'content': redact(text)[:4096]}]}}
                self.matrix.send_content(self.event['room_id'], content,
                    f"codex:{self.event['event_id']}:{self.run_id}:summary:{identity}")
            return
        if kind not in {'commandExecution', 'mcpToolCall', 'fileChange'} or not identity:
            return
        if method not in {'item/started', 'item/completed'}:
            return
        now = int(time.time() * 1000)
        call = self.calls.setdefault(identity, {'item': {}})
        call['item'].update(item)
        item = call['item']
        if method == 'item/started':
            if 'started_at' in call:
                return
            call['started_at'] = now
        if kind == 'commandExecution':
            name = 'execute_command'
            arguments = {key: item[key] for key in ('command', 'cwd') if key in item}
            result = {key: item[key] for key in ('exitCode', 'processId') if key in item}
            if 'exitCode' in result:
                result['exit_code'] = result.pop('exitCode')
            if 'aggregatedOutput' in item:
                result['output'] = item['aggregatedOutput']
        elif kind == 'mcpToolCall':
            name = '.'.join(filter(None, (item.get('server'), item.get('tool')))) or kind
            arguments, result = item.get('arguments') or {}, item.get('result')
        else:
            name, arguments, result = 'apply_patch', {'changes': item.get('changes', [])}, None
        failed = bool(item.get('error')) or item.get('exitCode') not in (None, 0) or item.get('status') in {'failed', 'declined'}
        failed = failed or (isinstance(result, dict) and result.get('isError') is True)
        status = 'running'
        if method == 'item/completed':
            if failed:
                status = 'failed'
            elif item.get('status') == 'completed' and (kind != 'commandExecution' or item.get('exitCode') == 0):
                status = 'succeeded'
        block = {'type': 'tool_call', 'tool_name': name, 'tool_call_id': identity,
                 'arguments': bounded(arguments if isinstance(arguments, dict) else {'value': arguments}), 'status': status}
        if 'started_at' in call:
            block['started_at'] = call['started_at']
        if status != 'running':
            duration = item.get('durationMs')
            block['finished_at'] = call['started_at'] + duration if 'started_at' in call and isinstance(duration, (int, float)) and duration >= 0 else now
        if method == 'item/completed':
            if item.get('error'):
                result = {'error': item['error'], 'result': result}
            if result is not None:
                block['result'] = bounded(result)
        body = f'Codex {kind}: {name} · {status}'
        details = {'arguments': block['arguments']}
        if 'result' in block:
            details['result'] = block['result']
        body += '\n' + json.dumps(details, ensure_ascii=False)
        content = {'msgtype': 'm.text', 'body': body, 'org.agentteams.progress_only': True,
                   'm.mentions': {'user_ids': []},
                   'm.relates_to': {'m.in_reply_to': {'event_id': self.event['event_id']}},
                   'org.agentteams.run': {'version': '1', 'run_id': self.run_id, 'blocks': [block]}}
        if call.get('event_id'):
            content = {**content, 'm.new_content': content,
                       'm.relates_to': {'rel_type': 'm.replace', 'event_id': call['event_id']}}
        event_id = self.matrix.send_content(self.event['room_id'], content,
            f"codex:{self.event['event_id']}:{self.run_id}:tool:{identity}:{method}")
        if not call.get('event_id') and event_id:
            call['event_id'] = event_id
