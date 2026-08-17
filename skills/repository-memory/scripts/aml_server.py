#!/usr/bin/env python3
"""Synchronous Add/Search server for the Agent Memory Leaderboard contract.

The server is deliberately dependency-free.  It exposes only the two
participant operations plus an unauthenticated health probe; the actual
storage and ranking are delegated to the standalone repository-memory core.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from standalone_memory import standalone_memory_client


MAX_BODY_BYTES = 4 * 1024 * 1024


class AMLService:
    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.memory = standalone_memory_client()

    def authorized(self, headers: Any) -> bool:
        if not self.api_key:
            return True
        values = [headers.get("Authorization", ""), headers.get("X-Api-Key", "")]
        for value in values:
            token = value.strip()
            if token.casefold().startswith("bearer ") or token.casefold().startswith("token "):
                token = token.split(" ", 1)[1].strip()
            if hmac.compare_digest(token, self.api_key):
                return True
        return False

    def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        user_id = payload.get("user_id")
        session_id = payload.get("session_id")
        messages = payload.get("messages")
        result = self.memory.ingest_aml(
            request_id=str(request_id or ""),
            user_id=str(user_id or ""),
            session_id=str(session_id or ""),
            messages=messages if isinstance(messages, list) else [],
        )
        return {
            "success": True,
            "request_id": str(request_id),
            "user_id": str(user_id),
            "session_id": str(session_id),
        }

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        top_k = payload.get("top_k")
        if not query or not user_id or isinstance(top_k, bool):
            raise ValueError("Search requires query, user_id and top_k")
        try:
            top_k = int(top_k)
        except (TypeError, ValueError) as exc:
            raise ValueError("top_k must be an integer") from exc
        if top_k < 1:
            raise ValueError("top_k must be positive")
        return {"data": self.memory.search_aml(query, user_id, min(top_k, 100))}


class Handler(BaseHTTPRequestHandler):
    service: AMLService

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep benchmark stdout clean; operators can use the process log if
        # they need access logs.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length is required") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body exceeds the 4 MiB limit")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"", "/health", "/v1/health"}:
            self._json(HTTPStatus.OK, {"ok": True, "service": "repository-memory", "protocol": "agent-memory-leaderboard"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"detail": {"reason": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path not in {"/add", "/search", "/v1/add", "/v1/search"}:
            self._json(HTTPStatus.NOT_FOUND, {"detail": {"reason": "not found"}})
            return
        if not self.service.authorized(self.headers):
            self._json(HTTPStatus.UNAUTHORIZED, {"detail": {"reason": "invalid API key"}})
            return
        try:
            payload = self._read_json()
            result = self.service.add(payload) if path.endswith("/add") else self.service.search(payload)
            self._json(HTTPStatus.OK, result)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": {"reason": str(exc)}})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": {"reason": "internal memory error"}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AML Add/Search adapter for repository-memory")
    parser.add_argument("--host", default=os.environ.get("AML_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AML_PORT", "8080")))
    parser.add_argument("--api-key", default=os.environ.get("AML_API_KEY", ""))
    args = parser.parse_args(argv)
    service = AMLService(args.api_key)
    handler = type("RepositoryMemoryAMLHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({"service": "repository-memory", "protocol": "agent-memory-leaderboard", "host": args.host, "port": args.port, "auth": bool(args.api_key)}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
