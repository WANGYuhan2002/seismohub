"""Title-similarity matching for the "upload missing PDFs" flow (2026-06-16).

Pure, DB-free functions so they're unit-testable. Used by the
``/api/papers/match_pdfs`` endpoint as the FALLBACK when a DOI cannot be
extracted from an uploaded PDF — DOI exact-match (in main.py) is always tried
first and is authoritative; title fuzzy-match only fills the gap and, by owner
decision (2026-06-16), only auto-attaches at a high, unique-candidate bar —
everything mid-confidence goes to a human-confirm queue, never auto-attached.

Thresholds (owner-set 2026-06-16):
  - >= TITLE_MATCH_AUTO and unique  -> auto-attach
  - TITLE_MATCH_MIN .. AUTO, or ambiguous (>=2 high candidates) -> confirm
  - < TITLE_MATCH_MIN -> no match (skip)
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Tuple

# Owner-set bars. AUTO is deliberately strict so a fuzzy guess never silently
# attaches a PDF to the wrong paper; MIN is the floor below which we don't even
# surface a candidate.
TITLE_MATCH_AUTO: float = 0.92
TITLE_MATCH_MIN: float = 0.75

# Keep CJK + alphanumerics; everything else (punctuation, spacing) is a separator.
_WORD_RE = re.compile(r"[a-z0-9一-鿿]+")


def normalize_title(title: Optional[str]) -> str:
    """Lowercase, strip punctuation/whitespace, collapse to single spaces.

    A title and its running-header / filename variant should normalise to the
    same token stream so SequenceMatcher compares meaning, not formatting.
    """
    if not title:
        return ""
    return " ".join(_WORD_RE.findall(title.lower()))


def title_similarity(a: Optional[str], b: Optional[str]) -> float:
    """0..1 similarity of two titles after normalisation. 0.0 if either empty."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def rank_title_candidates(
    title: Optional[str],
    papers: Iterable[Tuple[str, Optional[str]]],
    *,
    min_score: float = TITLE_MATCH_MIN,
    limit: int = 5,
) -> List[Tuple[str, float]]:
    """Rank library papers by title similarity to *title*.

    ``papers`` is an iterable of ``(paper_id, paper_title)``. Returns
    ``[(paper_id, score)]`` sorted high→low, keeping only score >= ``min_score``,
    truncated to ``limit``. Empty query title -> ``[]``.
    """
    if not normalize_title(title):
        return []
    scored: List[Tuple[str, float]] = []
    for pid, ptitle in papers:
        s = title_similarity(title, ptitle)
        if s >= min_score:
            scored.append((pid, round(s, 4)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def classify_title_match(candidates: List[Tuple[str, float]]) -> str:
    """Decide what to do with ranked title candidates.

    Returns:
      - ``"auto"``    — exactly one candidate clears TITLE_MATCH_AUTO (safe to attach)
      - ``"confirm"`` — mid-confidence, or two+ candidates clear AUTO (ambiguous)
      - ``"none"``    — no candidate at/above the floor
    """
    if not candidates:
        return "none"
    top_score = candidates[0][1]
    if top_score >= TITLE_MATCH_AUTO:
        # Unique only if no runner-up is also at the auto bar.
        if len(candidates) == 1 or candidates[1][1] < TITLE_MATCH_AUTO:
            return "auto"
        return "confirm"
    return "confirm"
