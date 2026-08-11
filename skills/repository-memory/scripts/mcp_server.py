#!/usr/bin/env python3
"""MCP stdio server backed by the CLI runtime.

The modern MCP revision is ``2026-07-28``: stdio uses newline-delimited
JSON-RPC, clients send protocol metadata per request, and ``server/discover``
is the discovery entrypoint.  The parser still accepts legacy Content-Length
frames and the initialize handshake so existing hosts can negotiate down while
they migrate.  Legacy support is compatibility-only; new behavior belongs on
the modern path.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from typing import Any

from version import VERSION


MODERN_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_PROTOCOLS = (MODERN_PROTOCOL, *LEGACY_PROTOCOLS)
SERVER_NAME = "repository-memory"
SERVER_VERSION = VERSION


def _tool_schema() -> list[dict[str, Any]]:
    return [
        {"name": "memory_doctor", "description": "Inspect the server-configured repository sources, adapters, freshness, and index state. Omit root unless an explicit verified Git root is required.", "inputSchema": {"type": "object", "properties": {"root": {"type": "string", "description": "Optional verified Git repository root; omit to use server discovery."}, "source": {"type": "string"}}}},
        {"name": "memory_sync", "description": "Fetch remote snapshots and update derived indexes without changing the worktree. Uses the server-configured root by default.", "inputSchema": {"type": "object", "properties": {"root": {"type": "string", "description": "Optional verified Git repository root; omit to use server discovery."}, "source": {"type": "string"}, "local": {"type": "boolean"}}, "additionalProperties": False}},
        {"name": "memory_search", "description": "Search repository evidence, native conversation memory, or both. Repository is the default; all keeps source groups separate.", "inputSchema": {"type": "object", "required": ["query"], "properties": {"root": {"type": "string", "description": "Optional verified Git repository root; omit to use server discovery."}, "source": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "deep": {"type": "boolean"}, "local": {"type": "boolean"}, "scope": {"type": "string", "enum": ["repository", "memory", "all"], "default": "repository"}}, "additionalProperties": False}},
        {"name": "memory_get", "description": "Resolve a memory result and its source evidence. Pass the result citation commit when pinning the evidence window matters.", "inputSchema": {"type": "object", "required": ["id"], "properties": {"root": {"type": "string", "description": "Optional verified Git repository root; omit to use server discovery."}, "id": {"type": "string"}, "commit": {"type": "string", "description": "Optional commit from the search citation; mismatch returns stale instead of silently reading a newer source."}}, "additionalProperties": False}},
        {"name": "memory_init", "description": "Explicitly register a user-provided knowledge directory and build its disposable local index. This changes user config/cache only, never canonical files.", "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "source_id": {"type": "string"}, "repository": {"type": "string"}, "profile": {"type": "string"}, "local_only": {"type": "boolean", "description": "Declare an intentional offline/local snapshot; it is fresh relative to its clean commit, not necessarily the latest remote revision."}, "sync": {"type": "boolean", "default": True}}, "additionalProperties": False}},
        {"name": "memory_ingest", "description": "Explicitly ingest a supplied session JSON/JSONL value into the configured memory backend. Never call this during ordinary search.", "inputSchema": {"type": "object", "required": ["session"], "properties": {"root": {"type": "string"}, "source": {"type": "string"}, "session": {}, "source_id": {"type": "string"}}, "additionalProperties": False}},
        {"name": "memory_context", "description": "Build a task context package from repository evidence and shared Team Memory. Provenance remains separated; no cross-backend score fusion is used.", "inputSchema": {"type": "object", "required": ["query"], "properties": {"root": {"type": "string"}, "source": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "repo": {"type": "string"}, "issue": {"type": "string"}, "branch": {"type": "string"}, "agent": {"type": "string"}, "local": {"type": "boolean"}}, "additionalProperties": False}},
        {"name": "memory_publish", "description": "Explicitly publish a compact shared Team Memory record. Default status is candidate; this is a write operation and never edits a canonical repository.", "inputSchema": {"type": "object", "required": ["memory"], "properties": {"memory": {"type": "object"}, "status": {"type": "string", "enum": ["candidate", "active"]}}, "additionalProperties": False}},
        {"name": "memory_feedback", "description": "Record whether a shared Team Memory result was helpful, stale, wrong, or not helpful.", "inputSchema": {"type": "object", "required": ["id", "rating"], "properties": {"id": {"type": "string"}, "rating": {"type": "string", "enum": ["helpful", "not_helpful", "stale", "wrong"]}, "note": {"type": "string"}}, "additionalProperties": False}},
        {"name": "memory_supersede", "description": "Explicitly publish a replacement Team Memory record and mark the old record superseded.", "inputSchema": {"type": "object", "required": ["id", "memory"], "properties": {"id": {"type": "string"}, "memory": {"type": "object"}}, "additionalProperties": False}},
    ]


def _json_result(value: Any) -> dict[str, Any]:
    return {"resultType": "complete", "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True)}], "structuredContent": value}


def _discover_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOLS),
        "capabilities": {"tools": {}},
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}},
        "instructions": "Use memory_context at task start when shared team knowledge may matter. Keep repository citations distinct from experience, decision, failure, solution, and handoff provenance. Publish only explicit, compact knowledge.",
        "ttlMs": 3600000,
        "cacheScope": "private",
    }


def _tools_result(tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {"resultType": "complete", "tools": tools, "ttlMs": 30000, "cacheScope": "private"}


def _request_version(request: dict[str, Any]) -> str | None:
    params = request.get("params")
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("io.modelcontextprotocol/protocolVersion")
    return str(value) if value else None


def _unsupported_version(requested: str) -> ValueError:
    error = ValueError(f"Unsupported protocol version: {requested}")
    setattr(error, "supported_versions", list(SUPPORTED_PROTOCOLS))
    setattr(error, "requested_version", requested)
    return error


def _stream_bytes() -> Any:
    """Return stdin/stdout-compatible binary input where available."""

    return getattr(sys.stdin, "buffer", sys.stdin)


def _as_bytes(value: bytes | str) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _iter_messages(stream: Any) -> Iterator[tuple[dict[str, Any], bool]]:
    """Yield ``(request, framed)`` pairs from MCP or newline JSON input."""

    buffer = bytearray()
    read_chunk = getattr(stream, "read1", stream.read)
    while True:
        # ``BufferedReader.read(n)`` may wait for n bytes on a live pipe;
        # ``read1`` returns as soon as one transport chunk is available so the
        # server can answer OpenClaw before stdin closes.
        chunk = read_chunk(4096)
        if not chunk:
            break
        buffer.extend(_as_bytes(chunk))

        while True:
            while buffer[:1] in (b" ", b"\t", b"\r", b"\n"):
                del buffer[:1]
            if not buffer:
                break

            # Standard MCP framing: headers, a blank line, then an exact byte
            # count.  Header names are case-insensitive, as required by HTTP.
            if bytes(buffer).lower().startswith(b"content-length:"):
                separator = bytes(buffer).find(b"\r\n\r\n")
                separator_len = 4
                if separator < 0:
                    separator = bytes(buffer).find(b"\n\n")
                    separator_len = 2
                if separator < 0:
                    break
                header_block = bytes(buffer[:separator]).decode("ascii", errors="replace")
                content_length = None
                for header in header_block.splitlines():
                    name, _, value = header.partition(":")
                    if name.strip().lower() == "content-length":
                        content_length = int(value.strip())
                        break
                if content_length is None:
                    raise ValueError("MCP frame has no Content-Length header")
                body_start = separator + separator_len
                if len(buffer) - body_start < content_length:
                    break
                body = bytes(buffer[body_start:body_start + content_length])
                del buffer[:body_start + content_length]
                yield json.loads(body.decode("utf-8")), True
                continue

            # Compatibility mode for newline-delimited JSON.  This is useful
            # for local diagnostics, but OpenClaw uses the framed branch.
            newline = bytes(buffer).find(b"\n")
            if newline < 0:
                break
            line = bytes(buffer[:newline]).strip()
            del buffer[:newline + 1]
            if line:
                yield json.loads(line.decode("utf-8")), False

    while buffer:
        while buffer[:1] in (b" ", b"\t", b"\r", b"\n"):
            del buffer[:1]
        if not buffer:
            break
        if bytes(buffer).lower().startswith(b"content-length:"):
            raise ValueError("incomplete MCP Content-Length frame")
        yield json.loads(bytes(buffer).decode("utf-8")), False
        buffer.clear()


def _write_response(response: dict[str, Any], framed: bool) -> None:
    body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framed:
        payload = b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
    else:
        payload = body + b"\n"
    output = getattr(sys.stdout, "buffer", None)
    if output is not None:
        output.write(payload)
        output.flush()
    else:
        sys.stdout.write(payload.decode("utf-8"))
        sys.stdout.flush()


def serve(dispatch: Callable[[str, dict[str, Any]], dict[str, Any]]) -> int:
    for request, framed in _iter_messages(_stream_bytes()):
        request_id = None
        try:
            method = request.get("method")
            request_id = request.get("id")
            if method == "notifications/initialized" or (method and method.startswith("notifications/")):
                continue
            if method == "server/discover":
                result = _discover_result()
            elif method == "initialize":
                # Legacy clients must receive the version they requested when
                # it is one of the retained compatibility revisions.
                params = request.get("params") if isinstance(request.get("params"), dict) else {}
                requested = str(params.get("protocolVersion") or LEGACY_PROTOCOLS[-1])
                negotiated = requested if requested in LEGACY_PROTOCOLS else LEGACY_PROTOCOLS[0]
                result = {"protocolVersion": negotiated, "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
            else:
                version = _request_version(request)
                if version and version not in SUPPORTED_PROTOCOLS:
                    raise _unsupported_version(version)
                if version == MODERN_PROTOCOL:
                    # Modern requests are independent; no connection state is
                    # created and the same validation runs for every request.
                    pass
                if method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = _tools_result(_tool_schema())
                elif method == "tools/call":
                    params = request.get("params") or {}
                    name = str(params.get("name") or "")
                    arguments = params.get("arguments") or {}
                    if name not in {tool["name"] for tool in _tool_schema()}:
                        raise ValueError(f"unknown tool: {name}")
                    result = _json_result(dispatch(name, arguments))
                else:
                    raise ValueError(f"unsupported method: {method}")
            if request_id is not None:
                _write_response({"jsonrpc": "2.0", "id": request_id, "result": result}, framed)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:  # MCP must keep the stream alive after one bad request.
            if request_id is None:
                continue
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            data = {"type": type(exc).__name__, "source": arguments.get("source"), "adapter": "repository-memory-runtime", "freshness": None}
            if hasattr(exc, "supported_versions"):
                data.update({"supported": getattr(exc, "supported_versions"), "requested": getattr(exc, "requested_version", None)})
                code = -32022
            else:
                code = -32000
            _write_response({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": str(exc), "data": data}}, framed)
    return 0
