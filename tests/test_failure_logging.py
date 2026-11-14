"""Drive the real Codex bridge with a subprocess speaking app-server JSON-RPC."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_interaction
sys.path.insert(0, str(Path(os.environ['AT_SOURCE']) / 'shared/lib'))
sys.path.insert(0, str(Path(os.environ['AT_SOURCE']).parent / 'scripts/lib'))
from runtime_observability import event

FAKE = '''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 result={}
 if m['method']=='thread/start': result={'thread':{'id':'thread-1'}}
 if m['method']=='turn/start': result={'turn':{'id':'turn-1'}}
 print(json.dumps({'id':m['id'],'result':result}),flush=True)
 if m['method']=='turn/start':
  for notification in NOTIFICATIONS: print(json.dumps(notification),flush=True)
'''

class FailureLoggingTests(unittest.TestCase):
    def run_notifications(self, notifications, expected_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            def trace(name, **fields):
                with patch.dict(os.environ, {'AGENTTEAMS_LOG_DIR':tmp, 'MODEL_API_KEY':'private-key-123'}):
                    event(name, **fields)
            program = 'NOTIFICATIONS='+repr(notifications)+'\n'+FAKE
            with patch('test_interaction.FAKE', program), patch('agentteams_codex.bridge.trace_event', side_effect=trace):
                runner = test_interaction.InteractionTests()
                if expected_error:
                    with self.assertRaisesRegex(Exception, expected_error):
                        runner.run_case('unused', 'unused')
                else:
                    runner.run_case('unused', 'unused')
            output = (Path(tmp)/'events.jsonl').read_text()
            self.assertNotIn('private-key-123', output)
            return [json.loads(line) for line in output.splitlines()]

    def test_model_connection_error_is_persisted(self):
        records = self.run_notifications([{'method':'turn/completed','params':{'turn':{'id':'turn-1','status':'failed','error':{'message':'connection refused to model gateway; api_key=private-key-123'}}}}], 'connection refused')
        end = next(r for r in records if r['event']=='inference.end')
        self.assertIn('connection refused', end['error']['message'])
        self.assertEqual(end['event_id'], '$request')

    def test_tool_failure_keeps_error_exit_code_and_item_id(self):
        records = self.run_notifications([
            {'method':'item/completed','params':{'item':{'type':'mcpToolCall','id':'tool-1','status':'failed','error':{'message':'MCP upstream HTTP 403 forbidden'}}}},
            {'method':'item/completed','params':{'item':{'type':'commandExecution','id':'tool-2','status':'completed','exitCode':127,'aggregatedOutput':'sh: command not found'}}},
            {'method':'item/completed','params':{'item':{'type':'agentMessage','text':'{}'}}},
            {'method':'turn/completed','params':{'turn':{'id':'turn-1','status':'completed'}}},
        ])
        failures = [r for r in records if r['event']=='tool.end']
        self.assertIn('403', failures[0]['error']['message'])
        self.assertEqual(failures[1]['exit_code'],127)
        self.assertEqual(failures[1]['item_id'],'tool-2')
        self.assertEqual(failures[1]['status'],'failed')
        self.assertIn('command not found', failures[1]['error'])
