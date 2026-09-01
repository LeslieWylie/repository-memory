#!/usr/bin/env python3
"""Resident loopback service for the optional local Hugging Face encoder.

Short-lived CLI and hook processes should not each load a large neural model.
This module owns one launchd-supervised process and exposes the narrow
OpenAI-compatible embeddings shape used by ``local_embedding``.  It binds to
loopback only, accepts no credentials, and never becomes a source of truth:
all vectors remain derived caches keyed by model and source commit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from discovery import config_path, read_config
from local_embedding import HF_DEFAULT_MODEL, HF_MAX_CHARS, HF_MAX_SEQUENCE_LENGTH


LABEL = os.environ.get("REPOSITORY_MEMORY_SEMANTIC_LAUNCHD_LABEL", "com.repository-memory.semantic")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8493
MAX_BODY_BYTES = 8 * 1024 * 1024


def _loopback_host(host: str) -> str:
    value = str(host or DEFAULT_HOST).strip()
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the local embedding service may bind only to loopback")
    return value


def _semantic_config() -> dict[str, Any]:
    value = read_config().get("semantic")
    return value if isinstance(value, dict) else {}


def _service_settings() -> dict[str, Any]:
    semantic = _semantic_config()
    endpoint = str(semantic.get("service_endpoint") or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}").rstrip("/")
    return {
        "model": str(semantic.get("model") or HF_DEFAULT_MODEL),
        "host": _loopback_host(str(semantic.get("service_host") or DEFAULT_HOST)),
        "port": int(semantic.get("service_port") or DEFAULT_PORT),
        "endpoint": endpoint,
    }


def configure(*, model: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    host = _loopback_host(host)
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("semantic service port must be between 1 and 65535")
    current = read_config()
    semantic = current.get("semantic") if isinstance(current.get("semantic"), dict) else {}
    semantic.update({
        "enabled": True,
        "provider": "huggingface",
        "model": str(model or HF_DEFAULT_MODEL),
        "allow_download": False,
        "service_host": host,
        "service_port": port,
        "service_endpoint": f"http://{host}:{port}",
    })
    current["semantic"] = semantic
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {
        "configured": True,
        "provider": "huggingface",
        "model": semantic["model"],
        "service_endpoint": semantic["service_endpoint"],
        "config_path": str(path),
    }


class EmbeddingEngine:
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer

        try:
            from transformers.utils import logging as transformers_logging

            transformers_logging.set_verbosity_error()
        except (ImportError, AttributeError):
            pass
        device = os.environ.get("REPOSITORY_MEMORY_EMBEDDING_DEVICE")
        if not device:
            try:
                import torch

                device = "mps" if torch.backends.mps.is_available() else "cpu"
            except Exception:
                device = "cpu"
        self.model = model
        self.encoder = SentenceTransformer(
            model,
            trust_remote_code=True,
            device=device,
            model_kwargs={"local_files_only": True},
        )
        self.encoder.max_seq_length = int(
            os.environ.get("REPOSITORY_MEMORY_EMBEDDING_MAX_TOKENS", HF_MAX_SEQUENCE_LENGTH)
        )
        self.dimension = int(self.encoder.get_sentence_embedding_dimension() or 0)
        if not self.dimension:
            probe = self.encoder.encode(["repository-memory"], normalize_embeddings=True, show_progress_bar=False)
            self.dimension = len(probe[0])
        self._lock = threading.Lock()

    def encode(self, texts: list[str]) -> list[list[float]]:
        values = [str(text or "")[:HF_MAX_CHARS] for text in texts]
        with self._lock:
            rows = self.encoder.encode(
                values,
                batch_size=min(16, max(1, len(values))),
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        return [[float(value) for value in row] for row in rows]


class Handler(BaseHTTPRequestHandler):
    engine: EmbeddingEngine

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._json(HTTPStatus.OK, {
                "ok": True,
                "provider": "huggingface",
                "model": self.engine.model,
                "dimension": self.engine.dimension,
            })
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/embeddings":
            self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("request body must be between 1 byte and 8 MiB")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict) or str(payload.get("model") or "") != self.engine.model:
                raise ValueError("requested model does not match the resident model")
            raw = payload.get("input")
            texts = [raw] if isinstance(raw, str) else raw
            if not isinstance(texts, list) or not texts or len(texts) > 64 or not all(isinstance(item, str) for item in texts):
                raise ValueError("input must be one string or a list of 1-64 strings")
            rows = self.engine.encode(texts)
            self._json(HTTPStatus.OK, {
                "object": "list",
                "model": self.engine.model,
                "data": [
                    {"object": "embedding", "index": index, "embedding": row}
                    for index, row in enumerate(rows)
                ],
            })
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "local embedding failed"}})


def serve() -> int:
    settings = _service_settings()
    engine = EmbeddingEngine(settings["model"])
    handler = type("ResidentEmbeddingHandler", (Handler,), {"engine": engine})
    server = ThreadingHTTPServer((settings["host"], settings["port"]), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _plist() -> str:
    script = str(Path(__file__).resolve())
    logs = Path.home() / ".local" / "share" / "repository-memory" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    arguments = "\n".join(f"        <string>{_xml(value)}</string>" for value in (sys.executable, script, "run"))
    path_value = ":".join(dict.fromkeys([str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
{arguments}
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>REPOSITORY_MEMORY_CONFIG</key><string>{_xml(str(config_path()))}</string>
    <key>HF_HUB_OFFLINE</key><string>1</string>
    <key>TOKENIZERS_PARALLELISM</key><string>false</string>
    <key>PATH</key><string>{_xml(path_value)}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{_xml(str(logs / "semantic-stdout.log"))}</string>
  <key>StandardErrorPath</key><string>{_xml(str(logs / "semantic-stderr.log"))}</string>
</dict></plist>
'''


def _launchctl(*args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, encoding="utf-8", timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def _health(endpoint: str, timeout: float = 1.0) -> dict[str, Any]:
    try:
        request = urllib.request.Request(f"{endpoint.rstrip('/')}/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid health response"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


def install(*, model: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    configured = configure(model=model, host=host, port=port)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plist(), encoding="utf-8")
    uid = str(os.getuid())
    _launchctl("bootout", f"gui/{uid}/{LABEL}")
    # ``bootout`` returns before launchd has always released the old job.  A
    # bootstrap in that teardown window fails with opaque error 5 even though
    # the plist is valid.  Wait for the label to disappear and retry the
    # registration briefly; service-install must be safe to run repeatedly.
    teardown_deadline = time.monotonic() + 5.0
    while time.monotonic() < teardown_deadline:
        loaded, _detail = _launchctl("print", f"gui/{uid}/{LABEL}")
        if not loaded:
            break
        time.sleep(0.1)
    bootstrap_deadline = time.monotonic() + 10.0
    ok, output = False, "launchd registration did not complete"
    while time.monotonic() < bootstrap_deadline:
        ok, output = _launchctl("bootstrap", f"gui/{uid}", str(path))
        if ok:
            break
        time.sleep(0.25)
    if not ok:
        raise RuntimeError(f"semantic launchd bootstrap failed: {output[:240]}")
    _launchctl("kickstart", "-k", f"gui/{uid}/{LABEL}")
    deadline = time.monotonic() + 60.0
    health = {"ok": False, "error": "service did not become ready"}
    while time.monotonic() < deadline:
        health = _health(configured["service_endpoint"])
        if health.get("ok"):
            break
        time.sleep(0.25)
    if not health.get("ok"):
        raise RuntimeError(f"semantic service failed to become ready: {health.get('error')}")
    return {
        "ok": True,
        "label": LABEL,
        "plist": str(path),
        "service": health,
        **configured,
    }


def status() -> dict[str, Any]:
    settings = _service_settings()
    loaded, output = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    health = _health(settings["endpoint"])
    return {
        "ok": bool(loaded and health.get("ok")),
        "label": LABEL,
        "loaded": loaded,
        "plist": str(plist_path()),
        "detail": output[:240],
        "service": health,
        "endpoint": settings["endpoint"],
    }


def stop() -> dict[str, Any]:
    ok, output = _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    return {"ok": ok, "label": LABEL, "message": output[:240], "plist": str(plist_path())}


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    if action == "run":
        raise SystemExit(serve())
    if action == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    raise SystemExit(f"unknown action: {action}")
