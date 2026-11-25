import json
import os
import sys
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"),
                str(Path(os.environ["AT_SOURCE"]) / "shared/lib")]
from agentteams_codex.tool_progress import ToolProgress, run_id


class ToolProgressTests(unittest.TestCase):
    def setUp(self):
        self.matrix = Mock()
        self.matrix.send_content.return_value = "$tool"
        self.event = {"room_id": "!task:test", "event_id": "$assignment"}
        self.progress = ToolProgress(self.matrix, self.event, "thread:turn")

    def content(self, index=-1):
        return self.matrix.send_content.call_args_list[index].args[1]

    def test_command_start_and_finish_edit_one_card_with_actual_details(self):
        item = {"type": "commandExecution", "id": "call-1", "command": "node --test", "cwd": "/workspace"}
        with patch("agentteams_codex.tool_progress.time.time", side_effect=[100, 102.5]):
            self.progress.update("item/started", item)
            self.progress.update("item/completed", {**item, "status": "completed", "exitCode": 0, "aggregatedOutput": "24 tests passed"})
        start, edit = self.content(0), self.content(1)
        block = start["org.agentteams.run"]["blocks"][0]
        self.assertEqual(block["tool_name"], "execute_command")
        self.assertEqual(block["arguments"], {"command": "node --test", "cwd": "/workspace"})
        self.assertEqual(block["status"], "running")
        self.assertTrue(start["org.agentteams.progress_only"])
        self.assertEqual(start["m.mentions"], {"user_ids": []})
        self.assertEqual(start["m.relates_to"]["m.in_reply_to"]["event_id"], "$assignment")
        self.assertEqual(edit["m.relates_to"], {"rel_type": "m.replace", "event_id": "$tool"})
        end = edit["m.new_content"]["org.agentteams.run"]["blocks"][0]
        self.assertEqual(end["tool_call_id"], block["tool_call_id"])
        self.assertEqual(end["status"], "succeeded")
        self.assertEqual((end["started_at"], end["finished_at"]), (100000, 102500))
        self.assertEqual(end["result"], {"output": "24 tests passed", "exit_code": 0})

    def test_consecutive_tools_are_not_throttled_and_failed_exit_is_not_success(self):
        for i in range(2):
            item = {"type": "commandExecution", "id": str(i), "command": "false"}
            self.progress.update("item/started", item)
            self.progress.update("item/completed", {**item, "status": "completed", "exitCode": 1, "aggregatedOutput": "failed"})
        self.assertEqual(self.matrix.send_content.call_count, 4)
        self.assertEqual(self.content()["m.new_content"]["org.agentteams.run"]["blocks"][0]["status"], "failed")

    def test_mcp_result_error_and_file_changes_use_public_protocol(self):
        item = {"type": "mcpToolCall", "id": "mcp1", "server": "teamharness", "tool": "task", "arguments": {"action": "begin"}}
        self.progress.update("item/started", item)
        self.progress.update("item/completed", {**item, "status": "completed", "result": {"isError": True, "content": [{"type": "text", "text": "not ready"}]}})
        block = self.content()["m.new_content"]["org.agentteams.run"]["blocks"][0]
        self.assertEqual(block["tool_name"], "teamharness.task")
        self.assertEqual(block["status"], "failed")
        self.progress.update("item/completed", {"type": "fileChange", "id": "file1", "status": "completed", "changes": [{"path": "team.html", "kind": "add", "diff": "+<h1>小队</h1>"}]})
        block = self.content()["org.agentteams.run"]["blocks"][0]
        self.assertEqual(block["tool_name"], "apply_patch")
        self.assertEqual(block["arguments"]["changes"][0]["path"], "team.html")
        self.assertNotIn("started_at", block)

    def test_background_digest_and_unrelated_items_do_not_send_messages(self):
        progress = ToolProgress(self.matrix, {**self.event, "_task_protocol_meta": {"teamharness_coordination": {"kind": "assistant_digest"}}}, "digest")
        progress.update("item/started", {"type": "commandExecution", "id": "1", "command": "ls"})
        self.progress.update("item/started", {"type": "agentMessage", "id": "2"})
        self.matrix.send_content.assert_not_called()

    def test_redacts_and_bounds_arguments_and_results(self):
        with patch.dict(os.environ, {"MODEL_API_KEY": "private-key-123"}):
            self.progress.update("item/completed", {"type": "mcpToolCall", "id": "secret", "server": "api", "tool": "query", "status": "completed", "arguments": {"api_key": "do-not-show", "command": "curl -H 'Authorization: Bearer private-key-123'"}, "result": {"output": "private-key-123" + "x" * 20000}})
        serialized = json.dumps(self.content())
        self.assertNotIn("private-key-123", serialized)
        self.assertNotIn("do-not-show", serialized)
        block = self.content()["org.agentteams.run"]["blocks"][0]
        self.assertLess(len(json.dumps(block["result"])), 5000)

    def test_missing_command_exit_code_is_not_reported_as_success(self):
        self.progress.update("item/completed", {"type": "commandExecution", "id": "async", "command": "sleep 60", "status": "inProgress", "processId": "42"})
        block = self.content()["org.agentteams.run"]["blocks"][0]
        self.assertEqual(block["status"], "running")
        self.assertNotIn("finished_at", block)

    def test_only_public_summary_and_commentary_are_projected_as_thinking(self):
        self.progress.update('item/completed', {'type': 'reasoning', 'id': 'summary',
            'summary': ['Check the page requirements.'], 'content': ['private raw reasoning']})
        self.progress.update('item/completed', {'type': 'agentMessage', 'id': 'commentary',
            'phase': 'commentary', 'text': 'I will run the focused tests.'})
        self.progress.update('item/completed', {'type': 'agentMessage', 'id': 'answer',
            'phase': 'final_answer', 'text': 'Done'})
        self.assertEqual(self.matrix.send_content.call_count, 2)
        self.assertEqual(self.content()['org.agentteams.run']['blocks'][0]['type'], 'thinking')
        self.assertNotIn('private raw reasoning', str(self.matrix.send_content.call_args_list))

    def test_task_run_identity_survives_transport_recovery(self):
        event = {'recovery': {'team_id': 'squad', 'task_id': 'execution', 'generation': 2}}
        self.assertEqual(run_id(event), 'codex:task:squad:execution:2')
        self.assertNotEqual(run_id(self.event), run_id({**self.event, 'event_id': '$other'}))

    def test_sdk_duration_is_used_when_available(self):
        item = {'type': 'mcpToolCall', 'id': 'duration', 'server': 'api', 'tool': 'get'}
        with patch('agentteams_codex.tool_progress.time.time', side_effect=[100, 105]):
            self.progress.update('item/started', item)
            self.progress.update('item/completed', {**item, 'status': 'completed', 'durationMs': 2400})
        block = self.content()['m.new_content']['org.agentteams.run']['blocks'][0]
        self.assertEqual(block['finished_at'] - block['started_at'], 2400)
