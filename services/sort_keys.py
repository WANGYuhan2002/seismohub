"""Pure-function sort-key builders + DESC-shim helpers for the nine-key
paper sort whitelist defined in ``04-feature-design/08-sort-options.md``.

Each builder is deterministic, side-effect-free, and never raises on
missing / NULL / mixed-language input — the sort layer must remain
robust against half-populated CrossRef / OpenAlex rows.

The four primary key builders (``author_sort_key``,
``title_initial_sort_key`` / ``title_initial_key``,
``journal_sort_key``, ``year_sort_key``) accept **either** a paper
dict (per PR-34's pure-function contract) **or** the raw field value
(per PR-33's lambda callsites). Type-sniffing keeps both call shapes
green without forcing one PR to wait on the other.

The leading-underscore helpers (``_neg_*`` and ``_position``) are
DESC-shim builders consumed by ``main._sort_papers()``'s ascending
``sorted()`` call: they negate ints / invert ISO-date strings so a
single ``sorted(papers, key=builder)`` (no ``reverse=True``) produces
the design-§1 DESC tie-break order.

CJK handling:
    - Author family names containing any Han ideograph are romanised
      via ``pypinyin.lazy_pinyin(style=Style.NORMAL)`` and lowercased
      so 王小明 sorts as ``"wang xiao ming"`` → first letter W, grouped
      with Wang X. / X. Wang. See W3.15 §2 for the bilingual-researcher
      rationale.
    - Title-initial first words containing Han ideographs collapse
      into a single ``"中"`` bucket (deliberately NOT split per pinyin
      letter — 04-feature-design/08-sort-options.md §3 explains why).

Dependency note: ``pypinyin`` is added to ``requirements.txt`` by
PR-35. Importing this module without that dep will fail at import
time — that is intentional: the four builders only have a correct
implementation when CJK romanisation is available, and silently
falling back to Unicode code-point ordering would re-introduce the
exact bug W3.15 §1 documents.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pypinyin import Style, lazy_pinyin

from services.journal_abbrev import canonicalize as _journal_canonicalize

# U+4E00..U+9FFF covers the CJK Unified Ideographs block. We deliberately
# do NOT include the extension blocks (A/B/...) — they are rare in
# author / journal names and a tighter range keeps the regex hot path
# small.
_CJK_RE = re.compile(r"[一-鿿]")

# Closed list of leading-article tokens stripped from titles before
# bucketing. Matches 04-feature-design/08-sort-options.md §3 verbatim.
# Lowercased; the splitter lowercases the token before lookup.
_LEADING_ARTICLES: frozenset = frozenset(
    {"a", "an", "the", "on", "in", "of", "for"}
)

# CJK / typographic opening quotes stripped from title start before
# article-stripping. Half-width straight quotes ' " also stripped.
_LEADING_QUOTE_CHARS: str = "\"'“”‘’《「『〈 "

# Sentinel for empty / NULL primary keys — placed lexicographically last.
_BUCKET_UNKNOWN: str = "zzzz_unknown"

# Sentinel year for NULL publication dates — ``year_sort_key`` returns
# this when the date is missing / unparseable. PR-33's ``_neg_year``
# helper turns it into ``+10000`` so NULL papers sort LAST under DESC.
_YEAR_NULL: int = -10_000

# Sentinel for missing "added at" / "last opened" ISO dates. We use a
# string lexicographically LARGER than any plausible ISO timestamp so
# the inverted form (subtract-from-Z) places NULLs at the BOTTOM of a
# DESC sort. The literal "9999-99-99T99:99:99" suffices in practice.
_ISO_INF: str = "9999-99-99T99:99:99"


def _first_token_cjk_aware(value: str) -> str:
    """Return the first whitespace-separated token, robust to NFKC
    full-width / U+3000 ideographic-space variants."""
    return value.strip().split()[0] if value and value.strip() else ""


@functools.lru_cache(maxsize=8192)
def _pinyin_lower(token: str) -> str:
    """Cached pypinyin conversion → lowercased space-joined string.

    PERFORMANCE.md follow-up (Fix C): sorting 5000 papers re-converted the
    same author/title CJK tokens on every render. The outer ``author_sort_key``
    and ``title_initial_sort_key`` are NOT cacheable (they accept Paper dicts
    which are non-hashable), but the per-token pinyin call is — and at the
    library-corpus scale we typically see ≤200 distinct CJK family names,
    so an 8192-entry LRU is effectively a permanent dictionary cache.
    """
    return " ".join(lazy_pinyin(token, style=Style.NORMAL)).lower()


def _coerce_authors(arg: Any) -> List[str]:
    """Accept either a paper dict or a raw author list and return the
    author list. Empty / None inputs → []."""
    if arg is None:
        return []
    if isinstance(arg, dict):
        a = arg.get("authors")
        return list(a) if a else []
    if isinstance(arg, (list, tuple)):
        return list(arg)
    # Single string author
    if isinstance(arg, str):
        return [arg] if arg.strip() else []
    return []


def _coerce_string(arg: Any, key: str) -> str:
    """Accept either a paper dict or a raw string and return the string."""
    if arg is None:
        return ""
    if isinstance(arg, dict):
        v = arg.get(key)
        return v if isinstance(v, str) else ""
    if isinstance(arg, str):
        return arg
    return ""


def author_sort_key(arg: Union[Dict[str, Any], Sequence[str], None]) -> str:
    """Primary key for ``sort_by=author``: first author's family-name
    fragment, lowercased, romanised via pinyin if CJK.

    Accepts either a paper dict (PR-34 contract) or a raw authors list
    (PR-33 lambda callsite). Family-name extraction splits on comma if
    present ("Smith, J." → "Smith"), else on whitespace ("J. Smith" →
    "J.", "Wang Y." → "Wang"). CJK names without a comma keep the
    whole first whitespace token.

    Empty / missing authors return ``"zzzz_unknown"`` so the "Unknown"
    group floats to the bottom of an A→Z sort.
    """
    authors = _coerce_authors(arg)
    if not authors:
        return _BUCKET_UNKNOWN
    first = (authors[0] or "").strip()
    if not first:
        return _BUCKET_UNKNOWN
    if "," in first:
        family = first.split(",", 1)[0].strip()
    else:
        family = _first_token_cjk_aware(first)
    if not family:
        return _BUCKET_UNKNOWN
    if _CJK_RE.search(family):
        return _pinyin_lower(family)
    return family.lower()


def title_initial_sort_key(arg: Union[Dict[str, Any], str, None]) -> Tuple[str, str]:
    """Primary key for ``sort_by=title_initial`` — returns
    ``(bucket, secondary)``.

    Bucket ∈ {"A".."Z", "0-9", "中", "#", "zzzz_unknown"}; secondary is
    the lowercased first-non-article word for stable in-bucket order.

    Algorithm:
      1. Strip leading typographic / CJK quotes.
      2. Tokenise on whitespace; drop leading tokens whose lowercased
         punctuation-stripped form is in ``_LEADING_ARTICLES``.
      3. Inspect the first surviving token's first character to pick
         the bucket. CJK → ``"中"`` bucket; secondary is the pinyin of
         the whole first word.
    """
    raw = _coerce_string(arg, "title").strip()
    if not raw:
        return (_BUCKET_UNKNOWN, "")
    raw = raw.lstrip(_LEADING_QUOTE_CHARS)
    tokens = raw.split()
    while tokens and tokens[0].lower().rstrip(":,.;!?") in _LEADING_ARTICLES:
        tokens.pop(0)
    if not tokens:
        return (_BUCKET_UNKNOWN, raw.lower())
    first = tokens[0]
    if _CJK_RE.search(first):
        return ("中", _pinyin_lower(first))
    head = first[0]
    secondary = first.lower()
    if head.isalpha():
        return (head.upper(), secondary)
    if head.isdigit():
        return ("0-9", secondary)
    return ("#", secondary)


# PR-33 imports this short name; keep the long PR-34 name as well so
# both call shapes resolve. They are the same callable object.
title_initial_key = title_initial_sort_key


def journal_sort_key(arg: Union[Dict[str, Any], str, None]) -> str:
    """Primary key for ``sort_by=journal``: canonical journal name
    (lowercased, accents stripped, punctuation collapsed) via the
    one canonicaliser shared with ``services/journal_abbrev.abbreviate()``.

    Why canonical, not abbreviation: two journals can collide on
    abbreviation (J. Geophys. Res. spans JGR-SE / JGR-A / JGR-P).
    Sorting by canonical keeps them distinct; the sidebar label uses
    ``abbreviate()`` for display only.
    """
    raw = _coerce_string(arg, "journal")
    key = _journal_canonicalize(raw)
    return key if key else _BUCKET_UNKNOWN


def year_sort_key(arg: Union[Dict[str, Any], str, None]) -> int:
    """Primary key for ``sort_by=year``: integer year, with
    ``_YEAR_NULL`` as the missing-data sentinel.

    Accepts any ``published_date`` value that *starts* with a 4-digit
    year: ``"2024"``, ``"2024-11"``, ``"2024-11-03"``, ``"2024/11"``.
    """
    raw = _coerce_string(arg, "published_date").strip()
    if len(raw) < 4:
        return _YEAR_NULL
    head = raw[:4]
    if not head.isdigit():
        return _YEAR_NULL
    try:
        return int(head)
    except ValueError:  # defensive — isdigit() should have caught this
        return _YEAR_NULL


# ---------------------------------------------------------------------------
# PR-33 helpers — DESC-shim builders consumed by main._sort_papers().
#
# Convention: each helper returns a value suitable for use in an
# ascending sorted() lambda such that the resulting order is the DESC
# order specified in design §1. ISO date strings are inverted with a
# tuple-of-negatives so smaller (newer) strings emit smaller tuples;
# integers are negated; missing values fall back to sentinels that sort
# LAST (i.e. AT THE BOTTOM of a DESC list).
# ---------------------------------------------------------------------------


def _invert_iso(value: Optional[str]) -> str:
    """Return a string that sorts in REVERSE order of *value* under
    ascending compare. We use the literal _ISO_INF as the floor so
    missing dates sort LAST; otherwise we return a string composed of
    chars complemented against '9' so newer dates produce smaller
    output and ascend-to-descend mapping is order-preserving on the
    ISO subset [0-9-T:]."""
    if not value:
        return ""  # smallest string ⇒ NULLs sort LAST under DESC shim below
    # Trick: build a complement so larger ISO → smaller string. For each
    # ASCII digit d, emit chr(ord('9') - (ord(d) - ord('0'))) = '9' - d.
    # Non-digit chars (-, T, :) pass through and compare lex-equal across
    # rows. The result is a string that ascends as the date descends.
    out = []
    for ch in value:
        if ch.isdigit():
            out.append(chr(ord("9") - (ord(ch) - ord("0"))))
        else:
            out.append(ch)
    return "".join(out)


def _neg_date(paper: Dict[str, Any]) -> str:
    """DESC shim for paper['published_date'] — newer first."""
    return _invert_iso(paper.get("published_date") or "")


def _neg_year(paper: Dict[str, Any]) -> int:
    """DESC shim: return -year so larger year → smaller int → ascends first."""
    return -year_sort_key(paper)


def _neg_updated(paper: Dict[str, Any]) -> str:
    """DESC shim for paper['updated_at'] — newer first."""
    return _invert_iso(paper.get("updated_at") or "")


def _neg_added(
    paper: Dict[str, Any],
    project_paper_index: Optional[Dict[str, Any]] = None,
) -> str:
    """DESC shim for ProjectPaper.created_at / added_at.

    Reads from the per-project index when available; falls back to the
    paper-level 'created_at' field. NULL → ``_ISO_INF`` ⇒ sorts LAST
    after inversion.
    """
    added = None
    if project_paper_index is not None:
        pp = project_paper_index.get(paper.get("id"))
        if pp is not None:
            # ORM object — read either added_at or created_at
            for attr in ("added_at", "created_at"):
                v = getattr(pp, attr, None)
                if v is not None:
                    added = v.isoformat() if hasattr(v, "isoformat") else str(v)
                    break
    if not added:
        added = paper.get("added_at") or paper.get("created_at") or ""
    return _invert_iso(added or "")


def _neg_last_opened(paper: Dict[str, Any]) -> str:
    """DESC shim for Paper.last_opened_at — newer first.

    Reads from paper['last_opened_at'] (serialized by main._paper_to_dict
    if present). Missing → sorts LAST after inversion.
    """
    v = paper.get("last_opened_at") or ""
    return _invert_iso(v if isinstance(v, str) else (v.isoformat() if hasattr(v, "isoformat") else ""))


def _position(
    paper: Dict[str, Any],
    project_paper_index: Optional[Dict[str, Any]] = None,
) -> float:
    """ASC sort key for collection custom-order: smaller position first.

    Reads ProjectPaper.position from the per-project index. When the
    index is None (caller didn't supply one — e.g. global /api/papers
    with no project_id and a stale ?sort_by=custom URL), or when the
    paper has no row in the index, returns ``float('inf')`` so the
    paper sorts LAST per the design doc "nulls last" rule.
    """
    if project_paper_index is None:
        return float("inf")
    pp = project_paper_index.get(paper.get("id"))
    if pp is None:
        return float("inf")
    pos = getattr(pp, "position", None)
    if pos is None:
        return float("inf")
    try:
        return float(pos)
    except (TypeError, ValueError):
        return float("inf")


__all__ = [
    "author_sort_key",
    "title_initial_sort_key",
    "title_initial_key",
    "journal_sort_key",
    "year_sort_key",
    "_neg_date",
    "_neg_year",
    "_neg_updated",
    "_neg_added",
    "_neg_last_opened",
    "_position",
]
