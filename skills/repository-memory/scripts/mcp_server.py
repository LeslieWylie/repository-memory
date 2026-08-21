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
        {"name": "memory_doctor", "title": "Inspect memory sources", "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}, "description": "Inspect the server-configured repository sources, adapters, freshness, and index state. Omit root unless an explicit verified Git root is required. Set local=true to avoid remote snapshot fetches and no_index=true for a metadata-only fast probe.", "inputSchema": {"type": "object", "properties": {"root": {"type": "string", "description": "Optional verified Git repository root; omit to use server discovery."}, "source": {"type": "string"}, "local": {"type": "boolean"}, "no_index": {"type": "boolean"}}, "additionalProperties": False}},
        {"name": "memory_sync", "title": "Refresh snapshots and indexes", "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}, "description": "Fetch remote snapshots and update derived indexes without changing the worktree. Uses the server-configured root by default.", "inputSchema": {"type": "object", "properties": {"root": {"type": "string", "description": "Optional verified Git repository root; omit to use server discovery."}, "source": {"type": "string"}, "local": {"type": "boolean"}}, "additionalProperties": False}},
        {"name": "memory_search", "title": "Search team & repository memory", "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}, "description": "Answer anything about this project, its history, past conversations, or prior team decisions — pass the user's question verbatim. Returns Git-cited repository evidence as the answer surface, plus conversation memory and team decisions as separate groups. Use `answerable`/`results` for claims; if empty, abstain. `abstain` describes the repository plane only — check `answered_by` before giving up, and answer from that plane's group when it is listed. See references/result-contract.md for the full contract.", "inputSchema": {"type": "object", "required": ["query"], "properties": {"root": {"type": "string", "description": "Optional verified Git repository root; omit to use server discovery."}, "source": {"type": "string"}, "query": {"type": "string", "description": "The user's question, verbatim."}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "deep": {"type": "boolean"}, "local": {"type": "boolean", "description": "Only set true when the user explicitly requests the local/offline worktree."}, "scope": {"type": "string", "enum": ["auto", "repository", "memory", "all"], "default": "auto", "description": "Omit this. `auto` searches every plane and keeps them separate."}}, "additionalProperties": False}},
        {"name": "memory_get", "title": "Resolve one memory with evidence", "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}, "description": "Resolve a memory result and its source evidence. Pass the result citation commit and line_start/line_end when pinning the exact evidence window.", "inputSchema": {"type": "object", "required": ["id"], "properties": {"root": {"type": "string", "description": "Optional verified Git repository root; omit to use server discovery."}, "id": {"type": "string"}, "commit": {"type": "string", "description": "Optional commit from the search citation; mismatch returns stale instead of silently reading a newer source."}, "line_start": {"type": "integer", "minimum": 1}, "line_end": {"type": "integer", "minimum": 1}}, "additionalProperties": False}},
        {"name": "memory_timeline", "title": "Read L0/L1 capture trace", "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}, "description": "Read the ordered L0/L1 trace for a session from the active local memory runtime. This is diagnostic provenance, not repository evidence, and never changes canonical Git data.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, "additionalProperties": False}},
        {"name": "memory_observe", "title": "Observe memory events", "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}, "description": "Observe durable local memory events without ranking or generating a conclusion. This is provenance, not repository evidence, and is read-only.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, "additionalProperties": False}},
        {"name": "memory_reflect", "title": "Generate candidate reflection", "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}, "description": "Generate a bounded, candidate-labelled reflection over local memory. It is a derived aid, not an accepted fact; inspect citations before reuse.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "session_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "additionalProperties": False}},
    ]


def _json_result(value: Any) -> dict[str, Any]:
    return {"resultType": "complete", "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True)}], "structuredContent": value}


def _discover_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOLS),
        "capabilities": {"tools": {}},
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}},
        "instructions": "Use memory_doctor, memory_sync, memory_search, memory_get, memory_timeline, memory_observe, and memory_reflect for read/diagnostic work. Reflection is generated and candidate-labelled, not an accepted fact. Session ingest, feedback, promotion, and other writes are explicit CLI operations and are not exposed through this MCP surface. Keep repository citations distinct from native memory provenance.",
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
