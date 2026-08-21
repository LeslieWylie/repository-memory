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
from tokenize_query import carved_query_terms, date_aliases, query_terms

from models import SourceView

EXCLUDED = {".git", ".remember", ".cache", ".venv", "venv", "__pycache__", "output", "tmp", "node_modules", "build", "dist"}
OPERATIONAL_DIRS = {"skills", "scripts", "tests", "test", "eval", "evals", "fixtures", "logs", "templates"}
EXCLUDED_FILENAMES = {"template.md", "template.yaml", "template.yml"}
EXTENSIONS = {".md", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".json"}
SECRET_NAME = re.compile(r"(^|/)(\.env(?:\.|$)|.*\.(?:pem|key|p12|pfx|secret|secrets?))$", re.IGNORECASE)
SECRET_CONTENT = re.compile(r"-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/.+=]{16,}|\bsk-[A-Za-z0-9_-]{16,}", re.IGNORECASE)
DATE_RE = re.compile(r"20\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?|20\d{2}-W\d{1,2}", re.IGNORECASE)
GENERIC_SUPPORT_TERMS = {
    "note", "report", "weekly", "paper", "model", "card", "update", "result",
    # Question-framing nouns of the 历史/进展/情况 family, measured leaking:
    # an L1 memory of a past *abstention* answered "火星殖民项目的预算" through
    # the lone word 项目, and a labelling-prompt dump answered
    # "我们公司什么时候上市" through scattered 公司/上市. They still retrieve
    # and rank; they just cannot carry a claim alone.
    "项目", "公司",
    # CJK pronouns/connectives carry no claim of their own.  They are excluded
    # here rather than in ``tokenize_query.STOP_TERMS`` so retrieval and layer
    # routing keep seeing them; only claim coverage ignores them.
    "我们", "你们", "他们", "咱们", "关于", "以及", "还有", "这个", "那个", "一下", "现在",
}
QUERY_BOUNDARY = re.compile(r"最近|最新|上次|之前|以前|历史|目前|当前|本周|本月|今天|昨天|正在|在做|在干|做什么|干什么|干啥|干嘛|进展|情况")
GENERIC_LAYER_ALIASES = {
    "paper": {"paper", "papers", "publication", "publications", "research"},
    "model": {"model", "models", "system", "systems"},
    "person": {"person", "people", "author", "authors", "researcher", "researchers", "team"},
    "benchmark": {"benchmark", "benchmarks", "evaluation", "evaluations", "eval", "test", "tests"},
    "note": {"note", "notes", "memo", "memos", "record", "records"},
    "report": {"report", "reports", "summary", "summaries", "finding", "findings", "conclusion", "conclusions", "status"},
    "survey": {"survey", "surveys", "overview", "overviews", "guide", "guides", "section", "sections"},
}


def _compound_parts(term: str) -> list[str]:
    """Split a hyphenated compound into the concepts it is made of.

    Repository cards write "long context" where users write "long-context", so
    keeping both forms improves recall without a synonym table.  A purely
    numeric part is not a concept the compound decomposes into, though: ``08-18``
    split into ``08`` and ``18`` matched every MR number, GPU size and line count
    in the corpus and buried the one line that actually carried the date, so the
    window picker cited a section eleven months away from the question.
    """

    return [part for part in re.split(r"[-/]", term) if len(part) >= 2 and not part.isdigit()]


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
    # A Markdown heading that carries a query term names a whole section about
    # it; a body line carrying the same term is inside some *other* section,
    # mentioning it in passing.  Weight headings above body mentions, or the
    # tie-break below hands the citation to whichever passing mention appears
    # first: measured live, `## 2026-08-18` at line 77 lost to an incident
    # retro inside the 08-20 section that referenced the date once, and the
    # question about 08-18 was answered partial out of the wrong section.
    def _line_score(line: str) -> int:
        score = sum(line.casefold().count(term) for term in terms)
        if score and line.lstrip().startswith("#"):
            score += 1
        return score

    line_scores = [_line_score(line) for line in file_lines]
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
        elif file_lines[best].lstrip().startswith("#"):
            # The anchor is a section heading: cite the section it opens, not
            # the tail of whichever section happens to end just above it.
            start = best
            end = min(len(file_lines), start + max_lines)
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


def _term_supported(term: str, excerpt_value: str) -> bool:
    """Report whether the excerpt actually carries this query term.

    Non-CJK terms keep the strict substring rule.  CJK has no word delimiter,
    so a query phrase is often a compound of what the document wrote: a note
    saying "记忆钩子" does support a question about "自动记忆钩子", and demanding
    the verbatim compound made every ordinary CJK question stall at ``partial``
    and therefore abstain.  Credit is still evidence-based — the document must
    contain a contiguous run of the term, at least three characters long and at
    least half of it — so scattered characters, or a bare "项目" standing in for
    "虚构项目", do not count.
    """

    if term in excerpt_value:
        return True
    if not all("\u3400" <= char <= "\u9fff" for char in term):
        return False
    shortest = max(3, (len(term) + 1) // 2)
    for width in range(len(term) - 1, shortest - 1, -1):
        for index in range(len(term) - width + 1):
            if term[index:index + width] in excerpt_value:
                return True
    return False


def _claim_support(
    query_terms: list[str],
    excerpt: str,
    line_start: int,
    line_end: int,
    *,
    unreachable: frozenset[str] = frozenset(),
    real_terms: frozenset[str] = frozenset(),
    carved: frozenset[str] = frozenset(),
    path: str = "",
) -> dict[str, Any]:
    """Report how much of the query this excerpt actually carries.

    ``unreachable`` names terms the corpus has never contained *and* that
    this tokenizer carved out of an unsegmented run rather than the user
    delimiting them — the caller supplies it because only the caller holds
    the corpus.  Such a term is not a claim the evidence can support or
    refute; it is a guess at a word boundary that turned out wrong.
    Requiring one holds coverage permanently below 1.0, which is abstention
    by tokenizer rather than by evidence: measured against the live
    1696-document source, every natural CJK question abstained, including
    ``octo-daemon 的健康监控 cron 是怎么配置的？`` — where ``是怎么配置`` occurs
    in no document while every term the user actually typed was present.

    A term the user delimited stays required even when the corpus lacks it.
    That is the case where abstaining is right, and it is what keeps a query
    naming something absent from being answered anyway.  And if excluding
    would empty the requirement, nothing is excluded: an all-carved,
    all-absent query must abstain, not fall through the empty-set branch
    below into ``direct``.

    ``real_terms`` names the terms a word segmenter produced rather than this
    module joining them.  They are never dropped by the probe above, and they
    are restored when the join that absorbed them is dropped, so a question
    whose specific words the corpus lacks abstains on those words instead of
    on whatever generic phrase outlived them.

    ``path`` is part of the citation and therefore part of the evidence.
    Retrieval already reads it — ``lower_document_by_path`` indexes
    ``f"{relative} {text}"`` — so a document can be retrieved *because* its
    path matched and then be unable to prove the very term that found it.  On
    the live source that was most of the abstentions: ``rlvr-auto-survey
    standup 李宁`` returned ``standup/李宁.md`` first and marked both
    ``standup`` and ``李宁`` unmatched, because they are in the filename rather
    than in the quoted window.  A per-person or per-date layout keeps
    attribution in the path by design; the citation the caller receives carries
    that path, so crediting it is honest.  Spans stay excerpt-only — a path
    match has no line to point at.
    """

    raw_support_terms = list(dict.fromkeys(
        term for term in query_terms
        if (len(term) >= 3 or (len(term) >= 2 and all("\u3400" <= char <= "\u9fff" for char in term)))
        and term not in GENERIC_SUPPORT_TERMS
    ))
    # A claim is something the user said.  A join this tokenizer manufactured
    # is a recall device, and the corpus-frequency probe below cannot save the
    # requirement from it: with substring matching, a two-character join like
    # ``要切`` is "reachable" through ``需要切换`` and ``日报写`` through
    # ``日报写入``, so df==0 almost never holds and the join gated the claim
    # forever.  When the segmenter produced real words, they are the claims;
    # drop the joins from the requirement before the longest-form collapse so
    # the components they absorbed stay required.  The builtin n-gram path has
    # no real words and keeps the measured collapse-then-probe behaviour.
    if real_terms and carved:
        kept = [term for term in raw_support_terms if term not in carved or term in real_terms]
        if kept:
            raw_support_terms = kept
    # CJK token expansion deliberately adds short n-grams for recall.  Those
    # n-grams are not independent claims: if a longer CJK term contains one,
    # keep only the longest form for claim support so a hit on "评测结果" is
    # not downgraded merely because "评测" and "结果" were also generated.
    #
    # Collapse against the full set, before anything is excluded.  Excluding
    # first orphans the fragments: dropping the unreachable "是怎么配置" left
    # "是怎么", "怎么配" and "么配置" with no longer form to hide behind, so they
    # were promoted into the requirement and the query abstained anyway — the
    # same abstention, now demanded by three fragments instead of one.
    support_terms = [
        term for term in raw_support_terms
        if not (
            all("\u3400" <= char <= "\u9fff" for char in term)
            and any(len(other) > len(term) and term in other for other in raw_support_terms)
        )
    ]
    if unreachable:
        retained = [term for term in support_terms if term not in unreachable]
        dropped = [term for term in support_terms if term in unreachable]
        if dropped:
            # Collapsing to the longest form hid the segmenter's real words
            # inside a join this module manufactured.  When that join turns out
            # to be unreachable it is dropped as a bad word boundary — but the
            # words it absorbed are claims the user actually made, so they come
            # back rather than leaving the question with only whatever generic
            # phrase happened to survive.  ``real_terms`` is empty on the
            # builtin n-gram path, where every fragment *is* a guess, so that
            # path keeps the collapse-then-exclude behaviour it was measured on.
            restored = [
                term for term in raw_support_terms
                if term in real_terms
                and term not in unreachable
                and term not in retained
                and any(len(item) > len(term) and term in item for item in dropped)
            ]
            retained = [term for term in raw_support_terms if term in set(retained) | set(restored)]
        if retained:
            support_terms = retained
    excerpt_value = excerpt.casefold()
    evidence_value = f"{path} {excerpt}".casefold() if path else excerpt_value
    # A date the evidence wrote in Chinese proves a query that normalized to
    # ISO.  Appending is safe where substituting would not be: the ISO form is
    # added beside the original, so evidence that already writes both is
    # unaffected and no existing term stops matching.
    aliases = date_aliases(evidence_value)
    if aliases:
        evidence_value = f"{evidence_value} {' '.join(aliases)}"
    matched = [term for term in support_terms if _term_supported(term, evidence_value)]
    unmatched = [term for term in support_terms if not _term_supported(term, evidence_value)]
    if not support_terms or len(matched) == len(support_terms):
        status = "direct"
    elif matched:
        status = "partial"
    else:
        status = "unknown"
    spans = []
    for offset, line in enumerate(excerpt.splitlines()):
        line_terms = [term for term in matched if _term_supported(term, line.casefold())]
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
    paths: set[str] = set()
    databases = [indexed.get("fts_path"), indexed.get("fts_path_paths")]
    for database in databases:
        if not database:
            continue
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT paths.path FROM documents JOIN paths ON paths.rowid = documents.rowid WHERE documents MATCH ? LIMIT ?",
                    (expression, int(limit)),
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError):
            continue
        paths.update(str(row[0]) for row in rows if row and row[0])
        if len(paths) >= limit:
            break
    return paths or None


def _document_cache(source: SourceView, indexed: dict[str, Any] | None, deep: bool) -> dict[str, Any]:
    """Reuse per-view document maps across queries in a long-lived MCP process."""

    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    cached = metadata.get("_fallback_document_cache")
    cache_key = (str(source.commit), bool(deep), id(indexed))
    if isinstance(cached, dict) and cached.get("key") == cache_key:
        return cached
    indexed_documents = indexed.get("documents") if isinstance(indexed, dict) else None
    all_documents = (
        [(str(item.get("path")), str(item.get("text") or "")) for item in indexed_documents if isinstance(item, dict) and item.get("path")]
        if isinstance(indexed_documents, list)
        else [(relative, None) for relative in paths(source.path, deep)]
    )
    value = {
        "key": cache_key,
        "indexed_documents": indexed_documents,
        "document_metadata": {str(item.get("path")): item for item in indexed_documents or [] if isinstance(item, dict) and item.get("path")},
        "all_documents": all_documents,
        "all_document_by_path": dict(all_documents),
        "lower_document_by_path": {relative: f"{relative} {text}".casefold() for relative, text in all_documents if text is not None},
    }
    if isinstance(metadata, dict):
        metadata["_fallback_document_cache"] = value
    return value


def search(source: SourceView, query: str, limit: int = 5, deep: bool = False) -> list[dict[str, Any]]:
    raw_terms = query_terms(query)
    has_compound_term = any("-" in term for term in raw_terms)
    # Treat hyphenated natural-language concepts as both a phrase and tokens.
    # Repository cards often write "long context" while users write
    # "long-context"; retaining both forms improves recall without adding a
    # model- or provider-specific synonym table.
    terms = list(dict.fromkeys(
        raw_terms
        + [part for term in raw_terms for part in _compound_parts(term)]
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
    cache = _document_cache(source, indexed, deep)
    indexed_documents = cache.get("indexed_documents")
    document_metadata = cache["document_metadata"]
    all_documents = cache["all_documents"]
    # Content/path FTS is an acceleration lane for genuinely large indexes.
    # Medium indexes may already have an old sidecar from an experiment, but
    # using it as a hard candidate gate can reduce semantic/temporal recall.
    indexed_count = int(indexed.get("document_count") or len(indexed_documents or [])) if isinstance(indexed, dict) else 0
    fts_paths = _fts_candidates(indexed, terms) if indexed_count >= 5000 else None
    documents = [(relative, text) for relative, text in all_documents if fts_paths is None or relative in fts_paths]
    # A semantic rewrite can have no lexical hit. Keep the full path/text map
    # only as a cheap in-process view of the already-loaded JSON cache, then
    # narrow it to semantic candidates after scoring below.
    all_document_by_path = cache["all_document_by_path"]
    lower_document_by_path = cache["lower_document_by_path"]
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
        term: sum(term in lower_document_by_path.get(relative, f"{relative} {text}".casefold()) for relative, text in documents)
        for term in terms
    }
    term_idf = {
        term: max(1.0, math.log((len(documents) + 1) / (frequency + 1)))
        for term, frequency in document_frequency.items()
    }
    # The same corpus counts answer a second question: which carved fragments
    # does this source never contain at all?  A fragment with zero document
    # frequency is a word boundary this tokenizer guessed wrong, not a claim
    # the evidence could ever carry — ``_claim_support`` drops those from the
    # requirement rather than abstaining on them forever.  Terms the user
    # delimited are excluded from the probe: their absence is a real answer.
    carved_terms = carved_query_terms(query) & set(terms)
    real_terms = frozenset(set(terms) - carved_terms)
    unreachable = frozenset(
        term for term in carved_terms
        if not document_frequency.get(term)
    )
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
            if any(term in lower_document_by_path.get(relative, f"{relative} {text}".casefold()) for term in named_terms)
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
        # A join this tokenizer manufactured (``模型上线`` from ``模型``+``上线``)
        # is a recall device, not an independent signal: measured live, the
        # document that carried 27b, 模型 and 上线 lost the ranking to a report
        # that happened to write the join verbatim, because the join crowded
        # its own components out of coverage.  Count coverage over the words
        # the user (or the segmenter) actually produced; joins keep helping
        # recall and tf scoring but no longer gate coverage.
        coverage_source_terms = [
            term for term in raw_content_terms
            if not (
                term in carved_terms
                and any(other != term and other in term for other in raw_content_terms)
            )
        ]
        for term in coverage_source_terms:
            if len(term) < 2:
                continue
            if any(
                len(other) > len(term)
                and all("\u3400" <= char <= "\u9fff" for char in term)
                and term in other
                for other in coverage_source_terms
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
        full_conjunctive = len(coverage_terms) >= 2 and raw_coverage == len(coverage_terms)
        if (
            preferred_layers
            and not deep
            and not latest
            and layer not in preferred_layers
            # A document covering every independent query signal outranks the
            # layer heuristic that would have hidden it: measured live, 模型
            # routed to models/ and excluded the standup entry that carried
            # 27b, 模型 and 上线 together — the only document that answered.
            and not full_conjunctive
            and not (relationship_query and named_terms and (layer in entity_layers or relative in graph_paths))
        ):
            # A relationship query can legitimately cross from a named entity
            # card into a second entity layer (for example model -> benchmark).
            # Do not open this exception for notes/reports that merely mention
            # the entity.
            continue
        if date_terms:
            # The index records date anchors from paths *and* headings
            # (``local_index._document_dates``); this filter read only the path
            # half, so a document that dates its sections in ``## 2026-08-18``
            # was excluded outright.  That made the year-qualified question
            # strictly worse than the bare one: ``8月18日`` reached
            # ``standup/武垚乐.md`` while ``2026年8月18日`` could not, because only
            # the second parses as a full ISO date.  Heading anchors are
            # structure the indexer already derived, not a widening of the
            # filter to arbitrary body text.
            anchors = document_metadata.get(relative) if isinstance(document_metadata.get(relative), dict) else {}
            anchor_text = " ".join(str(value) for value in (anchors.get("dates") or []))
            if not all(term in path_text or term in anchor_text for term in date_terms):
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
        support = _claim_support(terms, excerpt, start or excerpt_start, end or excerpt_end, unreachable=unreachable, real_terms=real_terms, carved=frozenset(carved_terms), path=relative)
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
                + sum(
                    260 for term in filename_terms
                    if term in Path(relative).name.casefold()
                    and (len(term) >= 4 or (len(term) >= 2 and all("\u3400" <= char <= "\u9fff" for char in term)))
                )
                # A per-person or per-entity layout puts the answer's owner in
                # the file stem.  An exact stem match is that structure speaking
                # — measured live, 刘伯潇's own standup lost to a passing mention
                # in a colleague's file.
                + sum(1200 for term in filename_terms if term == Path(relative).stem.casefold())
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
