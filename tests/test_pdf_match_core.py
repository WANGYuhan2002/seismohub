"""Unit tests for the shared PDF matching core (services/pdf_match_core.py, Task D)."""
import os
from collections import namedtuple

os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")
os.environ.setdefault("SEISMO_DB_PATH", "/tmp/test_pdf_match_core.db")

import pytest

from services.analyze_service import normalize_doi
from services.pdf_match_core import (
    LibraryIndex,
    MatchSignals,
    extract_match_signals,
    match_signals,
)

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

Row = namedtuple("Row", "id title doi link pdf_path pdf_sha256 authors published_date")


def _row(id, title, doi="", link="", pdf_path=None, pdf_sha256=None):
    return Row(id, title, doi, link, pdf_path, pdf_sha256, [], "2026-01-01")


def _lib(rows):
    return LibraryIndex(rows=rows)


# ── normalize_doi (step 1) ────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("10.1234/x.2026.", "10.1234/x.2026"),
    ("(10.1234/x.2026)", "10.1234/x.2026"),
    ("10.1234/x];", "10.1234/x"),
    ("https://doi.org/10.1093/gji/ggag248", "10.1093/gji/ggag248"),
    ("doi:10.1190/geo-2024-0901", "10.1190/geo-2024-0901"),
    ("10.1234/clean", "10.1234/clean"),
    # GSA book DOIs legitimately END in a parenthesised code — must NOT be truncated
    ("10.1130/2011.2483(00)", "10.1130/2011.2483(00)"),
    ("(10.1130/2011.2483(00))", "10.1130/2011.2483(00)"),
])
def test_normalize_doi_trailing_punct(raw, expected):
    assert normalize_doi(raw) == expected


# ── LibraryIndex ──────────────────────────────────────────────────────────

def test_index_lookups():
    lib = _lib([
        _row("p1", "Full Waveform Inversion of the Andes", doi="10.1/AbC"),
        _row("p2", "Distributed Acoustic Sensing Urban", link="https://arxiv.org/abs/2606.17018v1"),
        _row("p3", "Already Has A File", pdf_path="/x.pdf", pdf_sha256="deadbeef"),
    ])
    assert lib.by_doi["10.1/abc"].id == "p1"          # keyed lowercase
    assert lib.by_arxiv["2606.17018"].id == "p2"      # arxiv id from link
    assert lib.by_sha["deadbeef"].id == "p3"
    assert len(lib.by_norm_title.get("full waveform inversion of the andes", [])) == 1


def test_decide_never_overwrites():
    lib = _lib([
        _row("empty", "T", ),
        _row("haspdf", "T2", pdf_path="/y.pdf", pdf_sha256="aaa"),
    ])
    assert lib.decide(lib.by_id["empty"], "newsha") == "attach"
    assert lib.decide(lib.by_id["haspdf"], "newsha") == "conflict"     # never overwrite
    assert lib.decide(lib.by_id["haspdf"], "aaa") == "already_attached"
    # intra-batch: after attaching to 'empty', a different file is a conflict
    lib.register_attached(lib.by_id["empty"], "newsha")
    assert lib.decide(lib.by_id["empty"], "newsha") == "already_attached"
    assert lib.decide(lib.by_id["empty"], "othersha") == "conflict"


# ── match_signals ladder ──────────────────────────────────────────────────

def test_sha_short_circuits():
    lib = _lib([_row("p", "T", pdf_sha256="samesha")])
    res = match_signals(MatchSignals(sha256="samesha", dois=["10.1/x"]), lib)
    assert res.action == "already_attached" and res.method == "sha256"


def test_doi_wins_when_title_agrees():
    lib = _lib([
        _row("d", "Slow Slip On The Megathrust", doi="10.1/match"),
        _row("t", "Some Other Seismic Paper"),
    ])
    sig = MatchSignals(sha256="s", dois=["10.1/MATCH"], title="Slow slip on the megathrust")
    res = match_signals(sig, lib)
    assert res.method == "doi" and res.paper_id == "d" and res.action == "attach"


def test_scraped_cited_doi_rejected_when_title_disagrees():
    # sig's DOI resolves to Y, but the PDF's title is nothing like Y's → the DOI
    # is treated as a citation on the page, NOT this paper's own → no DOI attach.
    lib = _lib([_row("Y", "Nonlinear Site Response In Sedimentary Basins", doi="10.1/cited")])
    sig = MatchSignals(sha256="s", dois=["10.1/cited"], title="A Totally Unrelated Manuscript Heading")
    res = match_signals(sig, lib)
    assert res.method != "doi"
    assert res.action == "unmatched"


def test_bib_doi_trusted_even_if_title_disagrees():
    # a .bib sidecar DOI is authoritative — no title cross-check
    lib = _lib([_row("b", "Library Title", doi="10.1/bibauthoritative")])
    sig = MatchSignals(sha256="s", title="Completely Different PDF Title")
    res = match_signals(sig, lib, bib_entry={"doi": "10.1/bibauthoritative", "title": "x"})
    assert res.method == "doi" and res.paper_id == "b"


def test_arxiv_matches_no_doi_preprint():
    lib = _lib([_row("a", "Preprint", link="http://arxiv.org/abs/2607.01649")])
    res = match_signals(MatchSignals(sha256="s", arxiv_id="2607.01649"), lib)
    assert res.method == "arxiv" and res.paper_id == "a"


def test_bib_arxiv_id_matches():
    # titles deliberately differ so ONLY the bib arxiv-id can match it
    lib = _lib([_row("a", "Library Title Differs Here", link="https://arxiv.org/abs/2606.31288v1")])
    sig = MatchSignals(sha256="s")  # nothing scraped from the PDF
    res = match_signals(sig, lib, bib_entry={"arxiv_id": "2606.31288",
                                             "title": "A Completely Different Bib Title"})
    assert res.method == "arxiv" and res.paper_id == "a"


def test_exact_title_unique_attaches():
    lib = _lib([_row("x", "SubRidge: a Subduction-to-Ridge Model")])
    sig = MatchSignals(sha256="s", title="SubRidge: a Subduction to Ridge Model")
    res = match_signals(sig, lib)
    assert res.method == "title_exact" and res.paper_id == "x" and res.action == "attach"


def test_bib_doi_takes_priority():
    lib = _lib([_row("b", "T", doi="10.5194/egusphere-x")])
    sig = MatchSignals(sha256="s")  # no scraped doi
    res = match_signals(sig, lib, bib_entry={"doi": "10.5194/egusphere-x", "title": "T"})
    assert res.method == "doi" and res.paper_id == "b"


def test_ambiguous_titles_need_review():
    lib = _lib([
        _row("v1", "Deep Learning for Seismic Phase Picking"),
        _row("v2", "Deep Learning for Seismic Phase Picking"),  # duplicate title (preprint vs published)
    ])
    sig = MatchSignals(sha256="s", title="Deep Learning for Seismic Phase Picking")
    res = match_signals(sig, lib)
    # two identical titles both clear AUTO -> not a unique exact hit -> needs_review
    assert res.action == "needs_review"
    assert len(res.candidates) >= 2


def test_oup_doi_with_article_id_suffix_trusted():
    # OUP prints '10.1093/gji/ggag248/8719323' (real DOI + its own article id).
    # The greedy regex over-captures it; it resolves only via truncation, and
    # because the suffix is the publisher's OWN id it is trusted even though the
    # publisher first-page title is masthead garbage.
    lib = _lib([_row("g", "Linearised Versus Nonlinear Uncertainty", doi="10.1093/gji/ggag248")])
    sig = MatchSignals(sha256="s", dois=["10.1093/gji/ggag248/8719323"],
                       title="© The Author(s) 2026. Published by Oxford University Press")
    res = match_signals(sig, lib)
    assert res.method == "doi" and res.paper_id == "g"


def test_no_match_is_unmatched():
    lib = _lib([_row("p", "Something About Volcanoes")])
    sig = MatchSignals(sha256="s", title="Quantum Chromodynamics on a Lattice")
    res = match_signals(sig, lib)
    assert res.action == "unmatched"


def test_conflict_when_target_has_pdf():
    lib = _lib([_row("p", "T", doi="10.1/x", pdf_path="/z.pdf", pdf_sha256="old")])
    res = match_signals(MatchSignals(sha256="new", dois=["10.1/x"]), lib)
    assert res.action == "conflict" and res.method == "doi"


# ── extract_match_signals (needs fitz) ────────────────────────────────────

@pytest.mark.skipif(fitz is None, reason="PyMuPDF not available")
def test_extract_signals_from_pdf():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "A Great Seismic Title\nAuthor One and Author Two\n"
                               "https://doi.org/10.1234/abc.2026.")
    doc.set_metadata({"title": "A Great Seismic Title", "author": "Author One"})
    data = doc.tobytes()
    doc.close()

    sig = extract_match_signals(data, "paper.pdf")
    assert "10.1234/abc.2026" in sig.dois          # trailing '.' stripped by normalize_doi
    assert sig.meta_title == "A Great Seismic Title"
    assert sig.heuristic_title.startswith("A Great Seismic Title")
    assert sig.title == "A Great Seismic Title"
