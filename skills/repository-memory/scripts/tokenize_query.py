#!/usr/bin/env python3
"""One query tokenizer for every retrieval plane.

``re`` treats a contiguous CJK sentence as a single ``\\w`` token, so a natural
question such as ``李宁最近在做什么`` arrives as one string that occurs verbatim
nowhere.  Four call sites in this runtime each solved that differently, and two
of them did not solve it at all:

* ``fallback.py`` sliced 2/3/4-character windows out of every CJK run;
* ``standalone_memory.py`` expanded CJK runs into character bigrams;
* ``team_memory.py`` and ``local_memory.py`` did nothing, so a Chinese question
  produced one unmatchable token and those planes were unreachable in Chinese.

Every upstream implementation we reviewed that works in Chinese segments first:
TencentDB Agent Memory tokenizes with ``@node-rs/jieba`` before BM25, MemOS
calls ``jieba.lcut`` in its ``FastTokenizer``, and Hindsight exposes ``jieba``
and ``chinese_compatible`` as index-time tokenizer choices.  The one that does
not — MemPalace's ``\\w{2,}`` — is English-only.

So this module segments with jieba when it is installed and falls back to the
character n-gram carving otherwise.  jieba is an optional extra: this package
has no hard dependencies and must keep working without it.

Two measured constraints shape the implementation.

Segmentation runs on CJK runs only, never on the whole query.  jieba cuts
``rlvr-auto-survey`` into five tokens and ``octo-loop`` into three; the outer
word regex is the only reason those survive as typed, and the only reason
``8月18日`` reaches the date grammar as one unit instead of as four pieces.

Adjacent segments are joined back together.  jieba has no entry for
``火山云`` and returns ``火山`` + ``云``, which the old 3-gram carving happened to
get right.  Joining neighbours recovers the compound without a user dictionary
or any list of project names.

``carved`` marks a term this module manufactured rather than one the user
delimited.  A term that reconstitutes an entire CJK run as typed is the user's;
anything narrower is this tokenizer's guess at a word boundary.  Callers treat
the two differently — see ``fallback._claim_support``.
"""

from __future__ import annotations

import re
from typing import Any

BUILTIN_TOKENIZER = "builtin-ngram"
JIEBA_TOKENIZER = "jieba"

WORD_RE = re.compile(r"[\w./:-]{2,}", re.UNICODE)
CJK_RUN = re.compile(r"[㐀-鿿]+")
CJK_CHAR = re.compile(r"[㐀-鿿]")

# A date written the Chinese way.  This is a grammar over digits and three
# characters, not a vocabulary: it does not grow when a project, a person, or a
# system is added, which is the same test every other list in retrieval has to
# pass.  ``日`` is optional because ``8月18`` occurs, and the year is optional
# because ``8月18日`` is how anyone actually asks.
# ``号`` is the spoken register of ``日`` — ``8月20号`` and ``8月20日`` are the
# same date, and humans type the first.
CJK_DATE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(?:(\d{1,2})(?:日|号)?)?")

# Question scaffolding, dropped before terms are formed.  This is bounded by
# how the language asks questions, not by what the corpus contains: it does not
# grow when a project, a person, or a system is added.  A list of domain nouns
# would, and there is deliberately none of that anywhere in retrieval.
#
# Segmentation is what makes this list expressible at all.  Without it the only
# way to cut ``做什么`` out of an unsegmented run was to match the literal
# string, which is why the pre-jieba version of this set carried entries like
# ``在做``/``干什么``/``干啥`` — fragments, not words.  Those are gone; jieba
# returns ``做`` and ``什么`` separately.
STOP_TERMS = {
    # closed-class function words and particles
    "的", "了", "是", "在", "做", "干", "有", "被", "把", "给", "让", "对", "从", "到", "会", "能", "要", "过",
    "和", "与", "或", "也", "就", "都", "吗", "呢", "啥", "嘛", "呀", "吧",
    # personal pronouns: deictic, and in a shared corpus they select everything
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们",
    # interrogatives.  ``干什么`` is here and ``做什么`` is not because jieba
    # lexicalizes the first as one word and splits the second into ``做`` +
    # ``什么`` — measured, and the reason a term list must be checked against
    # the segmenter rather than assembled by intuition.
    "什么", "干什么", "怎么", "怎样", "咋", "哪些", "哪个", "如何", "谁", "为什么", "多少",
    # colloquial register of the same interrogatives — measured on live human
    # phrasing: ``在干嘛`` and ``做得怎么样`` each blocked an answerable question
    # because the colloquial form was treated as a content claim the corpus
    # never writes.
    "干嘛", "干啥", "咋样", "怎么样", "得怎么样", "啥时候",
    # temporal and aspectual deictics
    "最近", "最新", "上次", "之前", "以前", "目前", "当前", "本周", "本月", "今天", "昨天", "正在",
    "明天", "前天", "后天",
    # question framing that behaves as scaffolding in every phrasing we see
    "历史", "进展", "情况", "近况", "时候",
}

_JIEBA: Any | None = None
_JIEBA_ERROR: str | None = None
_JIEBA_PROBED = False


def _cache_directory() -> str | None:
    """Return a private home for jieba's prefix-dictionary cache.

    jieba writes a 9 MB pickle to ``tempfile.gettempdir()`` under a fixed name
    the first time it initializes.  Relocating it is not an optimization —
    measured on this machine, loading that pickle costs ~388 ms against ~338 ms
    to rebuild from the 4.8 MB dictionary, so the cache is slightly *slower*
    either way.  It is relocated because a shared temp directory is the wrong
    place to leave a 9 MB file under a name that collides between users, and
    because a package that writes outside the directories it owns is harder to
    reason about than one that does not.
    """

    try:
        from discovery import cache_root

        directory = cache_root() / "jieba"
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)
    except (ImportError, OSError, TypeError, ValueError):
        return None


def _load_jieba() -> Any | None:
    """Import jieba lazily, once, and never let its absence break retrieval."""

    global _JIEBA, _JIEBA_ERROR, _JIEBA_PROBED
    if _JIEBA_PROBED:
        return _JIEBA
    _JIEBA_PROBED = True
    try:
        import logging

        import jieba

        # This process may be an MCP server whose stdout carries JSON-RPC and
        # nothing else.  jieba logs dictionary building at INFO by default.
        jieba.setLogLevel(logging.ERROR)
        directory = _cache_directory()
        if directory:
            jieba.dt.tmp_dir = directory
        _JIEBA = jieba
        _JIEBA_ERROR = None
    except Exception as exc:  # optional extra must never break the lexical path
        _JIEBA = None
        _JIEBA_ERROR = f"{type(exc).__name__}: {str(exc)[:200]}"
    return _JIEBA


def tokenizer_status() -> dict[str, Any]:
    """Report which tokenizer is active, for diagnostics and doctor.

    Whether jieba happens to be installed changes what a Chinese query
    retrieves.  A measurement that does not say which tokenizer produced it is
    not attributable, so this travels next to ``semantic_available``.
    """

    module = _load_jieba()
    if module is not None:
        return {"name": JIEBA_TOKENIZER, "available": True, "segments_cjk": True}
    return {
        "name": BUILTIN_TOKENIZER,
        "available": True,
        "segments_cjk": False,
        "error": _JIEBA_ERROR or "jieba is not installed; install the 'cjk' extra for word segmentation",
    }


def segment(run: str) -> list[str]:
    """Split one contiguous CJK run into words, in order.

    Returns ``[run]`` unchanged when jieba is unavailable — the caller carves
    that case itself, because a windowing scheme has no word boundaries to
    group by.
    """

    module = _load_jieba()
    if module is None:
        return [run]
    return [piece for piece in module.lcut(run) if piece.strip()]


def _carved_run_terms(run: str) -> list[tuple[str, bool]]:
    """Expand one CJK run by sliding windows, the pre-jieba behaviour.

    Cut at the scaffolding instead of closing the gap.  Splicing the remainder
    together invented terms that span the seam — ``我们最近关于`` became
    ``我们关于`` — and a term like that cannot occur in any document, so it
    capped claim support at ``partial`` for ordinary CJK questions forever.
    """

    markers = sorted(STOP_TERMS, key=len, reverse=True)
    candidate = run
    for marker in markers:
        candidate = candidate.replace(marker, "\x00")
    terms: list[tuple[str, bool]] = []
    for piece in candidate.split("\x00"):
        piece = piece.strip()
        if not piece:
            continue
        terms.append((piece, piece != run))
        for width in (2, 3, 4):
            if len(piece) < width:
                continue
            terms.extend((piece[index:index + width], True) for index in range(len(piece) - width + 1))
    return terms


def _run_terms(run: str) -> list[tuple[str, bool]]:
    """Expand one CJK run into ``(term, carved)`` pairs."""

    if _load_jieba() is None:
        return _carved_run_terms(run)
    # Drop scaffolding, then keep the surviving words grouped by adjacency so a
    # join never spans a hole — same reason the windowing path cuts rather than
    # splices.
    groups: list[list[str]] = [[]]
    for piece in segment(run):
        if piece in STOP_TERMS:
            if groups[-1]:
                groups.append([])
            continue
        groups[-1].append(piece)
    terms: list[tuple[str, bool]] = []
    for group in groups:
        if not group:
            continue
        # Longest first: ``fallback._claim_support`` collapses a term into a
        # longer one that already matched, and reads them in this order.
        if len(group) > 2:
            whole = "".join(group)
            terms.append((whole, whole != run))
        for left, right in zip(group, group[1:]):
            joined = left + right
            terms.append((joined, joined != run))
        # A piece is what the segmenter decided is a word, so it is a claim the
        # user made, not a boundary this module guessed.  Marking it carved
        # would let the unreachable probe drop it for having no corpus hits,
        # and dropping the only specific word in a question is how "we do not
        # cover this topic" turns into a confident answer: measured live,
        # ``腌制泡菜的传统做法`` had 腌制/泡菜/腌制泡菜 all dropped at df 0 and
        # answered ``direct`` from five RLVR notes on the strength of the one
        # generic phrase left standing.  Only the joins above are synthesized.
        terms.extend((piece, False) for piece in group if len(piece) >= 2)
    return terms


def _iso_form(year: str | None, month: str, day: str | None) -> str | None:
    """Render one parsed date as the fragment an ISO-dated corpus contains."""

    month_value = int(month)
    if not 1 <= month_value <= 12:
        return None
    if day:
        day_value = int(day)
        if not 1 <= day_value <= 31:
            return None
        stem = f"{month_value:02d}-{day_value:02d}"
        return f"{year}-{stem}" if year else stem
    if year:
        return f"{year}-{month_value:02d}"
    # A bare ``8月`` names a month with no year.  ``08-`` is not a fragment
    # worth requiring, so leave the token alone rather than invent one.
    return None


def as_iso_date(token: str) -> str | None:
    """Normalize a token that is *entirely* a Chinese date, else ``None``.

    ``8月18日`` and ``2026-08-18`` are the same date, so this is normalization
    in the same sense as casefolding, not a synonym table.  It matters because
    the two halves of the system disagree by convention: people ask in the first
    form and Markdown headings are written in the second, so the term was
    required, occurred nowhere, and held claim coverage below 1.0 forever —
    measured on the live source, where ``武垚乐 8月18日 做了什么`` abstained
    against a file whose headings are all ``## 2026-08-18``.

    The normalized form replaces the token rather than joining it, because
    ``_claim_support`` requires *every* term: emitting both surface forms would
    abstain exactly as before, just with two unprovable terms instead of one.

    An unqualified ``8月18日`` becomes ``08-18`` and not ``2026-08-18``. Guessing
    the year from today's clock would answer a different question than the one
    asked whenever the corpus spans more than one year.
    """

    match = CJK_DATE.fullmatch(token)
    if match is None:
        return None
    return _iso_form(*match.groups())


def date_aliases(text: str) -> list[str]:
    """ISO renderings of every Chinese date written inside ``text``.

    The mirror of ``as_iso_date`` for the evidence side, so a corpus that writes
    ``8月18日`` in prose still proves a query that normalized to ``08-18``. Only
    proof is symmetric: ranking reads the index, which stores the corpus text as
    written, so an ISO query against a CJK-dated corpus still has to find the
    document some other way first.
    """

    forms = []
    for match in CJK_DATE.finditer(text):
        form = _iso_form(*match.groups())
        if form:
            forms.append(form)
    return forms


def expand(query: str) -> list[tuple[str, bool]]:
    """Expand a query into ``(term, carved)`` pairs."""

    expanded: list[tuple[str, bool]] = []
    for token in WORD_RE.findall(str(query or "")):
        value = token.casefold()
        if not CJK_CHAR.search(value):
            expanded.append((value, False))
            continue
        iso = as_iso_date(value)
        if iso is not None:
            # Delimited, not carved: the user named this date, so it stays
            # required.  Letting it be dropped would answer an August question
            # out of a July section and call that ``direct``.
            expanded.append((iso, False))
            continue
        # A mixed token such as ``v2版本`` is one unit to the word regex and to
        # the user.  Keep it whole, but only when it carries no scaffolding:
        # ``octo-daemon的健康监控是怎么配置的`` is also one token, and keeping
        # *that* whole manufactures a term no document contains.
        if not CJK_RUN.fullmatch(value) and not any(stop in value for stop in STOP_TERMS):
            expanded.append((value, False))
        for run in CJK_RUN.findall(value):
            # A lone CJK character — the ``月``/``日`` of ``8月18日`` — is not a
            # term.  It matches almost everything and carries almost nothing.
            if len(run) >= 2:
                expanded.extend(_run_terms(run))
    seen: dict[str, bool] = {}
    for value, carved in expanded:
        if not value or value in STOP_TERMS:
            continue
        # A term counts as delimited if it arrived that way even once.
        seen[value] = seen.get(value, True) and carved
    return list(seen.items())


def query_terms(query: str) -> list[str]:
    """Expand a query into conservative lexical terms.

    This is a tokenizer, not a synonym model: it never invents vocabulary the
    query did not contain.
    """

    return [term for term, _carved in expand(query)]


# Modal and directional verb morphemes — a closed grammatical class, like the
# stop set above. jieba glues them onto the following verb ("为什么要切" yields
# the "word" 要切), and treating that glue as a user claim blocked answerable
# questions: measured live, 要切 held "为什么要切 cuDNN" at partial forever while
# the document said 切 cuDNN. Only the *first character* of a two-character
# segmenter token is checked against this set, and the token is merely marked
# droppable — the corpus-frequency probe still keeps it whenever any document
# actually writes it, so a real word that happens to start with one of these
# (要求, 先验, 就绪…) is unaffected wherever it exists in the corpus.
MODAL_PREFIX_CHARS = frozenset("要再先还都也就别才又快去来")

# Directional verb complements — the other closed glue class. jieba lexicalizes
# verb+complement ("接进来", "切过去") as one token, and the corpus writes the
# bare verb: measured live, 接进来 held "kimi 的日志接进来了吗" at partial while
# the document said 接入. Suffix-matched, same droppable-not-dropped contract
# as the modal prefixes: the corpus-frequency probe has the final word.
DIRECTIONAL_SUFFIXES = ("进来", "进去", "出来", "出去", "上来", "上去", "下来", "下去", "起来", "过来", "过去", "回来", "回去")


def carved_query_terms(query: str) -> set[str]:
    """Return the terms this module manufactured rather than the user typed.

    Two-character segmenter tokens led by a closed-class modal/directional
    morpheme are included: they are word-boundary guesses of the same kind as
    this module's own joins, and the caller's zero-frequency probe — not this
    function — decides whether the corpus knows them.
    """

    carved = {term for term, carved in expand(query) if carved}
    for term, was_carved in expand(query):
        if not was_carved and all("\u3400" <= char <= "\u9fff" for char in term):
            if len(term) == 2 and term[0] in MODAL_PREFIX_CHARS:
                carved.add(term)
            elif 3 <= len(term) <= 4 and term.endswith(DIRECTIONAL_SUFFIXES):
                carved.add(term)
    return carved


def plane_terms(query: str, stop_words: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Terms for the memory planes, which match against stored text directly.

    ``stop_words`` stays a per-plane argument: each store drops the nouns that
    describe itself (``memory``, ``conversation``, ``project``) because in that
    store they select everything.  That is a field-specific filter, not a
    language one, so it does not belong in ``STOP_TERMS``.
    """

    return [term for term in query_terms(query) if term not in stop_words]


def fts5_can_match(term: str) -> bool:
    """True when SQLite's stock FTS5 tokenizer can match ``term`` as typed.

    ``unicode61`` classifies CJK ideographs as ordinary letters, so it indexes
    an entire contiguous run as a single token.  A body holding
    ``李宁最近在做调度链验证`` is stored under that one token, and a MATCH for
    ``"李宁"`` returns nothing — measured, not assumed.

    Every caller uses FTS as a candidate *pre-filter* and then matches again in
    Python with plain substrings, which is CJK-safe.  So when a query carries a
    term this rejects, the pre-filter would drop rows the Python pass would
    have matched, and the honest move is to skip it and scan.  Segmenting the
    query without this guard makes those planes strictly worse than leaving
    them unsegmented: the unsplit run at least matched its own echo.
    """

    return not CJK_CHAR.search(term)
