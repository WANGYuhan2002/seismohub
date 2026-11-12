"""Seismology-focused journal title → abbreviation map.

Drives the M1 must-do #3 left floating nav. Each entry in the left bar
renders as ``{first_author} · {journal_abbrev}``. The bulk of the table is
now *derived* from the single source-of-truth registry in
``services/journal_registry.py`` (BRIEF v2 §4) so the abbrev/color map, the
ISSN intake list, and the RSS list can no longer drift; the curated overlay
below only adds the entries the registry does not own — short self-referencing
forms, legacy ISO-4 alias spellings already present in the corpus, preprint
aliases, the medical-ultrasound entries, and the internal placeholders.
Unknown journals fall back to ``_iso4_fallback()`` which strips common
filler words and takes the first letter of each remaining word.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Dict, Tuple

from services.journal_registry import abbrev_color_map as _registry_abbrev_color_map


# Curated overlay — entries the registry does NOT generate but that the corpus
# and the existing tests rely on. Keys are normalized (lowercased, ASCII-folded,
# punctuation removed) so lookups tolerate capitalisation / punctuation drift.
# These are MERGED ON TOP of the registry-derived table in
# ``_build_abbrev_table`` (overlay wins on conflict), so a corpus-specific
# spelling here can refine the auto-derived entry.
_JOURNAL_ABBREVS_OVERLAY: Dict[str, Tuple[str, str]] = {
    # Legacy ISO-4 / alternate spellings that appear in stored `journal`
    # columns but are not the registry's canonical title form.
    "geophys j int": ("GJI", "#0f766e"),
    "bull seismol soc am": ("BSSA", "#7f1d1d"),
    "seismol res lett": ("SRL", "#c2410c"),
    "journal of geophysical research solid earth": ("JGR-SE", "#1e3a8a"),
    "j geophys res solid earth": ("JGR-SE", "#1e3a8a"),
    "jgr solid earth": ("JGR-SE", "#1e3a8a"),
    "j geophys res": ("JGR", "#1e3a8a"),
    "geophys res lett": ("GRL", "#1e3a8a"),
    "earth planet sci lett": ("EPSL", "#0f172a"),
    "phys earth planet inter": ("PEPI", "#5b21b6"),
    "pure appl geophys": ("PAGEOPH", "#0e7490"),
    "j volcanol geotherm res": ("JVGR", "#9d174d"),
    "geochem geophys geosyst": ("G3", "#365314"),
    "leading edge": ("TLE", "#854d0e"),
    "geophysics seg": ("Geophysics", "#1e40af"),
    "nature reviews earth and environment": ("Nat Rev EE", "#111827"),
    # ultrasound / medical (kept verbatim from the pre-registry table).
    "ultrasound in medicine and biology": ("UMB", "#0e7490"),
    # internal placeholder + empty sentinel.
    "others": ("Others", "#475569"),
    "": ("—", "#475569"),
    # Self-referencing short forms — many papers store the abbreviation
    # directly in the `journal` column, so the lookup also accepts it as a key.
    # (The registry already emits a self-ref for each abbrev, but these spell
    # out punctuation variants the normaliser would otherwise miss.)
    "jgr se": ("JGR-SE", "#1e3a8a"),
    "jgr-se": ("JGR-SE", "#1e3a8a"),
    "g-cubed": ("G3", "#365314"),
    "nat rev ee": ("Nat Rev EE", "#111827"),
    # Additional canonical entries for the SWP corpus not in the intake set.
    "biogeosciences": ("Biogeosci", "#365314"),
    "eth zurich conference poster": ("ETH Poster", "#475569"),
}


def _build_abbrev_table() -> Dict[str, Tuple[str, str]]:
    """Compose the lookup table: registry-derived base + curated overlay.

    The registry contributes one entry per canonical title AND one self-ref per
    abbreviation; the overlay then layers on the corpus-specific aliases and
    placeholders above. Overlay entries win on key collision so a hand-tuned
    spelling can refine the auto-derived value.
    """
    table: Dict[str, Tuple[str, str]] = dict(_registry_abbrev_color_map())
    table.update(_JOURNAL_ABBREVS_OVERLAY)
    return table


# Canonical abbreviation table. Built once at import. Kept as a module-level
# ``Dict[str, Tuple[str, str]]`` because ``main.list_canonical_journals`` imports
# it directly to populate the ``/api/journals/canonical`` picker.
_JOURNAL_ABBREVS_RAW: Dict[str, Tuple[str, str]] = _build_abbrev_table()


_PUNCT_RE = re.compile(r"[\s\-_.,:;/\\()\[\]{}'\"!?]+")
_FILLER_WORDS = {
    "the",
    "of",
    "and",
    "a",
    "an",
    "in",
    "on",
    "for",
    "to",
    "international",
    "research",
    "journal",
    "letters",
    "bulletin",
    "transactions",
    "review",
    "reviews",
    "society",
    "advances",
}


def _normalize(value: str) -> str:
    """Lowercase + strip accents + collapse punctuation to spaces."""
    s = unicodedata.normalize("NFKD", value or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# Public alias — `services/sort_keys.journal_sort_key` reuses the same
# canonicalisation as `abbreviate()` so the sort table and the
# abbreviation table cannot drift. Re-exported (not duplicated) per
# the "one canonicaliser" rule in 04-feature-design/08-sort-options.md §4.
canonicalize = _normalize


def _iso4_fallback(journal: str) -> str:
    """ISO-4-ish fallback abbreviation when the canonical map misses.

    Strips filler words then keeps the first three significant tokens.
    Each significant token is capitalised and abbreviated to its first 4
    letters; one-letter tokens stay intact.
    """
    tokens = [t for t in _normalize(journal).split() if t and t not in _FILLER_WORDS]
    if not tokens:
        return "—"
    abbreviated: list[str] = []
    for t in tokens[:3]:
        if len(t) <= 1:
            abbreviated.append(t.upper())
        else:
            abbreviated.append(t[:4].capitalize())
    return " ".join(abbreviated)


@lru_cache(maxsize=2048)
def abbreviate(journal: str | None) -> str:
    """Return the short label for the left nav.

    PERFORMANCE.md follow-up (Fix D): bumped cache from 512 → 2048. The
    sidebar renders ``{first_author} · {abbreviate(journal)}`` for every
    visible paper, and at a 5K-paper library the 512-entry LRU was
    cycling out hot keys on long scrolls.
    """
    if not journal:
        return "—"
    key = _normalize(journal)
    if key in _JOURNAL_ABBREVS_RAW:
        return _JOURNAL_ABBREVS_RAW[key][0]
    return _iso4_fallback(journal)


@lru_cache(maxsize=2048)
def journal_color(journal: str | None) -> str:
    """Return the hex color hint for the journal pip in the left nav."""
    key = _normalize(journal or "")
    if key in _JOURNAL_ABBREVS_RAW:
        return _JOURNAL_ABBREVS_RAW[key][1]
    return "#64748b"  # slate-500


__all__ = ["abbreviate", "journal_color", "canonicalize"]
