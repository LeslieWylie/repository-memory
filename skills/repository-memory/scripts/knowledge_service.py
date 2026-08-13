#!/usr/bin/env python3
"""User-level MemoryKnowledge service bootstrap.

MemoryKnowledge is optional: repository-memory remains useful with its local
citation index when the service is unavailable.  This manager keeps the
vendored TypeScript source, runtime dependencies, state, and launchd process
separate from the Git source repository.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from knowledge import KnowledgeClient, _config_path, _read_config
from memorycore_service import _credentials


LABEL = os.environ.get("REPOSITORY_MEMORY_KNOWLEDGE_LAUNCHD_LABEL", "com.repository-memoryknowledge")


def _config() -> dict[str, Any]:
    return _read_config()


def _knowledge_config() -> dict[str, Any]:
    value = _config().get("knowledge")
    return value if isinstance(value, dict) else {}


def _bundled_root() -> Path:
    return Path(__file__).resolve().parents[1] / "vendor" / "tencentdb-agent-memory-reference" / "MemoryKnowledge"


def discover_root() -> Path | None:
    cfg = _knowledge_config()
    candidates = []
    if cfg.get("root"):
        candidates.append(Path(str(cfg["root"])).expanduser())
    candidates.append(_bundled_root())
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "package.json").is_file() and (root / "src" / "server.ts").is_file():
            return root
    return None


def ensure_runtime_dependencies(root: Path) -> dict[str, Any]:
    tsx = root / "node_modules" / ".bin" / "tsx"
    if tsx.is_file():
        return {"status": "present", "root": str(root)}
    configured = os.environ.get("REPOSITORY_MEMORY_KNOWLEDGE_NODE_MODULES") or _knowledge_config().get("node_modules")
    if configured:
        source = Path(str(configured)).expanduser().resolve()
        if (source / ".bin" / "tsx").is_file() and not root.joinpath("node_modules").exists():
            root.joinpath("node_modules").symlink_to(source, target_is_directory=True)
            return {"status": "linked", "root": str(root), "dependency_source": str(source)}
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("MemoryKnowledge dependencies are missing and npm is unavailable")
    result = subprocess.run(
        [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"MemoryKnowledge dependency installation failed: {detail[:400]}")
    if not tsx.is_file():
        raise RuntimeError("MemoryKnowledge dependency installation completed without tsx")
    return {"status": "installed", "root": str(root)}


def configure(args: argparse.Namespace) -> dict[str, Any]:
    current = _config()
    memory = current.get("memorycore") if isinstance(current.get("memorycore"), dict) else {}
    knowledge = _knowledge_config()
    root = Path(args.root).expanduser().resolve() if args.root else (Path(str(knowledge.get("root"))).expanduser().resolve() if knowledge.get("root") else _bundled_root().resolve())
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else Path(str(knowledge.get("state_dir") or Path.home() / ".local" / "share" / "repository-memory" / "memoryknowledge")).expanduser()
    knowledge.update({
        "root": str(root),
        "endpoint": args.endpoint or knowledge.get("endpoint") or "http://127.0.0.1:8421",
        "port": int(args.port or knowledge.get("port") or 8421),
        "state_dir": str(state_dir),
        "service_id": args.service_id or knowledge.get("service_id") or memory.get("team_id") or "local",
        "team_id": args.team_id or knowledge.get("team_id") or memory.get("team_id") or "local",
        "user_id": args.user_id or knowledge.get("user_id") or memory.get("user_id") or getpass.getuser(),
        "agent_id": args.agent_id or knowledge.get("agent_id") or memory.get("agent_id") or "repository-memory",
    })
    if args.wiki_id:
        knowledge["wiki_id"] = args.wiki_id
    if args.node_modules:
        knowledge["node_modules"] = str(Path(args.node_modules).expanduser().resolve())
    current["knowledge"] = knowledge
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "config": str(path), "knowledge": {k: v for k, v in knowledge.items() if "key" not in k.lower()}}


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _plist() -> str:
    script = str(Path(__file__).resolve())
    config = str(_config_path())
    state_dir = Path(str(_knowledge_config().get("state_dir") or Path.home() / ".local" / "share" / "repository-memory" / "memoryknowledge")).expanduser()
    root = Path(str(_knowledge_config().get("root") or discover_root() or "")).expanduser().resolve()
    log_dir = Path.home() / ".local" / "share" / "repository-memory" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    node = os.environ.get("REPOSITORY_MEMORY_NODE") or shutil.which("node") or "/opt/homebrew/bin/node"
    python = sys.executable
    path_value = ":".join(dict.fromkeys([str(Path(node).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    values = [python, script, "run"]

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
  <key>WorkingDirectory</key><string>{xml(str(root))}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{xml(str(log_dir / "knowledge-stdout.log"))}</string>
  <key>StandardErrorPath</key><string>{xml(str(log_dir / "knowledge-stderr.log"))}</string>
</dict></plist>
'''


def _launchctl(*args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def install() -> dict[str, Any]:
    root = discover_root()
    if root is None:
        raise RuntimeError("MemoryKnowledge source is not configured")
    dependencies = ensure_runtime_dependencies(root)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plist(), encoding="utf-8")
    uid = str(os.getuid())
    _launchctl("bootout", f"gui/{uid}/{LABEL}")
    ok, output = _launchctl("bootstrap", f"gui/{uid}", str(path))
    if not ok:
        raise RuntimeError(f"launchd bootstrap failed: {output[:240]}")
    _launchctl("kickstart", "-k", f"gui/{uid}/{LABEL}")
    return {"ok": True, "label": LABEL, "plist": str(path), "runtime_dependencies": dependencies}


def stop() -> dict[str, Any]:
    ok, output = _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    return {"ok": ok, "label": LABEL, "message": output[:240]}


def status() -> dict[str, Any]:
    loaded, output = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    return {"ok": KnowledgeClient().health().get("status") == "ready", "service": {"label": LABEL, "loaded": loaded, "plist": str(plist_path()), "detail": output[:240]}, "knowledge": KnowledgeClient().health()}


def run() -> int:
    cfg = _knowledge_config()
    root = Path(str(cfg.get("root") or discover_root() or "")).expanduser().resolve()
    if not (root / "src" / "server.ts").is_file():
        print("MemoryKnowledge source is not configured", file=sys.stderr)
        return 2
    try:
        ensure_runtime_dependencies(root)
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    state_dir = Path(str(cfg.get("state_dir") or Path.home() / ".local" / "share" / "repository-memory" / "memoryknowledge")).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    configured = _config()
    memory_cfg = configured.get("memorycore") if isinstance(configured.get("memorycore"), dict) else {}
    credentials = _credentials({**memory_cfg, **cfg})
    node = os.environ.get("REPOSITORY_MEMORY_NODE") or shutil.which("node") or "/opt/homebrew/bin/node"
    env = os.environ.copy()
    env.update({
        "PORT": str(cfg.get("port") or 8421),
        "API_PREFIX": "/v3",
        "KNOWLEDGE_DATA_DIR": str(state_dir),
        "KNOWLEDGE_DB_PATH": str(state_dir / "knowledge.db"),
        "KNOWLEDGE_PUBLIC_BASE_URL": str(cfg.get("endpoint") or "http://127.0.0.1:8421").rstrip("/") + "/v3",
        "LLM_MODE": "custom" if credentials.get("base_url") else "proxy",
        "LLM_PROVIDER": "custom",
        "LLM_MODEL": str(credentials.get("model") or "gpt-5.6-luna"),
    })
    if credentials.get("base_url"):
        env["LLM_BASE_URL"] = str(credentials["base_url"])
    if credentials.get("api_key"):
        env["LLM_API_KEY"] = str(credentials["api_key"])
    process = subprocess.Popen([node, "--import", "tsx", "src/server.ts"], cwd=root, env=env)

    def terminate(_signum: int, _frame: Any) -> None:
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    return process.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knowledge-service")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure")
    configure.add_argument("--root")
    configure.add_argument("--endpoint")
    configure.add_argument("--port", type=int)
    configure.add_argument("--state-dir")
    configure.add_argument("--service-id")
    configure.add_argument("--team-id")
    configure.add_argument("--user-id")
    configure.add_argument("--agent-id")
    configure.add_argument("--wiki-id")
    configure.add_argument("--node-modules")
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
    elif args.command in {"install", "start"}:
        value = install()
    elif args.command == "stop":
        value = stop()
    elif args.command == "status":
        value = status()
    else:
        return run()
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if value.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
