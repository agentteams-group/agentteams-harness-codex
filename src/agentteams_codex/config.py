"""Translate the Controller contract without modifying Codex itself."""

import json
import os
from pathlib import Path
import sys

import yaml


def read_runtime(path: Path) -> dict:
    config = yaml.safe_load(path.read_text())
    if (
        not isinstance(config, dict)
        or config.get("member", {}).get("runtime") != "codex"
    ):
        raise ValueError("runtime.yaml must describe a codex member")
    return config


def prepare(config: dict, home: Path, workspace: Path, plugin: Path) -> tuple[str, str]:
    member = config["member"]
    role = member.get("role", "standalone").replace("_", "-")
    role = {
        "team-leader": "leader",
        "remote": "remote-member",
        "standalone": "worker",
    }.get(role, role)
    if role not in {"leader", "worker", "remote-member"}:
        raise ValueError(f"unsupported Codex role: {role}")
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    desired = config.get("desired", {})
    # Fail explicitly instead of silently ignoring an AgentSpec package.
    if desired.get("agentPackage", {}).get("ref"):
        raise ValueError(
            "Codex AgentSpec packages are not supported yet; use inlineConfig and Skills"
        )
    manifest = yaml.safe_load((plugin / "plugin.yaml").read_text())
    skills = []
    selected_ids = set()
    for section in ("agent", "team"):
        for skill in manifest.get("skills", {}).get(section, []):
            if role in skill.get("roles", []):
                selected_ids.add(skill["id"])
                if skill["id"] == "mcporter":
                    continue  # Codex exposes the same tools through native MCP.
                path = (plugin / skill["path"]).resolve()
                if not path.is_relative_to(plugin.resolve()):
                    raise ValueError("skill path escapes TeamHarness package")
                skills.append(path / "SKILL.md")
    unsupported_skills = set(desired.get("skills", [])) - selected_ids
    if unsupported_skills:
        raise ValueError(
            "Additional AT Skills are not supported yet: "
            + ", ".join(sorted(unsupported_skills))
        )
    # Skills are installed outside the coding checkout so project AGENTS.md
    # remains under the project's control.
    user_skills = home.parent / ".agents" / "skills"
    user_skills.mkdir(parents=True, exist_ok=True)
    for old in user_skills.iterdir():
        if old.is_symlink():
            old.unlink()
    for skill in skills:
        destination = user_skills / skill.parent.name
        if not destination.exists():
            destination.symlink_to(skill.parent)
    context = [
        (
            (plugin / "prompts/team/TEAMS.md").read_text()
            if config.get("team")
            else "You are an independent AgentTeams member. Answer in the current room; do not invent a Team."
        ),
        (plugin / f"prompts/agent/{role}.md").read_text() if config.get("team") else "",
        "Current AgentTeams context:\n"
        + yaml.safe_dump(
            {"member": member, "team": config.get("team", {})}, allow_unicode=True
        ),
        "Files received from Matrix are in inbox/. Read them with your tools. Put files you intend to return in outbox/. Reply directly in the current room; do not send the same reply again through MCP.",
        "Use your native TeamHarness MCP tools for collaboration, task state, file synchronization and artifacts.",
        "After sending a request to another member, end your turn so its reply can be processed as the next turn. Only report successful delivery or task completion after checking tool results and member evidence.",
    ]
    for field, value in desired.get("inlineConfig", {}).items():
        if value:
            context.append(f"{field}:\n{value}")
    model_config = desired.get("model", {})
    model = model_config.get("model") or os.getenv("CODEX_MODEL", "gpt-5.3-codex")
    lines = [
        f"model = {json.dumps(model)}",
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
    ]
    gateway = (
        os.getenv("CODEX_BASE_URL")
        or model_config.get("gatewayUrl")
        or os.getenv("AGENTTEAMS_AI_GATEWAY_URL")
    )
    # Explicit local-account mode is useful on trusted developer machines.
    # Production uses the Controller-issued gateway credential by default.
    if os.getenv("CODEX_AUTH_MODE", "gateway") != "chatgpt":
        if not gateway:
            raise ValueError("Codex requires a Responses API gateway URL")
        gateway = gateway.rstrip("/")
        if not gateway.endswith("/v1"):
            gateway += "/v1"
        lines += [
            'model_provider = "agentteams"',
            "[model_providers.agentteams]",
            'name = "AgentTeams"',
            f"base_url = {json.dumps(gateway)}",
            'env_key = "CODEX_GATEWAY_KEY"',
            'wire_api = "responses"',
            "supports_websockets = false",
        ]
    lines += [
        "[mcp_servers.teamharness]",
        f"command = {json.dumps(sys.executable)}",
        f"args = [{json.dumps(str(plugin / 'mcp/server.py'))}]",
        f"cwd = {json.dumps(str(plugin))}",
        "required = true",
        "startup_timeout_sec = 30",
        "env_vars = "
        + json.dumps(
            sorted(
                name
                for name in os.environ
                if name.startswith(("AGENTTEAMS_", "TEAMHARNESS_", "MC_"))
            )
        ),
    ]
    lines += [
        "[mcp_servers.teamharness.env]",
        f"AGENTTEAMS_AGENT_ROLE = {json.dumps(role)}",
        f"AGENTTEAMS_WORKER_ROLE = {json.dumps(role)}",
        f"TEAMHARNESS_SHARED_DIR = {json.dumps(str(workspace / 'shared'))}",
    ]
    for server in desired.get("mcpServers", []):
        name = server.get("name", "")
        if not name or name == "teamharness":
            raise ValueError(
                "MCP server names must be unique and must not replace teamharness"
            )
        lines.append(f"[mcp_servers.{json.dumps(name)}]")
        if server.get("url"):
            if server.get("transport", "http") != "http":
                raise ValueError(
                    "Codex supports Streamable HTTP MCP; legacy SSE is not supported"
                )
            lines.append(f"url = {json.dumps(server['url'])}")
            lines.append('bearer_token_env_var = "CODEX_GATEWAY_KEY"')
        elif server.get("command"):
            lines += [
                f"command = {json.dumps(server['command'])}",
                "args = " + json.dumps(server.get("args", [])),
            ]
        else:
            raise ValueError(f"MCP server {name} requires url or command")
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    return model, "\n\n".join(filter(None, context))
