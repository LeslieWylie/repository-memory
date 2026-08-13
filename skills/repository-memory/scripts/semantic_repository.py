"""Optional same-source semantic index for repository evidence.

The semantic index is derived cache only.  It never replaces lexical/path
matching, citation validation, or the canonical Git source.  A model is
downloaded only by an explicit semantic setup/configuration operation; normal
search and doctor are local-only and fall back cleanly when the model is not
available.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from discovery import config_path, read_config
from local_embedding import (
    embedding_status,
    pack,
    unpack,
    vectorize_many,
)
from local_index import index_path
from models import SourceView

SCHEMA_VERSION = 1


def _meta_path(view: SourceView, deep: bool) -> Path:
    return Path(f"{index_path(view, deep)}.semantic.json")


def _vectors_path(view: SourceView, deep: bool) -> Path:
    return Path(f"{index_path(view, deep)}.semantic.bin")


def _load_meta(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION else None


def _atomic_bytes(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="semantic-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_json(destination: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(destination, (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))


def _model_signature() -> tuple[str, str, int | None]:
    status = embedding_status(probe=True)
    return str(status.get("provider") or ""), str(status.get("model") or ""), int(status["dimension"]) if status.get("dimension") else None


def status(view: SourceView, deep: bool = False) -> dict[str, Any]:
    configured = embedding_status(probe=True)
    metadata = _load_meta(_meta_path(view, deep))
    provider, model, dimension = _model_signature()
    indexed = bool(
        metadata
        and metadata.get("commit") == view.commit
        and metadata.get("provider") == provider
        and metadata.get("model") == model
        and metadata.get("dimension") == dimension
        and _vectors_path(view, deep).exists()
    )
    return {
        "configured": bool(configured.get("configured")),
        "configured_by": configured.get("configured_by") or "explicit",
        "available": indexed and configured.get("available") is True,
        "provider_available": configured.get("available") is True,
        "provider": provider,
        "model": model,
        "dimension": dimension,
        "indexed": indexed,
        "indexed_commit": metadata.get("commit") if metadata else None,
        "current_commit": view.commit,
        "index_path": str(_meta_path(view, deep)),
        "strategy": "local-hybrid" if configured.get("available") is True else "lexical",
        "native_neural_model": bool(configured.get("native_neural_model")),
        "error": configured.get("error") if configured.get("available") is not True else None,
    }


def load(view: SourceView, deep: bool = False) -> dict[str, Any] | None:
    metadata = _load_meta(_meta_path(view, deep))
    if not metadata or metadata.get("commit") != view.commit:
        return None
    vectors_path = _vectors_path(view, deep)
    try:
        raw = vectors_path.read_bytes()
    except OSError:
        return None
    dimension = int(metadata.get("dimension") or 0)
    paths = metadata.get("paths") if isinstance(metadata.get("paths"), list) else []
    stride = dimension * 4
    if dimension <= 0 or len(raw) != len(paths) * stride:
        return None
    vectors = [unpack(raw[offset:offset + stride], dimension) for offset in range(0, len(raw), stride)]
    return {
        **metadata,
        "vectors": vectors,
        "available": True,
        "strategy": "local-hybrid",
        "native_neural_model": metadata.get("provider") != "builtin",
    }


def ensure(view: SourceView, local_index: dict[str, Any], deep: bool = False, *, allow_download: bool = False) -> dict[str, Any]:
    """Build or load the semantic cache for an already-built Git index."""

    configured = embedding_status(probe=True, allow_download=allow_download)
    if not configured.get("configured") or configured.get("available") is not True:
        # Keep the configured neural provider visible when it is unavailable,
        # but never manufacture a semantic cache from an unavailable model.
        if configured.get("provider") != "builtin":
            return {**configured, "indexed": False, "strategy": "lexical"}
        return {
            **configured,
            "indexed": False,
            "strategy": "lexical",
            "native_neural_model": False,
        }
    # The builtin projection is always available and intentionally cheap; use
    # it for repository recall as well as standalone memory.  It is still
    # clearly reported as non-neural and remains same-source only.
    provider = str(configured["provider"])
    model = str(configured["model"])
    dimension = int(configured["dimension"])
    metadata = _load_meta(_meta_path(view, deep))
    vectors_file = _vectors_path(view, deep)
    if (
        metadata
        and metadata.get("commit") == view.commit
        and metadata.get("provider") == provider
        and metadata.get("model") == model
        and metadata.get("dimension") == dimension
        and vectors_file.exists()
    ):
        loaded = load(view, deep)
        if loaded:
            return loaded
    documents = local_index.get("documents") if isinstance(local_index, dict) else []
    paths = [str(item.get("path")) for item in documents if isinstance(item, dict) and item.get("path")]
    texts = [
        f"{item.get('path')}\n{str(item.get('text') or '')[:12000]}"
        for item in documents
        if isinstance(item, dict) and item.get("path")
    ]
    vectors = vectorize_many(texts, allow_download=allow_download)
    if len(vectors) != len(paths) or not vectors:
        return {**configured, "indexed": False, "strategy": "lexical", "error": "semantic encoder returned no document vectors"}
    dimension = len(vectors[0])
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "source": view.spec.id,
        "repository": view.spec.repository,
        "commit": view.commit,
        "provider": provider,
        "model": model,
        "dimension": dimension,
        "native_neural_model": bool(configured.get("native_neural_model")),
        "paths": paths,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "vectors_path": str(vectors_file),
    }
    _atomic_bytes(vectors_file, b"".join(pack(vector) for vector in vectors))
    _atomic_json(_meta_path(view, deep), metadata)
    return {**metadata, "vectors": vectors, "available": True, "indexed": True, "strategy": "local-hybrid"}


def configure(*, model: str, enabled: bool = True, allow_download: bool = False) -> dict[str, Any]:
    path = config_path()
    value = read_config()
    semantic = value.get("semantic") if isinstance(value.get("semantic"), dict) else {}
    # Download permission is ephemeral.  It is never persisted as a default
    # for future doctor/search calls.
    semantic.update({"enabled": bool(enabled), "provider": "huggingface", "model": str(model), "allow_download": False})
    value["semantic"] = semantic
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, value)
    result = embedding_status(probe=True, allow_download=allow_download)
    if result.get("available") is True:
        runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
        runtime["python"] = sys.executable
        value["runtime"] = runtime
        _atomic_json(path, value)
    return {"config_path": str(path), "semantic": result, "canonical_repo_changed": False}


def model_status() -> dict[str, Any]:
    return {"semantic": embedding_status(probe=True), "config_path": str(config_path()), "canonical_repo_changed": False}


def summary(value: dict[str, Any] | None) -> dict[str, Any]:
    """Strip vectors before a semantic status enters a public response."""

    if not isinstance(value, dict):
        return {"configured": False, "available": False, "strategy": "lexical"}
    result = {key: item for key, item in value.items() if key not in {"vectors", "paths"}}
    if isinstance(value.get("paths"), list):
        result["document_count"] = len(value["paths"])
    return result
