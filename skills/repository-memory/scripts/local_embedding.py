#!/usr/bin/env python3
"""Dependency-free local vector embedding used by the standalone runtime.

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
Hosts that want a neural provider can add one later without changing the
standalone storage or result contract.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Iterable

EMBEDDING_PROVIDER = "builtin"
EMBEDDING_MODEL = "builtin-char-ngram-v1"
EMBEDDING_DIMENSION = 384
_WORD_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}|[\u3400-\u9fff]+", re.UNICODE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


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


def vectorize(text: str, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """Create a normalized signed-hash vector from words and CJK n-grams."""

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


def pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes | bytearray | memoryview | None, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    if not blob or len(blob) != dimension * 4:
        return []
    return list(struct.unpack(f"<{dimension}f", bytes(blob)))


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right))))
