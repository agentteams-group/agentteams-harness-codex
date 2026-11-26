import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

import yaml
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentteams_codex.config import prepare, read_runtime


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / "plugin"
        for path, text in {
            "plugin.yaml": "skills:\n  agent:\n    - id: basic\n      path: skills/basic\n      roles: [leader, worker]\n  team:\n    - id: delegate\n      path: skills/delegate\n      roles: [leader]\n",
            "skills/basic/SKILL.md": "basic skill",
            "skills/delegate/SKILL.md": "leader skill",
            "prompts/team/TEAMS.md": "team contract",
            "prompts/agent/worker.md": "You are the Worker.",
            "prompts/agent/leader.md": "You are the Leader.",
        }.items():
            destination = self.plugin / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text)
        self.config = {
            "member": {"runtime": "codex", "role": "worker"},
            "team": {"name": "team-a"},
            "desired": {
                "model": {
                    "model": "test-model",
                    "gatewayUrl": "https://gateway/model-route",
                },
                "inlineConfig": {"identity": "identity-marker"},
            },
        }

    def prepare(self, task_scope=None):
        with patch.dict(os.environ, {}, clear=True):
            model, instructions = prepare(
                self.config, self.root / ".codex", self.root / "workspace", self.plugin,
                task_scope=task_scope,
            )
        return (
            model,
            instructions,
            tomllib.loads((self.root / ".codex/config.toml").read_text()),
        )

    def test_assigned_skill_failures_do_not_prevent_native_config_and_recover(self):
        self.config["desired"]["skills"] = ["healthy", "broken", "missing"]
        for name, content in [("healthy", "# Healthy\n"), ("broken", "---\nname: [broken\n---\n")]:
            path = self.root / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.prepare()
        self.assertTrue((self.root / ".agents/skills/healthy/SKILL.md").is_file())
        self.assertFalse((self.root / ".agents/skills/broken").exists())
        import json
        self.assertEqual(len(json.loads((self.root / ".codex/skill-warnings.json").read_text())), 2)
        for name in ["broken", "missing"]:
            path = self.root / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Repaired\n")
        self.prepare()
        self.assertEqual(json.loads((self.root / ".codex/skill-warnings.json").read_text()), [])
        self.assertTrue((self.root / ".agents/skills/broken/SKILL.md").is_file())

    def test_assigned_role_contract_reaches_every_native_turn_without_a_file_tool(self):
        self.config["member"]["role"] = "team_leader"
        self.config["desired"]["skills"] = ["squad-role"]
        skill = self.root / "skills/squad-role/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Squad role\nUse the configured repository route and pass accepted bundles to review.\n")
        for scope in (None, {"planning": ["main"]}, {"execution": "work"},
                      {"review": ["work", "submission"]}, {"completion": "main"}):
            with self.subTest(scope=scope):
                _, instructions, _ = self.prepare(scope)
                self.assertIn(skill.read_text(), instructions)

    def test_missing_assigned_role_blocks_task_execution_but_keeps_configuration_repairable(self):
        self.config["desired"]["skills"] = ["missing-role"]
        self.prepare()
        for scope in ({"planning": ["main"]}, {"execution": "work"},
                      {"review": ["work", "submission"]}, {"completion": "main"}):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, "Assigned Skill unavailable"):
                self.prepare(scope)

    def test_worker_has_no_leader_skills_and_gateway_keeps_route(self):
        model, prompt, config = self.prepare()
        self.assertEqual(model, "test-model")
        self.assertIn("identity-marker", prompt)
        self.assertIn("You are the Worker", prompt)
        self.assertEqual(
            config["model_providers"]["agentteams"]["base_url"],
            "https://gateway/model-route/v1",
        )
        self.assertEqual(
            [path.name for path in (self.root / ".agents/skills").iterdir()], ["basic"]
        )
        self.assertTrue(config["mcp_servers"]["teamharness"]["required"])
        self.assertEqual(config["mcp_servers"]["teamharness"]["default_tools_approval_mode"], "approve")
        self.assertEqual(
            config["mcp_servers"]["teamharness"]["env"]["AGENTTEAMS_AGENT_ROLE"],
            "worker",
        )

    def test_managed_worker_uses_worker_gateway_credentials_and_matrix_transport(self):
        runtime_dir = self.root / "runtime"
        runtime_dir.mkdir()
        path = runtime_dir / "runtime.yaml"
        record = {**self.config, "kind": "MemberRuntimeConfig", "apiVersion": "agentteams.io/v1beta1",
                  "storage": {"memberPrefix": "agents/alice"}, "member": {**self.config["member"],
                  "runtimeName": "alice", "matrixUserId": "@alice:test"},
                  "matrix": {"transport": {"homeserver": "https://matrix"}},
                  "credentials": {"matrixTokenEnv": "MY_MATRIX", "gatewayKeyEnv": "MY_GATEWAY"}}
        path.write_text(yaml.safe_dump(record))
        env = {"AGENTTEAMS_WORKER_NAME": "alice", "AGENTTEAMS_CONTROLLER_URL": "http://controller",
               "AGENTTEAMS_PROCESS_ID": "boot", "MY_MATRIX": "matrix-example", "MY_GATEWAY": "worker-example",
               "CODEX_BASE_URL": "https://obsolete", "CODEX_GATEWAY_KEY": "obsolete", "CODEX_AUTH_MODE": "chatgpt"}
        with patch.dict(os.environ, env, clear=True):
            config = read_runtime(path)
            config["desired"]["inlineConfig"]["soul"] = "previous soul"
            (self.root / "SOUL.md").write_text("durable personality")
            model, prompt = prepare(config, self.root / ".codex", self.root / "workspace", self.plugin)
            self.assertIn("durable personality", prompt)
            self.assertNotIn("previous soul", prompt)
            self.assertEqual(model, "test-model")
            self.assertEqual(config["matrix"]["accessToken"], "matrix-example")
            self.assertEqual(config["desired"]["model"]["gatewayKey"], "worker-example")
            raw = (self.root / ".codex/config.toml").read_text()
            native = tomllib.loads(raw)
            self.assertEqual(native['sandbox_mode'], 'workspace-write')
            self.assertTrue(native['sandbox_workspace_write']['network_access'])
            self.assertTrue(native['sandbox_workspace_write']['exclude_slash_tmp'])
            self.assertEqual(tomllib.loads(raw)["model_providers"]["agentteams"]["base_url"], "https://gateway/model-route/v1")
            self.assertNotIn("worker-example", raw)
            record["storage"]["memberPrefix"] = "agents/bob"
            path.write_text(yaml.safe_dump(record))
            with self.assertRaisesRegex(ValueError, "storage scope"):
                read_runtime(path)

    def test_role_change_replaces_discovered_skills(self):
        self.config["member"]["role"] = "team_leader"
        _, prompt, _ = self.prepare()
        self.assertIn("You are the Leader", prompt)
        self.assertEqual(len(list((self.root / ".agents/skills").iterdir())), 2)
        self.config["member"]["role"] = "worker"
        self.prepare()
        self.assertFalse((self.root / ".agents/skills/delegate").exists())

    def test_gateway_disables_native_web_search_on_every_prepare(self):
        for _ in range(2):
            _, _, config = self.prepare()
            self.assertEqual(config.get("web_search"), "disabled")

    def test_unmanaged_account_keeps_native_network_default(self):
        _, _, config = self.prepare()
        self.assertNotIn('network_access', config['sandbox_workspace_write'])

    def test_chatgpt_keeps_native_web_search_default(self):
        self.prepare()
        with patch.dict(os.environ, {"CODEX_AUTH_MODE": "chatgpt"}, clear=True):
            prepare(
                self.config, self.root / ".codex", self.root / "workspace", self.plugin
            )
        config = tomllib.loads((self.root / ".codex/config.toml").read_text())
        self.assertNotIn("web_search", config)
        self.assertNotIn("model_provider", config)

    def test_native_mcp_inherits_at_credentials_by_name(self):
        with patch.dict(
            os.environ,
            {
                "AGENTTEAMS_WORKER_MATRIX_TOKEN": "test-only-token",
                "MC_HOST_storage": "test-only-storage",
                "PYTHONPATH": "/opt/agentteams/scripts/lib:/opt/agentteams/transport",
                "UNRELATED_SECRET": "test-only-secret",
            },
            clear=True,
        ):
            prepare(
                self.config, self.root / ".codex", self.root / "workspace", self.plugin
            )
        raw = (self.root / ".codex/config.toml").read_text()
        config = tomllib.loads(raw)["mcp_servers"]["teamharness"]
        self.assertIn("AGENTTEAMS_WORKER_MATRIX_TOKEN", config["env_vars"])
        self.assertIn("MC_HOST_storage", config["env_vars"])
        self.assertIn("PYTHONPATH", config["env_vars"])
        self.assertNotIn("UNRELATED_SECRET", config["env_vars"])
        self.assertNotIn("/opt/agentteams/scripts/lib", raw)
        self.assertNotIn("test-only-token", raw)
        self.assertEqual(
            config["env"]["TEAMHARNESS_SHARED_DIR"], str(self.root / "shared")
        )

    def test_mcp_approval_rules_refresh_for_each_turn(self):
        server = {"name": "github", "url": "https://gateway/mcp/github"}
        self.config["desired"]["mcpServers"] = [server]
        def rendered():
            return self.prepare()[2]["mcp_servers"]["github"]
        self.assertNotIn("default_tools_approval_mode", rendered())
        for mode, native in (("allow", "approve"), ("ask", "prompt")):
            server["approvals"] = {"tools": "*", "mode": mode}
            self.assertEqual(rendered()["default_tools_approval_mode"], native)
        server["approvals"]["mode"] = "deny"
        self.assertFalse(rendered()["enabled"])
        server["approvals"]["tools"] = "create_branch"
        with self.assertRaisesRegex(ValueError, "require tools"):
            rendered()

    def test_unsupported_package_and_sse_fail_explicitly(self):
        self.config["desired"]["agentPackage"] = {
            "ref": "https://example.invalid/package.zip"
        }
        with self.assertRaisesRegex(ValueError, "AgentSpec packages"):
            self.prepare()
        del self.config["desired"]["agentPackage"]
        self.config["desired"]["mcpServers"] = [
            {"name": "legacy", "url": "https://example.invalid/sse", "transport": "sse"}
        ]
        with self.assertRaisesRegex(ValueError, "legacy SSE"):
            self.prepare()

    def test_runtime_contract_rejects_other_runtime(self):
        path = self.root / "runtime.yaml"
        path.write_text("member:\n  runtime: qwenpaw\n")
        with self.assertRaisesRegex(ValueError, "codex member"):
            read_runtime(path)


if __name__ == "__main__":
    unittest.main()
