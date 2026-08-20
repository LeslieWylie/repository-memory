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
Two optional neural providers can be enabled through user configuration: a
local Hugging Face encoder, and a remote OpenAI-compatible ``/embeddings``
endpoint.  The dependency-free projection remains the safe fallback whenever an
optional provider is unavailable, offline, or misconfigured.

Why a remote endpoint is offered at all, given that this package is otherwise
zero-service: a local sentence-transformers encoder costs roughly 1.5-2 GB of
resident memory once torch and a multilingual model are loaded, which is not
affordable on a small machine.  The gateway provider costs no resident memory
and no disk, at the price of a network call during *index build only* --
vectors are persisted by ``semantic_repository``, so search itself stays local.

What neither provider may do is decide whether an answer exists.  Measured on
the live 2221-document corpus, every gateway model scored the deliberately
fictional query ``ZZZQWE \u865a\u6784\u9879\u76ee\u6700\u8fd1\u8fdb\u5c55`` *above* both real questions
(``text-embedding-3-small`` -0.198, ``Doubao-embedding`` -0.023,
``text-embedding-v3`` -0.021 real-minus-fictional margin), while the two
Chinese-native models still ranked the correct document first for both real
questions.  Cosine similarity is therefore usable for ranking and useless for
abstention.  This module only produces vectors; ``fallback.py`` keeps the
guarantee that a document with no lexical term support never becomes
answerable, and that guarantee is what makes a neural provider safe to enable.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import re
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from typing import Any

EMBEDDING_PROVIDER = "builtin"
EMBEDDING_MODEL = "builtin-char-ngram-v1"
EMBEDDING_DIMENSION = 384
HF_PROVIDER = "huggingface"
HF_DEFAULT_MODEL = "Alibaba-NLP/gte-multilingual-base"
HF_MAX_SEQUENCE_LENGTH = 512
HF_ALIASES = {"huggingface", "hf", "gte", "gte-multilingual"}

GATEWAY_PROVIDER = "gateway"
GATEWAY_ALIASES = {"gateway", "openai", "openai-compatible", "endpoint", "remote"}
# ``text-embedding-v3`` is the default because it was the only model measured to
# rank the correct document first for both hard Chinese questions *and* accept a
# reduced output width.  512 dimensions is a Matryoshka truncation the endpoint
# performs itself; it quarters vector storage against a 2048-wide default with
# no measured ranking loss on this corpus.
GATEWAY_DEFAULT_MODEL = "text-embedding-v3"
GATEWAY_DEFAULT_DIMENSIONS = 512
# The default model rejects batches larger than ten with HTTP 400.  A wrong
# batch size is a hard failure rather than a slow path, so the conservative
# value is the default and larger ones are opt-in per endpoint.
GATEWAY_DEFAULT_BATCH = 10
GATEWAY_DEFAULT_TIMEOUT = 30.0
# Embedding endpoints reject an over-long input outright rather than truncating
# it.  Chinese text is close to one token per character on most tokenizers, so
# this stays well inside a typical 8192-token window without needing to know
# which tokenizer the endpoint uses.
GATEWAY_MAX_CHARS = 6000
GATEWAY_PROBE_TTL_OK = 900.0
GATEWAY_PROBE_TTL_ERROR = 60.0
GATEWAY_PROBE_TTL_MAX = 3600.0
# A probe encodes one short string.  An endpoint that cannot answer that within
# a few seconds is not usable for indexing either, and the probe runs on the
# search path -- so it never inherits the much longer batch timeout.
GATEWAY_PROBE_TIMEOUT = 5.0

_WORD_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}|[\u3400-\u9fff]+", re.UNICODE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")

_HF_ENCODER: Any | None = None
_HF_DIMENSION: int | None = None
_HF_ERROR: str | None = None
_HF_ERROR_KEY: tuple[bool, str, bool] | None = None

_GATEWAY_PROBE: dict[str, Any] | None = None
_GATEWAY_PROBE_KEY: tuple[str, str, int | None] | None = None


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


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _configured_provider(config: dict[str, Any]) -> str:
    return str(
        os.environ.get("REPOSITORY_MEMORY_SEMANTIC_PROVIDER") or config.get("provider") or EMBEDDING_PROVIDER
    ).strip().casefold()


def _semantic_enabled(config: dict[str, Any]) -> bool:
    override = os.environ.get("REPOSITORY_MEMORY_SEMANTIC_ENABLED")
    return bool(config.get("enabled", False)) if override is None else _truthy(override)


def _hf_config() -> tuple[bool, str, bool]:
    config = _semantic_config()
    model = str(os.environ.get("REPOSITORY_MEMORY_SEMANTIC_MODEL") or config.get("model") or HF_DEFAULT_MODEL).strip()
    enabled = _semantic_enabled(config) and _configured_provider(config) in HF_ALIASES
    allow_download = bool(config.get("allow_download", False))
    return enabled, model, allow_download


def _positive_int(raw: Any, default: int, *, maximum: int | None = None) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum) if maximum else value


def _gateway_config() -> dict[str, Any]:
    """Resolve remote-endpoint settings without ever returning the API key.

    The key is deliberately absent from this dict.  Every other field here is
    safe to log, and ``embedding_status`` returns a subset of it verbatim; by
    keeping the secret out of the structure entirely there is no path by which
    a future diagnostic can print it by accident.
    """

    config = _semantic_config()
    endpoint = str(
        os.environ.get("REPOSITORY_MEMORY_SEMANTIC_ENDPOINT") or config.get("endpoint") or ""
    ).strip().rstrip("/")
    model = str(
        os.environ.get("REPOSITORY_MEMORY_SEMANTIC_MODEL") or config.get("model") or GATEWAY_DEFAULT_MODEL
    ).strip()
    if model in {EMBEDDING_MODEL, HF_DEFAULT_MODEL}:
        # A model name left behind by a previous provider is not a valid remote
        # model; switching providers without switching models must not send a
        # local model name to an endpoint that has never heard of it.
        model = GATEWAY_DEFAULT_MODEL
    dimensions = _positive_int(
        os.environ.get("REPOSITORY_MEMORY_SEMANTIC_DIMENSIONS") or config.get("dimensions"),
        GATEWAY_DEFAULT_DIMENSIONS,
    )
    batch = _positive_int(
        os.environ.get("REPOSITORY_MEMORY_SEMANTIC_BATCH") or config.get("batch_size"),
        GATEWAY_DEFAULT_BATCH,
        maximum=256,
    )
    try:
        timeout = float(os.environ.get("REPOSITORY_MEMORY_SEMANTIC_TIMEOUT") or config.get("timeout_seconds") or GATEWAY_DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = GATEWAY_DEFAULT_TIMEOUT
    selected = _semantic_enabled(config) and _configured_provider(config) in GATEWAY_ALIASES
    return {
        "enabled": bool(selected and endpoint),
        "selected": bool(selected),
        "endpoint": endpoint,
        "model": model,
        "dimensions": dimensions,
        "batch_size": batch,
        "timeout": max(1.0, min(timeout, 300.0)),
    }


def _json_pointer(payload: Any, path: str) -> Any:
    """Walk a dot-separated key path into already-parsed JSON."""

    current = payload
    for key in path.split("."):
        key = key.strip()
        if not key or not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _credential_from_file(raw_path: str, json_path: str) -> str:
    """Read a credential out of a file some other tool already manages.

    The motivating case is an agent host launched from a GUI, which inherits no
    shell environment: an environment variable is unreachable there, so a
    credential-by-name scheme silently degrades to no credential at all, and the
    remote provider quietly stops being used at exactly the moment it was asked
    for.  Pointing at the file that already holds the secret keeps it out of
    *our* configuration without requiring us to hold it at all.

    ``json_path`` is a dot path for the common case where that file is JSON
    belonging to another program.  Without it the whole file is the credential,
    which is the other common shape.  Every failure is silent and returns the
    empty string: an unreadable credential is not an error worth crashing a
    search over, because the local projection still answers the query.
    """

    try:
        path = os.path.expanduser(str(raw_path).strip())
        if not path:
            return ""
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, UnicodeDecodeError):
        return ""
    if not json_path:
        return content.strip()
    try:
        value = _json_pointer(json.loads(content), str(json_path))
    except (ValueError, TypeError):
        return ""
    return str(value).strip() if isinstance(value, str) else ""


def _gateway_api_key() -> str:
    """Resolve the endpoint credential from the least persistent source first.

    Preferring an environment variable, then a *named* variable from config,
    then a file this configuration only points at, keeps the secret out of
    ``config.json``; a literal ``api_key`` is accepted last because some
    endpoints are reached from contexts with no environment control.  The value
    is never returned to a caller other than the request builder.
    """

    direct = os.environ.get("REPOSITORY_MEMORY_SEMANTIC_API_KEY")
    if direct and direct.strip():
        return direct.strip()
    config = _semantic_config()
    name = str(config.get("api_key_env") or "").strip()
    if name:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    source = str(config.get("api_key_file") or "").strip()
    if source:
        value = _credential_from_file(source, str(config.get("api_key_json_path") or "").strip())
        if value:
            return value
    return str(config.get("api_key") or "").strip()



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
    allow_download = bool(allow_download) or os.environ.get("REPOSITORY_MEMORY_SEMANTIC_ALLOW_DOWNLOAD", "").strip().casefold() in {"1", "true", "yes", "on"}
    cache_key = (enabled, model, allow_download)
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


def _normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values] if norm else values


def _probe_cache_key(config: dict[str, Any]) -> str:
    raw = f"{config['endpoint']}\n{config['model']}\n{config['dimensions']}"
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=8).hexdigest()


def _probe_cache_path() -> Any | None:
    try:
        from discovery import cache_root

        return cache_root() / "semantic-gateway-probe.json"
    except (ImportError, OSError, TypeError, ValueError):
        return None


def _read_probe_raw(key: str) -> dict[str, Any] | None:
    path = _probe_cache_path()
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("key") == key else None


def _read_probe_cache(key: str) -> dict[str, Any] | None:
    """Return a still-valid cached probe result, or ``None``.

    The CLI is spawned once per request, so an in-process cache alone would
    make every search pay a network round trip.  A short-lived file under the
    cache root is what keeps the remote provider off the hot path.  A failure
    expires far sooner than a success so a recovered endpoint is picked up
    quickly, but repeated failures back off exponentially: without that, a
    permanently dead endpoint would charge every search a fresh timeout once
    per minute, forever.
    """

    value = _read_probe_raw(key)
    if value is None:
        return None
    try:
        age = time.time() - float(value.get("checked_at") or 0.0)
    except (TypeError, ValueError):
        return None
    if value.get("ok"):
        ttl = GATEWAY_PROBE_TTL_OK
    else:
        failures = _positive_int(value.get("failures"), 1)
        ttl = min(GATEWAY_PROBE_TTL_ERROR * (2 ** min(failures - 1, 6)), GATEWAY_PROBE_TTL_MAX)
    return value if 0 <= age < ttl else None


def _record_failure(key: str, error: str) -> dict[str, Any]:
    previous = _read_probe_raw(key)
    failures = _positive_int(previous.get("failures"), 0) + 1 if previous and not previous.get("ok") else 1
    result = {"ok": False, "dimension": None, "error": error, "failures": failures}
    _write_probe_cache(key, result)
    return result


def _write_probe_cache(key: str, value: dict[str, Any]) -> None:
    global _GATEWAY_PROBE, _GATEWAY_PROBE_KEY
    payload = {**value, "key": key, "checked_at": time.time()}
    _GATEWAY_PROBE = payload
    _GATEWAY_PROBE_KEY = key
    path = _probe_cache_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return


def _gateway_request(texts: list[str], config: dict[str, Any]) -> list[list[float]]:
    """Post one batch to an OpenAI-compatible ``/embeddings`` endpoint."""

    body: dict[str, Any] = {"model": config["model"], "input": texts}
    if config["dimensions"]:
        body["dimensions"] = int(config["dimensions"])
    request = urllib.request.Request(
        f"{config['endpoint']}/embeddings",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    key = _gateway_api_key()
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise ValueError(f"endpoint returned {len(rows) if isinstance(rows, list) else 0} vectors for {len(texts)} inputs")
    # Some gateways reorder a batch and rely on the caller honouring ``index``.
    ordered = sorted(rows, key=lambda row: int(row.get("index", 0)) if isinstance(row, dict) else 0)
    vectors: list[list[float]] = []
    for row in ordered:
        values = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(values, list) or not values:
            raise ValueError("endpoint returned an empty embedding")
        vectors.append(_normalize([float(value) for value in values]))
    width = len(vectors[0])
    if any(len(vector) != width for vector in vectors):
        raise ValueError("endpoint returned vectors of mixed width")
    return vectors


def _scrub(exc: BaseException) -> str:
    """Describe a failure without letting a credential into a diagnostic."""

    text = f"{type(exc).__name__}: {exc}"
    key = _gateway_api_key()
    if key:
        text = text.replace(key, "***")
    return text[:300]


def _gateway_probe(config: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Check the endpoint at most once per TTL and learn its real width.

    The configured ``dimensions`` is a request, not a guarantee: an endpoint
    that ignores it would return a different width than we advertise, and
    ``semantic_repository`` would then rebuild the whole index on every search
    because the cached metadata never matches the reported dimension.  Reading
    the width from an actual response is what makes the cache converge.
    """

    global _GATEWAY_PROBE, _GATEWAY_PROBE_KEY
    key = _probe_cache_key(config)
    if not force:
        if _GATEWAY_PROBE_KEY == key and _GATEWAY_PROBE is not None:
            return _GATEWAY_PROBE
        cached = _read_probe_cache(key)
        if cached is not None:
            _GATEWAY_PROBE, _GATEWAY_PROBE_KEY = cached, key
            return cached
    try:
        vectors = _gateway_request(["repository-memory"], {**config, "timeout": min(config["timeout"], GATEWAY_PROBE_TIMEOUT)})
        result = {"ok": True, "dimension": len(vectors[0]), "error": None}
        _write_probe_cache(key, result)
    except Exception as exc:  # optional provider must never break lexical fallback
        result = _record_failure(key, _scrub(exc))
    return result


def _gateway_encode(values: list[str], config: dict[str, Any]) -> tuple[array.array, int] | None:
    """Encode a whole corpus remotely, or return ``None`` and leave no trace.

    All-or-nothing is deliberate.  A partial result would mix widths inside one
    index, and a silently half-remote index cannot be described truthfully by
    the single provider/model/dimension triple the cache metadata carries.
    """

    if not values:
        return array.array("f"), 0
    size = max(1, int(config["batch_size"]))
    buffer = array.array("f")
    width = 0
    for start in range(0, len(values), size):
        batch = [text[:GATEWAY_MAX_CHARS] for text in values[start:start + size]]
        try:
            vectors = _gateway_request(batch, config)
        except Exception as exc:
            # Record the failure so the next call reports the provider as
            # unavailable instead of rebuilding the index against a provider
            # that cannot answer.
            _record_failure(_probe_cache_key(config), _scrub(exc))
            return None
        width = width or len(vectors[0])
        if len(vectors[0]) != width:
            _record_failure(_probe_cache_key(config), "endpoint returned vectors of mixed width")
            return None
        for vector in vectors:
            buffer.extend(vector)
    _write_probe_cache(_probe_cache_key(config), {"ok": True, "dimension": width, "error": None})
    return buffer, width


def _gateway_status(config: dict[str, Any], *, probe: bool) -> dict[str, Any]:
    """Describe the remote provider without asserting more than was verified."""

    base = {
        "configured": True,
        "configured_by": "explicit",
        "provider": GATEWAY_PROVIDER,
        "model": config["model"],
        "endpoint": config["endpoint"],
        "requested_dimensions": config["dimensions"],
        "batch_size": config["batch_size"],
        "native_neural_model": True,
        # Presence only.  The credential itself is never part of this dict.
        "api_key_present": bool(_gateway_api_key()),
    }
    if not config["enabled"]:
        return {
            **base,
            "available": False,
            "dimension": None,
            "strategy": "lexical-fallback",
            "fallback": True,
            "error": "semantic endpoint is not configured; set an endpoint URL before enabling the gateway provider",
        }
    if not probe:
        # An unverified endpoint is not a working provider.  Reporting it as
        # available here would let a doctor summary imply usable semantic
        # recall from configuration alone.
        return {**base, "available": False, "dimension": None, "strategy": "lexical-fallback", "fallback": True, "verified": False}
    result = _gateway_probe(config)
    if result.get("ok"):
        return {
            **base,
            "available": True,
            "dimension": int(result["dimension"]),
            "strategy": "local-hybrid",
            "fallback": False,
            "verified": True,
        }
    return {
        **base,
        "available": False,
        "dimension": None,
        "strategy": "lexical-fallback",
        "fallback": True,
        "verified": False,
        "error": result.get("error") or "semantic endpoint is unreachable",
    }


def embedding_status(*, probe: bool = True, allow_download: bool = False) -> dict[str, Any]:
    """Return configured and effective provider state for doctor and sync."""

    gateway = _gateway_config()
    if gateway["selected"]:
        return _gateway_status(gateway, probe=probe)
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


def _builtin_spec(*, configured: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = {
        "configured": bool(configured.get("configured")) if configured else True,
        "available": True,
        "provider": EMBEDDING_PROVIDER,
        "model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIMENSION,
        "native_neural_model": False,
        "strategy": "local-hybrid",
        "fallback": bool(configured.get("configured")) if configured else False,
    }
    if configured:
        spec.update(
            {
                "configured_provider": configured.get("provider"),
                "configured_model": configured.get("model"),
                "error": configured.get("error"),
            }
        )
    return spec


def active_embedding_spec() -> dict[str, Any]:
    """Return the provider actually used for new vectors."""

    status = embedding_status(probe=True)
    if status.get("available") is True:
        return status
    return _builtin_spec(configured=status)


def _gateway_recent_failure(config: dict[str, Any]) -> bool:
    """Answer from cache only, so a dead endpoint costs no per-query timeout."""

    key = _probe_cache_key(config)
    if _GATEWAY_PROBE_KEY == key and _GATEWAY_PROBE is not None:
        return not _GATEWAY_PROBE.get("ok")
    cached = _read_probe_cache(key)
    return cached is not None and not cached.get("ok")


def _builtin_vector(text: str, dimension: int | None = None) -> list[float]:
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
    return _normalize(vector)


def vectorize(text: str, dimension: int | None = None) -> list[float]:
    """Encode one text with the configured provider, else the local projection.

    A query vector has to come from the same encoder as the document vectors it
    will be compared against.  When it does not, ``cosine`` sees two different
    widths and returns 0.0, so a provider that degrades mid-session loses the
    semantic lane rather than producing meaningless similarities.
    """

    gateway = _gateway_config()
    if gateway["enabled"] and not _gateway_recent_failure(gateway):
        encoded = _gateway_encode([str(text or "")], gateway)
        if encoded is not None and encoded[1]:
            return list(encoded[0])
    enabled, _model, _allow_download = _hf_config()
    if enabled:
        encoder = _load_hf_encoder(allow_download=False)
        if encoder is not None:
            values = encoder.encode([str(text or "")[:12000]], normalize_embeddings=True, show_progress_bar=False)[0]
            return [float(value) for value in values]
    return _builtin_vector(text, dimension)


def encode_document_vectors(texts: Iterable[str], *, allow_download: bool = False) -> tuple[array.array, int, dict[str, Any]]:
    """Encode a corpus into one flat float buffer and report the real provider.

    The buffer is an ``array('f')`` rather than a list of lists because the
    difference is not cosmetic at corpus scale: 37k documents at 512 dimensions
    is 75 MB as packed floats and roughly 600 MB as Python float objects, and
    the second number does not fit on a small machine.  ``semantic_repository``
    writes this buffer straight to disk, and its reader already loads it back
    the same way.

    The returned spec describes the vectors that were actually produced, not
    the ones that were requested.  An optional provider can fail after the
    readiness check and leave the corpus to the local projection; recording the
    configured triple then would claim a cache we do not hold, and the mismatch
    would rebuild the whole index on every later search.
    """

    # Bound input size before tokenization as an additional memory guard.
    values = [str(text or "")[:12000] for text in texts]
    gateway = _gateway_config()
    if gateway["enabled"] and not _gateway_recent_failure(gateway):
        encoded = _gateway_encode(values, gateway)
        if encoded is not None and (not values or len(encoded[0]) == len(values) * encoded[1]):
            buffer, width = encoded
            spec = _gateway_status(gateway, probe=True)
            if width:
                spec = {**spec, "available": True, "dimension": width}
            return buffer, width, spec
    enabled, model, _configured_download = _hf_config()
    if enabled:
        encoder = _load_hf_encoder(allow_download=allow_download)
        if encoder is not None:
            buffer = array.array("f")
            width = 0
            for row in encoder.encode(values, batch_size=16, normalize_embeddings=True, show_progress_bar=False):
                width = width or len(row)
                buffer.extend(float(value) for value in row)
            return buffer, width, {
                "configured": True,
                "available": True,
                "provider": HF_PROVIDER,
                "model": model,
                "dimension": width or _HF_DIMENSION,
                "native_neural_model": True,
                "strategy": "local-hybrid",
                "fallback": False,
            }
    buffer = array.array("f")
    for value in values:
        buffer.extend(_builtin_vector(value))
    return buffer, EMBEDDING_DIMENSION, _builtin_spec(configured=embedding_status(probe=False))


def encode_documents(texts: Iterable[str], *, allow_download: bool = False) -> tuple[list[list[float]], dict[str, Any]]:
    """Row-oriented view of :func:`encode_document_vectors`."""

    buffer, width, spec = encode_document_vectors(texts, allow_download=allow_download)
    if not width:
        return [], spec
    rows = [list(buffer[start:start + width]) for start in range(0, len(buffer), width)]
    return rows, spec


def vectorize_many(texts: Iterable[str], *, allow_download: bool = False) -> list[list[float]]:
    """Encode a batch efficiently, using the configured provider when ready."""

    return encode_documents(texts, allow_download=allow_download)[0]


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
