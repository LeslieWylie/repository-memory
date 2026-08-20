"""Optional same-source semantic index for repository evidence.

The semantic index is derived cache only.  It never replaces lexical/path
matching, citation validation, or the canonical Git source.  A model is
downloaded only by an explicit semantic setup/configuration operation; normal
search and doctor are local-only and fall back cleanly when the model is not
available.
"""

from __future__ import annotations

import datetime as dt
import array
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from discovery import config_path, read_config
from local_embedding import (
    GATEWAY_ALIASES,
    embedding_status,
    encode_document_vectors,
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


def _atomic_buffer(destination: Path, buffer: array.array) -> None:
    """Write a float buffer without materializing a second copy of it.

    ``tobytes()`` on a 37k-document index would allocate another 75 MB beside
    the buffer we already hold; writing through the buffer protocol does not.
    The file is little-endian on every platform, matching what ``load`` expects.
    """

    payload = buffer
    if sys.byteorder != "little":
        payload = array.array("f", buffer)
        payload.byteswap()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temporary = tempfile.mkstemp(prefix="semantic-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(memoryview(payload))
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


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
    dimension = int(metadata.get("dimension") or 0)
    paths = metadata.get("paths") if isinstance(metadata.get("paths"), list) else []
    if dimension <= 0:
        return None
    try:
        # Keep the derived vector cache compact. The old reader unpacked every
        # float into a nested Python list on every request, creating hundreds
        # of MB of transient objects for a large repository.
        raw = vectors_path.read_bytes()
        vectors = array.array("f")
        vectors.frombytes(raw)
    except (OSError, ValueError, OverflowError):
        return None
    if sys.byteorder != "little":
        vectors.byteswap()
    if len(vectors) != len(paths) * dimension:
        return None
    return {
        **metadata,
        "vector_store": vectors,
        "available": True,
        "strategy": "local-hybrid",
        "native_neural_model": metadata.get("provider") != "builtin",
    }


def _deferred(reason: str) -> dict[str, Any]:
    return {
        "configured": True,
        "available": False,
        "indexed": False,
        "strategy": "lexical",
        "deferred": True,
        "defer_reason": reason,
    }


def ensure(
    view: SourceView,
    local_index: dict[str, Any],
    deep: bool = False,
    *,
    allow_download: bool = False,
    build: bool = True,
) -> dict[str, Any]:
    """Build or load the semantic cache for an already-built Git index.

    ``build=False`` is the request path for a large source: loading a cache that
    already exists costs one file read, while building one costs an encode of
    the whole corpus.  Deferring the *build* is what keeps a first query fast;
    deferring the *load* as well would mean a cache could be paid for once and
    then never used.  A missing cache returns before the readiness probe so the
    common no-cache case stays free.
    """

    if not build:
        cached = _load_meta(_meta_path(view, deep))
        if not cached or cached.get("commit") != view.commit or not _vectors_path(view, deep).exists():
            return _deferred("large_repository_first_pass")
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
    if not build:
        # A cache exists but does not describe the provider/model/dimension in
        # force now.  ``vectorize`` at query time uses the *current* provider,
        # so scoring those vectors would compare two different embedding spaces.
        # Defer instead, and let the rescue path rebuild if lexical finds nothing.
        return _deferred("semantic_cache_signature_mismatch")
    documents = local_index.get("documents") if isinstance(local_index, dict) else []
    paths = [str(item.get("path")) for item in documents if isinstance(item, dict) and item.get("path")]
    texts = [
        f"{item.get('path')}\n{str(item.get('text') or '')[:12000]}"
        for item in documents
        if isinstance(item, dict) and item.get("path")
    ]
    vectors, dimension, effective = encode_document_vectors(texts, allow_download=allow_download)
    del texts
    if not dimension or len(vectors) != len(paths) * dimension:
        return {**configured, "indexed": False, "strategy": "lexical", "error": "semantic encoder returned no document vectors"}
    # Describe the vectors that were actually produced, not the ones that were
    # requested.  An optional provider can fail after the readiness check and
    # leave the corpus to the local projection; recording the configured triple
    # then would claim a cache we do not hold, and the mismatch would rebuild
    # the whole index on every later search.
    provider = str(effective.get("provider") or provider)
    model = str(effective.get("model") or model)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "source": view.spec.id,
        "repository": view.spec.repository,
        "commit": view.commit,
        "provider": provider,
        "model": model,
        "dimension": dimension,
        "native_neural_model": bool(effective.get("native_neural_model")),
        "paths": paths,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "vectors_path": str(vectors_file),
    }
    _atomic_buffer(vectors_file, vectors)
    _atomic_json(_meta_path(view, deep), metadata)
    return {**metadata, "vector_store": vectors, "available": True, "indexed": True, "strategy": "local-hybrid"}


def configure(
    *,
    model: str,
    enabled: bool = True,
    allow_download: bool = False,
    provider: str = "huggingface",
    endpoint: str | None = None,
    dimensions: int | None = None,
    api_key_env: str | None = None,
    api_key_file: str | None = None,
    api_key_json_path: str | None = None,
) -> dict[str, Any]:
    path = config_path()
    value = read_config()
    semantic = value.get("semantic") if isinstance(value.get("semantic"), dict) else {}
    # Download permission is ephemeral.  It is never persisted as a default
    # for future doctor/search calls.
    semantic.update({"enabled": bool(enabled), "provider": str(provider), "model": str(model), "allow_download": False})
    if str(provider).strip().casefold() in GATEWAY_ALIASES:
        if endpoint:
            semantic["endpoint"] = str(endpoint).strip().rstrip("/")
        if dimensions:
            semantic["dimensions"] = int(dimensions)
        # Persist the *name* of the variable holding the credential, never the
        # credential.  A configuration file is copied, backed up and diffed;
        # a secret written into it leaks by every one of those routes.  A file
        # path is the same bargain: it says where the secret is, not what it is,
        # and it survives into contexts that have no environment to read.
        if api_key_env:
            semantic["api_key_env"] = str(api_key_env).strip()
        if api_key_file:
            semantic["api_key_file"] = str(api_key_file).strip()
        if api_key_json_path:
            semantic["api_key_json_path"] = str(api_key_json_path).strip()
        semantic.pop("api_key", None)
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
    result = {key: item for key, item in value.items() if key not in {"vectors", "vector_store", "paths"}}
    if isinstance(value.get("paths"), list):
        result["document_count"] = len(value["paths"])
    return result
