#!/usr/bin/env python3
"""Local embedding providers used by the standalone runtime.

The upstream local-memory implementations use a downloaded MiniLM model and a
SQLite vector extension.  Repository Memory cannot require either a daemon or
a native extension for its default install, so this module keeps the same
operational contract with a deterministic local projection:

* every document and query gets a normalized dense vector;
* vectors are persisted with model/provider/dimension metadata;
* cosine similarity is stable across processes and machines;
* no network, model endpoint, API key, or third-party Python package is used.

This is intentionally named ``builtin-char-ngram-v1`` in diagnostics.  It is a
real local vector index, but it is not presented as a neural MiniLM model.
An optional Hugging Face provider can be enabled through user configuration.
The dependency-free projection remains the safe fallback when the optional
model or its dependencies are unavailable.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import struct
from collections.abc import Iterable
from typing import Any

EMBEDDING_PROVIDER = "builtin"
EMBEDDING_MODEL = "builtin-char-ngram-v1"
EMBEDDING_DIMENSION = 384
HF_PROVIDER = "huggingface"
HF_DEFAULT_MODEL = "Alibaba-NLP/gte-multilingual-base"
HF_MAX_SEQUENCE_LENGTH = 512
_WORD_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}|[\u3400-\u9fff]+", re.UNICODE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")

_HF_ENCODER: Any | None = None
_HF_DIMENSION: int | None = None
_HF_ERROR: str | None = None
_HF_ERROR_KEY: tuple[str, bool] | None = None


def _features(text: str) -> Iterable[tuple[str, float]]:
    normalized = " ".join(str(text or "").casefold().split())
    for token in _WORD_RE.findall(normalized):
        yield f"w:{token}", 1.0
        if not _CJK_RE.search(token):
            for index in range(max(0, len(token) - 2)):
                yield f"c:{token[index:index + 3]}", 0.75
    compact = re.sub(r"\s+", "", normalized)
    for size, weight in ((2, 1.2), (3, 1.0), (4, 0.7)):
        for index in range(max(0, len(compact) - size + 1)):
            gram = compact[index:index + size]
            if _CJK_RE.search(gram):
                yield f"g{size}:{gram}", weight


def _semantic_config() -> dict[str, Any]:
    """Read user-level semantic configuration without making it mandatory."""

    try:
        from discovery import read_config

        value = read_config().get("semantic")
    except (ImportError, OSError, TypeError, ValueError):
        value = None
    return value if isinstance(value, dict) else {}


def _hf_config() -> tuple[bool, str, bool]:
    config = _semantic_config()
    provider = str(config.get("provider") or "builtin").strip().casefold()
    model = str(config.get("model") or HF_DEFAULT_MODEL).strip()
    enabled = bool(config.get("enabled", False)) and provider in {"huggingface", "hf", "gte", "gte-multilingual"}
    allow_download = bool(config.get("allow_download", False))
    return enabled, model, allow_download


def _load_hf_encoder(*, allow_download: bool = False) -> Any | None:
    """Load the configured SentenceTransformers model lazily.

    Search, doctor, and normal sync never download a model.  Only the explicit
    semantic setup command may opt into downloading through ``allow_download``.
    """

    global _HF_ENCODER, _HF_DIMENSION, _HF_ERROR, _HF_ERROR_KEY
    enabled, model, _configured_download = _hf_config()
    if not enabled:
        return None
    # ``allow_download`` is an operation-level capability.  A persisted
    # preference must never make every doctor/search call the network.
    allow_download = bool(allow_download)
    cache_key = (model, allow_download)
    if _HF_ENCODER is not None and _HF_ERROR_KEY == cache_key:
        return _HF_ENCODER
    if _HF_ERROR_KEY == cache_key and _HF_ERROR:
        return None
    _HF_ERROR_KEY = cache_key
    try:
        from sentence_transformers import SentenceTransformer

        model_kwargs = {} if allow_download else {"local_files_only": True}
        if not allow_download:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        device = os.environ.get("REPOSITORY_MEMORY_EMBEDDING_DEVICE")
        if not device:
            try:
                import torch

                device = "mps" if torch.backends.mps.is_available() else "cpu"
            except Exception:
                device = "cpu"
        encoder = SentenceTransformer(
            model,
            trust_remote_code=True,
            device=device,
            model_kwargs=model_kwargs,
        )
        # Whole repository files can be long reports.  One vector per
        # document is a recall candidate, not the citation window; keep the
        # encoder bounded and let lexical citation search provide the exact
        # line-level evidence.
        encoder.max_seq_length = int(os.environ.get("REPOSITORY_MEMORY_EMBEDDING_MAX_TOKENS", HF_MAX_SEQUENCE_LENGTH))
        dimension = encoder.get_sentence_embedding_dimension()
        if not dimension:
            probe = encoder.encode(["repository-memory"], normalize_embeddings=True, show_progress_bar=False)
            dimension = len(probe[0])
        _HF_ENCODER = encoder
        _HF_DIMENSION = int(dimension)
        _HF_ERROR = None
        return encoder
    except Exception as exc:  # optional provider must never break lexical fallback
        _HF_ENCODER = None
        _HF_DIMENSION = None
        _HF_ERROR = f"{type(exc).__name__}: {str(exc)[:300]}"
        return None


def embedding_status(*, probe: bool = True, allow_download: bool = False) -> dict[str, Any]:
    """Return configured and effective provider state for doctor and sync."""

    enabled, model, _configured_download = _hf_config()
    if not enabled:
        return {
            # The dependency-free projection is the default active provider,
            # not an unconfigured placeholder.  ``configured_by`` makes the
            # distinction visible without making callers special-case a
            # missing embedding lane.
            "configured": True,
            "configured_by": "default",
            "available": True,
            "provider": EMBEDDING_PROVIDER,
            "model": EMBEDDING_MODEL,
            "dimension": EMBEDDING_DIMENSION,
            "native_neural_model": False,
            "strategy": "local-hybrid",
            "fallback": False,
        }
    encoder = _load_hf_encoder(allow_download=allow_download) if probe else None
    if encoder is not None:
        return {
            "configured": True,
            "available": True,
            "provider": HF_PROVIDER,
            "model": model,
            "dimension": _HF_DIMENSION,
            "native_neural_model": True,
            "strategy": "local-hybrid",
            "fallback": False,
            "download_allowed": bool(allow_download),
        }
    return {
        "configured": True,
        "available": False,
        "provider": HF_PROVIDER,
        "model": model,
        "dimension": None,
        "native_neural_model": True,
        "strategy": "lexical-fallback",
        "fallback": True,
        "download_allowed": bool(allow_download),
        "error": _HF_ERROR or "model is not cached or optional dependencies are unavailable",
    }


def active_embedding_spec() -> dict[str, Any]:
    """Return the provider actually used for new vectors."""

    status = embedding_status(probe=True)
    if status.get("available") is True:
        return status
    return {
        "configured": bool(status.get("configured")),
        "available": True,
        "provider": EMBEDDING_PROVIDER,
        "model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIMENSION,
        "native_neural_model": False,
        "strategy": "local-hybrid",
        "fallback": bool(status.get("configured")),
        "configured_provider": status.get("provider"),
        "configured_model": status.get("model"),
        "error": status.get("error"),
    }


def vectorize(text: str, dimension: int | None = None) -> list[float]:
    """Create a normalized signed-hash vector from words and CJK n-grams."""

    enabled, _model, _allow_download = _hf_config()
    if enabled:
        encoder = _load_hf_encoder(allow_download=False)
        if encoder is not None:
            values = encoder.encode([str(text or "")[:12000]], normalize_embeddings=True, show_progress_bar=False)[0]
            return [float(value) for value in values]
    dimension = int(dimension or EMBEDDING_DIMENSION)

    vector = [0.0] * dimension
    count = 0
    for feature, weight in _features(text):
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
        first = int.from_bytes(digest[:8], "little") % dimension
        second = int.from_bytes(digest[8:], "little") % dimension
        sign = 1.0 if digest[0] & 1 else -1.0
        vector[first] += weight * sign
        vector[second] += weight * 0.35 * (-sign if digest[1] & 1 else sign)
        count += 1
    if not count:
        return vector
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def vectorize_many(texts: Iterable[str], *, allow_download: bool = False) -> list[list[float]]:
    """Encode a batch efficiently, using the configured provider when ready."""

    # Bound input size before tokenization as an additional memory guard.
    values = [str(text or "")[:12000] for text in texts]
    enabled, _model, _configured_download = _hf_config()
    if enabled:
        encoder = _load_hf_encoder(allow_download=allow_download)
        if encoder is not None:
            encoded = encoder.encode(
                values,
                batch_size=16,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [[float(value) for value in row] for row in encoded]
    return [vectorize(value) for value in values]


def pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes | bytearray | memoryview | None, dimension: int | None = None) -> list[float]:
    if not blob:
        return []
    if dimension is None:
        if len(blob) % 4:
            return []
        dimension = len(blob) // 4
    elif len(blob) != dimension * 4:
        return []
    return list(struct.unpack(f"<{dimension}f", bytes(blob)))


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right))))
