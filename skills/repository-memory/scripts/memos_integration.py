"""MemOS Local integration for the unified repository-memory product.

The Git evidence lane remains owned by repository-memory.  This module owns
only the local conversation-memory lane: it discovers a MemOS Local checkout,
installs its OpenClaw plugin without editing the upstream checkout, and makes
the OpenClaw memory slot explicit.  The upstream source is intentionally not
reimplemented here; this is the small product boundary that keeps the two
systems operationally consistent.

No provider URL, model, credential, or absolute checkout path is embedded in
the Skill.  A source path may be supplied through the environment or the
user-level repository-memory config.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from discovery import config_path, data_root, read_config


PLUGIN_ID = "memos-local-openclaw-plugin"
PLUGIN_RELATIVE = Path("apps") / "memos-local-openclaw"
SOURCE_ENV = "MEMOS_SOURCE_ROOT"
CONFIG_ENV = "MEMOS_OPENCLAW_CONFIG"


def _command(name: str) -> str | None:
    return shutil.which(name)


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(command, cwd=str(cwd) if cwd else None, text=True, encoding="utf-8", capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "command": command, "error": str(exc)}
    output = (result.stdout or result.stderr or "").strip()
    return {"ok": result.returncode == 0, "command": command, "returncode": result.returncode, "output": output[-2000:]}


def _version(command: str | None, args: list[str]) -> str | None:
    if not command:
        return None
    result = _run([command, *args], timeout=10)
    if not result.get("ok"):
        return None
    value = str(result.get("output") or "").splitlines()
    return value[-1].strip() if value else None


def _source_candidates(start: Path | None = None) -> list[Path]:
    config = read_config()
    memos = config.get("memos") if isinstance(config.get("memos"), dict) else {}
    values: list[str] = []
    for value in (os.environ.get(SOURCE_ENV), memos.get("source_root"), memos.get("sourceRoot")):
        if value:
            values.append(str(value))

    current = (start or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        values.extend([str(candidate), str(candidate / "MemOS-reference"), str(candidate / "MemOS")])

    cache = data_root() / "sources"
    if cache.is_dir():
        try:
            values.extend(str(path) for path in cache.iterdir() if path.is_dir())
        except OSError:
            pass

    output: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = Path(value).expanduser().resolve()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if (path / PLUGIN_RELATIVE / "package.json").is_file():
            output.append(path)
        elif (path / "package.json").is_file() and path.name == "memos-local-openclaw":
            output.append(path.parent.parent)
    return output


def discover_source(start: Path | None = None, explicit: str | None = None) -> Path | None:
    if explicit:
        candidates = [Path(explicit).expanduser().resolve()]
    else:
        candidates = _source_candidates(start)
    for path in candidates:
        if (path / PLUGIN_RELATIVE / "package.json").is_file():
            return path
    return None


def _plugin_root(source: Path | None) -> Path | None:
    return source / PLUGIN_RELATIVE if source else None


def _package(plugin: Path | None) -> dict[str, Any]:
    if not plugin:
        return {}
    path = plugin / "package.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _git_revision(source: Path | None) -> str | None:
    if not source or not (source / ".git").exists():
        return None
    result = _run(["git", "-C", str(source), "rev-parse", "HEAD"], timeout=10)
    return str(result.get("output") or "").splitlines()[-1] if result.get("ok") and result.get("output") else None


def _git_clean(source: Path | None) -> bool | None:
    if not source or not (source / ".git").exists():
        return None
    result = _run(["git", "-C", str(source), "status", "--porcelain"], timeout=10)
    return bool(result.get("ok") and not str(result.get("output") or "").strip())


def _openclaw_config_path(explicit: str | None = None) -> Path:
    value = explicit or os.environ.get(CONFIG_ENV)
    if value:
        return Path(value).expanduser().resolve()
    config = read_config()
    memos = config.get("memos") if isinstance(config.get("memos"), dict) else {}
    configured = memos.get("openclaw_config") or memos.get("openclawConfig")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    return Path.home() / ".openclaw" / "openclaw.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _configured_source(source: Path) -> dict[str, Any]:
    config = read_config()
    memos = config.get("memos") if isinstance(config.get("memos"), dict) else {}
    config["memos"] = {**memos, "source_root": str(source), "plugin_id": PLUGIN_ID}
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return {"config": str(path), "source_root": str(source), "plugin_id": PLUGIN_ID}


def _plugin_state(config: dict[str, Any], config_path_value: Path, source: Path | None) -> dict[str, Any]:
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    slots = plugins.get("slots") if isinstance(plugins.get("slots"), dict) else {}
    entry = entries.get(PLUGIN_ID) if isinstance(entries.get(PLUGIN_ID), dict) else {}
    load = plugins.get("load") if isinstance(plugins.get("load"), dict) else {}
    paths = load.get("paths") if isinstance(load.get("paths"), list) else []
    extension_candidates = [Path(str(item)).expanduser() for item in paths if isinstance(item, str) and PLUGIN_ID in item]
    extension = next((item for item in extension_candidates if item.exists()), None)
    if extension is None:
        default = config_path_value.parent / "extensions" / PLUGIN_ID
        extension = default if default.exists() else None
    return {
        "installed": bool(extension and (extension / "package.json").is_file()),
        "extension": str(extension) if extension else None,
        "enabled": entry.get("enabled") is True,
        "slot": slots.get("memory"),
        "is_active_memory_slot": slots.get("memory") == PLUGIN_ID,
        "builtin_memory_search_enabled": bool(config.get("agents", {}).get("defaults", {}).get("memorySearch", {}).get("enabled", False)) if isinstance(config.get("agents"), dict) else False,
    }


def doctor(*, source: str | None = None, openclaw_config: str | None = None, start: Path | None = None) -> dict[str, Any]:
    root = discover_source(start=start, explicit=source)
    plugin = _plugin_root(root)
    package = _package(plugin)
    config_path_value = _openclaw_config_path(openclaw_config)
    config = _read_json(config_path_value)
    package_json = plugin / "package.json" if plugin else None
    return {
        "schema_version": 1,
        "provider": "memos-local-openclaw",
        "source": {
            "discovered": root is not None,
            "root": str(root) if root else None,
            "plugin": str(plugin) if plugin else None,
            "revision": _git_revision(root),
            "package_version": package.get("version"),
            "package_json": str(package_json) if package_json else None,
            "source_clean": _git_clean(root),
        },
        "runtime": {
            "node": _version(_command("node"), ["--version"]),
            "npm": _version(_command("npm"), ["--version"]),
            "openclaw": _version(_command("openclaw"), ["--version"]),
            "node_required": package.get("engines", {}).get("node") if isinstance(package.get("engines"), dict) else None,
        },
        "build": {
            "dist": bool(plugin and (plugin / "dist" / "index.js").is_file()),
            "dependencies": bool(plugin and (plugin / "node_modules").is_dir()),
            "lockfile": bool(plugin and (plugin / "package-lock.json").is_file()),
        },
        "openclaw": {"config": str(config_path_value), "configured": config_path_value.is_file(), **_plugin_state(config, config_path_value, root)},
        "canonical_repo_changed": False,
        "next": (["set MEMOS_SOURCE_ROOT or pass --source <MemOS checkout>"] if root is None else []),
    }


def configure(*, source: str | None = None, openclaw_config: str | None = None, start: Path | None = None) -> dict[str, Any]:
    root = discover_source(start=start, explicit=source)
    if root is None:
        raise RuntimeError("MemOS source not found; set MEMOS_SOURCE_ROOT or pass --source <checkout>")
    result: dict[str, Any] = {"ok": True, **_configured_source(root), "canonical_repo_changed": False}
    if openclaw_config:
        path = _openclaw_config_path(openclaw_config)
        config = _read_json(path)
        state = _plugin_state(config, path, root)
        extension = Path(str(state["extension"])) if state.get("extension") else None
        if extension:
            result["openclaw"] = _update_openclaw(path, extension)
    return result


def _copy_source(plugin: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(plugin, destination, ignore=shutil.ignore_patterns("node_modules", "dist", "*.tsbuildinfo"))
    # The checked-in upstream package currently declares CommonJS while its
    # tsconfig emits ESM and uses import.meta.  Keep this compatibility fix in
    # our generated staging copy only; never modify the user's MemOS checkout.
    entry = destination / "index.ts"
    if entry.is_file():
        text = entry.read_text(encoding="utf-8")
        text = text.replace("path.dirname(fileURLToPath(import.meta.url))", "__dirname")
        entry.write_text(text, encoding="utf-8")


def _copy_bundled_skill(staged: Path) -> None:
    source = staged / "skill" / "memos-memory-guide"
    target = staged / "dist" / "skill" / "memos-memory-guide"
    if not source.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def _stage_root(source: Path, revision: str | None) -> Path:
    version = revision or str(int(time.time()))
    destination = data_root() / "memos" / "staged" / version / PLUGIN_ID
    if destination.is_dir() and (destination / "dist" / "index.js").is_file() and (destination / "node_modules").is_dir():
        return destination
    _copy_source(_plugin_root(source) or source, destination)
    return destination


def _backup(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.bak.repository-memory-memos-{int(time.time())}")
    shutil.copy2(path, backup)
    return backup


def _update_openclaw(path: Path, extension: Path, agent_ids: list[str] | None = None) -> dict[str, Any]:
    config = _read_json(path)
    if not config:
        raise RuntimeError(f"OpenClaw config not found: {path}")
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    slots = plugins.get("slots") if isinstance(plugins.get("slots"), dict) else {}
    slots["memory"] = PLUGIN_ID
    plugins["slots"] = slots
    load = plugins.get("load") if isinstance(plugins.get("load"), dict) else {}
    paths = load.get("paths") if isinstance(load.get("paths"), list) else []
    if str(extension) not in paths:
        paths.append(str(extension))
    load["paths"] = paths
    plugins["load"] = load
    allow = plugins.get("allow") if isinstance(plugins.get("allow"), list) else []
    if PLUGIN_ID not in allow:
        allow.append(PLUGIN_ID)
    plugins["allow"] = allow
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    entry = entries.get(PLUGIN_ID) if isinstance(entries.get(PLUGIN_ID), dict) else {}
    entry["enabled"] = True
    entry["hooks"] = {**(entry.get("hooks") if isinstance(entry.get("hooks"), dict) else {}), "allowConversationAccess": True}
    entries[PLUGIN_ID] = entry
    for legacy in ("active-memory", "memmy-memory"):
        if isinstance(entries.get(legacy), dict):
            entries[legacy]["enabled"] = False
    plugins["entries"] = entries
    config["plugins"] = plugins

    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    memory_search = defaults.get("memorySearch") if isinstance(defaults.get("memorySearch"), dict) else {}
    memory_search["enabled"] = False
    defaults["memorySearch"] = memory_search
    agents["defaults"] = defaults
    rows = agents.get("list") if isinstance(agents.get("list"), list) else []
    selected = set(agent_ids or [])
    if selected:
        known = {str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")}
        missing = selected - known
        if missing:
            raise RuntimeError(f"OpenClaw agent id(s) not found: {', '.join(sorted(missing))}")
    for row in rows:
        if not isinstance(row, dict) or (selected and str(row.get("id")) not in selected):
            continue
        tools = row.get("tools") if isinstance(row.get("tools"), dict) else {}
        allowed = tools.get("alsoAllow") if isinstance(tools.get("alsoAllow"), list) else []
        for name in ("memory_search", "memory_get", "memory_timeline"):
            if name not in allowed:
                allowed.append(name)
        tools["alsoAllow"] = allowed
        row["tools"] = tools
    agents["list"] = rows
    config["agents"] = agents
    backup = _backup(path)
    _write_json(path, config)
    return {"config": str(path), "backup": str(backup), "slot": PLUGIN_ID, "disabled_builtin_memory_search": True, "disabled_legacy_plugins": [name for name in ("active-memory", "memmy-memory") if isinstance(entries.get(name), dict) and entries[name].get("enabled") is False], "agents": sorted(selected) if selected else "all"}


def install(*, source: str | None = None, openclaw_config: str | None = None, agent_ids: list[str] | None = None, start: Path | None = None, build: bool = True, install_dependencies: bool = True, timeout: int = 900) -> dict[str, Any]:
    root = discover_source(start=start, explicit=source)
    if root is None:
        raise RuntimeError("MemOS source not found; set MEMOS_SOURCE_ROOT or pass --source <checkout>")
    plugin = _plugin_root(root)
    if not plugin or not (plugin / "package.json").is_file():
        raise RuntimeError("MemOS checkout does not contain apps/memos-local-openclaw/package.json")
    revision = _git_revision(root)
    staged = _stage_root(root, revision)
    commands: list[dict[str, Any]] = []
    if install_dependencies and not (staged / "node_modules").is_dir():
        commands.append(_run([_command("npm") or "npm", "install", "--ignore-scripts"], cwd=staged, timeout=timeout))
        if not commands[-1].get("ok"):
            raise RuntimeError(f"MemOS npm install failed: {commands[-1].get('output') or commands[-1].get('error')}")
    if build and not (staged / "dist" / "index.js").is_file():
        # The current upstream checkout is strict TypeScript but its OpenClaw
        # SDK callback types are intentionally loose.  Keep upstream untouched
        # and use the narrow compatibility flags only in our isolated build.
        commands.append(_run([_command("npm") or "npm", "run", "build", "--", "--noImplicitAny", "false", "--strictNullChecks", "false", "--module", "commonjs", "--moduleResolution", "node"], cwd=staged, timeout=timeout))
        if not commands[-1].get("ok"):
            raise RuntimeError(f"MemOS build failed: {commands[-1].get('output') or commands[-1].get('error')}")
    _copy_bundled_skill(staged)
    config_path_value = _openclaw_config_path(openclaw_config)
    if not config_path_value.is_file():
        raise RuntimeError(f"OpenClaw config not found: {config_path_value}")
    # ``--link`` is intentional: the staged directory is our managed,
    # revision-pinned install copy.  OpenClaw rejects combining ``--link``
    # with ``--force``; a later revision gets a new staged path.
    openclaw = _run([_command("openclaw") or "openclaw", "plugins", "install", "--link", str(staged)], timeout=timeout)
    if not openclaw.get("ok"):
        raise RuntimeError(f"OpenClaw MemOS plugin install failed: {openclaw.get('output') or openclaw.get('error')}")
    extension = Path.home() / ".openclaw" / "extensions" / PLUGIN_ID
    if config_path_value.parent != Path.home() / ".openclaw":
        extension = config_path_value.parent / "extensions" / PLUGIN_ID
    update = _update_openclaw(config_path_value, extension, agent_ids)
    configured = _configured_source(root)
    return {"ok": True, "provider": "memos-local-openclaw", "source": {"root": str(root), "revision": revision, "staged": str(staged)}, "build": {"commands": commands, "dist": (staged / "dist" / "index.js").is_file(), "dependencies": (staged / "node_modules").is_dir()}, "openclaw_install": openclaw, "openclaw_config": update, "configured": configured, "canonical_repo_changed": False}


def disable(*, openclaw_config: str | None = None) -> dict[str, Any]:
    path = _openclaw_config_path(openclaw_config)
    config = _read_json(path)
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    slots = plugins.get("slots") if isinstance(plugins.get("slots"), dict) else {}
    slots["memory"] = "memory-core"
    plugins["slots"] = slots
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    if isinstance(entries.get(PLUGIN_ID), dict):
        entries[PLUGIN_ID]["enabled"] = False
    plugins["entries"] = entries
    config["plugins"] = plugins
    backup = _backup(path)
    _write_json(path, config)
    return {"ok": True, "config": str(path), "backup": str(backup), "slot": "memory-core", "canonical_repo_changed": False}
