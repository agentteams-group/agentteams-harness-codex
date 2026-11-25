"""Recovery adapter unit regressions; these do not run the native model."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

source = Path(os.environ["AT_SOURCE"])
lib = source / "shared/lib" if (source / "shared/lib").is_dir() else Path("/opt/agentteams/scripts/lib")
transport = source / "deepseek-harness/scripts" if (source / "deepseek-harness").is_dir() else source
sys.path[:0] = [str(lib), str(transport), str(Path(__file__).resolve().parents[1] / "src")]
from task_recovery import Recovery, RecoveryConflict
from worker_sessions import WorkerSessions
from agentteams_codex.bridge import Bridge, Journal


class RecoveryTests(unittest.TestCase):
    def test_submitted_task_displays_its_saved_business_result_in_the_same_run(self):
        for status, summary in [
            ('SUCCESS', '已生成 quiet-result.txt，逐字节验证通过。'),
            ('REVISION_NEEDED', '检查发现内容与要求不符，需修正后复审。'),
            ('BLOCKED', '输入文件缺失，当前无法执行验证。'),
        ]:
            with self.subTest(status=status):
                self.recovery.current.return_value = {
                    **self.task, 'status': 'submitted', 'task_plan_id': 'plan',
                    'submission_id': 'submission', 'result_status': status}
                with patch('agentteams_codex.bridge.publish_raw_saved', return_value='$result') as send:
                    self.bridge.deliver_task(self.recovery, 't1', 'team', '!task:test',
                                             self.task['execution'], summary)
                self.assertEqual(send.call_args.args[2], summary)
                content = send.call_args.kwargs['content']
                self.assertEqual(content['org.agentteams.run']['blocks'],
                                 [{'type': 'text', 'text': summary}])
                self.assertEqual(content['org.agentteams.run']['run_id'],
                                 content['org.agentteams.run_id'])

    def test_stage_delivery_precedes_handoff_context_injection_without_a_model_call(self):
        from types import SimpleNamespace
        order = []
        outgoing = {'task': {'id': 't1', 'submissionId': 's1'}, 'notification': {'status': 'queued', 'eventId': 'n1'}}
        scope_path = Path(self.tmp.name) / 'scope.json'
        scope_path.write_text(json.dumps({'outgoing': outgoing}))
        scope = SimpleNamespace(path=scope_path, reply=lambda: '业务结论')
        self.bridge.plugin = Path(self.tmp.name)
        self.bridge.persist_codex = Mock(side_effect=lambda **kw: order.append('persist'))
        self.bridge.deliver_task = Mock(side_effect=lambda *a: order.append('publish-result') or '$result')
        self.recovery.prepare_reply.return_value = ({'execution': {'checkpoint': {'reply_txn': 'one'}}}, '业务结论')
        self.recovery.delivered.side_effect = lambda *a: order.append('ack-result')
        receipt = {'receiptId': 'sent-one', 'fact': '已向下游发送真实交接，不需要回复。'}
        native = SimpleNamespace(controller_json=Mock(), dispatched_receipts=Mock(side_effect=lambda *a, **kw: order.append('confirm-handoff') or [receipt]))
        client = Mock()
        client.request.side_effect = lambda *a: order.append('inject')
        with patch('agentteams_codex.bridge.common', return_value=native), patch('agentteams_codex.bridge.saved_message', side_effect=[None, 'digest']):
            self.bridge.finish_task_delivery(client, {'event_id': '$source', 'recovery': {'task_id': 't1', 'team_id': 'team'}, 'room_id': '!task:test'}, {}, scope, 'native-thread')
        self.assertLess(order.index('publish-result'), order.index('confirm-handoff'))
        self.assertLess(order.index('ack-result'), order.index('inject'))
        client.start.assert_not_called()
        self.assertEqual(client.request.call_args.args[0], 'thread/inject_items')
        self.assertEqual(json.loads(scope_path.read_text())['observed']['sent-one']['rows'], ['digest'])

    def test_late_handoff_receipt_survives_restart_and_injects_without_inference(self):
        from types import SimpleNamespace
        thread = '445a4b30-92ca-4fd8-91e4-000000000002'
        rollout = self.bridge.home / 'sessions' / ('rollout-test-' + thread + '.jsonl')
        rollout.parent.mkdir()
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': thread}}) + '\n')
        pending = {'thread_id': thread, 'scope_file': str(self.bridge.home / 'missing-scope.json'), 'observed': {},
                   'outgoing': {'task': {'id': 't1', 'submissionId': 's1'}, 'notification': {'status': 'queued', 'eventId': 'n1'}}}
        self.bridge.workspace = self.bridge.home
        self.bridge.config['member'] = {'role': 'worker', 'runtime': 'codex'}
        self.bridge.plugin = self.bridge.home
        self.bridge.persist_codex = Mock()
        self.bridge.journal.data['handoff_context'] = {'$source': pending}
        self.bridge.journal.save()
        native = SimpleNamespace(controller_json=Mock(), dispatched_receipts=Mock(return_value=[]))
        with patch('agentteams_codex.bridge.common', return_value=native), patch('agentteams_codex.bridge.Client') as factory:
            self.bridge.flush_handoff_context()
            factory.assert_not_called()
            self.assertIn('$source', self.bridge.journal.data['handoff_context'])
            self.bridge.journal = Journal(self.bridge.journal.path)
            receipt = {'receiptId': json.dumps(['dispatch', 'n1', 'matrix:$sent']), 'fact': 'Confirmed actual downstream notification.'}
            native.dispatched_receipts.return_value = [receipt]
            client = factory.return_value.__enter__.return_value
            def persist_native(method, request):
                if method == 'thread/inject_items':
                    with rollout.open('a') as stream:
                        stream.write(json.dumps({'type': 'response_item', 'payload': request['items'][0]}) + '\n')
                return {}
            client.request.side_effect = persist_native
            self.bridge.flush_handoff_context()
            self.assertEqual(self.bridge.journal.data['handoff_context'], {})
            self.assertEqual([call.args[0] for call in client.request.call_args_list], ['thread/resume', 'thread/inject_items'])
            client.start.assert_not_called()
            self.bridge.run_turn.assert_not_called()
            self.bridge.journal = Journal(self.bridge.journal.path)
            self.bridge.flush_handoff_context()
            self.assertEqual(client.request.call_count, 2)

    def test_confirmed_inner_delivery_does_not_repeat_outer_reply_preparation(self):
        event = {'room_id': '!task:test', 'event_id': '$source', 'recovery': {
            'task_id': 't1', 'team_id': 'team', 'spec': 'requirements',
            **{k: self.task['execution'][k] for k in ('instance_id', 'generation', 'process_id')}}}
        record = {'event': event}
        self.bridge.persist_codex = Mock()
        self.bridge.run_turn.side_effect = lambda *a: record.update(task_result_delivered='$business-result') or '业务结果'
        with patch('agentteams_codex.bridge.task_thread', return_value=('existing-thread', self.view)):
            self.bridge.process_recovered_task('$source', record)
        self.recovery.prepare_reply.assert_not_called()
        self.assertEqual(record['status'], 'completed')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.config = {"team": {"name": "team"}}
        self.bridge.home = Path(self.tmp.name)
        self.bridge.user_id = "@alice:test"
        self.bridge.journal = Journal(Path(self.tmp.name)/"journal.json")
        self.bridge.journal.data["cursor"] = "keep-all-backlog"
        self.bridge.matrix = Mock()
        self.bridge.run_turn = Mock()
        self.task = {"task_id": "t1", "status": "in_progress", "room_id": "!task:test", "execution": {
            "generation": 2, "process_id": "process", "runtime": "codex", "state": "active", "instance_id": "new-start", "checkpoint": {"version": 3, "completed_steps": ["one"], "remaining_steps": ["two", "three"]}}}
        self.recovery = Mock()
        self.recovery.process = "process"
        self.view = {"compatible": False, "runtime": "codex", "work": {"kind": "task", "team_id": "team", "task_id": "t1"}, "cache": None,
                     "instance_id": "new-start", "config_generation": "yaml1", "checkpoint_version": 3, "generation": 2}
        self.session_get = patch.object(WorkerSessions, "load", side_effect=self.session_view)
        self.session_get.start()
        self.addCleanup(self.session_get.stop)
        self.registration = patch("worker_matrix_state.WorkerMatrix.start")
        self.registration.start()
        self.addCleanup(self.registration.stop)
        self.recovery.resume_text.side_effect = Recovery.resume_text
        self.recovery.digest.side_effect = Recovery.digest
        self.recovery.discover.return_value = {"tasks": [{"task": self.task, "team_id": "team", "etag": "e"}], "instance_id": "new-start"}
        self.recovery.claim.return_value = {"task": self.task, "spec": "Do one, two, three", "saved_result": "Saved answer"}
        self.recovery.current.return_value = self.task
        self.factory = patch("task_recovery.Recovery", return_value=self.recovery)
        self.factory.start().digest.side_effect = Recovery.digest
        self.addCleanup(self.factory.stop)
        self.enable = patch("task_recovery.enabled", return_value=True)
        self.enable.start()
        self.addCleanup(self.enable.stop)

    def test_startup_recovers_task_without_new_matrix_input(self):
        self.bridge.journal.ingest([{"event_id": "$unrelated", "body": "keep me"},
            {"event_id": "$assignment", "room_id": "!task:test", "coordination_session": "coordination-task:t1:d1"},
            {"event_id": "$other-team", "room_id": "!other:test", "coordination_session": "coordination-task:t1:d2"}], "keep-all-backlog")
        self.bridge.recover_tasks()
        events = self.bridge.journal.data["events"]
        recovered = events["recovery:team/t1:2"]["event"]
        self.assertIn('"completed_steps": ["one"]', recovered["body"])
        self.assertEqual(recovered["recovery"]["task_id"], "t1")
        self.assertIsNone(recovered["recovery"]["thread_id"])
        self.assertEqual(events["$unrelated"]["status"], "queued")
        self.assertEqual(events["$assignment"]["status"], "superseded")
        self.assertEqual(events["$other-team"]["status"], "queued")
        self.assertEqual(recovered["session_source_event_ids"], ["$assignment"])
        self.assertEqual(self.bridge.journal.data["cursor"], "keep-all-backlog")
        self.bridge.matrix.send_text.assert_not_called()

    def test_self_discovery_does_not_depend_on_local_team_cache(self):
        self.bridge.config = {"member": {"role": "standalone"}}
        self.bridge.recover_tasks()
        self.recovery.discover.assert_called_once()
        self.assertIn("recovery:team/t1:2", self.bridge.journal.data["events"])

    def test_recovery_sources_survive_another_generation_without_synthetic_receipts(self):
        self.bridge.journal.ingest([{"event_id": "recovery:team/t1:1", "room_id": "!task:test",
            "session_source_event_ids": ["$assignment", "recovery:team/t1:0"],
            "recovery": {"task_id": "t1", "team_id": "team"}}], "keep-all-backlog")
        self.bridge.recover_tasks()
        recovered = self.bridge.journal.data["events"]["recovery:team/t1:2"]["event"]
        self.assertEqual(recovered["session_source_event_ids"], ["$assignment"])

    def session_view(self, work):
        cp = self.task["execution"]["checkpoint"]
        return {**self.view, "checkpoint_digest": Recovery.digest(cp),
                "checkpoint_version": cp["version"], "generation": self.task["execution"]["generation"]}

    def test_only_shared_descriptor_and_matching_native_bytes_can_resume(self):
        thread = "445a4b30-92ca-4fd8-91e4-000000000001"
        data = (json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n").encode()
        path = self.bridge.home / "sessions" / ("rollout-test-" + thread + ".jsonl")
        path.parent.mkdir()
        path.write_bytes(data)
        self.view.update(compatible=True, cache={"runtime": "codex", "native_ref": thread, "expires_at": 10**12,
            "native_digest": hashlib.sha256(data).hexdigest(), "config_generation": "yaml1",
            "checkpoint_version": 3, "checkpoint_digest": Recovery.digest(self.task["execution"]["checkpoint"])})
        self.bridge.journal.data["task_sessions"] = {"team/t1": {"thread_id": "obsolete"}}
        work = self.view["work"]
        from agentteams_codex.bridge import task_thread
        def select():
            return task_thread(self.bridge.home, self.recovery, self.task, work)[0]
        self.assertEqual(select(), thread)
        path.write_bytes(data + b'{}\n')
        self.assertIsNone(select())
        path.unlink()
        self.assertIsNone(select())
        path.write_bytes(data)
        self.task["execution"]["checkpoint"]["version"] += 1
        self.assertIsNone(select())

    def test_uncertain_operation_cannot_start_inference_from_journal(self):
        self.task["execution"]["state"] = "needs_verification"
        self.bridge.recover_tasks()
        self.assertFalse(self.bridge.journal.data["events"])
        self.bridge.run_turn.assert_not_called()

    def test_saved_result_only_resends_stable_transaction(self):
        self.task["status"] = "submitted"
        cp = self.task["execution"]["checkpoint"]
        cp.update(delivery="pending", reply_txn="stable-reply")
        self.bridge.matrix.request.side_effect = [
            {"device_id": "B", "user_id": "@alice:test"},
            {"chunk": [{"type": "m.room.message", "room_id": "!task:test", "event_id": "$sent", "sender": "@alice:test",
                        "content": {"msgtype": "m.text", "body": "Saved answer", "org.agentteams.reply_txn": "stable-reply"}}]}]
        cp.update(result={"size": 12, "sha256": hashlib.sha256(b"Saved answer").hexdigest()},
                  reply_attempt={"state": "sending", "user_id": "@alice:test", "device_id": "A"})
        self.bridge.recover_tasks()
        self.bridge.matrix.send_content.assert_not_called()
        self.bridge.matrix.send_text.assert_not_called()
        self.recovery.delivered.assert_called_once_with("t1", "team", "$sent")
        self.bridge.run_turn.assert_not_called()
        self.assertFalse(self.bridge.journal.data["events"])

    def test_delivered_progress_is_not_authoritative_task_completion(self):
        self.task["execution"]["checkpoint"].update(delivery="delivered", reply_txn="step-one-reply")
        self.bridge.recover_tasks()
        record = self.bridge.journal.data["events"]["recovery:team/t1:2"]
        self.assertIn('"remaining_steps": ["two", "three"]', record["event"]["body"])

    def test_pending_progress_is_delivered_before_resuming_active_task(self):
        self.task["execution"]["checkpoint"].update(delivery="pending", reply_txn="step-one-reply")
        self.bridge.deliver_task = Mock(return_value="$old")
        self.recovery.current.return_value = self.task
        self.bridge.recover_tasks()
        self.recovery.delivered.assert_called_once()
        self.assertIn("recovery:team/t1:2", self.bridge.journal.data["events"])

    def test_next_generation_runs_after_progress_but_its_delivered_turn_is_not_repeated(self):
        cp = self.task["execution"]["checkpoint"]
        cp.update(delivery="delivered", reply_txn="step-one-reply")
        self.recovery.process = "process"
        self.bridge.persist_codex = Mock()
        self.bridge.deliver_task = Mock(return_value="$new")
        self.bridge.run_turn.return_value = "Step two done"
        saved = {**cp, "version": 4, "delivery": "pending", "reply_txn": "step-two-reply"}
        self.recovery.prepare_reply.return_value = ({"execution": {"checkpoint": saved}}, "Step two done")
        delivered = {**saved, "version": 5, "delivery": "delivered"}
        self.recovery.delivered.return_value = {"execution": {"checkpoint": delivered}}
        record = {"event": {"room_id": "!task:test", "recovery": {"task_id": "t1", "team_id": "team", "spec": "Three steps", "process_id": "process", "instance_id": "new-start", "generation": 2}},
                  "status": "queued", "thread_id": "native"}
        self.bridge.process_recovered_task("recovery:team/t1:2", record)
        self.bridge.run_turn.assert_called_once()
        self.assertEqual(record["reply_txn"], "step-two-reply")
        self.task["execution"]["checkpoint"] = delivered
        record["status"] = "running"  # Crash after delivery, before local completion persisted.
        self.bridge.process_recovered_task("recovery:team/t1:2", record)
        self.bridge.run_turn.assert_called_once()
        self.assertEqual(record["status"], "completed")

    def test_reassignment_prevents_model_execution(self):
        self.recovery.current.return_value = None
        record = {"event": {"recovery": {"task_id": "t1", "team_id": "team"}}, "status": "queued"}
        self.bridge.process_recovered_task("recovery:t1", record)
        self.assertEqual(record["status"], "superseded")
        self.bridge.run_turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
