#!/usr/bin/env python3
"""User-level MemoryCore bootstrap and launchd supervisor.

This file is intentionally an operational script, not part of the Skill
instructions.  It discovers a compatible local MemoryCore checkout and the
configured model route at runtime, writes only non-secret user configuration,
and injects credentials into the child process just before launch.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from memorycore import (
    MemoryCoreClient,
    _config_path,
    _first_string,
    _read_json,
    _secret_from_keychain,
)

# Kept stable for upgrades of the existing user-level service.  It is an
# operational identifier, not part of the Skill/agent contract; deployments
# may override it without changing the repository.
LABEL = os.environ.get("REPOSITORY_MEMORY_LAUNCHD_LABEL", "com.repository-memorycore")
# Labels a previous deployment registered the service under.  Like LABEL above,
# this is deployment state, not repository content: only the machine that ran
# the older service knows its label.  Empty default, comma-separated override.
LEGACY_LABELS = tuple(value for value in os.environ.get("REPOSITORY_MEMORY_LEGACY_LABELS", "").split(",") if value.strip())
RUNTIME_PATCH = Path(__file__).resolve().parent / "vendor-patches" / "tencentdb-memorycore-local-timer-instance.patch"


def _config() -> dict[str, Any]:
    return _read_json(_config_path())


def _memory_config() -> dict[str, Any]:
    value = _config().get("memorycore")
    return value if isinstance(value, dict) else {}


def _candidate_roots() -> list[Path]:
    config = _memory_config()
    candidates: list[Path] = []
    explicit = os.environ.get("REPOSITORY_MEMORY_MEMORYCORE_ROOT") or config.get("root")
    if explicit:
        candidates.append(Path(str(explicit)).expanduser())
    # Prefer the clean TencentDB source snapshot shipped with this Skill when
    # no user root has been selected.  It is installed under the user data
    # directory and never points the service at the repository worktree.
    bundled = Path(__file__).resolve().parents[1] / "vendor" / "tencentdb-agent-memory-reference" / "MemoryCore"
    candidates.append(bundled)
    current = Path.cwd().resolve()
    candidates.extend([current, *current.parents])
    search_paths = os.environ.get("REPOSITORY_MEMORY_MEMORYCORE_SEARCH_PATHS")
    if search_paths:
        candidates.extend(Path(value).expanduser() for value in search_paths.split(os.pathsep) if value.strip())
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        try:
            # Keep discovery structural: vendor/repository names are not part
            # of the public Skill contract.  A compatible checkout is
            # identified by the gateway package markers below.
            for child in desktop.iterdir():
                if not child.is_dir():
                    continue
                candidates.append(child)
                candidates.append(child / "MemoryCore")
        except OSError:
            pass
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        marker = str(resolved)
        if marker in seen:
            continue
        seen.add(marker)
        if (resolved / "package.json").is_file() and (resolved / "src" / "gateway" / "server.ts").is_file():
            result.append(resolved)
    return result


def discover_root() -> Path | None:
    return _candidate_roots()[0] if _candidate_roots() else None


def _pipeline_patch_state(root: Path) -> dict[str, Any]:
    """Return whether the local MemoryCore timer-instance fix is applied.

    Standalone MemoryCore stores L1 data under the configured instance, but an
    older local timer callback dropped that instance when it created the L2
    task.  The patch is kept in this Skill and applied to the discovered
    checkout; MemoryCore itself is not copied into this repository.
    """
    checks = {
        "timer_entry_instance": (root / "src" / "core" / "state" / "types.ts", "export interface TimerEntry {\n  /** The state-backend instance that owns this timer. */\n  instanceId: string;"),
        "local_timer_instance": (root / "src" / "core" / "state" / "local-backend.ts", "onTimerExpired!({ instanceId, member, fireAtMs })"),
        "gateway_timer_instance": (root / "src" / "gateway" / "server.ts", "let instanceId: string = entry.instanceId"),
        "l3_policy": (root / "src" / "utils" / "pipeline-factory.ts", "REPOSITORY_MEMORY_DISABLE_AUTO_L3"),
    }
    present: dict[str, bool] = {}
    for name, (path, marker) in checks.items():
        try:
            present[name] = marker in path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            present[name] = False
    if all(present.values()):
        status = "applied"
    elif any(present.values()):
        status = "partial"
    else:
        status = "missing"
    return {"status": status, "checks": present, "patch": str(RUNTIME_PATCH)}


def ensure_pipeline_patch(root: Path) -> dict[str, Any]:
    """Apply the runtime-only MemoryCore patch idempotently and verifiably.

    A discovered upstream checkout may be a Git worktree, while the bundled
    TencentDB snapshot is deliberately installed as a plain user-level
    directory.  Both are supported: Git worktrees use ``git apply`` and
    immutable vendor snapshots use the standard ``patch`` utility.  The patch
    is applied only to the user-level runtime copy, never to the canonical
    repository vendor files or to an unrelated dirty checkout.
    """
    state = _pipeline_patch_state(root)
    if state["status"] == "applied":
        return state
    if state["status"] == "partial":
        raise RuntimeError("MemoryCore timer-instance patch is partially applied; repair the checkout before starting")
    if not RUNTIME_PATCH.is_file():
        raise RuntimeError(f"repository-memory runtime patch is missing: {RUNTIME_PATCH}")

    try:
        worktree = Path(subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()).resolve()
    except (OSError, subprocess.CalledProcessError):
        patch = shutil.which("patch")
        if not patch:
            raise RuntimeError("MemoryCore vendor snapshot is not a Git worktree and patch is unavailable")
        check = subprocess.run(
            [patch, "--dry-run", "-p1", "-i", str(RUNTIME_PATCH)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout).strip()
            raise RuntimeError(f"cannot apply MemoryCore vendor runtime patch: {detail[:400]}")
        applied = subprocess.run(
            [patch, "-p1", "-i", str(RUNTIME_PATCH)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if applied.returncode != 0:
            detail = (applied.stderr or applied.stdout).strip()
            raise RuntimeError(f"MemoryCore vendor runtime patch failed: {detail[:400]}")
    else:
        try:
            relative_root = root.resolve().relative_to(worktree)
        except ValueError as exc:
            raise RuntimeError("MemoryCore checkout is outside its Git worktree") from exc

        command = ["git", "-C", str(worktree), "apply"]
        if str(relative_root) != ".":
            command.extend(["--directory", str(relative_root)])
        command.append(str(RUNTIME_PATCH))
        check = subprocess.run([*command[:-1], "--check", command[-1]], capture_output=True, text=True, check=False)
        if check.returncode != 0:
            detail = (check.stderr or check.stdout).strip()
            raise RuntimeError(f"cannot apply MemoryCore runtime patch: {detail[:400]}")
        applied = subprocess.run(command, capture_output=True, text=True, check=False)
        if applied.returncode != 0:
            detail = (applied.stderr or applied.stdout).strip()
            raise RuntimeError(f"MemoryCore runtime patch failed: {detail[:400]}")
    state = _pipeline_patch_state(root)
    if state["status"] != "applied":
        raise RuntimeError("MemoryCore runtime patch command succeeded but verification failed")
    return state


def ensure_runtime_dependencies(root: Path) -> dict[str, Any]:
    """Ensure a bundled or user-provided TypeScript runtime can start."""

    tsx = root / "node_modules" / ".bin" / "tsx"
    if tsx.is_file() or (root / "node_modules" / "tsx").is_dir():
        return {"status": "present", "root": str(root)}
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("MemoryCore dependencies are missing and npm is unavailable")
    try:
        result = subprocess.run(
            [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("MemoryCore dependency installation timed out after 180s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"MemoryCore dependency installation failed: {detail[:400]}")
    if not tsx.is_file() and not (root / "node_modules" / "tsx").is_dir():
        raise RuntimeError("MemoryCore dependency installation completed without tsx")
    return {"status": "installed", "root": str(root)}


def _openclaw_model_config(model_ref: str, agent_id: str | None = None) -> dict[str, str | None]:
    if not model_ref:
        return {"base_url": None, "api_key": None, "model": None, "source": None}
    provider, _, model = model_ref.partition("/")
    model = model or model_ref
    paths = []
    configured = os.environ.get("REPOSITORY_MEMORY_OPENCLAW_MODELS")
    if configured:
        paths.append(Path(configured).expanduser())
    configured_agent = agent_id or os.environ.get("OPENCLAW_AGENT_ID")
    openclaw_home = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")).expanduser()
    configured_openclaw = os.environ.get("REPOSITORY_MEMORY_OPENCLAW_CONFIG") or os.environ.get("OPENCLAW_CONFIG")
    if configured_openclaw:
        paths.append(Path(configured_openclaw).expanduser())
    if configured_agent:
        paths.append(openclaw_home / "agents" / configured_agent / "agent" / "models.json")
    paths.append(openclaw_home / "openclaw.json")
    for path in paths:
        payload = _read_json(path)
        providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else payload.get("models", {}).get("providers") if isinstance(payload.get("models"), dict) else {}
        if not isinstance(providers, dict):
            continue
        provider_payload = providers.get(provider) if provider else None
        if not isinstance(provider_payload, dict):
            continue
        models = provider_payload.get("models")
        if not isinstance(models, list):
            models = []
        selected = next((item for item in models if isinstance(item, dict) and str(item.get("id")) == model), None)
        if selected is None and models:
            selected = models[0] if isinstance(models[0], dict) else {}
        return {
            "base_url": _first_string(provider_payload.get("baseUrl"), provider_payload.get("base_url")),
            "api_key": _first_string(provider_payload.get("apiKey"), provider_payload.get("api_key")),
            "model": _first_string(selected.get("id") if isinstance(selected, dict) else None, model),
            "source": str(path),
        }
    return {"base_url": None, "api_key": None, "model": model, "source": None}


def _credentials(cfg: dict[str, Any]) -> dict[str, str | None]:
    model_ref = str(cfg.get("llm_model") or os.environ.get("REPOSITORY_MEMORY_LLM_MODEL_REF") or "")
    provider_data = _openclaw_model_config(model_ref, str(cfg.get("agent_id") or "") or None)
    base_url = _first_string(os.environ.get("REPOSITORY_MEMORY_LLM_BASE_URL"), cfg.get("llm_base_url"), provider_data.get("base_url"))
    model = _first_string(os.environ.get("REPOSITORY_MEMORY_LLM_MODEL"), cfg.get("llm_model_name"), provider_data.get("model"), model_ref.rsplit("/", 1)[-1])
    api_key = _first_string(os.environ.get("REPOSITORY_MEMORY_LLM_API_KEY"), _secret_from_keychain(str(cfg.get("keychain_service") or "repository-memorycore"), str(cfg.get("keychain_account") or getpass.getuser())), provider_data.get("api_key"))
    return {"base_url": base_url, "model": model, "api_key": api_key, "source": "environment/keychain/openclaw" if api_key else provider_data.get("source")}


def _gateway_api_key(cfg: dict[str, Any]) -> str | None:
    configured_file = cfg.get("gateway_api_key_file")
    if configured_file:
        try:
            value = Path(str(configured_file)).expanduser().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            value = ""
        if value:
            return value
    return os.environ.get("TDAI_GATEWAY_API_KEY") or os.environ.get("REPOSITORY_MEMORY_GATEWAY_API_KEY")


def _runtime_gateway_config(root: Path, state_dir: Path, memory: dict[str, Any] | None = None) -> Path:
    """Create a user-owned gateway config with the Skill module enabled.

    The source snapshot remains untouched.  The generated file is derived
    state under the user data directory and contains no credentials.
    """

    source = root / "tdai-gateway.standalone.yaml"
    if not source.is_file():
        return source
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "tdai-gateway.generated.yaml"
    base = source.read_text(encoding="utf-8")
    if "\nskill:\n" not in base:
        base = base.rstrip() + "\n\n# Generated by repository-memory; do not edit the vendor checkout.\nskill:\n  enabled: true\n  routing:\n    mode: bm25\n  extraction:\n    enabled: true\n    queue:\n      backend: local\n"
    if isinstance(memory, dict) and memory.get("pipeline_mode") == "fast":
        # Fast mode is explicitly opt-in for local verification. It changes
        # only generated user config, never the vendor checkout.
        overrides = {
            "triggerEveryN": "1",
            "everyNConversations": "1",
            "l1IdleTimeoutSeconds": "1",
            "l2DelayAfterL1Seconds": "0",
            "l2MinIntervalSeconds": "0",
            "l2MaxIntervalSeconds": "30",
        }
        for key, value in overrides.items():
            base = re.sub(rf"^(\s+{re.escape(key)}:\s*).*$", rf"\g<1>{value}", base, flags=re.MULTILINE)
    target.write_text(base, encoding="utf-8")
    os.chmod(target, 0o600)
    return target


def configure(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).expanduser().resolve() if args.root else discover_root()
    if root is None:
        raise RuntimeError("MemoryCore source not found; pass --memorycore-root")
    current = _config()
    memory = _memory_config()
    model_ref = args.model or memory.get("llm_model") or os.environ.get("REPOSITORY_MEMORY_LLM_MODEL_REF") or ""
    model_data = _openclaw_model_config(str(model_ref), str(args.agent_id or memory.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID") or "") or None)
    state_dir = str(Path(args.state_dir).expanduser()) if args.state_dir else memory.get("state_dir") or str(Path.home() / ".local" / "share" / "repository-memory" / "memorycore")
    gateway_key_file = Path(str(memory.get("gateway_api_key_file") or (Path(state_dir).parent / "gateway-api-key"))).expanduser()
    if not gateway_key_file.exists():
        gateway_key_file.parent.mkdir(parents=True, exist_ok=True)
        gateway_key_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        os.chmod(gateway_key_file, 0o600)
    else:
        os.chmod(gateway_key_file, 0o600)
    memory.update({
        "root": str(root),
        "endpoint": args.endpoint or memory.get("endpoint") or "http://127.0.0.1:8420",
        "llm_model": model_ref or None,
        "llm_base_url": args.llm_base_url or memory.get("llm_base_url") or model_data.get("base_url"),
        "state_dir": state_dir,
        "gateway_api_key_file": str(gateway_key_file),
        "keychain_service": memory.get("keychain_service") or "repository-memorycore",
        "keychain_account": memory.get("keychain_account") or getpass.getuser(),
        "use_openclaw_credential": True,
        "team_id": args.team_id or memory.get("team_id") or "local",
        "agent_id": args.agent_id or memory.get("agent_id") or os.environ.get("OPENCLAW_AGENT_ID") or getpass.getuser(),
        "user_id": args.user_id or memory.get("user_id") or getpass.getuser(),
    })
    if getattr(args, "pipeline_mode", None):
        memory["pipeline_mode"] = args.pipeline_mode
    current["memorycore"] = memory
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "config": str(path), "memorycore": {k: v for k, v in memory.items() if "key" not in k.lower()}, "credential_source": "runtime-discovered", "model_configured": bool(model_data.get("base_url"))}


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _plist() -> str:
    script = str(Path(__file__).resolve())
    config = str(_config_path())
    log_dir = str(Path.home() / ".local" / "share" / "repository-memory" / "logs")
    memory_root = str(_memory_config().get("root") or discover_root() or Path.cwd())
    node = os.environ.get("REPOSITORY_MEMORY_NODE") or shutil.which("node") or "/opt/homebrew/bin/node"
    path_value = ":".join(dict.fromkeys([
        str(Path(node).parent),
        "/opt/homebrew/opt/node/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]))
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    values = [sys.executable, script, "run"]
    def xml(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    arguments = "\n".join(f"        <string>{xml(value)}</string>" for value in values)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
{arguments}
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>REPOSITORY_MEMORY_CONFIG</key><string>{xml(config)}</string>
    <key>REPOSITORY_MEMORY_NODE</key><string>{xml(node)}</string>
    <key>PATH</key><string>{xml(path_value)}</string>
  </dict>
  <key>WorkingDirectory</key><string>{xml(memory_root)}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{xml(log_dir + "/stdout.log")}</string>
  <key>StandardErrorPath</key><string>{xml(log_dir + "/stderr.log")}</string>
</dict></plist>
'''


def _launchctl(*args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def install() -> dict[str, Any]:
    root = Path(str(_memory_config().get("root") or discover_root() or "")).expanduser().resolve()
    dependencies = ensure_runtime_dependencies(root)
    patch = ensure_pipeline_patch(root)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plist(), encoding="utf-8")
    uid = str(os.getuid())
    _launchctl("bootout", f"gui/{uid}/{LABEL}")
    legacy_stopped: list[str] = []
    for legacy_label in LEGACY_LABELS:
        stopped, _ = _launchctl("bootout", f"gui/{uid}/{legacy_label}")
        if stopped:
            legacy_stopped.append(legacy_label)
    ok, output = _launchctl("bootstrap", f"gui/{uid}", str(path))
    if not ok:
        raise RuntimeError(f"launchd bootstrap failed: {output[:240]}")
    _launchctl("kickstart", "-k", f"gui/{uid}/{LABEL}")
    return {"ok": True, "label": LABEL, "plist": str(path), "persistent": True, "legacy_stopped": legacy_stopped, "runtime_dependencies": dependencies, "runtime_patch": patch}


def stop() -> dict[str, Any]:
    ok, output = _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    return {"ok": ok, "label": LABEL, "message": output[:240]}


def status() -> dict[str, Any]:
    client = MemoryCoreClient()
    health = client.health(refresh=True)
    loaded, output = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    return {"ok": health.get("status") == "ready", "service": {"label": LABEL, "loaded": loaded, "plist": str(plist_path()), "detail": output[:240]}, "memory": health}


def run() -> int:
    cfg = _memory_config()
    root = Path(str(cfg.get("root") or discover_root() or "")).expanduser().resolve()
    if not (root / "src" / "gateway" / "server.ts").is_file():
        print("MemoryCore source is not configured", file=sys.stderr)
        return 2
    try:
        ensure_pipeline_patch(root)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    credentials = _credentials(cfg)
    if not credentials.get("base_url") or not credentials.get("api_key"):
        print("MemoryCore LLM gateway credentials are not available; configure Keychain or runtime env", file=sys.stderr)
        return 2
    node = os.environ.get("REPOSITORY_MEMORY_NODE") or shutil.which("node") or "/opt/homebrew/bin/node"
    env = os.environ.copy()
    runtime_config = _runtime_gateway_config(root, Path(str(cfg.get("state_dir") or Path.home() / ".local" / "share" / "repository-memory" / "memorycore")), cfg)
    env.update({
        "TDAI_GATEWAY_CONFIG": str(runtime_config),
        "TDAI_GATEWAY_HOST": str(cfg.get("host") or "127.0.0.1"),
        "TDAI_GATEWAY_PORT": str(cfg.get("port") or 8420),
        "TDAI_DATA_DIR": str(cfg.get("state_dir") or Path.home() / ".local" / "share" / "repository-memory" / "memorycore"),
        "TDAI_LLM_PROVIDER": "openai",
        "TDAI_LLM_BASE_URL": str(credentials["base_url"]),
        "TDAI_LLM_MODEL": str(credentials["model"]),
        "TDAI_LLM_API_KEY": str(credentials["api_key"]),
        # L2 may be generated by the native pipeline. L3 acceptance is an
        # explicit repository-memory promote operation, never an auto-worker.
        "REPOSITORY_MEMORY_DISABLE_AUTO_L3": "1",
    })
    gateway_api_key = _gateway_api_key(cfg)
    if gateway_api_key:
        env["TDAI_GATEWAY_API_KEY"] = gateway_api_key
    process = subprocess.Popen([node, "--import", "tsx", "src/gateway/server.ts"], cwd=root, env=env)
    def terminate(_signum: int, _frame: Any) -> None:
        if process.poll() is None:
            process.terminate()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    return process.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memorycore-service")
    sub = parser.add_subparsers(dest="command", required=True)
    configure_parser = sub.add_parser("configure")
    configure_parser.add_argument("--memorycore-root", dest="root")
    configure_parser.add_argument("--endpoint")
    configure_parser.add_argument("--llm-base-url")
    configure_parser.add_argument("--model")
    configure_parser.add_argument("--state-dir")
    configure_parser.add_argument("--team-id")
    configure_parser.add_argument("--agent-id")
    configure_parser.add_argument("--user-id")
    configure_parser.add_argument("--pipeline-mode", choices=("native", "fast"))
    sub.add_parser("install")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "configure":
        value = configure(args)
    elif args.command == "install" or args.command == "start":
        value = install()
    elif args.command == "stop":
        value = stop()
    elif args.command == "status":
        value = status()
    elif args.command == "run":
        return run()
    else:
        raise RuntimeError(f"unknown command: {args.command}")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if value.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
