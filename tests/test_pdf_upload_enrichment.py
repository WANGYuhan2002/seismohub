"""T27 — PDF upload should prefer online (CrossRef/OpenAlex/S2) metadata
over the raw PyMuPDF first-page scrape, especially when the scraped
"title" is publisher boilerplate (Springer running header, ISSN line,
volume/issue/page banner).

These are pure-function tests on the heuristic + the enrich wrapper.
The end-to-end upload route is covered by smoke tests; here we lock
the detection + mocked-lookup contract.
"""

from __future__ import annotations

from unittest.mock import patch
from types import SimpleNamespace

import pytest


# ---- _looks_like_mangled_title ---------------------------------------------

@pytest.mark.parametrize("text", [
    "Vol.: (0123456789) Geomech. Geophys. Geo-energ. Geo-resour. (2025) 11:60 https://doi.org/10.1007/s40948-025-00972-0 REVIEW",
    "Vol.: (0123456789) some title",
    "Volume: 12 (2024) — running header",
    "ISSN: 0094-8276  GRL Letters",
    "Received: 12 Jan 2024 / Accepted: 4 Feb 2024",
    "https://doi.org/10.1007/s40948-025-00972-0",
    "(2025) 11:60 https://doi.org/...",
    "abc",  # too short
    "",
])
def test_mangled_title_detector_flags_garbage(text):
    from main import _looks_like_mangled_title
    assert _looks_like_mangled_title(text) is True, text


@pytest.mark.parametrize("text", [
    "Directional sensitivity of fibre optic cables for surface microseismic monitoring",
    "A three-dimensional distributed acoustic sensing array",
    "Distributed Acoustic Sensing for Reservoir Monitoring",
    "Channelized lithospheric erosion shapes the western margin of the Tibetan Plateau",
])
def test_mangled_title_detector_accepts_real_titles(text):
    from main import _looks_like_mangled_title
    assert _looks_like_mangled_title(text) is False, text


# ---- _enrich_metadata_from_lookup ------------------------------------------

def _ok_result(fields: dict):
    return SimpleNamespace(status="ok", fields=fields, source="crossref")


def test_enrich_uses_doi_when_available():
    """Given a DOI, ignore the heuristic title entirely and return the
    CrossRef fields."""
    from main import _enrich_metadata_from_lookup

    clean = {
        "title": "Directional sensitivity of fibre optic cables for surface microseismic monitoring",
        "authors": ["Jane Doe", "John Smith"],
        "journal": "Geomechanics and Geophysics for Geo-Energy and Geo-Resources",
        "year": "2025",
        "published_date": "2025-03-15",
        "abstract": "We measure directional sensitivity...",
        "doi": "10.1007/s40948-025-00972-0",
        "link": "https://doi.org/10.1007/s40948-025-00972-0",
    }
    with patch("main.lookup_metadata_by_identifier", return_value=_ok_result(clean)):
        out = _enrich_metadata_from_lookup(
            auto_doi="10.1007/s40948-025-00972-0",
            auto_title="Vol.: (0123456789) garbage https://doi.org/...",
        )
    assert out is not None
    assert out["title"].startswith("Directional sensitivity")
    assert out["authors"] == ["Jane Doe", "John Smith"]
    assert out["journal"].startswith("Geomechanics")


def test_enrich_skips_title_lookup_when_title_is_mangled():
    """No DOI + mangled title → don't even try S2; return None."""
    from main import _enrich_metadata_from_lookup

    with patch("main.lookup_metadata_by_identifier") as mid, \
         patch("main.lookup_metadata_by_title_authors") as mtitle:
        out = _enrich_metadata_from_lookup(
            auto_doi="",
            auto_title="Vol.: (0123456789) Geomech. Geophys. (2025) 11:60",
        )
    assert out is None
    assert not mid.called  # no DOI passed → no DOI lookup
    assert not mtitle.called  # mangled title → no S2 either


def test_enrich_falls_back_to_title_search_when_no_doi():
    """No DOI + clean-looking title → query S2 by title."""
    from main import _enrich_metadata_from_lookup

    clean = {
        "title": "A three-dimensional distributed acoustic sensing array",
        "authors": ["Zhongwen Zhan"],
        "journal": "Geophysical Research Letters",
        "year": "2024",
        "abstract": "We deploy a 3D DAS array...",
        "doi": "10.1029/2024GL...",
    }
    with patch("main.lookup_metadata_by_identifier") as mid, \
         patch("main.lookup_metadata_by_title_authors", return_value=_ok_result(clean)) as mtitle:
        out = _enrich_metadata_from_lookup(
            auto_doi="",
            auto_title="A three-dimensional distributed acoustic sensing array",
        )
    assert out is not None
    assert out["title"].startswith("A three-dimensional")
    assert not mid.called
    assert mtitle.called


def test_enrich_returns_none_when_doi_lookup_fails():
    """DOI lookup returns failed status → fall through to (mangled) title
    check → None."""
    from main import _enrich_metadata_from_lookup

    failed = SimpleNamespace(status="failed", fields={}, source=None)
    with patch("main.lookup_metadata_by_identifier", return_value=failed):
        out = _enrich_metadata_from_lookup(
            auto_doi="10.1234/bogus",
            auto_title="Vol.: (0123456789) garbage",
        )
    assert out is None


def test_enrich_survives_exception_in_lookup():
    """Network error / unexpected exception inside lookup → return None,
    don't crash the caller."""
    from main import _enrich_metadata_from_lookup

    with patch("main.lookup_metadata_by_identifier", side_effect=RuntimeError("boom")):
        out = _enrich_metadata_from_lookup(
            auto_doi="10.1234/example",
            auto_title="Vol.: (0123456789) garbage",  # mangled — no fallback
        )
    assert out is None


# ---- T30: _paper_needs_metadata_refresh ------------------------------------

def test_needs_refresh_flags_mangled_title():
    from main import _paper_needs_metadata_refresh
    from types import SimpleNamespace
    p = SimpleNamespace(
        title="Vol.: (0123456789) Geomech. Geophys. Geo-energ. (2025) 11:60 https://doi.org/x",
        doi="10.1007/s40948-025-00972-0",
        authors=["someone"],  # has authors, but title still mangled → still refresh
    )
    assert _paper_needs_metadata_refresh(p) is True


def test_needs_refresh_flags_doi_with_empty_authors():
    from main import _paper_needs_metadata_refresh
    from types import SimpleNamespace
    # Clean-looking title, has DOI, but authors empty → pre-T27 row,
    # backfill should re-enrich.
    p = SimpleNamespace(
        title="Directional sensitivity of fibre optic cables for surface microseismic monitoring",
        doi="10.1007/s40948-025-00972-0",
        authors=[],
    )
    assert _paper_needs_metadata_refresh(p) is True


def test_needs_refresh_skips_clean_row():
    from main import _paper_needs_metadata_refresh
    from types import SimpleNamespace
    p = SimpleNamespace(
        title="A three-dimensional distributed acoustic sensing array",
        doi="10.1029/2024GL...",
        authors=["Zhongwen Zhan", "Mateeva"],
    )
    assert _paper_needs_metadata_refresh(p) is False


def test_needs_refresh_skips_row_without_doi_and_clean_title():
    """No DOI + clean-looking title + no authors — we still can't help
    much (would force every author-less PDF-only upload through S2),
    but we still flag it as needing refresh so the heuristic title path
    in _enrich_metadata_from_lookup runs at least once.

    The "no_signal" skip in _backfill_mangled_metadata gates the actual
    network call; this predicate just says "consider it".
    """
    from main import _paper_needs_metadata_refresh
    from types import SimpleNamespace
    # Clean title, no DOI, no authors — does NOT trigger refresh because
    # the predicate requires either mangled title OR (doi + empty authors).
    p = SimpleNamespace(
        title="Clean Title",
        doi=None,
        authors=[],
    )
    assert _paper_needs_metadata_refresh(p) is False
