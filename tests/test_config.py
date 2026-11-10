import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
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

    def prepare(self):
        with patch.dict(os.environ, {}, clear=True):
            model, instructions = prepare(
                self.config, self.root / ".codex", self.root / "workspace", self.plugin
            )
        return (
            model,
            instructions,
            tomllib.loads((self.root / ".codex/config.toml").read_text()),
        )

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
        self.assertEqual(
            config["mcp_servers"]["teamharness"]["env"]["AGENTTEAMS_AGENT_ROLE"],
            "worker",
        )

    def test_role_change_replaces_discovered_skills(self):
        self.config["member"]["role"] = "team_leader"
        _, prompt, _ = self.prepare()
        self.assertIn("You are the Leader", prompt)
        self.assertEqual(len(list((self.root / ".agents/skills").iterdir())), 2)
        self.config["member"]["role"] = "worker"
        self.prepare()
        self.assertFalse((self.root / ".agents/skills/delegate").exists())

    def test_native_mcp_inherits_at_credentials_by_name(self):
        with patch.dict(
            os.environ,
            {
                "AGENTTEAMS_WORKER_MATRIX_TOKEN": "test-only-token",
                "MC_HOST_storage": "test-only-storage",
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
        self.assertNotIn("test-only-token", raw)
        self.assertEqual(
            config["env"]["TEAMHARNESS_SHARED_DIR"], str(self.root / "workspace/shared")
        )

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
