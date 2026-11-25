"""Translate the Controller contract without modifying Codex itself."""

import json
import os
from pathlib import Path
import sys
import subprocess

import yaml
from task_recovery import enabled as managed_worker
from worker_runtime_config import project
from agentteams_skill_warnings import check_skill, report_warnings
from .native_telemetry import configuration as native_telemetry_configuration


def read_runtime(path: Path) -> dict:
    config = yaml.safe_load(path.read_text())
    if (
        not isinstance(config, dict)
        or config.get("member", {}).get("runtime") != "codex"
    ):
        raise ValueError("runtime.yaml must describe a codex member")
    if managed_worker():
        if config["member"].get("role") == "manager":
            raise ValueError("Worker received a Manager runtime configuration")
        legacy = None
        compatibility = path.parent.parent / "openclaw.json"
        if config.get("desired", {}).get("model") is None and compatibility.is_file():
            legacy = json.loads(compatibility.read_text())
        projected = project(config, "codex", os.environ["AGENTTEAMS_WORKER_NAME"], legacy)
        matrix = projected["channels"]["matrix"]
        config["matrix"] = {**config.get("matrix", {}), "accessToken": matrix["accessToken"]}
        model_ref = projected["agents"]["defaults"]["model"]["primary"]
        provider_id, model = model_ref.split("/", 1)
        provider = projected["models"]["providers"][provider_id]
        config.setdefault("desired", {})["model"] = {**(config.get("desired", {}).get("model") or {}),
                                                    "model": model, "gatewayKey": provider["apiKey"],
                                                    "gatewayUrl": provider["baseUrl"]}
    return config


def _role(config: dict) -> str:
    member = config["member"]
    role = member.get("role", "standalone").replace("_", "-")
    role = {
        "team-leader": "leader",
        "remote": "remote-member",
        "standalone": "worker",
    }.get(role, role)
    if role not in {"manager", "leader", "worker", "remote-member"}:
        raise ValueError(f"unsupported Codex role: {role}")
    return role


def sync_assigned_skills(config: dict, home: Path, plugin: Path, builtin_ids=None) -> list[str]:
    names = set(config.get("desired", {}).get("skills", []))
    if not names:
        builtin_ids = set()
    elif builtin_ids is None:
        manifest = yaml.safe_load((plugin / "plugin.yaml").read_text())
        role = _role(config)
        builtin_ids = {entry["id"] for section in ("manager", "agent", "team")
                       for entry in manifest.get("skills", {}).get(section, []) if role in entry.get("roles", [])}
    root = home.parent / "skills"
    target = home.parent / ".agents" / "skills"
    target.mkdir(parents=True, exist_ok=True)
    warnings = []
    for name in sorted(names - set(builtin_ids)):
        try:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError("invalid skill name")
            source = root / name
            if not source.resolve().is_relative_to(root.resolve()):
                raise ValueError("skill path escapes workspace")
            prefix = os.getenv("AGENTTEAMS_STORAGE_PREFIX")
            worker = os.getenv("AGENTTEAMS_WORKER_NAME")
            if prefix and worker:
                subprocess.run(["mc", "mirror", f"{prefix.rstrip('/')}/agents/{worker}/skills/{name}/", str(source), "--overwrite"],
                               check=True, capture_output=True, timeout=15)
            check_skill(source)
            destination = target / name
            if not destination.exists():
                destination.symlink_to(source)
        except Exception as exc:
            warnings.append(f"Skill {name}: load failed ({type(exc).__name__})")
            if name and name not in {".", ".."} and "/" not in name and "\\" not in name:
                destination = target / name
                if destination.is_symlink():
                    destination.unlink()
    home.mkdir(parents=True, exist_ok=True)
    (home / "skill-warnings.json").write_text(json.dumps(warnings))
    report_warnings(warnings)
    return warnings


def prepare(config: dict, home: Path, workspace: Path, plugin: Path, *, trace_attributes=None, task_scope=None) -> tuple[str, str]:
    member = config["member"]
    role = _role(config)
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
    for section in ("manager", "agent", "team"):
        for skill in manifest.get("skills", {}).get(section, []):
            if role in skill.get("roles", []):
                selected_ids.add(skill["id"])
                if skill["id"] == "mcporter":
                    continue  # Codex exposes the same tools through native MCP.
                path = (plugin / skill["path"]).resolve()
                if not path.is_relative_to(plugin.resolve()):
                    raise ValueError("skill path escapes TeamHarness package")
                skills.append(path / "SKILL.md")
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
    sync_assigned_skills(config, home, plugin, selected_ids)
    assigned = []
    for name in sorted(set(desired.get("skills", [])) - selected_ids):
        source = user_skills / name
        if (source.is_symlink() and source.resolve().is_relative_to((home.parent / "skills").resolve())
                and (source / "SKILL.md").is_file()):
            assigned.append(source / "SKILL.md")
        elif any((task_scope or {}).get(key) for key in ("execution", "planning", "review", "completion")):
            raise ValueError(f"Assigned Skill unavailable for Task work: {name}")
    context = [
        (
            (plugin / "prompts/team/TEAMS.md").read_text()
            if config.get("team") and role != "manager"
            else "" if role == "manager" else "You are an independent AgentTeams member. Answer in the current room; do not invent a Team."
        ),
        (plugin / f"prompts/agent/{role}.md").read_text() if config.get("team") and role != "manager" else "",
        "Current AgentTeams context:\n"
        + yaml.safe_dump(
            {"member": member, "team": config.get("team", {})}, allow_unicode=True
        ),
        "" if any((task_scope or {}).get(key) for key in ("execution", "planning", "review", "completion")) else
        "Files received from Matrix are in inbox/. Read them with your tools. Put files you intend to return in outbox/. Reply directly in the current room; do not send the same reply again through MCP.",
        "Use your native TeamHarness task tool for Task operations. The task system prepares inputs, advances accepted dependencies and delivers results to the original conversation automatically. Read its receipts; do not duplicate notifications.",
    ]
    if role == "manager":
        for path in manifest.get("prompts", {}).get("manager", {}).values():
            context.append((plugin / path).read_text())
    task_contract = plugin / "skills/team/task-management/SKILL.md"
    if task_contract.is_file():
        contract = task_contract.read_text()
        context.append(contract.partition("## Plan as Leader")[0] if role == "manager" else contract)
    inline = dict(desired.get("inlineConfig", {}))
    if managed_worker():
        for field, filename in (("identity", "IDENTITY.md"), ("soul", "SOUL.md"), ("agents", "AGENTS.md")):
            asset = home.parent / filename
            if asset.is_file():
                inline[field] = asset.read_text()
    for field, value in inline.items():
        if value:
            context.append(f"{field}:\n{value}")
    # Explicit Worker associations are role contracts, including in Task phases
    # where the native policy deliberately disallows shell/file tools.
    context.extend(f"Assigned Skill {skill.parent.name} (already loaded; follow this contract):\n{skill.read_text()}"
                   for skill in assigned)
    model_config = desired.get("model", {})
    model = model_config.get("model") or os.getenv("CODEX_MODEL", "gpt-5.3-codex")
    lines = [
        f"model = {json.dumps(model)}",
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
    ]
    gateway = model_config["gatewayUrl"] if managed_worker() else (
        os.getenv("CODEX_BASE_URL")
        or model_config.get("gatewayUrl")
        or os.getenv("AGENTTEAMS_AI_GATEWAY_URL")
    )
    # Explicit local-account mode is useful on trusted developer machines.
    # Production uses the Controller-issued gateway credential by default.
    if managed_worker() or os.getenv("CODEX_AUTH_MODE", "gateway") != "chatgpt":
        if not gateway:
            raise ValueError("Codex requires a Responses API gateway URL")
        gateway = gateway.rstrip("/")
        if not gateway.endswith("/v1"):
            gateway += "/v1"
        lines += [
            # Responses gateways need not implement Codex's native search tool.
            'web_search = "disabled"',
            'model_provider = "agentteams"',
            "[model_providers.agentteams]",
            'name = "AgentTeams"',
            f"base_url = {json.dumps(gateway)}",
            'env_key = "CODEX_GATEWAY_KEY"',
            'wire_api = "responses"',
            "supports_websockets = false",
        ]
    lines += ['[sandbox_workspace_write]', 'exclude_slash_tmp = true']
    if os.getenv('AGENTTEAMS_CONTROLLER_URL'):
        lines += ['network_access = true']
    lines += [
        "[mcp_servers.teamharness]",
        f"command = {json.dumps(sys.executable)}",
        'args = ["-m", "agentteams_codex.task_protocol"]',
        f"cwd = {json.dumps(str(plugin))}",
        "required = true",
        # Builtin TeamHarness already enforces Controller role/task authority.
        'default_tools_approval_mode = "approve"',
        "startup_timeout_sec = 30",
        "env_vars = "
        + json.dumps(
            sorted(
                name
                for name in os.environ
                if name == "PYTHONPATH" or name.startswith(("AGENTTEAMS_", "TEAMHARNESS_", "MC_"))
            ) + ["AGENTTEAMS_CODEX_TASK_CONTEXT", "AGENTTEAMS_CODEX_INPUT_BARRIER", "AGENTTEAMS_CODEX_NATIVE_SCOPE"]
        ),
    ]
    lines += [
        "[mcp_servers.teamharness.env]",
        f"AGENTTEAMS_AGENT_ROLE = {json.dumps(role)}",
        f"AGENTTEAMS_WORKER_ROLE = {json.dumps(role)}",
        f"AGENTTEAMS_PLUGIN_DIR = {json.dumps(str(plugin))}",
        f"TEAMHARNESS_SHARED_DIR = {json.dumps(os.getenv('TEAMHARNESS_SHARED_DIR') or os.getenv('AGENTTEAMS_SHARED_DIR') or str(home.parent / 'shared'))}",
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
    lines += native_telemetry_configuration(trace_attributes)
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    return model, "\n\n".join(filter(None, context))
