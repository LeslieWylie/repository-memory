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
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from discovery import config_path, read_config
from local_embedding import (
    GATEWAY_ALIASES,
    GATEWAY_MAX_CHARS,
    embedding_status,
    encode_document_vectors,
)
from local_index import index_path
from models import SourceView

SCHEMA_VERSION = 2
# Keep stored chunk provenance inside the exact payload bound used by the
# configured gateway.  Otherwise metadata could cite lines that were silently
# truncated before embedding.
SEMANTIC_CHUNK_MAX_CHARS = GATEWAY_MAX_CHARS
SEMANTIC_INCREMENTAL_MAX_NEW_CHUNKS = 96
_DATED_MARKDOWN_HEADING = re.compile(
    r"^#{1,3}\s+20\d{2}(?:-\d{1,2}(?:-\d{1,2})?|年\d{1,2}月(?:\d{1,2}日)?)"
)


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


def _chunk_payload(path: str, text: str, line_start: int, line_end: int) -> dict[str, Any]:
    encoded_text = f"{path}\n{text}"
    return {
        "path": path,
        "line_start": line_start,
        "line_end": line_end,
        "digest": hashlib.sha256(encoded_text.encode("utf-8")).hexdigest(),
        "text": encoded_text,
    }


def _bounded_section_chunks(path: str, lines: list[str], line_offset: int) -> list[dict[str, Any]]:
    """Split one Markdown section without losing physical line provenance."""

    if not lines:
        return []
    content_limit = max(512, SEMANTIC_CHUNK_MAX_CHARS - len(path) - 1)
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines):
            line_size = len(lines[end]) + 1
            if end > start and size + line_size > content_limit:
                break
            size += line_size
            end += 1
            if size >= content_limit:
                break
        # One generated/minified line can exceed the provider guard.  Keep its
        # physical line locator while slicing the text into bounded payloads.
        if end == start + 1 and len(lines[start]) > content_limit:
            for offset in range(0, len(lines[start]), content_limit):
                chunks.append(_chunk_payload(
                    path,
                    lines[start][offset:offset + content_limit],
                    line_offset + start + 1,
                    line_offset + start + 1,
                ))
        else:
            chunks.append(_chunk_payload(
                path,
                "\n".join(lines[start:end]),
                line_offset + start + 1,
                line_offset + end,
            ))
        start = max(end, start + 1)
    return chunks


def document_chunks(path: str, text: str) -> list[dict[str, Any]]:
    """Return stable, line-addressable semantic chunks for one document.

    Small documents remain one vector.  Long dated Markdown is split at date
    headings before applying the provider-size bound.  That seam matters for
    append/prepend-heavy standups: adding today's dated section creates one new
    digest while yesterday's section keeps its vector even though every later
    physical line number moved.  Ordinary headings are deliberately not chunk
    boundaries: generated indexes and heading-dense notes otherwise create
    hundreds of tiny vectors with no retrieval benefit.
    """

    lines = text.splitlines()
    if len(path) + 1 + len(text) <= SEMANTIC_CHUNK_MAX_CHARS:
        return [_chunk_payload(path, text, 1, max(1, len(lines)))]
    markdown = Path(path).suffix.casefold() in {".md", ".mdx"}
    boundaries = [
        index for index, line in enumerate(lines)
        if markdown and _DATED_MARKDOWN_HEADING.match(line)
    ]
    if not boundaries or boundaries[0] != 0:
        boundaries.insert(0, 0)
    boundaries.append(len(lines))
    chunks: list[dict[str, Any]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        chunks.extend(_bounded_section_chunks(path, lines[start:end], start))
    return chunks


def _all_chunks(local_index: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for item in local_index.get("documents", []) if isinstance(local_index, dict) else []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        chunks.extend(document_chunks(str(item["path"]), str(item.get("text") or "")))
    return chunks


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
        "indexed": True,
        "strategy": "local-hybrid",
        "native_neural_model": metadata.get("provider") != "builtin",
    }


def _prior_chunk_vectors(
    view: SourceView,
    deep: bool,
    *,
    provider: str,
    model: str,
    dimension: int,
) -> tuple[dict[str, Any], array.array, dict[str, int]] | None:
    """Load the newest compatible chunk cache from an earlier commit."""

    current = _meta_path(view, deep)
    suffix = "-deep.json.semantic.json" if deep else "-default.json.semantic.json"
    candidates = sorted(
        (path for path in current.parent.glob(f"*{suffix}") if path != current),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for metadata_path in candidates:
        metadata = _load_meta(metadata_path)
        if not metadata:
            continue
        if (
            metadata.get("provider") != provider
            or metadata.get("model") != model
            or int(metadata.get("dimension") or 0) != dimension
        ):
            continue
        chunks = metadata.get("chunks") if isinstance(metadata.get("chunks"), list) else []
        if not chunks:
            continue
        vectors_path = Path(str(metadata.get("vectors_path") or metadata_path.with_suffix(".bin")))
        try:
            raw = vectors_path.read_bytes()
            vectors = array.array("f")
            vectors.frombytes(raw)
        except (OSError, ValueError, OverflowError):
            continue
        if sys.byteorder != "little":
            vectors.byteswap()
        if len(vectors) != len(chunks) * dimension:
            continue
        offsets = {
            str(chunk.get("digest")): offset
            for offset, chunk in enumerate(chunks)
            if isinstance(chunk, dict) and chunk.get("digest")
        }
        if offsets:
            return metadata, vectors, offsets
    return None


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

    ``build=False`` is the request path for a large source.  A genuinely new
    source still defers before probing the provider, but a later commit may
    reuse chunk vectors from the newest compatible cache and encode only a
    bounded delta.  This keeps a daily standup repository semantic after its
    first explicit sync instead of invalidating the entire corpus on every
    commit.
    """

    metadata_path = _meta_path(view, deep)
    vectors_file = _vectors_path(view, deep)
    metadata = _load_meta(metadata_path)
    current_cache_exists = bool(metadata and metadata.get("commit") == view.commit and vectors_file.exists())
    if not build and not current_cache_exists:
        suffix = "-deep.json.semantic.json" if deep else "-default.json.semantic.json"
        prior_metadata_exists = any(
            candidate != metadata_path and _load_meta(candidate)
            for candidate in metadata_path.parent.glob(f"*{suffix}")
        )
        if not prior_metadata_exists:
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
    if not build and current_cache_exists and metadata:
        return _deferred("semantic_cache_signature_mismatch")

    chunks = _all_chunks(local_index)
    if not chunks:
        return {**configured, "indexed": False, "strategy": "lexical", "error": "semantic index has no document chunks"}
    prior = _prior_chunk_vectors(view, deep, provider=provider, model=model, dimension=dimension)
    if not build and prior is None:
        # A cache exists, but not in the embedding space the current query
        # encoder uses.  Never compare vectors from two providers/models.
        return _deferred("semantic_cache_signature_mismatch")

    prior_metadata, prior_vectors, prior_offsets = prior or ({}, array.array("f"), {})
    missing_by_digest: dict[str, dict[str, Any]] = {}
    reused_chunk_count = 0
    for chunk in chunks:
        digest = str(chunk["digest"])
        if digest in prior_offsets:
            reused_chunk_count += 1
        else:
            missing_by_digest.setdefault(digest, chunk)
    missing = list(missing_by_digest.values())
    if not build and len(missing) > SEMANTIC_INCREMENTAL_MAX_NEW_CHUNKS:
        return {
            **_deferred("incremental_delta_too_large"),
            "reused_chunk_count": reused_chunk_count,
            "missing_chunk_count": len(missing),
            "incremental_limit": SEMANTIC_INCREMENTAL_MAX_NEW_CHUNKS,
        }

    encoded = array.array("f")
    effective = configured
    if missing:
        encoded, encoded_dimension, effective = encode_document_vectors(
            [str(chunk["text"]) for chunk in missing],
            allow_download=allow_download,
        )
        if not encoded_dimension or len(encoded) != len(missing) * encoded_dimension:
            return {**configured, "indexed": False, "strategy": "lexical", "error": "semantic encoder returned no chunk vectors"}
        effective_provider = str(effective.get("provider") or provider)
        effective_model = str(effective.get("model") or model)
        compatible_with_prior = (
            not prior
            or (
                effective_provider == provider
                and effective_model == model
                and encoded_dimension == dimension
            )
        )
        if not compatible_with_prior:
            # A gateway can fail after its readiness probe and fall back to the
            # builtin encoder.  Reusing gateway vectors beside builtin vectors
            # would silently corrupt similarity.  The explicit build path may
            # restart as a full, single-space encode; a search stays lexical.
            if not build:
                return _deferred("incremental_encoder_changed")
            prior_metadata, prior_vectors, prior_offsets = {}, array.array("f"), {}
            reused_chunk_count = 0
            missing = chunks
            encoded, encoded_dimension, effective = encode_document_vectors(
                [str(chunk["text"]) for chunk in missing],
                allow_download=allow_download,
            )
            if not encoded_dimension or len(encoded) != len(missing) * encoded_dimension:
                return {**configured, "indexed": False, "strategy": "lexical", "error": "semantic encoder returned no chunk vectors"}
        dimension = encoded_dimension

    encoded_offsets = {str(chunk["digest"]): offset for offset, chunk in enumerate(missing)}
    vectors = array.array("f")
    for chunk in chunks:
        digest = str(chunk["digest"])
        if digest in prior_offsets:
            offset = prior_offsets[digest] * dimension
            vectors.extend(prior_vectors[offset:offset + dimension])
        else:
            offset = encoded_offsets[digest] * dimension
            vectors.extend(encoded[offset:offset + dimension])
    if len(vectors) != len(chunks) * dimension:
        return {**configured, "indexed": False, "strategy": "lexical", "error": "semantic chunk assembly was incomplete"}

    # Describe the vectors that were actually produced, not the ones that were
    # requested.  An optional provider can fail after the readiness check and
    # leave the corpus to the local projection; recording the configured triple
    # then would claim a cache we do not hold, and the mismatch would rebuild
    # the whole index on every later search.
    provider = str(effective.get("provider") or provider)
    model = str(effective.get("model") or model)
    paths = [str(chunk["path"]) for chunk in chunks]
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
        "chunks": [
            {key: chunk[key] for key in ("path", "line_start", "line_end", "digest")}
            for chunk in chunks
        ],
        "document_count": len(set(paths)),
        "chunk_count": len(chunks),
        "reused_chunk_count": reused_chunk_count,
        "encoded_chunk_count": len(missing),
        "incremental_from_commit": prior_metadata.get("commit") if prior_metadata else None,
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
    result = {key: item for key, item in value.items() if key not in {"vectors", "vector_store", "paths", "chunks"}}
    if isinstance(value.get("paths"), list):
        result["chunk_count"] = len(value["paths"])
        result["document_count"] = len(set(str(path) for path in value["paths"]))
    return result
