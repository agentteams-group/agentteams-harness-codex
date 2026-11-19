"""One native exec request followed by a terminal Responses event."""
import json
from http.server import BaseHTTPRequestHandler


class Model(BaseHTTPRequestHandler):
    sent_tool = False
    requests = []

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).requests.append(request)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        def send(kind, **value):
            self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **value}) + '\n\n').encode())
            self.wfile.flush()
        rid = 'resp_' + str(len(self.requests))
        response = {'id': rid, 'object': 'response', 'status': 'in_progress', 'output': []}
        send('response.created', response=response)
        if not type(self).sent_tool:
            type(self).sent_tool = True
            names = [t.get('name') for t in request.get('tools', [])]
            assert 'exec_command' in names, names
            item = {'id': 'fc_control', 'type': 'function_call', 'call_id': 'call_control',
                'name': 'exec_command', 'arguments': json.dumps({'cmd': self.tool_command, 'yield_time_ms': 10000})}
        else:
            item = {'id': 'msg_continued', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'CONTINUED_OK', 'annotations': []}]}
        send('response.output_item.added', output_index=0, item=item)
        send('response.output_item.done', output_index=0, item=item)
        send('response.completed', response={**response, 'status': 'completed', 'output': [item],
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}})

    def log_message(self, *_args):
        pass
