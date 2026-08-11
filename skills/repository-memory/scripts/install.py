#!/usr/bin/env python3
"""Install Repository Memory for local AI hosts from one checked-out source.

The installer is intentionally stdlib-only.  It keeps one canonical runtime in
the user data directory, publishes copies into detected Skill directories, and
registers the same stdio MCP command with supported hosts.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from version import VERSION

SKILL_NAME = "repository-memory"
MCP_NAME = "repository-memory"
OPENCLAW_PLUGIN_ID = "repository-memory-autocapture"
LEGACY_SKILL_NAMES = {"rlvr-memory"}
LEGACY_OPENCLAW_PLUGIN_IDS = {"rlvr-memory-autocapture"}
MCP_TOOLS = [
    "memory_doctor",
    "memory_sync",
    "memory_search",
    "memory_get",
    "memory_init",
    "memory_ingest",
    "memory_context",
    "memory_team_sync",
    "memory_team_activate",
    "memory_publish",
    "memory_feedback",
    "memory_supersede",
]
OPENCLAW_TOOLS = [f"{MCP_NAME}__{name}" for name in MCP_TOOLS]


def _home() -> Path:
    return Path.home().resolve()


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", _home() / ".local" / "share")).expanduser().resolve()


def _source_skill() -> Path:
    return Path(__file__).resolve().parents[1]


def _canonical_skill() -> Path:
    return _data_home() / "repository-memory" / "skill" / SKILL_NAME


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def _copy_skill(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-", dir=destination.parent))
    staged = temporary / SKILL_NAME
    try:
        shutil.copytree(source, staged, ignore=_ignore)
        marker = staged / ".repository-memory-install.json"
        marker.write_text(
            json.dumps({"schema_version": 1, "installed_at": int(time.time())}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if destination.exists() or destination.is_symlink():
            marker = destination / ".repository-memory-install.json"
            if not marker.is_file():
                raise RuntimeError(f"refusing to replace unmanaged Skill directory: {destination}")
            shutil.rmtree(destination)
        staged.replace(destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _copy_openclaw_extension(source: Path, destination: Path) -> None:
    """Install the lifecycle extension without replacing an unmanaged plugin."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    marker_name = ".repository-memory-autocapture-managed"
    if destination.exists() and not (destination / marker_name).is_file():
        raise RuntimeError(f"refusing to replace unmanaged OpenClaw extension: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix=".repository-memory-extension-", dir=destination.parent))
    staged = temporary / destination.name
    try:
        shutil.copytree(source, staged, ignore=_ignore)
        (staged / marker_name).write_text("managed by repository-memory\n", encoding="utf-8")
        if destination.exists():
            shutil.rmtree(destination)
        staged.replace(destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _run(command: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, output


def _audit_log() -> Path:
    return _data_home() / "repository-memory" / "audit.jsonl"


def _mcp_command(canonical: Path) -> tuple[str, list[str]]:
    """Return one transparent audited MCP command for every host."""

    return sys.executable, [
        str(canonical / "scripts" / "audit_proxy.py"),
        "--log",
        str(_audit_log()),
        "--",
        sys.executable,
        str(canonical / "scripts" / "repository-memory.py"),
        "mcp",
    ]


def _install_codex(canonical: Path, register_mcp: bool) -> dict[str, Any]:
    root = Path(os.environ.get("CODEX_HOME", _home() / ".codex")).expanduser().resolve()
    destination = root / "skills" / SKILL_NAME
    _copy_skill(canonical, destination)
    registered = False
    detail = "skipped"
    executable = shutil.which("codex")
    if register_mcp and executable:
        exists, _ = _run([executable, "mcp", "get", MCP_NAME])
        if exists:
            removed, remove_detail = _run([executable, "mcp", "remove", MCP_NAME])
            if not removed:
                registered, detail = False, f"existing MCP is not audited and could not be replaced: {remove_detail[:240]}"
            else:
                command, command_args = _mcp_command(canonical)
                registered, detail = _run([
                    executable,
                    "mcp",
                    "add",
                    "--env",
                    "REPOSITORY_MEMORY_AUTODISCOVER=0",
                    MCP_NAME,
                    "--",
                    command,
                    *command_args,
                ])
                detail = "replaced with audited proxy" if registered else detail
        else:
            command, command_args = _mcp_command(canonical)
            registered, detail = _run([
                executable,
                "mcp",
                "add",
                "--env",
                "REPOSITORY_MEMORY_AUTODISCOVER=0",
                MCP_NAME,
                "--",
                command,
                *command_args,
            ])
    return {"skill": str(destination), "mcp_registered": registered, "mcp_detail": detail}


def _install_claude(canonical: Path, register_mcp: bool) -> dict[str, Any]:
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", _home() / ".claude")).expanduser().resolve()
    destination = root / "skills" / SKILL_NAME
    _copy_skill(canonical, destination)
    registered = False
    detail = "skipped"
    executable = shutil.which("claude")
    if register_mcp and executable:
        exists, _ = _run([executable, "mcp", "get", MCP_NAME])
        if exists:
            removed, remove_detail = _run([executable, "mcp", "remove", "--scope", "user", MCP_NAME])
            if not removed:
                registered, detail = False, f"existing MCP is not audited and could not be replaced: {remove_detail[:240]}"
            else:
                command, command_args = _mcp_command(canonical)
                registered, detail = _run([
                    executable,
                    "mcp",
                    "add",
                    "--scope",
                    "user",
                    MCP_NAME,
                    "-e",
                    "REPOSITORY_MEMORY_AUTODISCOVER=0",
                    "--",
                    command,
                    *command_args,
                ])
                detail = "replaced with audited proxy" if registered else detail
        else:
            command, command_args = _mcp_command(canonical)
            registered, detail = _run([
                executable,
                "mcp",
                "add",
                "--scope",
                "user",
                MCP_NAME,
                "-e",
                "REPOSITORY_MEMORY_AUTODISCOVER=0",
                "--",
                command,
                *command_args,
            ])
    return {"skill": str(destination), "mcp_registered": registered, "mcp_detail": detail}


def _install_openclaw(
    canonical: Path,
    runtime: Path,
    config_path: Path | None = None,
    agent_ids: list[str] | None = None,
    all_agents: bool = False,
) -> dict[str, Any]:
    path = config_path or (_home() / ".openclaw" / "openclaw.json")
    if not path.is_file():
        return {"config": str(path), "agents": [], "mcp_registered": False, "detail": "OpenClaw is not configured"}
    config = _read_json(path)
    extension_source = canonical / "openclaw-extension"
    # A caller may point OpenClaw at an isolated profile.  Keep the extension
    # beside that profile's config instead of silently writing to the default
    # user's ~/.openclaw directory.
    extension_destination = path.parent / "extensions" / OPENCLAW_PLUGIN_ID
    _copy_openclaw_extension(extension_source, extension_destination)
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    rows = agents.get("list") if isinstance(agents.get("list"), list) else []
    configured_ids = {
        str(row.get("id") or Path(str(row.get("workspace") or "")).name)
        for row in rows
        if isinstance(row, dict) and row.get("workspace")
    }
    selected_ids = configured_ids if all_agents else set(agent_ids or [])
    if not selected_ids:
        raise RuntimeError("OpenClaw installation requires --openclaw-agent <id>; use --openclaw-all-agents only when intentional")
    unknown_ids = selected_ids - configured_ids
    if unknown_ids:
        raise RuntimeError(f"OpenClaw agent id(s) not found: {', '.join(sorted(unknown_ids))}")
    installed: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("workspace"):
            continue
        agent_id = str(row.get("id") or Path(str(row["workspace"])).name)
        if agent_id not in selected_ids:
            continue
        workspace = Path(str(row["workspace"])).expanduser().resolve()
        destination = workspace / "skills" / SKILL_NAME
        _copy_skill(canonical, destination)
        skills = row.get("skills") if isinstance(row.get("skills"), list) else []
        # Keep old Skill files available for rollback, but remove their active
        # registration so the model cannot choose between two memory contracts.
        skills = [name for name in skills if name not in LEGACY_SKILL_NAMES]
        if SKILL_NAME not in skills:
            row["skills"] = [*skills, SKILL_NAME]
        else:
            row["skills"] = skills
        tools = row.get("tools") if isinstance(row.get("tools"), dict) else {}
        allowed = tools.get("alsoAllow") if isinstance(tools.get("alsoAllow"), list) else []
        tools["alsoAllow"] = [*allowed, *(name for name in OPENCLAW_TOOLS if name not in allowed)]
        row["tools"] = tools
        installed.append({"agent": agent_id, "skill": str(destination)})

    config.setdefault("agents", {})["list"] = rows
    mcp = config.get("mcp") if isinstance(config.get("mcp"), dict) else {}
    servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else {}
    command, command_args = _mcp_command(canonical)
    servers[MCP_NAME] = {
        "command": command,
        "args": command_args,
        "env": {"REPOSITORY_MEMORY_AUTODISCOVER": "0"},
        "toolFilter": {"include": MCP_TOOLS},
    }
    mcp["servers"] = servers
    config["mcp"] = mcp
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    load = plugins.get("load") if isinstance(plugins.get("load"), dict) else {}
    load_paths = load.get("paths") if isinstance(load.get("paths"), list) else []
    if str(extension_destination) not in load_paths:
        load_paths.append(str(extension_destination))
    load["paths"] = load_paths
    plugins["load"] = load
    allow = plugins.get("allow") if isinstance(plugins.get("allow"), list) else []
    if OPENCLAW_PLUGIN_ID not in allow:
        allow.append(OPENCLAW_PLUGIN_ID)
    plugins["allow"] = allow
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    disabled_legacy_plugins: list[str] = []
    for legacy_id in LEGACY_OPENCLAW_PLUGIN_IDS:
        if legacy_id == OPENCLAW_PLUGIN_ID:
            continue
        legacy = entries.get(legacy_id)
        if isinstance(legacy, dict) and legacy.get("enabled") is not False:
            legacy["enabled"] = False
            disabled_legacy_plugins.append(legacy_id)
    entries[OPENCLAW_PLUGIN_ID] = {
        "enabled": True,
        "config": {
            "enabled": True,
            "guardEnabled": True,
            "enforcement": "audit",
            "runtime": str(runtime),
            "agentIds": sorted(selected_ids),
        },
        # OpenClaw requires an explicit opt-in before a non-bundled plugin can
        # receive conversation lifecycle events such as agent_end.
        "hooks": {"allowConversationAccess": True},
    }
    plugins["entries"] = entries
    config["plugins"] = plugins
    backup = path.with_name(f"{path.name}.bak.repository-memory-{int(time.time())}")
    shutil.copy2(path, backup)
    _atomic_json(path, config)
    return {"config": str(path), "backup": str(backup), "agents": installed, "mcp_registered": True, "autocapture": {"plugin": OPENCLAW_PLUGIN_ID, "extension": str(extension_destination), "agent_ids": sorted(set(agent_ids or [])), "disabled_legacy_plugins": disabled_legacy_plugins}}


def _install_cli(canonical: Path) -> Path:
    destination = _home() / ".local" / "bin" / "repository-memory"
    destination.parent.mkdir(parents=True, exist_ok=True)
    script = canonical / "scripts" / "repository-memory.py"
    wrapper = f"#!/bin/sh\nexec {json.dumps(sys.executable)} {json.dumps(str(script))} \"$@\"\n"
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(wrapper, encoding="utf-8")
    os.chmod(temporary, 0o755)
    os.replace(temporary, destination)
    return destination


def _configure_source(canonical: Path, source_root: Path, local_only: bool = False) -> dict[str, Any]:
    command = [
        sys.executable,
        str(canonical / "scripts" / "repository-memory.py"),
        "init",
        "--path",
        str(source_root.resolve()),
        "--id",
        source_root.name,
        "--json",
    ]
    if local_only:
        command.insert(-1, "--local-only")
    ok, output = _run(command)
    if not ok:
        raise RuntimeError(f"source initialization failed: {output[:500]}")
    return json.loads(output)


def _verify(canonical: Path, require_repository: bool) -> dict[str, Any]:
    doctor_ok, doctor_output = _run([
        sys.executable,
        str(canonical / "scripts" / "repository-memory.py"),
        "doctor",
        "--json",
    ])
    if not doctor_ok:
        raise RuntimeError(f"installed doctor failed: {doctor_output[:500]}")
    doctor = json.loads(doctor_output)
    repository_status = str((doctor.get("repository") or {}).get("status") or "unknown")
    if require_repository and repository_status != "ready":
        raise RuntimeError(f"installed repository is not ready: {repository_status}")

    modern_meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "repository-memory-installer", "version": VERSION},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    modern_requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": modern_meta}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": modern_meta}},
    ]

    def run_probe(requests: list[dict[str, Any]]) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
        command, command_args = _mcp_command(canonical)
        process = subprocess.run(
            [command, *command_args],
            input="\n".join(json.dumps(item) for item in requests) + "\n",
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        return process, responses

    try:
        process, responses = run_probe(modern_requests)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"installed MCP verification failed: {exc}") from exc

    protocol = "2026-07-28"
    if (
        process.returncode
        or len(responses) < 2
        or "error" in responses[0]
        or "error" in responses[1]
        or responses[0].get("result", {}).get("supportedVersions") is None
    ):
        # A host may still expose a legacy-only transport.  Verify it, but
        # report the downgrade explicitly instead of calling it modern.
        legacy_requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        try:
            process, responses = run_probe(legacy_requests)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"installed MCP verification failed: {exc}") from exc
        protocol = "legacy-fallback"
    if process.returncode:
        raise RuntimeError(f"installed MCP verification failed: {(process.stderr or process.stdout)[:500]}")
    if len(responses) < 2 or "error" in responses[1]:
        raise RuntimeError(f"installed MCP verification failed: {(process.stderr or process.stdout)[:500]}")
    tools = sorted(tool["name"] for tool in responses[1]["result"]["tools"])
    if tools != sorted(MCP_TOOLS):
        raise RuntimeError(f"installed MCP tool mismatch: {tools}")
    return {
        "doctor": {"status": doctor.get("status"), "repository": repository_status},
        "mcp": {"status": "ready", "protocol": protocol, "tools": tools},
    }


def install(args: argparse.Namespace) -> dict[str, Any]:
    version = tuple(int(part) for part in platform.python_version_tuple()[:2])
    if version < (3, 10):
        raise RuntimeError("Repository Memory requires Python 3.10 or newer")
    source = _source_skill()
    canonical = _canonical_skill()
    _copy_skill(source, canonical)
    cli = _install_cli(canonical)

    targets = set(args.target or ["auto"])
    if "all" in targets:
        targets = {"codex", "claude", "openclaw"}
    elif "auto" in targets:
        targets = set()
        if shutil.which("codex") or (_home() / ".codex").exists():
            targets.add("codex")
        if shutil.which("claude") or (_home() / ".claude").exists():
            targets.add("claude")
        if shutil.which("openclaw") or (_home() / ".openclaw" / "openclaw.json").exists():
            targets.add("openclaw")

    hosts: dict[str, Any] = {}
    if "codex" in targets:
        hosts["codex"] = _install_codex(canonical, not args.no_mcp)
    if "claude" in targets:
        hosts["claude"] = _install_claude(canonical, not args.no_mcp)
    if "openclaw" in targets:
        hosts["openclaw"] = _install_openclaw(
            canonical,
            cli,
            # Keep the caller's lexical profile path. Resolving it here can
            # turn macOS /var symlinks into /private/var and make the receipt
            # point at a different-looking profile than the one supplied.
            Path(args.openclaw_config).expanduser() if args.openclaw_config else None,
            args.openclaw_agent,
            args.openclaw_all_agents,
        )

    source_status = None
    if args.source_root:
        source_status = _configure_source(canonical, Path(args.source_root).expanduser().resolve(), args.source_local_only)
    verification = None if args.no_verify else _verify(canonical, require_repository=bool(args.source_root))
    return {
        "schema_version": 1,
        "status": "installed",
        "canonical_skill": str(canonical),
        "cli": str(cli),
        "targets": sorted(targets),
        "hosts": hosts,
        "source": source_status,
        "verification": verification,
        "next_step": "restart or start a new agent turn, then ask it to use repository-memory",
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Install Repository Memory for local AI hosts")
    value.add_argument("--target", action="append", choices=["auto", "all", "codex", "claude", "openclaw"])
    value.add_argument("--all", action="store_true", help="install for Codex, Claude Code, and every configured OpenClaw agent")
    value.add_argument("--source-root", help="register and index a repository or document directory")
    value.add_argument("--source-local-only", action="store_true", help="declare --source-root as an intentional offline/local snapshot")
    value.add_argument("--openclaw-config", help="override the OpenClaw config path")
    value.add_argument("--openclaw-agent", action="append", help="install and enable repository-memory only for this OpenClaw agent; repeat for multiple agents")
    value.add_argument("--openclaw-all-agents", action="store_true", help="explicitly install and enable repository-memory for every configured OpenClaw agent")
    value.add_argument("--no-mcp", action="store_true", help="install Skills without registering Codex/Claude MCP")
    value.add_argument("--no-verify", action="store_true", help="skip installed doctor and MCP smoke checks")
    value.add_argument("--json", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.all:
        args.target = ["all"]
    try:
        result = install(args)
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = {"schema_version": 1, "status": "error", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
