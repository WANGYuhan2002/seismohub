"""scripts/harvest_literature_list.py: reading-list entries -> metadata (offline; HTTP is faked).

Fixed 2026-11-13: arXiv entries get their title and authors from the arXiv API (the identity check, gate 4, compares
the title with the downloaded PDF), October IDs such as 2310.01234 take the arXiv branch, and a free-form citation
resolves only to a Crossref record that agrees with it.
"""
from __future__ import annotations

import json
import urllib.parse

import pytest

fitz = pytest.importorskip("fitz")

from scripts import harvest_literature_list as hl  # noqa: E402
from services import fullbundle_harvest as fb  # noqa: E402

ARXIV_API = "https://export.arxiv.org/api/query?"
ARXIV_PDF = "https://export.arxiv.org/pdf/2310.01234"
CROSSREF = "https://api.crossref.org/works"
TITLE = "Tidal Modulation of Aftershocks & Slow Slip Following the Noto Earthquake"

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title type="html">ArXiv Query: search_query=&amp;id_list=2310.01234</title>
  <id>http://arxiv.org/api/query-id</id>
  <entry>
    <id>http://arxiv.org/abs/2310.01234v2</id>
    <published>2023-10-02T17:59:59Z</published>
    <title>Tidal Modulation of Aftershocks &amp; Slow Slip
      Following the Noto Earthquake</title>
    <author><name>Jane Doe</name></author>
    <author><name>John  Q. Roe</name></author>
  </entry>
</feed>
"""

ATOM_ERROR = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format_for_2310.01234</id>
    <title>Error</title>
    <summary>incorrect id format for 2310.01234</summary>
    <author><name>arXiv api core</name></author>
  </entry>
</feed>
"""


class FakeHttp:
    """PoliteSession stand-in: longest-prefix URL routing; records every URL it was asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, *, headers=None, sensitive_headers=(), max_bytes=0, retries=1):
        self.calls.append(url)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                status, body = self.routes[prefix]
                return fb.HttpResult(url=url, status=status, final_url=url, data=body)
        return fb.HttpResult(url=url, status=404, final_url=url)


def _make_pdf(title: str, *lines: str, pages: int = 4) -> bytes:
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        y = 40
        if index == 0:
            for text in (title,) + lines:
                page.insert_text((36, y), text, fontsize=9)
                y += 14
        while y < 780:
            page.insert_text((36, y), "ambient noise surface wave tomography " * 3, fontsize=6)
            y += 9
    data = doc.tobytes()
    doc.close()
    return data


def _crossref_item(doi: str, title: str, journal: str, year: int, *families: str) -> dict:
    return {"DOI": doi, "type": "journal-article", "title": [title], "container-title": [journal],
            "issued": {"date-parts": [[year]]}, "author": [{"given": "A.", "family": f} for f in families]}


def _crossref_search(*items: dict) -> bytes:
    return json.dumps({"message": {"items": list(items)}}).encode()


AKI = _crossref_item("10.5555/aki-1957", "Space and time spectra of stationary stochastic waves, with special "
                     "reference to microtremors", "Bulletin of the Earthquake Research Institute", 1957, "Aki")
UNRELATED = _crossref_item("10.5555/retail-2021", "Deep learning for retail demand forecasting",
                           "Journal of Retail Analytics", 2021, "Smith", "Jones")


# ---------------------------------------------------------------- arXiv entries

@pytest.mark.parametrize("spec", [
    "arXiv:2310.01234",                                 # contains "10." - once sent to the Crossref query
    "https://arxiv.org/abs/2310.01234v2",
    "2310.01234",                                       # bare ID: no prefix at all
    "2310.01234v2",
    "10.48550/arXiv.2310.01234",                        # the arXiv DOI is an arXiv ID too
    "https://doi.org/10.48550/arXiv.2310.01234v2",
])
def test_arxiv_entries_get_title_and_authors_from_the_arxiv_api(spec):
    http = FakeHttp({ARXIV_API: (200, ATOM.encode())})
    meta = hl.resolve_citation_to_metadata(http, spec)
    assert meta == {
        "doi": "10.48550/arXiv.2310.01234", "arxiv_id": "2310.01234", "title": TITLE, "journal": "arXiv",
        "year": "2023", "authors": ["Jane Doe", "John Q. Roe"], "link": "https://arxiv.org/abs/2310.01234",
    }
    assert len(http.calls) == 1 and http.calls[0].startswith(ARXIV_API)  # no Crossref lookup
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(http.calls[0]).query)
    assert query["id_list"] == ["2310.01234"]


def test_old_style_arxiv_id_keeps_its_archive_name():
    http = FakeHttp({ARXIV_API: (200, ATOM.encode())})
    meta = hl.resolve_citation_to_metadata(http, "arXiv:physics/0601001")
    assert meta["arxiv_id"] == "physics/0601001" and meta["doi"] == ""
    assert meta["title"] == TITLE  # whatever the API answers for that ID


def test_arxiv_error_entry_gives_no_title():
    http = FakeHttp({ARXIV_API: (200, ATOM_ERROR.encode())})
    meta = hl.resolve_citation_to_metadata(http, "arXiv:2310.01234")
    assert meta["title"] == "" and meta["authors"] == [] and meta["arxiv_id"] == "2310.01234"


def test_arxiv_api_failure_still_returns_the_identifier():
    meta = hl.resolve_citation_to_metadata(FakeHttp({ARXIV_API: (503, b"")}), "2310.01234")
    assert meta["arxiv_id"] == "2310.01234" and meta["doi"] == "10.48550/arXiv.2310.01234" and meta["title"] == ""


def test_a_doi_wins_over_an_arxiv_id_in_the_same_entry():
    record = _crossref_item("10.1029/2023JB012345", TITLE, "JGR Solid Earth", 2023, "Doe", "Roe")
    http = FakeHttp({f"{CROSSREF}/10.1029/2023JB012345": (200, json.dumps({"message": record}).encode()),
                     ARXIV_API: (200, ATOM.encode())})
    meta = hl.resolve_citation_to_metadata(http, "Doe 2023 10.1029/2023JB012345 (arXiv:2310.01234)")
    assert meta["doi"] == "10.1029/2023JB012345" and meta["arxiv_id"] == "" and meta["journal"] == "JGR Solid Earth"
    assert not any(url.startswith(ARXIV_API) for url in http.calls)


def test_bare_arxiv_id_now_passes_the_identity_gate():
    """Before the fix the arXiv entry had no title; OpenAlex is not asked about 10.48550 DOIs, so gate 4 had nothing
    to compare the PDF with and rejected the right paper as identity_mismatch."""
    pdf = _make_pdf(TITLE, "Jane Doe, John Q. Roe", "arXiv:2310.01234v2 [physics.geo-ph] 2 Oct 2023")
    http = FakeHttp({ARXIV_API: (200, ATOM.encode()), ARXIV_PDF: (200, pdf)})
    meta = hl.resolve_citation_to_metadata(http, "2310.01234")

    def harvest(title: str):
        ref = fb.PaperRef(paper_id="lit_0001", doi=meta["doi"], title=title, journal=meta["journal"],
                          link=meta["link"], source="standalone_pack", arxiv_id=meta["arxiv_id"])
        return fb.FullBundleHarvester(http, channels=["arxiv"], min_bytes=0).harvest(ref)

    out = harvest(meta["title"])
    assert out.app_hit is not None and out.app_hit.channel == "arxiv" and out.status == fb.SUCCESS
    untitled = harvest("")  # what the old resolver handed over
    assert untitled.app_hit is None
    assert any("identity_mismatch" in (a.reason or "") for a in untitled.pdf_attempts)


# ---------------------------------------------------------------- free-form citations

def test_unknown_citation_does_not_take_crossrefs_first_hit():
    spec = "Aki 1957 space and time spectra of stationary stochastic waves"
    http = FakeHttp({f"{CROSSREF}?": (200, _crossref_search(UNRELATED))})
    meta = hl.resolve_citation_to_metadata(http, spec)
    assert meta["doi"] == "" and meta["arxiv_id"] == "" and meta["title"] == spec  # reported as unresolved


def test_citation_takes_the_first_record_that_agrees():
    spec = "Aki 1957 space and time spectra of stationary stochastic waves"
    http = FakeHttp({f"{CROSSREF}?": (200, _crossref_search(UNRELATED, AKI))})
    meta = hl.resolve_citation_to_metadata(http, spec)
    assert meta["doi"] == "10.5555/aki-1957" and meta["year"] == "1957" and meta["authors"] == ["A. Aki"]


def test_citation_agrees():
    record = {"title": "High-Resolution Surface-Wave Tomography from Ambient Seismic Noise", "journal": "Science",
              "year": "2005", "authors": ["N. M. Shapiro", "M. Campillo", "L. Stehly", "M. H. Ritzwoller"]}
    assert hl.citation_agrees("Shapiro Campillo 2005 High-resolution surface-wave tomography ambient seismic noise",
                              record)
    assert not hl.citation_agrees("Kanamori 1977 energy release in great earthquakes", record)
    assert not hl.citation_agrees("Shapiro 2005", {"title": "Unrelated", "authors": ["X. Shapiro"], "year": "1999"})
    assert not hl.citation_agrees("of the and", record)  # no content words: nothing to agree with
