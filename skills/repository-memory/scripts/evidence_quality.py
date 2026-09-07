"""Evidence eligibility and attribution, independent of storage and ranking.

Readback proves that a record exists. It does not prove that its speaker did
the work, that a question contains its answer, or that an old refusal answers
a new question. These rules apply before ranking and again at the answer
boundary so adapters cannot accidentally bypass them.
"""
from __future__ import annotations

import re
from typing import Any

_QUESTION = re.compile(r"[?？]|什么|怎么|如何|哪些|哪个|哪天|几点|何时|能否|是否|为什么|多少|\b(?:what|when|where|who|why|how|whether)\b", re.I)
_REQUEST = re.compile(r"^(?:请|麻烦)?(?:使用|用).*?(?:查询|检索|查证|回答)|^(?:请)?(?:查询|检索|查证|回答|告诉我)|^(?:please\s+)?(?:search|look up|tell me|answer)\b", re.I)
_REFUSAL = re.compile(r"^(?:结论[：:]\s*)?(?:拒答|没有(?:找到)?(?:有效)?证据|未(?:能)?找到.*?证据|无法(?:确认|确定|回答)|证据不足|没有足够.*?信息|no (?:supporting )?evidence|(?:i )?(?:cannot|can't) (?:confirm|determine|answer)|not enough (?:evidence|information))", re.I)
_ACK = re.compile(r"^(?:收到|好的|好|已记住|记住了|明白|了解|ok|okay|noted|understood|got it)[。.!！\s]*$", re.I)
_ASCII = re.compile(r"[a-zA-Z0-9_]+(?:[-_./:@][a-zA-Z0-9_]+)*")


def statement_kind(role: str | None, text: str | None) -> str:
    """Conservative speech-act classification, with no project vocabulary.

    Trailing presentation instructions are not an assertion. A declarative
    fact followed by a question remains evidence; a question followed by
    "just give the conclusion" does not become one.
    """
    value = str(text or "").strip()
    plain = re.sub(r"[`*_]", "", value).strip()
    if not plain:
        return "empty"
    if _ACK.fullmatch(plain):
        return "acknowledgement"
    first = re.split(r"[。.!！\n](?!\d)", plain, maxsplit=1)[0].strip()
    if str(role or "").casefold() == "user":
        if value.endswith(("?", "？")) or _QUESTION.search(first) or _REQUEST.search(first):
            return "question"
    # A leading abstention with a later, separate positive paragraph is a
    # mixed answer. Keep it available and let claim support evaluate it.
    if _REFUSAL.search(plain) and len(re.split(r"\n\s*\n", plain)) == 1:
        return "abstention"
    return "statement"


def missing_identifiers(query: str, text: str) -> list[str]:
    """Never let a coined name disappear in CJK tokenization or coverage.

    Digits, versions and compound names have exact identity. Ordinary English
    words still use the lexical/semantic claim gate. ASCII boundaries allow
    identifiers touching Chinese but prevent A-12 matching A-123.
    """
    from tokenize_query import CJK_DATE, as_iso_date, date_aliases
    query = CJK_DATE.sub(lambda m: as_iso_date(m.group(0)) or m.group(0), query)
    identifiers = dict.fromkeys(t.casefold() for t in _ASCII.findall(query)
                                if re.search(r"[0-9]|[-_./:@]", t))
    lower = text.casefold() + " " + " ".join(date_aliases(text))
    return [token for token in identifiers
            if not re.search(r"(?<![a-z0-9_])" + re.escape(token) + r"(?![a-z0-9_])", lower)]


def attribution(*, recorded_by: str | None = None, provenance: dict[str, Any] | None = None,
                path: str | None = None) -> dict[str, Any]:
    provenance = provenance or {}
    session = provenance.get("session_id") or provenance.get("session")
    agent_match = re.search(r"(?:^|/)agent:([^:]+):", str(session or ""))
    performers = provenance.get("work_performed_by")
    # This field must be explicit. A recorder, Git author, file subject or
    # tool mentioned in a paragraph is not proof of execution ownership.
    performers = performers if isinstance(performers, list) else []
    return {
        "recorded_by": recorded_by or provenance.get("agent_id") or provenance.get("agent") or (agent_match.group(1) if agent_match else None),
        "source_session": session,
        "work_performed_by": performers,
        "ownership_status": "explicit" if performers else "not_established",
        "source_path": path,
        "rule": "Recording, quoting, or using a tool does not establish who performed the work.",
    }


def requested_team(query: str) -> str | None:
    match = re.search(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_.-]*)\s*(?:团队|team\b)", query, re.I)
    return match.group(1) if match else None


def ownership_supported(query: str, item: dict[str, Any]) -> bool:
    """A named team's work needs attribution, not mere tool-name overlap."""
    team = requested_team(query)
    if not team:
        return True
    performers = (item.get("attribution") or {}).get("work_performed_by") or []
    if any(str(p).casefold() == team.casefold() for p in performers):
        return True
    text = str(item.get("excerpt") or item.get("content") or "")
    return bool(re.search(re.escape(team) + r"\s*(?:团队|team)\s*(?:本周|本月|已经|已|主要|负责|完成|实现|修复|推进|built\b|fixed\b|delivered\b|completed\b)", text, re.I))
