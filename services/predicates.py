"""Predicate-based tracker model — the v2 matcher core (BRIEF v2 §1, §2, §3).

A tracker's ``config`` used to be the flat legacy shape
``{"topics": [...], "keywords": [...], "journals": [...]}`` (optionally nested
under ``config.query``). BRIEF v2 §1 replaces that with a richer, composable
shape::

    {
        "match": "all" | "any",        # across-predicate combiner
        "predicates": [
            {"type": "semantic",    "description": "...",
             "query_embedding": [..], "threshold": 0.62},
            {"type": "keyword",     "any_of": ["slow slip", "DAS"]},
            {"type": "journal",     "any_of": ["GRL", "Nature"]},
            {"type": "author",      "any_of": ["Fichtner", "Beroza"]},
            {"type": "cos_topic",   "any_of": ["Fibre-optic seismology"]},
            {"type": "institution", "any_of": ["ETH Zurich"]},
            {"type": "date_window", "after": "2024-01-01", "before": "2025-12-31"},
        ],
    }

Each predicate exposes ``eval(paper) -> (hit: bool, score: float in 0..1)``. A
registry (:func:`build_predicates`) dispatches by ``type`` and ALSO understands
the LEGACY config so existing trackers (and the contract tests in
``tests/test_tracker_matching.py``) keep working untouched.

Two design pillars are wired here:

* **SOURCE vs MATCH** (BRIEF v2 §2). ``journal`` / ``author`` /
  ``arxiv_category`` predicates *define intake* — a tracker built only out of
  those is an "explicit follow" and BYPASSES the seismology domain gate. The
  rest (semantic / keyword / cos_topic / institution / date_window) *filter*
  the pool. :func:`is_source_only_config` lets intake code ask whether a config
  is a pure explicit-follow.

* **Tiered evaluation** (BRIEF v2 §1). Cheap deterministic predicates
  (journal, author, cos_topic, institution, date_window, keyword) are ordered
  *before* the expensive ``semantic`` one, and :func:`match_tracker`
  short-circuits ``match=any`` on the first hit — so a paper that already
  matched on its journal never pays for an embedding cosine.

Semantic matching reuses :mod:`services.embedding_service` (numpy brute-force
cosine — NO sqlite-vec) against the paper's stored ``embedding`` column and the
tracker's cached ``query_embedding``. When either embedding is missing the
predicate simply scores 0.0 and never matches; it NEVER raises into the matcher.

This module is import-safe: ``embedding_service`` is only touched at eval time
(its google-genai import is itself lazy), so merely importing :mod:`predicates`
never drags in numpy/genai — the same contract ``cos_topics`` / ``journal_
registry`` honour for ``db.py`` and pytest collection.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "Predicate",
    "SemanticPredicate",
    "KeywordPredicate",
    "JournalPredicate",
    "AuthorPredicate",
    "CosTopicPredicate",
    "InstitutionPredicate",
    "DateWindowPredicate",
    "PREDICATE_REGISTRY",
    "SOURCE_PREDICATE_TYPES",
    "build_predicates",
    "match_tracker",
    "is_source_only_config",
    "normalize_config",
]


# ---------------------------------------------------------------------------
# Source vs match (BRIEF v2 §2)
# ---------------------------------------------------------------------------
#
# These predicate types DEFINE the intake pool (an explicit follow). The rest
# only FILTER an already-gathered pool. A tracker whose predicates are ALL of
# these types is a pure explicit-follow and bypasses the domain gate.
SOURCE_PREDICATE_TYPES = frozenset({"journal", "author", "arxiv_category"})


# Default semantic cosine threshold when a tracker stores a query embedding but
# omits an explicit ``threshold``. Deliberately conservative — gemini-embedding
# cosine for an on-topic title/abstract vs a focused description typically lands
# well above this, while off-topic pairs sit below it.
DEFAULT_SEMANTIC_THRESHOLD = 0.62


# CJK ranges (CJK Unified Ideographs, Hiragana/Katakana, Hangul). Mirrors the
# detection used by analyze_service._keyword_hits / main._keyword_hits so the
# keyword predicate matches CJK identically to the legacy path.
def _is_cjk(text: str) -> bool:
    return any(
        "一" <= c <= "鿿"
        or "぀" <= c <= "ヿ"
        or "가" <= c <= "힯"
        for c in text
    )


def _field_str(paper: Any, name: str) -> str:
    """Read a string-ish field from a Paper ORM object OR a plain dict."""
    val = getattr(paper, name, None)
    if val is None and isinstance(paper, dict):
        val = paper.get(name)
    return "" if val is None else str(val)


def _field_list(paper: Any, name: str) -> List[str]:
    """Read a list-ish field (authors / institutions / topics) as ``List[str]``."""
    val = getattr(paper, name, None)
    if val is None and isinstance(paper, dict):
        val = paper.get(name)
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val if x is not None]
    return [str(val)]


# ---------------------------------------------------------------------------
# Predicate base + subclasses
# ---------------------------------------------------------------------------


class Predicate:
    """Base predicate. Subclasses set ``type`` and implement :meth:`eval`.

    ``eval(paper)`` returns ``(hit, score)`` where ``hit`` is whether this
    predicate considers the paper a match and ``score`` is a 0..1 confidence
    (1.0 for deterministic hits; the cosine for semantic). A non-hit returns
    ``(False, score)`` where score is still the best evidence we have (useful
    for ranking near-misses, but never counted as a match).
    """

    type: str = "base"
    # is_source: does this predicate define intake (vs. merely filter)?
    is_source: bool = False
    # is_cheap: deterministic / no network — evaluated before expensive ones.
    is_cheap: bool = True

    def eval(self, paper: Any) -> Tuple[bool, float]:  # pragma: no cover - abstract
        raise NotImplementedError

    # Convenience so a predicate reads naturally in a boolean context isn't
    # supported on purpose — callers must use eval() to get the score too.
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.__class__.__name__} type={self.type!r}>"


def _coerce_terms(value: Any) -> List[str]:
    """Normalise a predicate's ``any_of`` / scalar value into a clean term list."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out: List[str] = []
    for item in items:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


class KeywordPredicate(Predicate):
    """Free-text keyword/phrase presence in title + abstract + authors + AI text.

    OR-within: a hit if ANY of ``any_of`` appears in the haystack. Matching is
    case-insensitive and word-boundary aware for ASCII terms (so "ice" does not
    fire inside "police"); ≤3-char codes ("DAS", "FWI") and CJK terms fall back
    to substring containment — byte-for-byte the legacy
    ``analyze_service._keyword_hits`` semantics so legacy trackers are
    unchanged. Filters the pool (not a source).
    """

    type = "keyword"
    is_source = False
    is_cheap = True

    def __init__(self, terms: Sequence[str]):
        self.terms: List[str] = _coerce_terms(terms)

    def eval(self, paper: Any) -> Tuple[bool, float]:
        if not self.terms:
            return (False, 0.0)
        haystack = _keyword_haystack(paper)
        if not haystack:
            return (False, 0.0)
        for kw in self.terms:
            kw_l = kw.strip().lower()
            if not kw_l:
                continue
            if _is_cjk(kw_l):
                if kw_l in haystack:
                    return (True, 1.0)
            elif len(kw_l) <= 3:
                if re.search(r"\b" + re.escape(kw_l) + r"\b", haystack):
                    return (True, 1.0)
            else:
                if re.search(r"(?:^|\W)" + re.escape(kw_l) + r"(?:$|\W)", haystack):
                    return (True, 1.0)
        return (False, 0.0)


def _keyword_haystack(paper: Any) -> str:
    """Lowercased title + abstract + author names + AI summary + AI keywords.

    Mirrors ``main._topic_monitor_haystack`` / the lazy haystack built in
    ``analyze_service._auto_assign_to_topic_monitors`` so the keyword predicate
    sees the same text the legacy matcher did. Reads the AI summary/keywords off
    the paper's ``ai_analysis`` relationship when present (dicts skip it).
    """
    parts: List[str] = []
    title = _field_str(paper, "title")
    if title:
        parts.append(title)
    abstract = _field_str(paper, "abstract")
    if abstract:
        parts.append(abstract)
    for a in _field_list(paper, "authors"):
        if a:
            parts.append(a)
    # AI summary + keywords — only available on an ORM Paper with a loaded
    # ai_analysis. Never fail if the relationship is absent / unloaded.
    analysis = getattr(paper, "ai_analysis", None)
    if analysis is not None:
        summary = getattr(analysis, "summary", None)
        if summary:
            parts.append(str(summary))
        kws = getattr(analysis, "keywords", None)
        if isinstance(kws, (list, tuple)):
            for k in kws:
                if k:
                    parts.append(str(k))
    return " ".join(parts).lower()


class JournalPredicate(Predicate):
    """Journal membership by canonical abbreviation (e.g. "GRL", "Nature").

    SOURCE predicate (BRIEF v2 §2): defines intake. A hit when the paper's
    journal — abbreviated via :func:`services.journal_abbrev.abbreviate` —
    upper-cased, equals any configured abbreviation (also upper-cased). The
    config stores upper-case abbreviations exactly like
    ``main.create_project`` normalises ``config.journals``.
    """

    type = "journal"
    is_source = True
    is_cheap = True

    def __init__(self, abbrevs: Sequence[str]):
        self.abbrevs = {a.strip().upper() for a in _coerce_terms(abbrevs)}

    def eval(self, paper: Any) -> Tuple[bool, float]:
        if not self.abbrevs:
            return (False, 0.0)
        journal = _field_str(paper, "journal")
        if not journal:
            return (False, 0.0)
        try:
            from services.journal_abbrev import abbreviate as _abbrev
            paper_abbrev = (_abbrev(journal) or "").upper()
        except Exception:
            paper_abbrev = journal.strip().upper()
        if paper_abbrev and paper_abbrev in self.abbrevs:
            return (True, 1.0)
        # Defensive: also accept the raw journal string upper-cased so a config
        # that stored the full title still matches (the picker stores abbrevs,
        # but this keeps hand-written configs forgiving).
        if journal.strip().upper() in self.abbrevs:
            return (True, 1.0)
        return (False, 0.0)


class AuthorPredicate(Predicate):
    """Author-name follow. SOURCE predicate (BRIEF v2 §2): defines intake.

    A hit when any configured name appears (case-insensitive substring) in any
    of the paper's author strings — so "Fichtner" matches "Andreas Fichtner".
    Substring (not word-boundary) because surnames are commonly the query and
    author strings carry initials/punctuation.
    """

    type = "author"
    is_source = True
    is_cheap = True

    def __init__(self, names: Sequence[str]):
        self.names = [n.strip().lower() for n in _coerce_terms(names) if n.strip()]

    def eval(self, paper: Any) -> Tuple[bool, float]:
        if not self.names:
            return (False, 0.0)
        authors = [a.lower() for a in _field_list(paper, "authors") if a]
        if not authors:
            return (False, 0.0)
        for needle in self.names:
            for author in authors:
                if needle in author:
                    return (True, 1.0)
        return (False, 0.0)


class CosTopicPredicate(Predicate):
    """Canonical COS-topic overlap — the legacy ``config.topics`` behaviour.

    A hit when any configured topic name overlaps (case-insensitive, with
    substring containment in either direction) the paper's topics. Topics are
    drawn from the paper's ``cos_topic_match`` (AI labels, passed in by the
    matcher) when available, else the paper's stored ``topics`` list. The
    bidirectional-substring rule reproduces the drift-tolerant comparison in
    ``analyze_service._auto_assign_to_topic_monitors`` (so "Fibre-optic
    seismology" still matches an AI label "Fibre-optic seismology / DAS").
    Filters the pool (not a source).
    """

    type = "cos_topic"
    is_source = False
    is_cheap = True

    def __init__(self, topics: Sequence[str]):
        self.topics = [t.strip().lower() for t in _coerce_terms(topics) if t.strip()]

    def eval(self, paper: Any) -> Tuple[bool, float]:
        if not self.topics:
            return (False, 0.0)
        # Prefer AI-provided matched topic names stashed on the paper by the
        # matcher (see match_tracker / _auto_assign_to_topic_monitors); fall
        # back to the paper's own topics list.
        matched = getattr(paper, "_matched_topic_names", None)
        if matched is None:
            names = [t.strip().lower() for t in _field_list(paper, "topics") if t.strip()]
        else:
            names = [str(t).strip().lower() for t in matched if str(t).strip()]
        if not names:
            return (False, 0.0)
        for pt in self.topics:
            for mn in names:
                if pt == mn or pt in mn or mn in pt:
                    return (True, 1.0)
        return (False, 0.0)


class InstitutionPredicate(Predicate):
    """Institution-affiliation overlap. Filters the pool (not a source).

    A hit when any configured institution appears (case-insensitive substring)
    in any of the paper's ``institutions`` strings — so "ETH" matches
    "ETH Zurich". Substring because affiliation strings are long and noisy.
    """

    type = "institution"
    is_source = False
    is_cheap = True

    def __init__(self, names: Sequence[str]):
        self.names = [n.strip().lower() for n in _coerce_terms(names) if n.strip()]

    def eval(self, paper: Any) -> Tuple[bool, float]:
        if not self.names:
            return (False, 0.0)
        institutions = [i.lower() for i in _field_list(paper, "institutions") if i]
        if not institutions:
            return (False, 0.0)
        for needle in self.names:
            for inst in institutions:
                if needle in inst:
                    return (True, 1.0)
        return (False, 0.0)


class DateWindowPredicate(Predicate):
    """Publication-date window ``[after, before]`` (inclusive). Filters the pool.

    A hit when the paper's ``published_date`` parses to a date within the
    window. Either bound may be omitted (open-ended). A paper with an
    unparseable / missing date never matches (conservative — a date filter
    should not silently admit undated papers).
    """

    type = "date_window"
    is_source = False
    is_cheap = True

    def __init__(self, after: Optional[str], before: Optional[str]):
        self.after: Optional[date] = _parse_date(after)
        self.before: Optional[date] = _parse_date(before)

    def eval(self, paper: Any) -> Tuple[bool, float]:
        if self.after is None and self.before is None:
            # Degenerate window — nothing to constrain; treat as non-matching so
            # an empty date predicate can't single-handedly admit everything.
            return (False, 0.0)
        d = _parse_date(_field_str(paper, "published_date"))
        if d is None:
            return (False, 0.0)
        if self.after is not None and d < self.after:
            return (False, 0.0)
        if self.before is not None and d > self.before:
            return (False, 0.0)
        return (True, 1.0)


def _parse_date(raw: Any) -> Optional[date]:
    """Best-effort parse of a free-form date string to a ``date``.

    Accepts ``YYYY-MM-DD`` / ``YYYY/MM/DD`` / ``YYYY-MM`` / ``YYYY``. Returns
    ``None`` on anything else (mirrors tracker_scheduler._parse_published_date).
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    return None


class SemanticPredicate(Predicate):
    """Cosine semantic match (BRIEF v2 §3) — the expensive, last-evaluated one.

    Scores ``cosine(query_embedding, paper.embedding)`` via
    :mod:`services.embedding_service` (numpy brute force). A hit when the cosine
    is ``>= threshold``. The tracker caches its description's query embedding in
    ``config`` (``query_embedding``); the paper carries its title+abstract
    embedding in the ``embedding`` TEXT column. When either embedding is missing
    or fails to decode the predicate scores 0.0 and never matches — it never
    raises into the matcher. Filters the pool (not a source).
    """

    type = "semantic"
    is_source = False
    is_cheap = False

    def __init__(
        self,
        query_embedding: Optional[Sequence[float]] = None,
        threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
        description: str = "",
    ):
        self.description = description or ""
        try:
            self.threshold = float(threshold)
        except (TypeError, ValueError):
            self.threshold = DEFAULT_SEMANTIC_THRESHOLD
        # Cache the decoded numpy query vector once (lazily, to keep import
        # side-effect free). ``None`` means "no usable query embedding".
        self._query_vec = None
        self._query_raw = list(query_embedding) if query_embedding else None
        self._decoded = False

    def _ensure_query_vec(self):
        if self._decoded:
            return self._query_vec
        self._decoded = True
        if not self._query_raw:
            self._query_vec = None
            return None
        try:
            import numpy as np
            arr = np.asarray(self._query_raw, dtype=np.float32)
            if arr.ndim != 1 or arr.size == 0:
                arr = None
            self._query_vec = arr
        except Exception:
            self._query_vec = None
        return self._query_vec

    def eval(self, paper: Any) -> Tuple[bool, float]:
        qvec = self._ensure_query_vec()
        if qvec is None:
            return (False, 0.0)
        blob = _field_str(paper, "embedding")
        if not blob:
            return (False, 0.0)
        try:
            from services.embedding_service import cosine_sim, decode_vector
            pvec = decode_vector(blob)
            score = cosine_sim(qvec, pvec)
        except Exception:
            return (False, 0.0)
        score = float(score)
        return (score >= self.threshold, score)


# Registry: predicate ``type`` -> builder taking the raw predicate dict.
# Each builder is total — it tolerates a missing/odd field and yields a
# predicate that simply never matches rather than raising.
def _build_semantic(spec: Dict[str, Any]) -> SemanticPredicate:
    threshold = spec.get("threshold")
    if threshold is None:
        threshold = DEFAULT_SEMANTIC_THRESHOLD
    qe = (
        spec.get("query_embedding")
        or spec.get("embedding")
        or spec.get("query_vector")
    )
    return SemanticPredicate(
        query_embedding=qe,
        threshold=threshold,
        description=str(spec.get("description") or spec.get("query") or ""),
    )


def _terms_of(spec: Dict[str, Any]) -> List[str]:
    """Pull the term list from any of the accepted keys on a predicate spec."""
    for key in ("any_of", "values", "value", "terms", "items"):
        if key in spec and spec[key] is not None:
            return _coerce_terms(spec[key])
    return []


PREDICATE_REGISTRY: Dict[str, Any] = {
    "semantic": _build_semantic,
    "keyword": lambda spec: KeywordPredicate(_terms_of(spec)),
    "journal": lambda spec: JournalPredicate(_terms_of(spec)),
    "author": lambda spec: AuthorPredicate(_terms_of(spec)),
    "cos_topic": lambda spec: CosTopicPredicate(_terms_of(spec)),
    "topic": lambda spec: CosTopicPredicate(_terms_of(spec)),  # alias
    "institution": lambda spec: InstitutionPredicate(_terms_of(spec)),
    "date_window": lambda spec: DateWindowPredicate(
        spec.get("after") or spec.get("from") or spec.get("start"),
        spec.get("before") or spec.get("to") or spec.get("end"),
    ),
}


# ---------------------------------------------------------------------------
# Config normalisation (new shape + legacy shape)
# ---------------------------------------------------------------------------


def _legacy_query_view(config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge top-level legacy keys with their ``config.query`` mirrors.

    The contract tests write BOTH ``config.topics`` and
    ``config.query.topics`` (see tests/test_tracker_matching._seed_tracker).
    Top-level wins when both are present and non-empty; ``query`` fills gaps.
    Returns a dict with possibly-present ``topics`` / ``keywords`` / ``journals``
    / ``min_relevance``.
    """
    out: Dict[str, Any] = {}
    query = config.get("query") if isinstance(config.get("query"), dict) else {}
    for key in ("topics", "keywords", "journals"):
        top = config.get(key)
        if top:
            out[key] = top
        elif query.get(key):
            out[key] = query.get(key)
    # min_relevance: top-level wins, else query.
    if config.get("min_relevance") is not None:
        out["min_relevance"] = config.get("min_relevance")
    elif query.get("min_relevance") is not None:
        out["min_relevance"] = query.get("min_relevance")
    return out


def normalize_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a canonical ``{"match", "predicates": [...]}`` dict for any config.

    Accepts the NEW predicate shape (passed through, with defaults filled) AND
    the LEGACY ``{topics, keywords, journals}`` / ``config.query.*`` shape
    (translated into ``cos_topic`` / ``keyword`` / ``journal`` predicate specs).
    Never raises; an unrecognised / empty config yields ``predicates: []``.

    Legacy semantics preserved: the legacy matcher treated topic OR keyword OR
    journal as sufficient (an OR across the three), so the translated config
    uses ``match="any"``.
    """
    if not isinstance(config, dict):
        return {"match": "any", "predicates": []}

    # NEW shape — an explicit predicates list present.
    raw_preds = config.get("predicates")
    if isinstance(raw_preds, list):
        match = str(config.get("match") or "any").strip().lower()
        if match not in ("all", "any"):
            match = "any"
        specs: List[Dict[str, Any]] = [p for p in raw_preds if isinstance(p, dict)]
        return {"match": match, "predicates": specs}

    # LEGACY shape — translate topics/keywords/journals into predicate specs.
    legacy = _legacy_query_view(config)
    specs = []
    topics = _coerce_terms(legacy.get("topics"))
    if topics:
        specs.append({"type": "cos_topic", "any_of": topics})
    keywords = _coerce_terms(legacy.get("keywords"))
    if keywords:
        specs.append({"type": "keyword", "any_of": keywords})
    journals = _coerce_terms(legacy.get("journals"))
    if journals:
        specs.append({"type": "journal", "any_of": journals})
    # Legacy combiner is OR across the three families.
    return {"match": "any", "predicates": specs}


def build_predicates(config: Optional[Dict[str, Any]]) -> List[Predicate]:
    """Build a list of :class:`Predicate` from a tracker config (either shape).

    Dispatches each normalised predicate spec through
    :data:`PREDICATE_REGISTRY`. Unknown predicate types are skipped (forward
    compatibility — an older build won't crash on a newer predicate type). The
    returned list is TIERED: cheap deterministic predicates first, the
    expensive ``semantic`` predicate last, so :func:`match_tracker` can
    short-circuit before paying for a cosine.
    """
    normalized = normalize_config(config)
    predicates: List[Predicate] = []
    for spec in normalized["predicates"]:
        ptype = str(spec.get("type") or "").strip().lower()
        builder = PREDICATE_REGISTRY.get(ptype)
        if builder is None:
            continue
        try:
            predicates.append(builder(spec))
        except Exception:
            # A malformed single predicate must not sink the whole tracker.
            continue
    # Tier: cheap (deterministic) predicates first, expensive (semantic) last.
    predicates.sort(key=lambda p: 0 if getattr(p, "is_cheap", True) else 1)
    return predicates


def is_source_only_config(config: Optional[Dict[str, Any]]) -> bool:
    """True if every predicate in ``config`` is a SOURCE type (explicit follow).

    Per BRIEF v2 §2 such a tracker (journal-only or author-only) BYPASSES the
    seismology domain gate at intake. Returns ``False`` for an empty config (no
    predicates means nothing to follow, so it is NOT an explicit follow that
    should widen intake).
    """
    predicates = build_predicates(config)
    if not predicates:
        return False
    return all(getattr(p, "is_source", False) for p in predicates)


# ---------------------------------------------------------------------------
# The matcher entry point
# ---------------------------------------------------------------------------


def match_tracker(paper: Any, project: Any) -> Tuple[bool, float]:
    """Evaluate ``paper`` against ``project``'s tracker config.

    Returns ``(belongs, score)``:

    * ``belongs`` — whether the paper matches under the across-predicate
      combiner (``match="all"`` requires every predicate to hit; ``match="any"``
      requires at least one). OR-within-a-predicate (``any_of``) is handled
      inside each predicate's ``eval``.
    * ``score`` — the BEST predicate score among the predicates that hit
      (0..1), conventionally the semantic cosine when one matched. When nothing
      hits, the best score seen across all predicates is returned (useful for
      ranking near-misses, but ``belongs`` is ``False``).

    Tiered / short-circuit: predicates come back cheap-first from
    :func:`build_predicates`; for ``match="any"`` we return on the first hit
    (so a journal/keyword hit avoids the semantic cosine), and for
    ``match="all"`` we bail on the first miss.

    ``project`` may be an ORM ``Project`` or anything exposing ``.config``; a
    plain dict config is also accepted directly.
    """
    config = _config_of(project)
    normalized = normalize_config(config)
    match_mode = normalized["match"]
    predicates = build_predicates(config)

    if not predicates:
        return (False, 0.0)

    # cos_topic predicates can consult AI-provided matched topic names if the
    # matcher stashed them on the paper (set in _auto_assign_to_topic_monitors).
    best_hit_score = 0.0
    best_any_score = 0.0
    any_hit = False
    all_hit = True

    for pred in predicates:
        try:
            hit, score = pred.eval(paper)
        except Exception:
            hit, score = (False, 0.0)
        score = _clamp01(score)
        if score > best_any_score:
            best_any_score = score
        if hit:
            any_hit = True
            if score > best_hit_score:
                best_hit_score = score
            if match_mode == "any":
                # Short-circuit: cheapest hit wins, semantic never evaluated.
                return (True, best_hit_score)
        else:
            all_hit = False
            if match_mode == "all":
                # Short-circuit: one miss sinks an ALL match.
                return (False, best_any_score)

    if match_mode == "all":
        # Reached only if every predicate hit.
        return (all_hit, best_hit_score if all_hit else best_any_score)
    # match == "any" and we never hit.
    return (any_hit, best_hit_score if any_hit else best_any_score)


def _config_of(project: Any) -> Dict[str, Any]:
    if isinstance(project, dict):
        # Either a raw config dict, or a project-like dict with a 'config' key.
        if "predicates" in project or "match" in project or "query" in project:
            return project
        cfg = project.get("config")
        return cfg if isinstance(cfg, dict) else project
    cfg = getattr(project, "config", None)
    return cfg if isinstance(cfg, dict) else {}


def _clamp01(x: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v
