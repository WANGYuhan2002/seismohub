"""New explicit-follow fetcher tests (BRIEF v2 §2).

Covers ``services.fetch_service``'s two NEW targeted fetchers and the
predicate->source planner, with ALL network mocked via the ``responses``
library (no live arXiv / OpenAlex calls):

* ``fetch_from_arxiv``           — parses the arXiv Atom feed into Paper-dicts;
  results carry ``explicit_follow=True`` and ``arxiv_category``; the DESC sort
  stop-on-older-than-date_from paging is honoured; dedup via the shared ``seen``
  set works.
* ``fetch_from_openalex_by_author`` — resolves an author id vs a free-text name
  into the right OpenAlex filter, parses works (authors / institutions /
  journal / abstract) into Paper-dicts with ``explicit_follow=True``, dedups.
* ``derive_sources_from_predicates`` — maps a tracker's SOURCE predicates
  (author / arxiv_category / journal) into the intake plan and flags pure
  explicit-follow trackers (pure-Python, no network).

The arXiv throttle (3s real sleep) is neutralised by monkeypatching
``time.sleep`` so the suite stays fast.
"""

from __future__ import annotations

import datetime as dt

import pytest
import responses as responses_lib

from services import fetch_service as fs


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Neutralise the arXiv 3-second ToU throttle + inter-call sleeps so the
    mocked fetchers run instantly."""
    monkeypatch.setattr(fs.time, "sleep", lambda *_a, **_k: None)
    # Reset the module-level throttle timestamp so test ordering can't make the
    # first call sleep.
    fs._last_arxiv_fetch_ts[0] = 0.0


def _arxiv_atom(entries_xml: str) -> str:
    """Wrap entry XML in a minimal arXiv Atom feed envelope."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom">\n'
        f"{entries_xml}\n"
        "</feed>"
    )


def _arxiv_entry(
    *,
    arxiv_id: str,
    title: str,
    summary: str,
    published: str,
    authors: list[str],
    primary_cat: str = "physics.geo-ph",
    doi: str | None = None,
) -> str:
    authors_xml = "".join(f"<author><name>{a}</name></author>" for a in authors)
    doi_xml = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    return f"""
    <entry>
      <id>http://arxiv.org/abs/{arxiv_id}</id>
      <title>{title}</title>
      <summary>{summary}</summary>
      <published>{published}</published>
      <updated>{published}</updated>
      {authors_xml}
      <arxiv:primary_category term="{primary_cat}" />
      <link rel="alternate" href="http://arxiv.org/abs/{arxiv_id}" />
      {doi_xml}
    </entry>"""


# ---------------------------------------------------------------------------
# fetch_from_arxiv
# ---------------------------------------------------------------------------


@responses_lib.activate
def test_fetch_from_arxiv_parses_entries_into_paper_dicts():
    body = _arxiv_atom(
        _arxiv_entry(
            arxiv_id="2406.00001",
            title="Distributed acoustic sensing of slow slip",
            summary="A long abstract " * 30,
            published="2024-06-10T00:00:00Z",
            authors=["Ada Lovelace", "Alan Turing"],
            primary_cat="physics.geo-ph",
            doi="10.1234/arxiv.2406.00001",
        )
    )
    responses_lib.add(
        responses_lib.GET, fs.ARXIV_API_URL, body=body, status=200,
        content_type="application/atom+xml",
    )

    seen: set[str] = set()
    papers = fs.fetch_from_arxiv(
        date_from=dt.date(2024, 6, 1),
        date_to=dt.date(2024, 6, 30),
        seen=seen,
        categories=["physics.geo-ph"],
    )

    assert len(papers) == 1
    p = papers[0]
    assert p["source"] == "arxiv"
    assert p["journal"] == "arXiv"
    assert p["title"] == "Distributed acoustic sensing of slow slip"
    assert p["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert p["date"] == "2024-06-10"
    assert p["doi"] == "10.1234/arxiv.2406.00001"
    # Explicit follow: bypass the domain gate at ingest.
    assert p["explicit_follow"] is True
    assert p["arxiv_category"] == "physics.geo-ph"
    assert p["categories"] == ["physics.geo-ph"]
    # The paper id was registered in the shared dedup set.
    assert p["id"] in seen


@responses_lib.activate
def test_fetch_from_arxiv_excludes_out_of_window_and_stops_paging():
    """An entry older than date_from is excluded AND sets the stop flag, which
    halts further PAGING. The single mocked page is registered once; because
    paging stops, no second page request is issued (responses would error on an
    unmatched extra call). Entries outside [date_from, date_to] never appear."""
    body = _arxiv_atom(
        _arxiv_entry(
            arxiv_id="2406.10000",
            title="In window recent paper",
            summary="abstract text " * 30,
            published="2024-06-20T00:00:00Z",
            authors=["A. Author"],
        )
        + _arxiv_entry(
            arxiv_id="2407.00001",
            title="After date_to (excluded)",
            summary="abstract text " * 30,
            published="2024-07-05T00:00:00Z",  # after date_to -> skipped
            authors=["D. Author"],
        )
        + _arxiv_entry(
            arxiv_id="2401.99999",
            title="Too old paper",
            summary="abstract text " * 30,
            published="2024-01-05T00:00:00Z",  # before date_from -> stop paging
            authors=["B. Author"],
        )
    )
    # A SMALL page (3 < page_size) also makes the loop break after one page; we
    # register exactly one response so an unexpected second call would fail.
    responses_lib.add(responses_lib.GET, fs.ARXIV_API_URL, body=body, status=200)

    seen: set[str] = set()
    papers = fs.fetch_from_arxiv(
        date_from=dt.date(2024, 6, 1),
        date_to=dt.date(2024, 6, 30),
        seen=seen,
    )
    titles = [p["title"] for p in papers]
    assert titles == ["In window recent paper"]
    assert "Too old paper" not in titles
    assert "After date_to (excluded)" not in titles
    # Exactly one HTTP call was made (paging stopped / single short page).
    assert len(responses_lib.calls) == 1


@responses_lib.activate
def test_fetch_from_arxiv_dedups_against_seen():
    body = _arxiv_atom(
        _arxiv_entry(
            arxiv_id="2406.00002",
            title="A DAS paper",
            summary="abstract " * 30,
            published="2024-06-10T00:00:00Z",
            authors=["A. Author"],
            doi="10.5555/dup",
        )
    )
    responses_lib.add(responses_lib.GET, fs.ARXIV_API_URL, body=body, status=200)

    # Pre-seed 'seen' with the id this entry will hash to. Post-P1-1 the dedup
    # key is source-INDEPENDENT (normalized DOI), so the same paper dedups across
    # sources — mirror that here via dedup_key instead of the old source-prefixed
    # stable_id("arxiv", ...). See _audit_2026-06-11/05-CLEANUP-EXECUTED.md.
    pre_id = fs.dedup_key(doi="10.5555/dup")
    seen = {pre_id}
    papers = fs.fetch_from_arxiv(
        date_from=dt.date(2024, 6, 1),
        date_to=dt.date(2024, 6, 30),
        seen=seen,
    )
    assert papers == []  # already seen -> skipped


@responses_lib.activate
def test_fetch_from_arxiv_network_error_returns_empty_not_raise():
    responses_lib.add(responses_lib.GET, fs.ARXIV_API_URL, status=503)
    papers = fs.fetch_from_arxiv(
        date_from=dt.date(2024, 6, 1),
        date_to=dt.date(2024, 6, 30),
        seen=set(),
    )
    assert papers == []


def test_fetch_from_arxiv_empty_categories_returns_empty():
    # No network expected when there are no categories to query.
    assert fs.fetch_from_arxiv(
        date_from=dt.date(2024, 6, 1),
        date_to=dt.date(2024, 6, 30),
        seen=set(),
        categories=[],
    ) == []


def test_parse_arxiv_datetime_variants():
    d = fs._parse_arxiv_datetime("2024-01-31T12:00:00Z")
    assert d is not None
    assert (d.year, d.month, d.day) == (2024, 1, 31)
    assert d.tzinfo is not None  # aware UTC
    # date-only fallback
    d2 = fs._parse_arxiv_datetime("2024-01-31")
    assert d2 is not None and d2.date() == dt.date(2024, 1, 31)
    # junk -> None
    assert fs._parse_arxiv_datetime("not-a-date") is None
    assert fs._parse_arxiv_datetime("") is None


# ---------------------------------------------------------------------------
# fetch_from_openalex_by_author
# ---------------------------------------------------------------------------


def test_normalize_openalex_author_filter_id_vs_name():
    # bare id
    assert fs._normalize_openalex_author_filter("A5023888391") == (
        "author.id", "A5023888391",
    )
    # full openalex URL -> id
    assert fs._normalize_openalex_author_filter(
        "https://openalex.org/A5023888391"
    ) == ("author.id", "A5023888391")
    # free-text name -> raw_author_name.search
    key, val = fs._normalize_openalex_author_filter("Andreas Fichtner")
    assert key == "raw_author_name.search"
    assert val == "Andreas Fichtner"


@responses_lib.activate
def test_fetch_from_openalex_by_author_parses_work():
    work = {
        "title": "Adjoint tomography of the Alpine crust",
        "doi": "https://doi.org/10.1093/gji/ggae001",
        "publication_date": "2024-03-15",
        "primary_location": {
            "landing_page_url": "https://academic.oup.com/gji/article/1",
            "source": {"display_name": "Geophysical Journal International"},
        },
        "authorships": [
            {
                "author": {"display_name": "Andreas Fichtner"},
                "institutions": [{"display_name": "ETH Zurich"}],
            },
            {
                "author": {"display_name": "Co Author"},
                "institutions": [{"display_name": "ETH Zurich"}],
            },
        ],
        "abstract_inverted_index": {
            "Adjoint": [0], "tomography": [1], "of": [2], "the": [3],
            "crust": [4], "using": [5], "seismic": [6], "waveforms.": [7],
        },
    }
    payload = {"results": [work], "meta": {"next_cursor": None}}
    responses_lib.add(
        responses_lib.GET, "https://api.openalex.org/works",
        json=payload, status=200,
    )

    seen: set[str] = set()
    papers = fs.fetch_from_openalex_by_author(
        "A5023888391",
        date_from=dt.date(2024, 1, 1),
        date_to=dt.date(2024, 12, 31),
        seen=seen,
    )
    assert len(papers) == 1
    p = papers[0]
    assert p["source"] == "openalex"
    assert p["title"] == "Adjoint tomography of the Alpine crust"
    assert p["journal"] == "Geophysical Journal International"
    assert p["doi"] == "10.1093/gji/ggae001"
    assert p["date"] == "2024-03-15"
    assert "Andreas Fichtner" in p["authors"]
    # Institutions are normalized + de-duplicated.
    assert p["institutions"] == ["ETH Zurich"]
    # Explicit follow.
    assert p["explicit_follow"] is True
    assert p["id"] in seen


@responses_lib.activate
def test_fetch_from_openalex_by_author_filters_out_of_window():
    work = {
        "title": "Older work",
        "doi": "10.1/old",
        "publication_date": "2020-01-01",  # before window
        "primary_location": {"source": {"display_name": "GJI"}},
        "authorships": [],
        "abstract_inverted_index": None,
    }
    payload = {"results": [work], "meta": {"next_cursor": None}}
    responses_lib.add(
        responses_lib.GET, "https://api.openalex.org/works",
        json=payload, status=200,
    )
    papers = fs.fetch_from_openalex_by_author(
        "Andreas Fichtner",
        date_from=dt.date(2024, 1, 1),
        date_to=dt.date(2024, 12, 31),
        seen=set(),
    )
    assert papers == []


@responses_lib.activate
def test_fetch_from_openalex_by_author_network_error_returns_partial():
    responses_lib.add(
        responses_lib.GET, "https://api.openalex.org/works", status=500,
    )
    papers = fs.fetch_from_openalex_by_author(
        "A123456",
        date_from=dt.date(2024, 1, 1),
        date_to=dt.date(2024, 12, 31),
        seen=set(),
    )
    assert papers == []


def test_fetch_from_openalex_by_author_blank_input_returns_empty():
    assert fs.fetch_from_openalex_by_author(
        "   ",
        date_from=dt.date(2024, 1, 1),
        date_to=dt.date(2024, 12, 31),
        seen=set(),
    ) == []


# ---------------------------------------------------------------------------
# derive_sources_from_predicates  (intake plan, no network)
# ---------------------------------------------------------------------------


def test_derive_sources_author_only_is_explicit_follow():
    config = {"match": "any", "predicates": [{"type": "author", "any_of": ["Fichtner", "Beroza"]}]}
    plan = fs.derive_sources_from_predicates({"config": config})
    assert plan["explicit_follow"] is True
    assert plan["authors"] == ["Fichtner", "Beroza"]
    assert plan["arxiv_categories"] == []
    assert plan["journals"] == []


def test_derive_sources_arxiv_category_only_is_explicit_follow():
    config = {
        "match": "any",
        "predicates": [{"type": "arxiv_category", "any_of": ["physics.geo-ph"]}],
    }
    plan = fs.derive_sources_from_predicates({"config": config})
    assert plan["explicit_follow"] is True
    assert plan["arxiv_categories"] == ["physics.geo-ph"]


def test_derive_sources_journal_only_expands_to_issns():
    """A journal-only follow is explicit AND expands the stored abbreviation to
    its registry ISSNs so OpenAlex/Crossref intake scans them."""
    config = {"match": "any", "predicates": [{"type": "journal", "any_of": ["GRL"]}]}
    plan = fs.derive_sources_from_predicates({"config": config})
    assert plan["explicit_follow"] is True
    assert plan["journals"] == ["GRL"]
    # GRL registry ISSNs.
    assert "0094-8276" in plan["issns"]
    assert "1944-8007" in plan["issns"]


def test_derive_sources_mixed_with_filter_is_not_explicit_follow():
    config = {
        "match": "any",
        "predicates": [
            {"type": "author", "any_of": ["Fichtner"]},
            {"type": "keyword", "any_of": ["DAS"]},  # a FILTER predicate
        ],
    }
    plan = fs.derive_sources_from_predicates({"config": config})
    assert plan["explicit_follow"] is False
    # The author source is still surfaced for intake even though the tracker is
    # not a *pure* explicit follow.
    assert plan["authors"] == ["Fichtner"]


def test_derive_sources_empty_config_yields_empty_plan():
    plan = fs.derive_sources_from_predicates({"config": {}})
    assert plan["explicit_follow"] is False
    assert plan["authors"] == []
    assert plan["arxiv_categories"] == []
    assert plan["journals"] == []
    assert plan["issns"] == []


# ---------------------------------------------------------------------------
# Institution normalisation — leading affiliation-index stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Affiliation index glued to the front (Crossref / ETH RC raw strings).
        ("1Department of Geophysics, Stanford University",
         "Department of Geophysics, Stanford University"),
        ("2 Institute of Geophysics, China Earthquake Administration",
         "Institute of Geophysics, China Earthquake Administration"),
        ("5Università degli Studi di Trieste, Trieste, Italy",
         "Università degli Studi di Trieste, Trieste, Italy"),
        # index before a lowercase "now at, …" footnote is still stripped.
        ("3now at, MIT Lincoln Laboratory", "now at, MIT Lincoln Laboratory"),
        # Ordinals are real words — never stripped.
        ("21st Century Seismology Institute", "21st Century Seismology Institute"),
        ("2nd Marine Geology Lab", "2nd Marine Geology Lab"),
        ("10th Mountain Research Center", "10th Mountain Research Center"),
        # Already-clean names are untouched.
        ("Stanford University", "Stanford University"),
        ("GFZ German Research Centre for Geosciences",
         "GFZ German Research Centre for Geosciences"),
    ],
)
def test_institution_leading_index_is_stripped(raw, expected):
    assert fs._normalize_institution_display_name(raw) == expected


def test_normalize_institutions_dedupes_after_stripping_index():
    # "1Stanford University" and "Stanford University" collapse to one entry
    # once the affiliation index is removed.
    out = fs.normalize_institutions(
        ["1Stanford University", "Stanford University"]
    )
    assert out == ["Stanford University"]
