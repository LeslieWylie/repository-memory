#!/usr/bin/env python3
"""Transparent stdio MCP proxy with metadata-only audit events.

The proxy never logs full queries, excerpts, or responses.  It forwards the
original bytes unchanged and records only tool names, request hashes, result
counts, freshness state, and latency in a user-owned JSONL file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def _default_log() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()
    return data_home / "repository-memory" / "audit.jsonl"


class FrameDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[bytes, dict[str, Any]]]:
        self.buffer.extend(chunk)
        output: list[tuple[bytes, dict[str, Any]]] = []
        while True:
            while self.buffer[:1] in (b" ", b"\t", b"\r", b"\n"):
                del self.buffer[:1]
            if not self.buffer:
                break
            lower = bytes(self.buffer).lower()
            if lower.startswith(b"content-length:"):
                separator = bytes(self.buffer).find(b"\r\n\r\n")
                separator_len = 4
                if separator < 0:
                    separator = bytes(self.buffer).find(b"\n\n")
                    separator_len = 2
                if separator < 0:
                    break
                header = bytes(self.buffer[:separator]).decode("ascii", errors="replace")
                length = None
                for line in header.splitlines():
                    name, _, value = line.partition(":")
                    if name.strip().casefold() == "content-length":
                        length = int(value.strip())
                        break
                if length is None:
                    raise ValueError("MCP frame has no Content-Length")
                body_start = separator + separator_len
                if len(self.buffer) - body_start < length:
                    break
                end = body_start + length
                raw = bytes(self.buffer[:end])
                body = bytes(self.buffer[body_start:end])
                del self.buffer[:end]
                output.append((raw, json.loads(body.decode("utf-8"))))
                continue
            newline = bytes(self.buffer).find(b"\n")
            if newline < 0:
                break
            raw = bytes(self.buffer[:newline + 1])
            del self.buffer[:newline + 1]
            line = raw.strip()
            if line:
                output.append((raw, json.loads(line.decode("utf-8"))))
        return output


class Audit:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.proxy_id = uuid.uuid4().hex
        # OpenClaw starts one configured MCP process per profile in the
        # supported installation path.  Preserve that profile identity in
        # metadata-only receipts; when a server is shared by multiple
        # profiles, leave it unset rather than guessing.
        self.agent_id = os.environ.get("REPOSITORY_MEMORY_AGENT_ID") or os.environ.get("OPENCLAW_AGENT_ID") or None
        self.pending: dict[str, dict[str, Any]] = {}
        self.request_protocols: dict[str, str | None] = {}
        self.negotiated_protocol: str | None = None
        self.lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        value = {
            "schema_version": 1,
            "timestamp": time.time(),
            "proxy_id": self.proxy_id,
            **event,
        }
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    @staticmethod
    def _params(request: dict[str, Any]) -> dict[str, Any]:
        params = request.get("params")
        return params if isinstance(params, dict) else {}

    @classmethod
    def _protocol_version(cls, request: dict[str, Any]) -> str | None:
        params = cls._params(request)
        meta = params.get("_meta")
        if isinstance(meta, dict):
            value = meta.get("io.modelcontextprotocol/protocolVersion")
            if value:
                return str(value)
        # Legacy initialize carries the negotiated version in the request
        # params.  Keep this field metadata-only and never log client details.
        if request.get("method") == "initialize" and params.get("protocolVersion"):
            return str(params["protocolVersion"])
        return None

    def request(self, request: dict[str, Any]) -> None:
        params = self._params(request)
        tool = params.get("name") if request.get("method") == "tools/call" else None
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        query = arguments.get("query")
        request_id = str(request.get("id")) if request.get("id") is not None else None
        protocol_version = self._protocol_version(request) or self.negotiated_protocol
        if request.get("method") == "initialize" and self._protocol_version(request):
            # Requests can be pipelined before the child has emitted the
            # initialize response.  The requested legacy version is still
            # safe metadata for labeling the following compatibility frames;
            # the response below replaces it with the negotiated value.
            self.negotiated_protocol = self._protocol_version(request)
        event = {
            "direction": "request",
            "id": request_id,
            "method": request.get("method"),
            "tool": tool,
            "agent": self.agent_id,
            "scope": arguments.get("scope"),
            "source": arguments.get("source"),
            "protocol_version": protocol_version,
            "modern_protocol": protocol_version == "2026-07-28",
            "query_sha256": hashlib.sha256(str(query).encode("utf-8")).hexdigest() if query is not None else None,
        }
        self.write(event)
        if request_id:
            self.request_protocols[request_id] = self._protocol_version(request)
        if request_id and tool:
            self.pending[request_id] = {
                "tool": tool,
                "started": time.perf_counter(),
                "protocol_version": event["protocol_version"],
                "modern_protocol": event["modern_protocol"],
            }

    @staticmethod
    def _payload(response: dict[str, Any]) -> dict[str, Any]:
        result = response.get("result")
        if not isinstance(result, dict):
            return {}
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    value = json.loads(str(item.get("text") or ""))
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    return value
        return result

    def response(self, response: dict[str, Any]) -> None:
        request_id = str(response.get("id")) if response.get("id") is not None else None
        pending = self.pending.pop(request_id, {}) if request_id else {}
        response_result = response.get("result") if isinstance(response.get("result"), dict) else {}
        negotiated = response_result.get("protocolVersion")
        if negotiated:
            self.negotiated_protocol = str(negotiated)
        request_protocol = self.request_protocols.pop(request_id, None) if request_id else None
        protocol_version = request_protocol or pending.get("protocol_version") or self.negotiated_protocol
        payload = self._payload(response)
        freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
        states = sorted({str(value.get("state")) for value in freshness.values() if isinstance(value, dict) and value.get("state")})
        verified = payload.get("verified") if isinstance(payload.get("verified"), list) else []
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        self.write({
            "direction": "response",
            "id": request_id,
            "tool": pending.get("tool"),
            "agent": self.agent_id,
            "protocol_version": protocol_version,
            "modern_protocol": protocol_version == "2026-07-28",
            "error": bool(response.get("error")),
            "latency_ms": round((time.perf_counter() - float(pending.get("started", time.perf_counter()))) * 1000, 3),
            "abstain": payload.get("abstain"),
            "verified_count": len(verified),
            "candidate_count": len(candidates),
            "freshness_states": states,
            "citation_valid_count": sum(bool(item.get("citation", {}).get("valid")) for item in verified if isinstance(item, dict)),
        })


def _stream_forward(stream: Any, target: Any, decoder: FrameDecoder, audit: Audit | None, direction: str) -> None:
    read_chunk = getattr(stream, "read1", stream.read)
    while True:
        chunk = read_chunk(4096)
        if not chunk:
            break
        for raw, message in decoder.feed(chunk):
            if audit:
                if direction == "request":
                    audit.request(message)
                else:
                    audit.response(message)
            target.write(raw)
            target.flush()


def _raw_forward(stream: Any, target: Any) -> None:
    read_chunk = getattr(stream, "read1", stream.read)
    while True:
        chunk = read_chunk(4096)
        if not chunk:
            break
        target.write(chunk)
        target.flush()


def main() -> int:
    parser = argparse.ArgumentParser(prog="repository-memory-audit-proxy")
    parser.add_argument("--log", default=str(_default_log()))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a child MCP command is required after --")
    audit = Audit(Path(args.log)) if os.environ.get("REPOSITORY_MEMORY_AUDIT", "1").casefold() not in {"0", "false", "no"} else None
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert child.stdin and child.stdout and child.stderr
    threads = [
        threading.Thread(target=_stream_forward, args=(sys.stdin.buffer, child.stdin, FrameDecoder(), audit, "request"), daemon=True),
        threading.Thread(target=_stream_forward, args=(child.stdout, sys.stdout.buffer, FrameDecoder(), audit, "response"), daemon=True),
        threading.Thread(target=_raw_forward, args=(child.stderr, sys.stderr.buffer), daemon=True),
    ]
    for thread in threads:
        thread.start()
    threads[0].join()
    child.stdin.close()
    child.wait()
    threads[1].join(timeout=2)
    threads[2].join(timeout=2)
    return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
