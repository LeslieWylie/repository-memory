#!/usr/bin/env python3
"""Conservative local exact-evidence fallback when an adapter is unavailable."""

from __future__ import annotations

import math
import re
import sqlite3
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

from citation import locate
from local_embedding import cosine, vectorize

from models import SourceView

EXCLUDED = {".git", ".remember", ".cache", ".venv", "venv", "__pycache__", "output", "tmp", "node_modules", "build", "dist"}
OPERATIONAL_DIRS = {"skills", "scripts", "tests", "test", "eval", "evals", "fixtures", "logs", "templates"}
EXCLUDED_FILENAMES = {"template.md", "template.yaml", "template.yml"}
EXTENSIONS = {".md", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".json"}
SECRET_NAME = re.compile(r"(^|/)(\.env(?:\.|$)|.*\.(?:pem|key|p12|pfx|secret|secrets?))$", re.IGNORECASE)
SECRET_CONTENT = re.compile(r"-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/.+=]{16,}|\bsk-[A-Za-z0-9_-]{16,}", re.IGNORECASE)
DATE_RE = re.compile(r"20\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?|20\d{2}-W\d{1,2}", re.IGNORECASE)
GENERIC_SUPPORT_TERMS = {"note", "report", "weekly", "paper", "model", "card", "update", "result"}
QUERY_BOUNDARY = re.compile(r"最近|最新|上次|之前|以前|历史|目前|当前|本周|本月|今天|昨天|正在|在做|在干|做什么|干什么|干啥|干嘛|进展|情况")
QUERY_STOP_TERMS = {
    "最近", "最新", "上次", "之前", "以前", "历史", "目前", "当前", "本周", "本月", "今天", "昨天", "正在", "在做", "在干",
    "做什么", "干什么", "干啥", "干嘛", "啥", "嘛", "在", "进展", "情况", "什么", "哪些", "如何", "吗", "呢",
}
GENERIC_LAYER_ALIASES = {
    "paper": {"paper", "papers", "publication", "publications", "research"},
    "model": {"model", "models", "system", "systems"},
    "person": {"person", "people", "author", "authors", "researcher", "researchers", "team"},
    "benchmark": {"benchmark", "benchmarks", "evaluation", "evaluations", "eval", "test", "tests"},
    "note": {"note", "notes", "memo", "memos", "record", "records"},
    "report": {"report", "reports", "summary", "summaries", "finding", "findings", "conclusion", "conclusions", "status"},
    "survey": {"survey", "surveys", "overview", "overviews", "guide", "guides", "section", "sections"},
}


def _term_forms(term: str) -> list[str]:
    """Return conservative morphology variants for lexical matching.

    This is intentionally language/provider agnostic.  It is not a synonym
    model: it only handles common inflectional forms so that ``rewards`` and
    ``reward`` or ``training`` and ``train`` do not become unrelated tokens.
    """

    value = term.casefold()
    forms = [value]
    if len(value) > 4 and value.endswith("ies"):
        forms.append(value[:-3] + "y")
    if len(value) > 4 and value.endswith("s") and not value.endswith("ss"):
        forms.append(value[:-1])
    if len(value) > 5 and value.endswith("ing"):
        stem = value[:-3]
        forms.append(stem)
        if stem.endswith("n"):
            forms.append(stem[:-1])
    if len(value) > 4 and value.endswith("ed"):
        forms.append(value[:-2])
    return list(dict.fromkeys(form for form in forms if len(form) >= 2))


def query_terms(query: str) -> list[str]:
    r"""Expand a query into conservative lexical terms.

    ``re`` treats a contiguous CJK sentence as one ``\w`` token.  That made
    a natural question such as ``李小明最近在干啥`` impossible to match against
    ``standup/李小明.md``.  We retain normal ASCII/path tokens and add short
    CJK fragments, while excluding temporal/question fragments.  This is a
    tokenizer, not a synonym model: it never invents domain vocabulary.
    """

    expanded: list[str] = []
    markers = sorted((*QUERY_STOP_TERMS, "的"), key=len, reverse=True)
    for term in re.findall(r"[\w./:-]{2,}", query, re.UNICODE):
        value = term.casefold()
        if not any("\u3400" <= char <= "\u9fff" for char in value):
            expanded.append(value)
            continue
        # Remove temporal/question scaffolding before making CJK fragments.
        # Otherwise ``最近的模型评审`` produces accidental terms such as
        # ``最近的`` and ``的模型`` which can outrank the dated report layer.
        candidate = value
        for marker in markers:
            candidate = candidate.replace(marker, "")
        candidate = candidate.strip()
        if not candidate:
            continue
        expanded.append(candidate)
        for width in (2, 3, 4):
            if len(candidate) < width:
                continue
            expanded.extend(candidate[index:index + width] for index in range(len(candidate) - width + 1))
    return list(dict.fromkeys(term for term in expanded if term and term not in QUERY_STOP_TERMS))


def _layer_matches(layer: str, query_terms: set[str]) -> bool:
    normalized = layer.casefold().replace("_", "-")
    aliases = {normalized, normalized.rstrip("s")}
    for values in GENERIC_LAYER_ALIASES.values():
        if normalized in values or normalized.rstrip("s") in values:
            return bool(query_terms & values)
    return bool(query_terms & aliases)


def _evidence_window(text: str, terms: list[str], max_lines: int = 12) -> tuple[str, int, int]:
    """Return a compact, multi-line window around the strongest evidence.

    Retrieval is document-level: the window is only a citation anchor.  It is
    intentionally allowed to contain a subset of a composite query so claim
    support can be reported separately by the caller.
    """

    file_lines = text.splitlines()
    if not file_lines:
        return "", 1, 1
    line_scores = [sum(line.casefold().count(term) for term in terms) for line in file_lines]
    matching = [index for index, score in enumerate(line_scores) if score]
    if not matching:
        start = 0
        end = min(len(file_lines), start + max_lines)
    else:
        best = max(matching, key=lambda index: (line_scores[index], -index))
        span_start, span_end = min(matching), max(matching)
        # A compact card or report section can put different query signals on
        # different lines.  Cite the whole local span when it is bounded,
        # instead of anchoring on one line and incorrectly labelling the
        # document ``partial`` merely because the excerpt was too narrow.
        # The expanded bound is still finite; widely separated matches remain
        # partial and require an explicit get/explain follow-up.
        if span_end - span_start + 1 <= max_lines * 2:
            start = max(0, span_start - max_lines // 4)
            end = min(len(file_lines), span_end + max_lines // 4 + 1)
        else:
            start = max(0, best - max_lines // 3)
            end = min(len(file_lines), start + max_lines)
            # Include nearby supporting lines when the document keeps a
            # compact card/section together, but never let a huge document
            # become a cite.
            nearby = [index for index in matching if start <= index < end]
            if nearby:
                start = max(0, min(start, min(nearby)))
                end = min(len(file_lines), start + max_lines)
    return "\n".join(file_lines[start:end]), start + 1, end


def _claim_support(query_terms: list[str], excerpt: str, line_start: int, line_end: int) -> dict[str, Any]:
    raw_support_terms = list(dict.fromkeys(
        term for term in query_terms
        if (len(term) >= 3 or (len(term) >= 2 and all("\u3400" <= char <= "\u9fff" for char in term)))
        and term not in GENERIC_SUPPORT_TERMS
    ))
    # CJK token expansion deliberately adds short n-grams for recall.  Those
    # n-grams are not independent claims: if a longer CJK term contains one,
    # keep only the longest form for claim support so a hit on "评测结果" is
    # not downgraded merely because "评测" and "结果" were also generated.
    support_terms = [
        term for term in raw_support_terms
        if not (
            all("\u3400" <= char <= "\u9fff" for char in term)
            and any(len(other) > len(term) and term in other for other in raw_support_terms)
        )
    ]
    excerpt_value = excerpt.casefold()
    matched = [term for term in support_terms if term in excerpt_value]
    unmatched = [term for term in support_terms if term not in excerpt_value]
    if not support_terms or len(matched) == len(support_terms):
        status = "direct"
    elif matched:
        status = "partial"
    else:
        status = "unknown"
    spans = []
    for offset, line in enumerate(excerpt.splitlines()):
        line_terms = [term for term in matched if term in line.casefold()]
        if line_terms:
            spans.append({"line_start": line_start + offset, "line_end": line_start + offset, "terms": line_terms})
    return {
        "matched_terms": matched,
        "unmatched_terms": unmatched,
        "coverage": round(len(matched) / len(support_terms), 4) if support_terms else 1.0,
        "claim_support": status,
        "supporting_spans": spans,
    }


def _preferred_layers(query: str, available_layers: set[str]) -> set[str]:
    """Infer source layers from the source's own directory names.

    No domain taxonomy is built into the generic runtime.  A source can still
    get useful layer routing when its directory names match query vocabulary,
    and temporal queries prefer generally named report/history layers when
    those layers actually exist.
    """

    value = query.casefold()
    raw_terms = set(query_terms(value))
    expanded_terms = {form for term in raw_terms for form in _term_forms(term)}
    layers: set[str] = set()
    for layer in available_layers:
        if _layer_matches(layer, expanded_terms):
            layers.add(layer)
    temporal_request = re.search(r"latest|recent|最新|最近|上次|之前|以前|历史|本周|本月|昨天|today|yesterday", value, re.IGNORECASE)
    if temporal_request and re.search(r"结论|finding|findings|evidence|summary|评估证据", value, re.IGNORECASE):
        report_layers = {
            layer for layer in available_layers
            if layer.casefold().rstrip("s") in {"report", "history", "archive", "summary"}
        }
        if report_layers:
            layers = report_layers
    if layers:
        return layers
    if re.search(r"how|what|why|which", value, re.IGNORECASE):
        explanatory_layers = {
            layer for layer in available_layers
            if layer.casefold().rstrip("s") in {"survey", "overview", "guide", "documentation", "docs"}
        }
        if explanatory_layers:
            return explanatory_layers
    topic_query = bool(re.search(r"learning|study|studies|method|methods|feedback|training|research|学习|研究|方法|反馈", value, re.IGNORECASE))
    if topic_query:
        paper_layers = {
            layer for layer in available_layers
            if layer.casefold().rstrip("s") in GENERIC_LAYER_ALIASES["paper"]
        }
        survey_layers = {
            layer for layer in available_layers
            if layer.casefold().rstrip("s") in GENERIC_LAYER_ALIASES["survey"]
        }
        if paper_layers and not layers:
            layers.update(paper_layers)
        if paper_layers and survey_layers and layers & paper_layers:
            layers.update(survey_layers)
    if re.search(r"latest|recent|最近|最新|上次|之前|以前|历史|本周|本月|昨天|today|yesterday", value, re.IGNORECASE):
        # ``recent`` alone is a topic-time request (for example "最近的模型
        # 评审"), not a personal standup request.  Only route to standup when
        # the query has an explicit activity/person cue.
        personal_request = bool(re.search(r"我|你|某人|谁|在做|在干|做什么|干什么|干啥|干嘛|what.*doing|doing|standup|日报", value, re.IGNORECASE))
        if personal_request and not re.search(r"report|weekly|monthly|周报|报告|结论|summary|finding", value, re.IGNORECASE):
            personal_layers = {
                layer for layer in available_layers
                if layer.casefold() in {"standup", "standups", "daily", "diary", "diaries", "journal", "journals"}
            }
            if personal_layers:
                return personal_layers
        temporal_groups = (
            {"report", "reports", "history", "archive", "archives"},
            {"weekly", "monthly", "daily"},
            {"notes", "note", "log", "logs"},
        )
        for group in temporal_groups:
            matches = {layer for layer in available_layers if layer.casefold() in group}
            if matches:
                return matches
    return layers


@lru_cache(maxsize=16)
def paths(root: Path, deep: bool = False) -> list[str]:
    result: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        relative = path.relative_to(root).as_posix()
        parts = Path(relative).parts
        if SECRET_NAME.search(relative) or any(part.startswith(".") for part in parts) or (not deep and (path.name.casefold() in EXCLUDED_FILENAMES or "template" in path.stem.casefold())):
            continue
        excluded = EXCLUDED | (OPERATIONAL_DIRS if not deep else set())
        if any(part in excluded for part in parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
            max_bytes = 8 * 1024 * 1024 if path.name == "index.yaml" else 512 * 1024
            if len(content.encode()) > max_bytes or "\x00" in content or SECRET_CONTENT.search(content):
                continue
        except (OSError, UnicodeDecodeError):
            continue
        result.append(relative)
    return sorted(result)


@lru_cache(maxsize=8192)
def _read_document(root: Path, relative: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size
    return (root / relative).read_text(encoding="utf-8")


def _fts_candidates(indexed: dict[str, Any] | None, terms: list[str], limit: int = 2048) -> set[str] | None:
    """Return a bounded lexical candidate set for large cached indexes."""

    if not isinstance(indexed, dict) or not indexed.get("fts_path"):
        return None
    usable = [term.replace('"', '""') for term in terms if len(term) >= 3]
    if not usable:
        return None
    expression = " OR ".join(f'"{term}"' for term in usable)
    try:
        connection = sqlite3.connect(f"file:{indexed['fts_path']}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT paths.path FROM documents JOIN paths ON paths.rowid = documents.rowid WHERE documents MATCH ? LIMIT ?",
                (expression, int(limit)),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return None
    return {str(row[0]) for row in rows if row and row[0]}


def search(source: SourceView, query: str, limit: int = 5, deep: bool = False) -> list[dict[str, Any]]:
    raw_terms = query_terms(query)
    has_compound_term = any("-" in term for term in raw_terms)
    # Treat hyphenated natural-language concepts as both a phrase and tokens.
    # Repository cards often write "long context" while users write
    # "long-context"; retaining both forms improves recall without adding a
    # model- or provider-specific synonym table.
    terms = list(dict.fromkeys(
        raw_terms
        + [part for term in raw_terms for part in re.split(r"[-/]", term) if len(part) >= 2]
        + [form for term in raw_terms for form in _term_forms(term)]
    ))
    terms = [term for term in terms if term not in {"what", "latest", "recent", "最近", "最新", "什么", "source", "evidence", "citation", "definition", "and", "or", "the", "a", "an", "relationship"}]
    date_terms = [term for term in terms if DATE_RE.search(term)]
    latest = bool(re.search(r"latest|recent|最近|最新|上次|之前|以前|历史|本周|本月", query, re.IGNORECASE)) or bool(date_terms)
    generic_temporal_terms = {"note", "report", "weekly", "paper", "model", "card", "update", "result"}
    specific_terms = [term for term in terms if term not in generic_temporal_terms]
    catalog_query = bool(
        re.search(
            r"\b(index|overview|catalog|taxonomy|list|directory|framework|pipeline|instruction following|relationship|what makes|paper citation|survey section|compare|comparison)\b|"
            r"\bpaper\s+(?:about|on|regarding)\b|索引|目录|分类|框架|哪些|关系|对比|比较",
            query,
            re.IGNORECASE,
        )
    )
    if not terms and not latest:
        return []
    generic_filename_terms = {"long", "context", "benchmark", "retrieval", "research", "result", "model", "paper", "survey", "section"}
    filename_terms = [term for term in terms if term not in generic_filename_terms]
    content_signal_terms = [term for term in terms if term not in generic_filename_terms | {"multi"} and len(term) >= 4]
    phrase_terms = [f"{left} {right}" for left, right in pairwise(raw_terms) if len(left) >= 3 and len(right) >= 3]
    hits: list[dict[str, Any]] = []
    indexed = source.metadata.get("local_index") if isinstance(source.metadata, dict) else None
    indexed_documents = indexed.get("documents") if isinstance(indexed, dict) else None
    document_metadata = {
        str(item.get("path")): item
        for item in indexed_documents or []
        if isinstance(item, dict) and item.get("path")
    }
    all_documents = (
        [(str(item.get("path")), str(item.get("text") or "")) for item in indexed_documents if isinstance(item, dict) and item.get("path")]
        if isinstance(indexed_documents, list)
        else [(relative, None) for relative in paths(source.path, deep)]
    )
    fts_paths = _fts_candidates(indexed, terms)
    documents = [(relative, text) for relative, text in all_documents if fts_paths is None or relative in fts_paths]
    # A semantic rewrite can have no lexical hit. Keep the full path/text map
    # only as a cheap in-process view of the already-loaded JSON cache, then
    # narrow it to semantic candidates after scoring below.
    all_document_by_path = dict(all_documents)
    # Keep the original semantic candidate behavior for small/non-FTS indexes:
    # without a lexical candidate set, do not discard documents solely
    # because the optional projection ranks them below its recall threshold.
    document_by_path = dict(all_documents) if fts_paths is not None else None
    loaded_documents: list[tuple[str, str]] = []
    for relative, indexed_text in documents:
        try:
            if indexed_text is not None:
                text = indexed_text
            else:
                stat = (source.path / relative).stat()
                text = _read_document(source.path, relative, stat.st_mtime_ns, stat.st_size)
        except (OSError, UnicodeDecodeError):
            continue
        loaded_documents.append((relative, text))
    documents = loaded_documents
    semantic_scores: dict[str, float] = {}
    semantic_candidates: set[str] = set()
    semantic_index = source.metadata.get("semantic_index") if isinstance(source.metadata, dict) else None
    if isinstance(semantic_index, dict) and semantic_index.get("available") is True:
        semantic_paths = semantic_index.get("paths") if isinstance(semantic_index.get("paths"), list) else []
        vector_store = semantic_index.get("vector_store")
        dimension = int(semantic_index.get("dimension") or 0)
        semantic_vectors = semantic_index.get("vectors") if isinstance(semantic_index.get("vectors"), list) else []
        query_vector = vectorize(query)
        if vector_store is not None and dimension == len(query_vector):
            # Read the compact flat float array without materializing a list
            # for every document.
            score_paths = semantic_paths
            if fts_paths:
                lexical_paths = set(fts_paths)
                score_paths = [relative for relative in semantic_paths if relative in lexical_paths]
            score_path_set = set(score_paths) if score_paths is not semantic_paths else None
            for offset, relative in enumerate(semantic_paths):
                if score_path_set is not None and relative not in score_path_set:
                    continue
                start = offset * dimension
                end = start + dimension
                if end > len(vector_store):
                    break
                score = sum(left * right for left, right in zip(query_vector, vector_store[start:end]))
                semantic_scores[str(relative)] = max(-1.0, min(1.0, score))
        else:
            # Backward-compatible reader for older derived caches.
            for relative, vector in zip(semantic_paths, semantic_vectors):
                if not isinstance(vector, list):
                    continue
                score = cosine(query_vector, vector)
                semantic_scores[str(relative)] = score
        ranked_semantic = sorted(semantic_scores.items(), key=lambda item: (-item[1], item[0]))
        # The semantic lane widens recall only inside this repository/source.
        # Lexical/path evidence still controls exact queries and citation
        # validation happens after ranking.
        semantic_candidates = {
            relative for relative, score in ranked_semantic[:64]
            if score >= 0.20
        }
        if document_by_path is not None:
            selected = set(fts_paths or set()) | semantic_candidates
            documents = [(relative, document_by_path[relative]) for relative in selected if relative in document_by_path]
        elif fts_paths is not None:
            documents = [(relative, text) for relative, text in documents if relative in semantic_candidates or relative in fts_paths]
    available_layers = {Path(relative).parts[0] for relative, _text in documents if Path(relative).parts}
    preferred_layers = _preferred_layers(query, available_layers)
    # Use corpus-local inverse document frequency as a generic specificity
    # signal.  Rare query concepts (for example a named method) should beat a
    # common word such as "training" without a hand-maintained domain list.
    document_frequency = {
        term: sum(term in f"{text.casefold()} {relative.casefold()}" for relative, text in documents)
        for term in terms
    }
    term_idf = {
        term: max(1.0, math.log((len(documents) + 1) / (frequency + 1)))
        for term, frequency in document_frequency.items()
    }
    basename_term_counts = {
        term: sum(term in Path(relative).stem.casefold() for relative, _text in documents)
        for term in raw_terms
    }
    cjk_terms = {
        term for term in raw_terms
        if len(term) >= 2 and all("\u3400" <= char <= "\u9fff" for char in term)
    }
    named_terms = {
        term for term in raw_terms
        if (len(term) >= 4 or term in cjk_terms)
        and term not in generic_temporal_terms
        and term not in {"latest", "recent", "recently", "today", "yesterday", "what", "how", "why", "which"}
        and not DATE_RE.search(term)
        and ("-" in term or any(char.isdigit() for char in term) or basename_term_counts.get(term, 0) <= 3)
    }
    relationship_query = bool(re.search(r"related|relationship|compare|comparison|关联|相关|对比|比较", query, re.IGNORECASE))
    graph_paths: set[str] = set()
    if relationship_query and named_terms and document_metadata:
        # Expand one hop through explicit local references. This borrows the
        # useful part of graph retrieval without adding a graph server or
        # inventing opaque similarity edges.
        anchor_paths = {
            relative
            for relative, text in all_document_by_path.items()
            if any(term in f"{relative} {text}".casefold() for term in named_terms)
        }
        for relative in anchor_paths:
            links = document_metadata.get(relative, {}).get("links", [])
            graph_paths.update(str(link) for link in links if str(link) in all_document_by_path)
        for relative, metadata in document_metadata.items():
            links = metadata.get("links", []) if isinstance(metadata, dict) else []
            if any(str(link) in anchor_paths for link in links):
                graph_paths.add(relative)
        if graph_paths:
            selected = {relative for relative, _text in documents} | graph_paths
            documents = [(relative, all_document_by_path[relative]) for relative in selected if relative in all_document_by_path]
    entity_layers = {
        layer for layer in available_layers
        if any(layer.casefold().rstrip("s") in GENERIC_LAYER_ALIASES[key] for key in ("paper", "model", "person", "benchmark"))
    }
    interrogative_query = bool(re.search(r"how|what|why|which", query, re.IGNORECASE))
    question_like = interrogative_query or bool(re.search(r"for|about|related|relationship|compare|对比|关联|相关|研究者|结论", query, re.IGNORECASE))
    relaxed = latest or any("-" in term for term in raw_terms) or question_like or len(preferred_layers) > 1
    if any("\u4e00" <= char <= "\u9fff" for char in query) and preferred_layers:
        relaxed = True
    content_terms = [term for term in terms if term not in {
        "how", "what", "why", "which", "do", "does", "did", "for", "about", "from", "the", "and", "or",
        "latest", "recent", "recently", "最近", "最新", "本周", "本月", "什么", "source", "evidence", "citation",
        "definition", "relationship", "related", "compare", "comparison", "result", "update",
    }]
    # Natural-language questions often use a different inflection or
    # paraphrase from the source.  Require a small lexical foothold, then
    # rank by document evidence instead of requiring every query token.
    minimum_matches = max(1, (len(content_terms) + 2) // 3) if relaxed else len(terms)
    if preferred_layers or question_like:
        # A named layer or an open question is already a routing signal.  One
        # content foothold is enough to rank a document; claim support still
        # reports which parts of the question the citation does not cover.
        minimum_matches = 1
    if any("\u4e00" <= char <= "\u9fff" for char in query):
        specific_terms = []
        minimum_matches = 1
    for relative, text in documents:
        haystack = text.casefold()
        path_text = relative.casefold()
        searchable = f"{haystack} {path_text}"
        raw_content_terms = [
            term for term in raw_terms
            if term not in {"how", "what", "why", "which", "do", "does", "did", "for", "about", "from", "the", "and", "or", "a", "an", "latest", "recent", "source", "evidence", "citation", "related", "relationship", "compare", "comparison"}
        ]
        # Prefer documents that cover several independent query signals over a
        # large document that happens to repeat one generic word.  Keep only
        # the longest CJK forms so generated fragments do not inflate
        # coverage.  This is a generic lexical precision rule, not a domain
        # synonym table or an embedding substitute.
        coverage_terms = []
        for term in raw_content_terms:
            if len(term) < 2:
                continue
            if any(
                len(other) > len(term)
                and all("\u3400" <= char <= "\u9fff" for char in term)
                and term in other
                for other in raw_content_terms
            ):
                continue
            coverage_terms.append(term)
        raw_coverage = sum(
            any(form in searchable for form in _term_forms(term))
            for term in coverage_terms
        )
        coverage_ratio = raw_coverage / max(1, len(coverage_terms))
        full_coverage_bonus = 4200 if len(coverage_terms) >= 2 and raw_coverage == len(coverage_terms) else 0
        heading_text = " ".join(
            line.lstrip("# ").casefold()
            for line in text.splitlines()[:40]
            if line.lstrip().startswith("#")
        )
        matched = sum(term in searchable for term in terms)
        layer = path_text.split("/", 1)[0]
        if (
            preferred_layers
            and not deep
            and not latest
            and layer not in preferred_layers
            and not (relationship_query and named_terms and (layer in entity_layers or relative in graph_paths))
        ):
            # A relationship query can legitimately cross from a named entity
            # card into a second entity layer (for example model -> benchmark).
            # Do not open this exception for notes/reports that merely mention
            # the entity.
            continue
        if date_terms and not all(term in path_text for term in date_terms):
            continue
        temporal_candidate = latest and bool(preferred_layers) and layer in preferred_layers
        layer_only_candidate = bool(preferred_layers) and not specific_terms and layer in preferred_layers
        semantic_candidate = relative in semantic_candidates
        graph_candidate = relative in graph_paths
        if matched < minimum_matches and not temporal_candidate and not layer_only_candidate and not semantic_candidate and not graph_candidate:
            continue
        excerpt_terms = list(dict.fromkeys(
            term for term in [*raw_terms, *terms]
            if term not in {"what", "latest", "recent", "source", "evidence", "citation", "model", "paper", "benchmark", "report", "note", "section"}
        ))
        excerpt, excerpt_start, excerpt_end = _evidence_window(text, excerpt_terms or terms)
        dates = [
            tuple(int(part) for part in re.findall(r"\d+", value)[:3])
            for value in (document_metadata.get(relative, {}).get("dates", []) if isinstance(document_metadata.get(relative), dict) else [])
        ]
        if not dates:
            dates = [
                tuple(int(part) for part in value.replace("/", "-").replace("-W", "-").split("-") if part.isdigit())
                for value in DATE_RE.findall(relative)
            ]
        latest_score = max(dates, default=(0, 0, 0)) if temporal_candidate else (0, 0, 0)
        latest_score = tuple((*latest_score, 0, 0)[:3])
        start, end = locate(source.path, relative, excerpt)
        if start is None:
            start, end = excerpt_start, excerpt_end
        primary_index_like = relative.casefold().endswith(("/index.yaml", "/index.yml", "/index.md"))
        index_like = primary_index_like or relative.casefold().endswith("/aliases.yaml")
        anchor_terms = [term for term in raw_terms if term in named_terms]
        path_anchor_bonus = sum(
            3000 for term in anchor_terms
            if term in path_text and (layer in preferred_layers or (relationship_query and layer in entity_layers))
        )
        named_anchor = bool(anchor_terms)
        # Citation validity is document-level.  A composite query can match
        # several sections of one document; report claim support separately
        # instead of discarding the document from ranking metrics.
        evidence_status = "secondary"
        support = _claim_support(terms, excerpt, start or excerpt_start, end or excerpt_end)
        semantic_score = semantic_scores.get(relative, 0.0)
        graph_bonus = 900 if graph_candidate else 0
        # Keep P@1 deterministic for exact/entity queries: the builtin
        # projection is a recall lane, not a replacement for a real lexical
        # foothold.  It may rescue a paraphrase with no term match, but it
        # must not reorder a document that already matched the query.
        semantic_bonus = int(max(0.0, semantic_score) * 1800) if matched == 0 else 0
        layer_bonus = 0
        if preferred_layers and layer in preferred_layers:
            layer_bonus += 72
            if latest:
                layer_bonus += 1400
        if catalog_query and primary_index_like and not has_compound_term:
            layer_bonus += 240
        if catalog_query and primary_index_like and has_compound_term:
            # A named entity inside a relationship/comparison query should
            # resolve to its card or cited index, not a global catalog that
            # merely repeats the entity name.
            layer_bonus -= 560
        if index_like and not primary_index_like:
            # Alias/metadata tables are useful expansion material, but the
            # canonical collection index is the better top-level answer when
            # both are present.
            layer_bonus -= 600
        if not catalog_query and index_like:
            # Collection indexes are useful for discovery, but a concrete
            # entity/feature query should prefer the cited card over the
            # index's repeated cross-references.
            if relationship_query and primary_index_like and layer in preferred_layers:
                layer_bonus += 700
            else:
                layer_bonus -= 2600 if named_anchor else 160
        if relative.casefold().endswith("-codex.md"):
            layer_bonus -= 220
        # A section/card heading is a stronger semantic anchor than repeated
        # cross-references in a later survey section.  Match a light plural
        # variant as well, without maintaining a domain-specific synonym map.
        heading_terms = [term.rstrip("s") for term in terms if len(term) >= 4 and term not in generic_filename_terms]
        heading_anchor = (
            (layer in preferred_layers and not (index_like and named_anchor))
            or (catalog_query and index_like)
            or (question_like and not named_anchor and not index_like)
        )
        heading_bonus = sum(420 for term in heading_terms if term and term in heading_text) if heading_anchor else 0
        heading_query_bonus = 0
        if heading_anchor:
            heading_query_bonus = sum(
                int(800 * term_idf.get(term, 1.0))
                for term in raw_content_terms
                if any(form in heading_text for form in _term_forms(term))
            )
        # Distinctive content terms should beat an index that merely repeats
        # cross-references.  Keep the boost data-driven and modest.
        content_bonus = sum(min(haystack.count(term), 4) * 20 for term in content_signal_terms) if has_compound_term and not index_like else 0
        if question_like and not named_anchor and not catalog_query and index_like:
            # A broad question should prefer a focused document over a large
            # cross-reference table.  Catalog queries are handled above.
            layer_bonus -= 220
        if interrogative_query and not preferred_layers and not catalog_query and index_like:
            layer_bonus -= 2200
        if latest and primary_index_like and not named_anchor and not date_terms and layer in preferred_layers:
            layer_bonus += 700
        if latest and not named_anchor and not date_terms and any(
            part.casefold() in {"weekly", "monthly", "daily", "history", "archive"}
            for part in Path(relative).parts
        ):
            layer_bonus += 800
        hits.append({
            "id": f"{source.spec.id}:{relative}",
            "kind": "document",
            "title": relative,
            "path": relative,
            "commit": source.commit,
            "line_start": start,
            "line_end": end,
            "excerpt": excerpt,
            "support": support,
            "semantic_score": round(semantic_score, 6),
            "retrieval_mode": "local-hybrid" if semantic_index and semantic_index.get("available") else "lexical",
            "evidence_status": evidence_status,
            "generated": False,
            "accepted": None,
            "related": [
                {
                    "id": f"{source.spec.id}:{target}",
                    "path": target,
                    "source": source.spec.id,
                    "repository": source.spec.repository,
                    "commit": source.commit,
                    "relation": "explicit-local-reference",
                }
                for target in (document_metadata.get(relative, {}).get("links", []) if isinstance(document_metadata.get(relative), dict) else [])
                if str(target) in all_document_by_path
            ],
            "citation": {"source": "repository", "backend": "repository-local-structured", "repository": source.spec.repository, "commit": source.commit, "path": relative, "memory_id": f"{source.spec.id}:{relative}", "line_start": start, "line_end": end, "evidence": excerpt, "generated": False, "accepted": None, "valid": bool(start and end), "stale": False},
            "_score": (
                sum(min(haystack.count(term), 4) * (6 + 18 * term_idf.get(term, 1.0)) for term in terms)
                + raw_coverage * 220
                + max(0, raw_coverage - 1) ** 2 * 280
                + int(coverage_ratio * 1800)
                + full_coverage_bonus
                + content_bonus
                + graph_bonus
                + semantic_bonus
                + path_anchor_bonus
                + sum(500 for term in raw_terms if "-" in term and term in haystack)
                + sum(180 for phrase in phrase_terms if phrase in haystack)
                + sum(28 for term in terms if term in relative.casefold())
                + sum(260 for term in filename_terms if term in Path(relative).name.casefold() and len(term) >= 4)
                + heading_bonus
                + heading_query_bonus
                + (72 if relative.split("/", 1)[0] in preferred_layers else 0)
                + layer_bonus
                + (4 if relative.startswith(("papers/", "models/", "people/", "benchmarks/", "survey/", "reports/", "notes/")) else 0)
                + (latest_score[0] * 1000000 + latest_score[1] * 10000 + latest_score[2] * 100 if temporal_candidate else 0)
            ),
        })
    hits.sort(key=lambda item: (-int(item.pop("_score", 0)), str(item.get("path") or "")))
    if relationship_query and named_terms:
        # A named relationship query should start at the concrete entity card
        # when one exists.  Collection indexes are still retained in the
        # result set, but they must not hide the path-addressable endpoint.
        path_anchored = [
            item for item in hits
            if sum(term in str(item.get("path") or "").casefold() for term in named_terms) > 0
        ]
        if path_anchored:
            best = max(
                path_anchored,
                key=lambda item: (
                    sum(term in str(item.get("path") or "").casefold() for term in named_terms),
                    float((item.get("support") or {}).get("coverage") or 0),
                    str(item.get("path") or ""),
                ),
            )
            hits.remove(best)
            hits.insert(0, best)
    report_like_layers = {
        layer for layer in preferred_layers
        if layer.casefold().rstrip("s") in {"report", "history", "archive", "summary"}
    }
    if relationship_query or (latest and not date_terms and not named_terms and report_like_layers):
        # Preserve the strongest result, then expose one result per useful
        # source layer before filling remaining slots.  This is source-local
        # diversity, not score fusion, and prevents a report/card layer from
        # hiding the related index needed to answer a join query.
        diversified: list[dict[str, Any]] = []
        used_layers: set[str] = set()
        for item in hits:
            layer = str(item.get("path") or "").split("/", 1)[0]
            if layer in used_layers:
                continue
            diversified.append(item)
            used_layers.add(layer)
            if len(diversified) >= limit:
                break
        if len(diversified) < limit:
            selected = {id(item) for item in diversified}
            diversified.extend(item for item in hits if id(item) not in selected)
        hits = diversified
    return hits[:limit]
