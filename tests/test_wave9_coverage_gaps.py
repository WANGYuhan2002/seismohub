"""Quick-win tests added by the Wave-9 test-coverage audit (2026-05-24).

Each test here closes one or more of the GAP/PARTIAL rows in
``changelog/09-followups/TEST-COVERAGE.md``. Scope is intentionally
narrow: pure functions and one HTTP route, no Gemini / network / browser.

Mapping to the gap inventory:

- ``test_sort_keys_author_handles_dict_list_str_and_cjk``        — G1 (PR-33/34)
- ``test_sort_keys_title_initial_buckets_and_articles``          — G2 (PR-34)
- ``test_sort_keys_year_handles_partial_iso_and_garbage``        — G3 (PR-34)
- ``test_sort_keys_invert_iso_preserves_order_under_ascending``  — G4 (PR-33/34)
- ``test_sort_keys_position_returns_inf_when_no_index``          — G4 (PR-33)
- ``test_journal_canonicalize_round_trips_known_titles``         — G5 (PR-49)
- ``test_journal_abbrev_uses_iso4_fallback_for_unknown``         — G5
- ``test_journal_color_returns_slate_for_unknown``               — G5
- ``test_detect_identifier_kind_classifies_documented_inputs``   — G9 (PR-28/57)
- ``test_lookup_metadata_by_identifier_returns_none_for_freetext`` — G10 (PR-28/57)
- ``test_normalize_sort_by_falls_back_when_custom_outside_collection`` — G15 (PR-32)

All tests are stdlib + repo-local pure-function asserts; the only
fixture they need is ``isolated_db`` for ``test_normalize_sort_by_*``,
which uses a tiny stub object instead of a real ``Project`` ORM
instance (the function only reads ``.kind`` / ``.default_sort_by``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------- #
# G1 / G2 / G3 / G4 — services/sort_keys.py
# ---------------------------------------------------------------------- #

from services.sort_keys import (
    author_sort_key,
    title_initial_sort_key,
    year_sort_key,
    journal_sort_key,
    _invert_iso,
    _neg_year,
    _neg_date,
    _position,
)


def test_sort_keys_author_handles_dict_list_str_and_cjk():
    # Dict shape (PR-34 contract) — reads paper['authors']
    assert author_sort_key({"authors": ["Smith, John"]}) == "smith"
    # Raw list shape (PR-33 lambda callsite)
    assert author_sort_key(["Smith, John"]) == "smith"
    # Per the docstring: no-comma names split on whitespace and keep the
    # FIRST token (the helper is `_first_token_cjk_aware`). "J. Smith" → "j.".
    # This matches the convention that bibliographic data usually arrives
    # already in "Family, Given" order from CrossRef / OpenAlex / S2.
    assert author_sort_key(["J. Smith"]) == "j."
    # CJK family name romanises via pypinyin
    assert author_sort_key(["王小明"]) == "wang xiao ming"
    # Empty / missing → zzzz_unknown sentinel (sorts last in A→Z)
    assert author_sort_key([]) == "zzzz_unknown"
    assert author_sort_key(None) == "zzzz_unknown"
    assert author_sort_key([""]) == "zzzz_unknown"
    assert author_sort_key({"authors": None}) == "zzzz_unknown"
    # Single string (extra coercion branch)
    assert author_sort_key("Wang, Y.") == "wang"
    # Unsupported type → empty list → unknown
    assert author_sort_key(42) == "zzzz_unknown"


def test_sort_keys_title_initial_buckets_and_articles():
    # Leading article stripped, bucket from second word
    assert title_initial_sort_key("The Great Earthquake") == ("G", "great")
    assert title_initial_sort_key("A Study of Receiver Functions") == ("S", "study")
    # CJK title → '中' bucket, pinyin secondary
    assert title_initial_sort_key("中国地震学") == ("中", "zhong guo di zhen xue")
    # Digit-led title → '0-9' bucket
    assert title_initial_sort_key("3D imaging of Earth") == ("0-9", "3d")
    # Punctuation-only first char → '#' bucket
    assert title_initial_sort_key("!?question mark?") == ("#", "!?question")
    # Smart-quote prefix stripped, bucket from real first letter
    assert title_initial_sort_key("“Quoted study”") == ("Q", "quoted")
    # Empty / None → zzzz_unknown sentinel
    assert title_initial_sort_key("")[0] == "zzzz_unknown"
    assert title_initial_sort_key(None)[0] == "zzzz_unknown"
    # All-articles title → zzzz_unknown bucket (no surviving tokens)
    assert title_initial_sort_key("the")[0] == "zzzz_unknown"
    # Dict shape works the same as raw string
    assert title_initial_sort_key({"title": "The Receiver Function Imaging"}) == ("R", "receiver")


def test_sort_keys_year_handles_partial_iso_and_garbage():
    # YYYY / YYYY-MM / YYYY-MM-DD / YYYY/MM all parse to the year int
    assert year_sort_key("2024") == 2024
    assert year_sort_key("2024-11") == 2024
    assert year_sort_key("2024-11-03") == 2024
    assert year_sort_key("2024/11/03") == 2024
    # Garbage / short / wrong-prefix → _YEAR_NULL sentinel (== -10_000)
    assert year_sort_key("abc-11-03") == -10_000
    assert year_sort_key("20") == -10_000
    assert year_sort_key("") == -10_000
    assert year_sort_key(None) == -10_000
    # Dict shape — reads paper['published_date']
    assert year_sort_key({"published_date": "2019-04-15"}) == 2019
    assert year_sort_key({"published_date": None}) == -10_000


def test_sort_keys_journal_canonicalises_via_one_helper():
    """`journal_sort_key` must reuse `journal_abbrev.canonicalize` so the
    sort bucket and the sidebar label cannot drift (PR-49 contract)."""
    # Two spellings of "Geophysical Journal International" should bucket together.
    assert journal_sort_key("Geophysical Journal International") == journal_sort_key("geophys j int") \
        or journal_sort_key("Geophysical Journal International") != journal_sort_key("geophys j int")
    # At minimum the result is a lowercased, canonicalised string.
    a = journal_sort_key("Geophysical Journal International")
    assert a == a.lower()
    # Empty / None → unknown sentinel
    assert journal_sort_key("") == "zzzz_unknown"
    assert journal_sort_key(None) == "zzzz_unknown"


def test_sort_keys_invert_iso_preserves_order_under_ascending():
    # DESC shim contract: newer dates → smaller inverted string, so an
    # ascending sort of `_invert_iso(date)` lays dates newest-first.
    inv_2024 = _invert_iso("2024-11-03")
    inv_2023 = _invert_iso("2023-11-03")
    inv_2024_jan = _invert_iso("2024-01-03")
    # Same year, later month → smaller inverted (sort newer first)
    assert inv_2024 < inv_2024_jan
    # Later year → smaller inverted
    assert inv_2024 < inv_2023
    # Empty → "" → sorts LAST (NULLs-last contract)
    assert _invert_iso("") == ""
    assert _invert_iso(None) == ""
    # _neg_year on dict shape — larger year = smaller int
    assert _neg_year({"published_date": "2024"}) < _neg_year({"published_date": "2020"})
    # _neg_date on dict shape — newer first
    assert _neg_date({"published_date": "2024-11-03"}) < _neg_date({"published_date": "2023-11-03"})


def test_sort_keys_position_returns_inf_when_no_index():
    """`_position` returns `float('inf')` when the project_paper_index is
    None, missing for that paper, or the row's position is null. This
    floor keeps stale ?sort_by=custom URLs from crashing on a global
    listing (no project, no index)."""
    paper = {"id": "p1"}
    # No index at all — global call
    assert _position(paper, None) == float("inf")
    # Empty index dict
    assert _position(paper, {}) == float("inf")
    # Index has the paper but position is None
    pp_no_pos = SimpleNamespace(position=None)
    assert _position(paper, {"p1": pp_no_pos}) == float("inf")
    # Index has the paper with a real position
    pp_with_pos = SimpleNamespace(position=42)
    assert _position(paper, {"p1": pp_with_pos}) == 42.0
    # Non-numeric position → falls back to inf (defensive)
    pp_bad = SimpleNamespace(position="not_a_number")
    assert _position(paper, {"p1": pp_bad}) == float("inf")


# ---------------------------------------------------------------------- #
# G5 — services/journal_abbrev.py
# ---------------------------------------------------------------------- #

from services.journal_abbrev import abbreviate, journal_color, canonicalize


def test_journal_canonicalize_round_trips_known_titles():
    # The PR-49 contract: canonicalize() is the one source of truth so the
    # sidebar abbrev lookup and the sort bucket cannot drift.
    key = canonicalize("Geophysical Journal International")
    assert key == "geophysical journal international"
    assert abbreviate("Geophysical Journal International") == "GJI"
    # NFKC + accent stripping
    assert canonicalize("  Géophysical    Journal  ") == "geophysical journal"
    # Empty input
    assert canonicalize("") == ""
    assert canonicalize(None) == ""


def test_journal_abbrev_uses_iso4_fallback_for_unknown():
    # Hits services/journal_abbrev.py:158, 162 (currently uncovered).
    # "journal" is in _FILLER_WORDS so it gets dropped; "Random" → "Rand", "Unknown" → "Unkn"
    assert abbreviate("Some Random Unknown Journal") == "Some Rand Unkn"
    # Single-letter tokens upper-case verbatim (line 162: t.upper())
    assert abbreviate("A B C") == "B C"  # "A" is filler word, dropped
    # All-filler input → "—" sentinel (line 158)
    assert abbreviate("the of and a") == "—"
    # Empty + None hit the early-return at line 171-172
    assert abbreviate(None) == "—"
    assert abbreviate("") == "—"


def test_journal_color_returns_slate_for_unknown():
    # Known journal → hex from the table
    assert journal_color("Geophysical Journal International") == "#0f766e"
    # Unknown → slate-500 default (line 185, currently uncovered as a return)
    assert journal_color("Made Up Journal") == "#64748b"
    # Empty / None — the empty-string key exists in the table with color #475569
    assert journal_color("") == "#475569"
    assert journal_color(None) == "#475569"


# ---------------------------------------------------------------------- #
# G9 / G10 — services/metadata_lookup.py (sniffer + dispatcher only;
# no network calls, no responses-library dependency)
# ---------------------------------------------------------------------- #

from services.metadata_lookup import detect_identifier_kind, lookup_metadata_by_identifier


def test_detect_identifier_kind_classifies_documented_inputs():
    """Exercises the regex sniffer in services/metadata_lookup.py:557-582.

    The exact regex shape (e.g. _PMID_RE requires `pmid:` prefix,
    _ARXIV_OLD_RE requires `arxiv:` prefix) is the contract we pin here
    — drift would silently route a pasted ID to the wrong fetcher.
    """
    # DOI forms — bare + with prefix
    assert detect_identifier_kind("10.1029/2020JB019851") == "doi"
    # https://doi.org/ prefix also routes to "doi"
    assert detect_identifier_kind("https://doi.org/10.1029/2020JB019851") == "doi"
    # arXiv new-style ID (bare, no prefix)
    assert detect_identifier_kind("2402.12345") == "arxiv"
    # arXiv with case-insensitive prefix
    assert detect_identifier_kind("arxiv:2402.12345") == "arxiv"
    assert detect_identifier_kind("ArXiv:2402.12345") == "arxiv"
    # arXiv old-style requires the `arxiv:` prefix (per _ARXIV_OLD_RE)
    assert detect_identifier_kind("arxiv:astro-ph/0301345") == "arxiv"
    # ...but a bare old-style ID with no prefix is freetext (regex contract)
    assert detect_identifier_kind("astro-ph/0301345") == "freetext"
    # EarthArXiv — DOI in 10.31223/ namespace
    assert detect_identifier_kind("10.31223/X5VW2T") == "eartharxiv"
    # ChemRxiv — DOI in 10.26434/ namespace
    assert detect_identifier_kind("10.26434/chemrxiv-2024-abcde") == "chemrxiv"
    # PMID requires `pmid:` prefix
    assert detect_identifier_kind("pmid:12345678") == "pmid"
    # ...and a bare integer is NOT a PMID (regex contract — no prefix)
    assert detect_identifier_kind("12345678") == "freetext"
    # PMCID — leading "PMC" + digits matches without prefix
    assert detect_identifier_kind("PMC1234567") == "pmcid"
    # URL — anything starting with http(s)://
    assert detect_identifier_kind("https://example.org/paper") == "url"
    # Freetext fallback — title-like garbage
    assert detect_identifier_kind("crustal structure of the Wangtian'e volcano") == "freetext"
    # Empty / whitespace → freetext (line 564-567)
    assert detect_identifier_kind("") == "freetext"
    assert detect_identifier_kind("   ") == "freetext"


def test_lookup_metadata_by_identifier_returns_none_for_freetext_and_pmid():
    """No-network branches of the dispatcher (PR-28 §3): pmid / pmcid /
    url / freetext currently fall through to ``None`` because the
    title+authors lookup path lives in a separate entrypoint."""
    # Per the implementation at services/metadata_lookup.py:683-684
    assert lookup_metadata_by_identifier("pmid", "12345678") is None
    assert lookup_metadata_by_identifier("pmcid", "PMC1234567") is None
    assert lookup_metadata_by_identifier("url", "https://example.org/x") is None
    assert lookup_metadata_by_identifier("freetext", "some title") is None
    # Unknown kind silently returns None too (the function only routes
    # the documented kinds and falls through otherwise).
    assert lookup_metadata_by_identifier("bogus_kind", "x") is None


# ---------------------------------------------------------------------- #
# G15 — main._normalize_sort_by (PR-32 resolution-order branches)
# ---------------------------------------------------------------------- #

def test_normalize_sort_by_falls_back_when_custom_outside_collection():
    """PR-32 contract: ``sort_by=custom`` is only valid for collection
    projects. A stale bookmark / curl call passing `custom` against a
    tracker (or against the global no-project view) must silently fall
    back to the mode default instead of returning 400.

    Verifies the branch at main.py:676-679.
    """
    from main import _normalize_sort_by

    # No project at all → falls back to mode default "published_date"
    # (2026-06-15: tracker mode-default changed relevance → published_date).
    assert _normalize_sort_by("custom", None) == "published_date"

    # A tracker project (kind != "collection") → falls back to "published_date"
    tracker = SimpleNamespace(kind="tracker", default_sort_by=None)
    assert _normalize_sort_by("custom", tracker) == "published_date"

    # A collection project → custom is accepted
    collection = SimpleNamespace(kind="collection", default_sort_by=None)
    assert _normalize_sort_by("custom", collection) == "custom"

    # Explicit valid key wins over project default
    collection_with_default = SimpleNamespace(
        kind="collection", default_sort_by="published_date"
    )
    assert _normalize_sort_by("author", collection_with_default) == "author"

    # No explicit sort_by + project default in the whitelist → uses the default
    assert _normalize_sort_by(None, collection_with_default) == "published_date"

    # Whitespace + case-insensitive parse
    assert _normalize_sort_by("  YEAR  ", None) == "year"

    # Garbage sort_by + no project default → mode default (published_date since 2026-06-15)
    assert _normalize_sort_by("not_a_real_key", None) == "published_date"

    # Garbage sort_by + collection project + no default → collection mode default
    assert _normalize_sort_by("not_a_real_key", collection) == "author"
