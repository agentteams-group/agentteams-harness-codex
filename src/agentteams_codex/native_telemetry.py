"""Configure the SDK's own OTLP exporter; never reconstruct model/tool spans."""
import json
import os
from urllib.parse import unquote

from .tool_progress import run_id


def trace_attributes(config, event, task_context):
    member = config['member']
    fields = {'agentteams.runtime': 'codex', 'agentteams.telemetry.schema': 'at.worker.v1',
              'agentteams.telemetry.capture_content': 'false',
              'agentteams.worker.name': member.get('runtimeName') or os.getenv('AGENTTEAMS_WORKER_NAME', 'unknown'),
              'agentteams.room.id': event['room_id'], 'agentteams.run.id': run_id(event)}
    if event['event_id'].startswith('$'):
        fields['agentteams.event.id'] = event['event_id']
    else:
        fields['agentteams.native.request.id'] = event['event_id']
    sources = task_context.get('sources') or []
    fields['agentteams.input.event_ids'] = json.dumps([source['eventId'] for source in sources], separators=(',', ':'))
    task_id = task_context.get('execution') or (task_context.get('review') or [''])[0] or task_context.get('completion')
    planning = task_context.get('planning') or []
    if not task_id and len(planning) == 1:
        task_id = planning[0]
    if task_id:
        fields['agentteams.task.id'] = task_id
    if config.get('team', {}).get('name'):
        fields['agentteams.team.id'] = config['team']['name']
    recovery = event.get('recovery') or {}
    generation = recovery.get('generation') or (task_context.get('context') or {}).get('generation')
    if generation:
        fields['agentteams.execution.generation'] = str(generation)
    notification = (event.get('_task_protocol_meta') or {}).get('teamharness_coordination') or {}
    if notification.get('id'):
        fields['agentteams.coordination.id'] = notification['id']
        fields['agentteams.coordination.kind'] = notification['kind']
    return fields


def configuration(attributes=None):
    if os.getenv('AGENTTEAMS_OTEL_ENABLED', '').lower() not in {'true', '1'}:
        return []
    endpoint = os.getenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT')
    if not endpoint:
        base = os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://localhost:4318')
        endpoint = base.rstrip('/') + '/v1/traces'
    headers = {}
    for item in os.getenv('OTEL_EXPORTER_OTLP_TRACES_HEADERS', os.getenv('OTEL_EXPORTER_OTLP_HEADERS', '')).split(','):
        if item.strip():
            key, separator, value = item.partition('=')
            if not separator:
                raise ValueError('Invalid native OTLP header configuration')
            headers[unquote(key.strip())] = unquote(value.strip())
    lines = ['[otel]', 'exporter = "none"', 'metrics_exporter = "none"', 'log_user_prompt = false',
             '[otel.trace_exporter.otlp-http]', 'endpoint = ' + json.dumps(endpoint), 'protocol = "json"',
             '[otel.trace_exporter.otlp-http.headers]']
    lines += [json.dumps(key) + ' = ' + json.dumps(value) for key, value in headers.items()]
    lines += ['[otel.span_attributes]']
    lines += [json.dumps(key) + ' = ' + json.dumps(value) for key, value in (attributes or {}).items()]
    return lines
