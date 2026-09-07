#!/usr/bin/env python3
"""Persist and benchmark Repository Memory vectors in embedded Qdrant.

The Git repository remains canonical. This prototype stores only derived
vectors and line-addressable payload metadata, and can be deleted safely.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

try:
    import numpy as np
    from qdrant_client import QdrantClient, models
except ImportError as exc:  # pragma: no cover - exercised by the README command
    raise SystemExit(
        "prototype dependencies are missing; run: "
        "uv pip install --python .venv/bin/python "
        "-r experiments/local-vector-db/requirements.txt"
    ) from exc


DEFAULT_DATABASE = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    / "repository-memory"
    / "vector-db"
    / "qdrant"
)
DEFAULT_QUERIES = [
    "去查武垚乐最近做什么",
    "武垚乐 8月18日 做了什么",
    "仓库记忆系统的向量检索为什么没有用起来",
    "团队知识库 RAG 怎么做",
    "Memmy 和 repository memory 有什么差距",
    "ZZZQWE 不存在的中文项目最近在做什么",
]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _discover_metadata() -> Path:
    root = Path.home() / ".cache" / "repository-memory" / "indexes"
    candidates: list[Path] = []
    for path in root.rglob("*.semantic.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            vectors = Path(str(value.get("vectors_path") or ""))
            chunks = value.get("chunks")
            if (
                value.get("schema_version") == 2
                and isinstance(chunks, list)
                and chunks
                and vectors.is_file()
                and int(value.get("dimension") or 0) > 0
            ):
                candidates.append(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if not candidates:
        raise FileNotFoundError(f"no usable semantic metadata under {root}")
    return max(candidates, key=lambda item: item.stat().st_mtime_ns)


def _load_cache(metadata_path: Path) -> tuple[dict[str, Any], array.array]:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    chunks = metadata.get("chunks")
    dimension = int(metadata.get("dimension") or 0)
    if not isinstance(chunks, list) or not chunks or dimension <= 0:
        raise ValueError("semantic metadata has no usable chunks or dimension")
    vectors_path = Path(str(metadata.get("vectors_path") or ""))
    if not vectors_path.is_file():
        raise FileNotFoundError(f"vector file does not exist: {vectors_path}")
    vectors = array.array("f")
    with vectors_path.open("rb") as handle:
        vectors.fromfile(handle, vectors_path.stat().st_size // vectors.itemsize)
    if sys.byteorder != "little":
        vectors.byteswap()
    expected = len(chunks) * dimension
    if len(vectors) != expected:
        raise ValueError(f"vector width mismatch: expected {expected} floats, got {len(vectors)}")
    return metadata, vectors


def _collection_name(metadata: dict[str, Any]) -> str:
    signature = "\0".join(
        [
            str(metadata.get("repository") or metadata.get("source") or "repository"),
            str(metadata.get("provider") or "unknown"),
            str(metadata.get("model") or "unknown"),
            str(metadata.get("dimension") or "0"),
        ]
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    return f"repository_memory_{digest}"


def _point_id(chunk: dict[str, Any]) -> str:
    digest = str(chunk.get("digest") or "")
    if len(digest) >= 32:
        return str(uuid.UUID(hex=digest[:32]))
    fallback = json.dumps(chunk, ensure_ascii=False, sort_keys=True)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, fallback))


def _entry(chunk: dict[str, Any]) -> list[Any]:
    return [
        str(chunk.get("path") or ""),
        int(chunk.get("line_start") or 0),
        int(chunk.get("line_end") or 0),
        str(chunk.get("digest") or ""),
    ]


def _payload(metadata: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": str(metadata.get("source") or ""),
        "repository": str(metadata.get("repository") or ""),
        "path": str(chunk.get("path") or ""),
        "line_start": int(chunk.get("line_start") or 0),
        "line_end": int(chunk.get("line_end") or 0),
        "digest": str(chunk.get("digest") or ""),
    }


def _batches(values: list[int], size: int = 128) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema_version": 1, "collections": {}}
    if not isinstance(value, dict) or not isinstance(value.get("collections"), dict):
        return {"schema_version": 1, "collections": {}}
    return value


def _sync(
    client: QdrantClient,
    database: Path,
    metadata_path: Path,
    metadata: dict[str, Any],
    vectors: array.array,
) -> dict[str, Any]:
    started = time.perf_counter()
    collection = _collection_name(metadata)
    dimension = int(metadata["dimension"])
    chunks = metadata["chunks"]
    manifest_path = database / "repository-memory-manifest.json"
    manifest = _manifest(manifest_path)
    old_collection = manifest["collections"].get(collection)
    old_entries = (
        old_collection.get("entries", {})
        if isinstance(old_collection, dict) and isinstance(old_collection.get("entries"), dict)
        else {}
    )

    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
        )
        old_entries = {}

    current_entries = {_point_id(chunk): _entry(chunk) for chunk in chunks}
    changed = [
        index
        for index, chunk in enumerate(chunks)
        if old_entries.get(_point_id(chunk)) != _entry(chunk)
    ]
    stale = sorted(set(old_entries) - set(current_entries))

    upsert_started = time.perf_counter()
    for indexes in _batches(changed):
        points = []
        for index in indexes:
            chunk = chunks[index]
            offset = index * dimension
            points.append(
                models.PointStruct(
                    id=_point_id(chunk),
                    vector=list(vectors[offset : offset + dimension]),
                    payload=_payload(metadata, chunk),
                )
            )
        client.upsert(collection_name=collection, points=points, wait=True)
    upsert_seconds = time.perf_counter() - upsert_started

    delete_started = time.perf_counter()
    for point_ids in _batches(stale, size=256):
        client.delete(collection_name=collection, points_selector=point_ids, wait=True)
    delete_seconds = time.perf_counter() - delete_started

    count = int(client.count(collection_name=collection, exact=True).count)
    if count != len(chunks):
        raise RuntimeError(f"Qdrant count mismatch: expected {len(chunks)}, got {count}")

    manifest["collections"][collection] = {
        "repository": metadata.get("repository"),
        "source": metadata.get("source"),
        "commit": metadata.get("commit"),
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "dimension": dimension,
        "metadata_path": str(metadata_path),
        "entries": current_entries,
        "updated_at_epoch": time.time(),
    }
    _atomic_json(manifest_path, manifest)
    return {
        "collection": collection,
        "points": count,
        "upserted": len(changed),
        "deleted": len(stale),
        "upsert_seconds": round(upsert_seconds, 6),
        "delete_seconds": round(delete_seconds, 6),
        "total_seconds": round(time.perf_counter() - started, 6),
        "no_op": not changed and not stale,
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _latency(values: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(values) * 1000, 3),
        "p95_ms": round(_percentile(values, 0.95) * 1000, 3),
        "min_ms": round(min(values) * 1000, 3),
        "max_ms": round(max(values) * 1000, 3),
    }


def _top_indices(scores: np.ndarray, limit: int) -> list[int]:
    size = min(limit, int(scores.shape[0]))
    if not size:
        return []
    candidates = np.argpartition(scores, -size)[-size:]
    return [int(index) for index in candidates[np.argsort(scores[candidates])[::-1]]]


def _python_scan(query: list[float], vectors: array.array, dimension: int) -> list[float]:
    result: list[float] = []
    for start in range(0, len(vectors), dimension):
        result.append(sum(left * right for left, right in zip(query, vectors[start : start + dimension])))
    return result


def _qdrant_query(
    client: QdrantClient,
    collection: str,
    query: list[float] | np.ndarray,
    limit: int,
) -> list[Any]:
    response = client.query_points(
        collection_name=collection,
        query=query,
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return list(response.points)


def _encode_queries(queries: list[str], dimension: int) -> tuple[list[list[float]], float]:
    scripts = Path(__file__).resolve().parents[2] / "skills" / "repository-memory" / "scripts"
    sys.path.insert(0, str(scripts))
    from local_embedding import vectorize_many  # type: ignore

    started = time.perf_counter()
    vectors = vectorize_many(queries, allow_download=False)
    elapsed = time.perf_counter() - started
    if len(vectors) != len(queries) or any(len(vector) != dimension for vector in vectors):
        widths = [len(vector) for vector in vectors]
        raise RuntimeError(f"query encoder does not match index dimension {dimension}: {widths}")
    return vectors, elapsed


def _benchmark(
    client: QdrantClient,
    collection: str,
    metadata: dict[str, Any],
    vectors: array.array,
    queries: list[str],
    iterations: int,
) -> dict[str, Any]:
    dimension = int(metadata["dimension"])
    chunks = metadata["chunks"]
    query_vectors, embedding_seconds = _encode_queries(queries, dimension)
    matrix = np.frombuffer(vectors, dtype=np.float32).reshape(len(chunks), dimension)
    rows: list[dict[str, Any]] = []

    for query, query_vector in zip(queries, query_vectors):
        numpy_query = np.asarray(query_vector, dtype=np.float32)
        exact_scores = matrix @ numpy_query
        exact_indices = _top_indices(exact_scores, 10)
        exact_ids = [_point_id(chunks[index]) for index in exact_indices]

        qdrant_points = _qdrant_query(client, collection, numpy_query, 10)
        qdrant_ids = [str(point.id) for point in qdrant_points]
        qdrant_paths = [str((point.payload or {}).get("path") or "") for point in qdrant_points]
        exact_paths = [str(chunks[index].get("path") or "") for index in exact_indices]

        qdrant_times: list[float] = []
        numpy_times: list[float] = []
        python_times: list[float] = []
        for _ in range(iterations):
            started = time.perf_counter()
            _qdrant_query(client, collection, numpy_query, 10)
            qdrant_times.append(time.perf_counter() - started)

            started = time.perf_counter()
            matrix @ numpy_query
            numpy_times.append(time.perf_counter() - started)

            started = time.perf_counter()
            _python_scan(query_vector, vectors, dimension)
            python_times.append(time.perf_counter() - started)

        score_delta = 0.0
        if qdrant_points and exact_indices:
            score_delta = abs(float(qdrant_points[0].score) - float(exact_scores[exact_indices[0]]))
        rows.append(
            {
                "query": query,
                "top1_exact_match": bool(qdrant_ids and exact_ids and qdrant_ids[0] == exact_ids[0]),
                "top5_chunk_overlap": round(len(set(qdrant_ids[:5]) & set(exact_ids[:5])) / 5, 3),
                "top5_document_overlap": round(len(set(qdrant_paths[:5]) & set(exact_paths[:5])) / max(1, len(set(exact_paths[:5]))), 3),
                "top1_score_delta": round(score_delta, 8),
                "qdrant_top3": [
                    {
                        "path": str((point.payload or {}).get("path") or ""),
                        "line_start": int((point.payload or {}).get("line_start") or 0),
                        "line_end": int((point.payload or {}).get("line_end") or 0),
                        "score": round(float(point.score), 6),
                    }
                    for point in qdrant_points[:3]
                ],
                "latency": {
                    "qdrant_local_exact": _latency(qdrant_times),
                    "numpy_exact": _latency(numpy_times),
                    "current_python_exact": _latency(python_times),
                },
            }
        )

    return {
        "query_count": len(queries),
        "iterations_per_query": iterations,
        "embedding_batch_seconds": round(embedding_seconds, 6),
        "embedding_average_ms": round(embedding_seconds * 1000 / max(1, len(queries)), 3),
        "top1_parity": round(sum(row["top1_exact_match"] for row in rows) / max(1, len(rows)), 3),
        "mean_top5_chunk_overlap": round(statistics.mean(row["top5_chunk_overlap"] for row in rows), 3),
        "rows": rows,
    }


def _incremental_probe(client: QdrantClient, dimension: int, vector: list[float]) -> dict[str, Any]:
    collection = "repository_memory_incremental_probe"
    if client.collection_exists(collection):
        client.delete_collection(collection)
    client.create_collection(
        collection_name=collection,
        vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
    )
    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "repository-memory-incremental-probe"))
    timings: dict[str, float] = {}
    try:
        started = time.perf_counter()
        client.upsert(
            collection_name=collection,
            points=[models.PointStruct(id=point_id, vector=vector, payload={"version": 1})],
            wait=True,
        )
        timings["insert_ms"] = round((time.perf_counter() - started) * 1000, 3)

        started = time.perf_counter()
        client.upsert(
            collection_name=collection,
            points=[models.PointStruct(id=point_id, vector=vector, payload={"version": 2})],
            wait=True,
        )
        timings["update_ms"] = round((time.perf_counter() - started) * 1000, 3)

        returned = _qdrant_query(client, collection, vector, 1)
        updated = bool(returned and (returned[0].payload or {}).get("version") == 2)

        started = time.perf_counter()
        client.delete(collection_name=collection, points_selector=[point_id], wait=True)
        timings["delete_ms"] = round((time.perf_counter() - started) * 1000, 3)
        remaining = int(client.count(collection_name=collection, exact=True).count)
        return {**timings, "updated_payload_visible": updated, "remaining_after_delete": remaining, "ok": updated and remaining == 0}
    finally:
        client.delete_collection(collection)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, help="semantic metadata JSON; defaults to newest usable cache")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--query", action="append", dest="queries", help="repeat to replace the default query set")
    parser.add_argument("--output", type=Path, help="report path; defaults beside the database")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if args.iterations < 1:
        raise SystemExit("--iterations must be at least 1")
    metadata_path = (args.metadata or _discover_metadata()).expanduser().resolve()
    database = args.database.expanduser().resolve()
    database.mkdir(parents=True, exist_ok=True)
    metadata, vectors = _load_cache(metadata_path)
    before_bytes = _directory_size(database)
    client = QdrantClient(path=str(database))
    try:
        sync = _sync(client, database, metadata_path, metadata, vectors)
        benchmark = _benchmark(
            client,
            sync["collection"],
            metadata,
            vectors,
            list(args.queries or DEFAULT_QUERIES),
            args.iterations,
        )
        dimension = int(metadata["dimension"])
        first_vector = list(vectors[:dimension])
        incremental = _incremental_probe(client, dimension, first_vector)
    finally:
        client.close()

    after_bytes = _directory_size(database)
    report = {
        "schema_version": 1,
        "prototype": True,
        "backend": "qdrant-client-local",
        "metadata_path": str(metadata_path),
        "database_path": str(database),
        "repository": metadata.get("repository"),
        "source": metadata.get("source"),
        "commit": metadata.get("commit"),
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "dimension": metadata.get("dimension"),
        "documents": metadata.get("document_count"),
        "chunks": metadata.get("chunk_count"),
        "source_vector_bytes": len(vectors) * vectors.itemsize,
        "database_bytes_before": before_bytes,
        "database_bytes_after": after_bytes,
        "sync": sync,
        "incremental_probe": incremental,
        "benchmark": benchmark,
        "limitations": [
            "embedded local mode is single-process prototype infrastructure, not a concurrent Qdrant server",
            "vector similarity ranks evidence but does not decide answerability",
            "quality parity checks storage/search against the same embeddings; end-to-end answer quality remains governed by Repository Memory retrieval rules",
        ],
    }
    output = args.output
    if output is None:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output = database.parent / f"benchmark-{timestamp}.json"
    output = output.expanduser().resolve()
    _atomic_json(output, report)
    summary = {
        "report": str(output),
        "database": str(database),
        "collection": sync["collection"],
        "points": sync["points"],
        "upserted": sync["upserted"],
        "deleted": sync["deleted"],
        "sync_seconds": sync["total_seconds"],
        "database_bytes": after_bytes,
        "top1_parity": benchmark["top1_parity"],
        "mean_top5_chunk_overlap": benchmark["mean_top5_chunk_overlap"],
        "incremental_ok": incremental["ok"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
