import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
source = Path(os.environ['AT_SOURCE'])
transport = source / 'deepseek-harness/scripts' if (source / 'deepseek-harness').is_dir() else source
plugin = source / 'plugins/teamharness' if (source / 'plugins/teamharness').is_dir() else source.parent / 'plugins/teamharness'
sys.path[:0] = [str(transport), str(source / 'shared/lib'), str(source.parent / 'scripts/lib')]
from agentteams_codex.config import prepare
from agentteams_codex.bridge import actionable_events, Bridge


class ManagerTests(unittest.TestCase):
    def test_matrix_sync_reports_the_manager_resource_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime.yaml'
            runtime.write_text('member:\n  runtime: codex\n  role: manager\n  personalRoomId: "!dm:local"\n')
            with patch.dict(os.environ, {'AGENTTEAMS_WORKER_NAME': 'manager',
                'AGENTTEAMS_WORKER_CR_NAME': 'default', 'AGENTTEAMS_AGENT_ROLE': 'manager',
                'TEAMHARNESS_RUNTIME_CONFIG': str(runtime), 'TEAMHARNESS_WORKSPACE': str(root / 'workspace'),
                'CODEX_HOME': str(root / '.codex'), 'AGENTTEAMS_MATRIX_URL': 'http://matrix',
                'AGENTTEAMS_WORKER_MATRIX_TOKEN': 'test-token', 'AGENTTEAMS_MATRIX_DOMAIN': 'local',
                'AGENTTEAMS_CONTROLLER_URL': 'http://controller', 'AGENTTEAMS_LOG_DIR': str(root / 'logs')}, clear=True):
                bridge = Bridge()
                def sync(*args):
                    bridge.stop.set()
                    return {'next_batch': 'cursor'}
                bridge.matrix.sync = Mock(side_effect=sync)
                with patch('agentteams_codex.bridge.subprocess.run') as report:
                    bridge.poll()
                self.assertTrue(bridge.reported_ready)
                self.assertGreater(bridge.last_sync, 0)
                self.assertEqual(report.call_args.args[0], ['agt', 'worker', 'report-ready', '--name', 'default'])

    def test_manager_uses_shared_prompts_and_resource_skills_without_a_team(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            _, prompt = prepare({
                'member': {'runtime': 'codex', 'role': 'manager', 'name': 'default'},
                'desired': {'model': {'model': 'test', 'gatewayUrl': 'http://gateway'}},
            }, root / '.codex', root / 'workspace', plugin)
            config = tomllib.loads((root / '.codex/config.toml').read_text())
            self.assertIn('Manager', prompt)
            self.assertNotIn('independent AgentTeams member', prompt)
            self.assertTrue((root / '.agents/skills/resource-management/SKILL.md').exists())
            self.assertTrue((root / '.agents/skills/task-diagnosis/SKILL.md').exists())
            self.assertEqual(config['mcp_servers']['teamharness']['env']['AGENTTEAMS_AGENT_ROLE'], 'manager')

    def test_manager_group_requires_authorized_sender_and_mention(self):
        event = {'type': 'm.room.message', 'sender': '@worker:local', 'event_id': '$one',
                 'content': {'msgtype': 'm.text', 'body': 'hello', 'm.mentions': {'user_ids': ['@manager:local']}}}
        payload = {'rooms': {'join': {'!room': {'timeline': {'events': [event]}}}}}
        args = (payload, {'!room': 'team'}, '@manager:local', {'@worker:local'})
        self.assertEqual(len(actionable_events(*args, allowed_senders={'@worker:local'})), 1)
        self.assertEqual(actionable_events(*args, allowed_senders={'@admin:local'}), [])
        event['content']['m.mentions'] = {}
        self.assertEqual(actionable_events(*args, allowed_senders={'@worker:local'}), [])
