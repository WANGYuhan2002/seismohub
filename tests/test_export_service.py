"""Tests for ``services/export_service.py`` (pre-Wave-9; was 32 % coverage).

Closes the gap left by the audit's per-file table row #10. All functions
here are pure (no DB, no network, no subprocess) — they take dict inputs
and return strings/bytes/iterators. We exercise the round-trip through
CSL-JSON / BibTeX, the citekey collision-suffix logic, the LaTeX escape
table, the CJK author handler, and the ZIP streaming generator including
the safe-archive-name reject-traversal contract.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from services.export_service import (
    _bibtex_escape,
    _citekey,
    _csl_authors,
    _csl_issued,
    _format_bibtex_authors,
    _safe_archive_name,
    _split_author,
    paper_to_bibtex,
    paper_to_csl,
    papers_to_bibtex_text,
    papers_to_csl_json,
    stream_all_zip,
    stream_pdfs_zip,
)


# ----------------------------------------------------------------------
# _split_author
# ----------------------------------------------------------------------


def test_split_author_last_comma_first():
    assert _split_author("Wang, Yuhan") == {"family": "Wang", "given": "Yuhan"}


def test_split_author_first_last_takes_last_token_as_family():
    assert _split_author("Yuhan Wang") == {"family": "Wang", "given": "Yuhan"}


def test_split_author_initial_then_surname():
    assert _split_author("Y. Wang") == {"family": "Wang", "given": "Y."}


def test_split_author_cjk_kept_as_literal_family():
    """CJK characters trigger the no-space-split branch."""
    out = _split_author("王小明")
    assert out == {"family": "王小明", "given": ""}


def test_split_author_single_token_goes_to_literal():
    assert _split_author("Plato") == {"literal": "Plato"}


def test_split_author_empty_returns_unknown_literal():
    assert _split_author("") == {"literal": "Unknown"}
    assert _split_author("   ") == {"literal": "Unknown"}


def test_split_author_handles_multi_token_given_name():
    out = _split_author("Mary Jane Watson")
    assert out["family"] == "Watson"
    assert out["given"] == "Mary Jane"


# ----------------------------------------------------------------------
# _csl_authors
# ----------------------------------------------------------------------


def test_csl_authors_normalises_list_of_strings():
    out = _csl_authors({"authors": ["Smith, J.", "Doe, A."]})
    assert out == [
        {"family": "Smith", "given": "J."},
        {"family": "Doe", "given": "A."},
    ]


def test_csl_authors_accepts_single_string():
    out = _csl_authors({"authors": "Smith, J."})
    assert out == [{"family": "Smith", "given": "J."}]


def test_csl_authors_passes_through_dict_shape():
    out = _csl_authors({"authors": [{"family": "Smith", "given": "John"}]})
    assert out == [{"family": "Smith", "given": "John"}]


def test_csl_authors_empty_returns_unknown_sentinel():
    """Empty authors → [{'literal': 'Unknown'}] so BibTeX still has an
    author field."""
    assert _csl_authors({"authors": []}) == [{"literal": "Unknown"}]
    assert _csl_authors({}) == [{"literal": "Unknown"}]


# ----------------------------------------------------------------------
# _csl_issued
# ----------------------------------------------------------------------


def test_csl_issued_parses_yyyy_mm_dd():
    assert _csl_issued({"published_date": "2024-11-03"}) == {"date-parts": [[2024, 11, 3]]}


def test_csl_issued_parses_yyyy_only():
    assert _csl_issued({"published_date": "2024"}) == {"date-parts": [[2024]]}


def test_csl_issued_handles_slash_separator():
    assert _csl_issued({"published_date": "2024/11/03"}) == {"date-parts": [[2024, 11, 3]]}


def test_csl_issued_returns_none_for_empty():
    assert _csl_issued({"published_date": ""}) is None
    assert _csl_issued({}) is None


def test_csl_issued_stops_at_non_numeric_part():
    """A malformed middle piece (e.g. "2024-XX") truncates at the first valid run."""
    out = _csl_issued({"published_date": "2024-XX-03"})
    assert out == {"date-parts": [[2024]]}


def test_csl_issued_returns_none_when_first_piece_invalid():
    assert _csl_issued({"published_date": "garbage"}) is None


# ----------------------------------------------------------------------
# _citekey
# ----------------------------------------------------------------------


def test_citekey_uses_surname_and_year():
    paper = {
        "authors": ["Niu, Yixian"],
        "published_date": "2024-11-03",
        "keywords": ["receiver functions"],
    }
    out = _citekey(paper, set())
    assert out.startswith("Niu2024")
    assert "receiverfunctions" in out


def test_citekey_dedup_suffix_on_collision():
    seen: set = set()
    first = _citekey({"authors": ["Wang, L."], "published_date": "2024"}, seen)
    second = _citekey({"authors": ["Wang, L."], "published_date": "2024"}, seen)
    third = _citekey({"authors": ["Wang, L."], "published_date": "2024"}, seen)
    assert first != second
    assert second != third
    # Suffix scheme is "a", "b", ...
    assert second.endswith("a") and not first.endswith("a")
    assert third.endswith("b")


def test_citekey_fallback_anon_for_missing_authors():
    out = _citekey({"authors": [], "published_date": ""}, set())
    assert "n.d." in out
    assert out.startswith("Unknown") or out.startswith("Anon")


def test_citekey_strips_non_alphanumeric():
    out = _citekey(
        {"authors": ["van der Berg, J."], "published_date": "2024"},
        set(),
    )
    assert "vanderBerg" in out or "Berg" in out or "Bergvanderber" in out
    # No spaces or punctuation in the final citekey
    assert " " not in out
    assert "," not in out


# ----------------------------------------------------------------------
# paper_to_csl
# ----------------------------------------------------------------------


def test_paper_to_csl_has_required_keys():
    paper = {
        "id": "p1",
        "title": "Crustal Structure",
        "authors": ["Niu, Y."],
        "journal": "Nature Geoscience",
        "doi": "10.1038/x",
        "link": "https://nature.com/x",
        "abstract": "We image the crust.",
        "published_date": "2024-11-03",
        "keywords": ["tectonics", "tibet"],
        "institutions": ["MIT"],
    }
    csl = paper_to_csl(paper, citekey="Niu2024")
    assert csl["id"] == "Niu2024"
    assert csl["type"] == "article-journal"
    assert csl["title"] == "Crustal Structure"
    assert csl["container-title"] == "Nature Geoscience"
    assert csl["DOI"] == "10.1038/x"
    assert csl["URL"] == "https://nature.com/x"
    assert csl["abstract"] == "We image the crust."
    assert csl["issued"] == {"date-parts": [[2024, 11, 3]]}
    assert csl["keyword"] == "tectonics, tibet"
    assert csl["publisher"] == "MIT"


def test_paper_to_csl_falls_back_to_id_when_no_citekey():
    paper = {"id": "some-id", "title": "x", "authors": ["A"]}
    csl = paper_to_csl(paper)
    assert csl["id"] == "some-id"


def test_paper_to_csl_handles_missing_optional_fields():
    csl = paper_to_csl({"id": "x", "title": "Bare", "authors": []})
    assert csl["id"] == "x"
    assert csl["title"] == "Bare"
    assert "container-title" not in csl
    assert "DOI" not in csl


# ----------------------------------------------------------------------
# papers_to_csl_json
# ----------------------------------------------------------------------


def test_papers_to_csl_json_assigns_unique_citekeys():
    papers = [
        {"id": "a", "title": "Paper 1", "authors": ["Smith, A."], "published_date": "2024"},
        {"id": "b", "title": "Paper 2", "authors": ["Smith, A."], "published_date": "2024"},
    ]
    out = papers_to_csl_json(papers)
    assert len(out) == 2
    ids = [entry["id"] for entry in out]
    assert len(set(ids)) == 2
    # Both should start with Smith2024
    assert all(i.startswith("Smith2024") for i in ids)


# ----------------------------------------------------------------------
# _bibtex_escape + _format_bibtex_authors
# ----------------------------------------------------------------------


def test_bibtex_escape_handles_ampersand_and_special_chars():
    out = _bibtex_escape("Tom & Jerry $100 100% _foo_ ^x ~y #1")
    assert "&" not in out.replace(r"\&", "")
    assert r"\&" in out
    assert r"\$" in out
    assert r"\%" in out
    assert r"\_" in out
    assert r"\#" in out


def test_bibtex_escape_runs_backslash_first():
    """Backslash must be escaped before any other escape inserts new backslashes."""
    # Just a backslash → \textbackslash{}
    out = _bibtex_escape("a\\b")
    assert r"\textbackslash{}" in out


def test_bibtex_escape_empty_string():
    assert _bibtex_escape("") == ""


def test_format_bibtex_authors_joins_with_and():
    authors = [
        {"family": "Smith", "given": "J."},
        {"family": "Doe", "given": "A."},
    ]
    out = _format_bibtex_authors(authors)
    assert out == "Smith, J. and Doe, A."


def test_format_bibtex_authors_wraps_literal_in_braces():
    """Consortium / literal names go in {...} so BibTeX doesn't word-split them."""
    out = _format_bibtex_authors([{"literal": "The LIGO Collaboration"}])
    assert out.startswith("{")
    assert out.endswith("}")
    assert "LIGO Collaboration" in out


def test_format_bibtex_authors_empty_returns_unknown():
    assert _format_bibtex_authors([]) == "Unknown"


# ----------------------------------------------------------------------
# paper_to_bibtex / papers_to_bibtex_text
# ----------------------------------------------------------------------


def test_paper_to_bibtex_emits_article_entry():
    paper = {
        "id": "p1",
        "title": "Crustal Imaging",
        "authors": ["Niu, Y."],
        "journal": "Nature Geoscience",
        "doi": "10.1038/x",
        "published_date": "2024-11-03",
    }
    out = paper_to_bibtex(paper, citekey="Niu2024")
    assert out.startswith("@article{Niu2024,")
    assert out.endswith("}")
    assert "author = {Niu, Y.}" in out
    assert "title = {Crustal Imaging}" in out
    assert "journal = {Nature Geoscience}" in out
    assert "year = {2024}" in out
    assert "doi = {10.1038/x}" in out


def test_paper_to_bibtex_escapes_ampersand_in_title():
    paper = {"id": "p1", "title": "Cats & Dogs", "authors": ["A, B"], "published_date": "2024"}
    out = paper_to_bibtex(paper, citekey="ck1")
    assert r"Cats \& Dogs" in out


def test_paper_to_bibtex_truncates_huge_abstract():
    """Abstracts longer than 4000 chars are clipped so older BibTeX
    parsers don't choke on multi-MB strings."""
    huge = "x" * 5000
    paper = {"id": "p", "title": "t", "authors": ["A"], "abstract": huge}
    out = paper_to_bibtex(paper, citekey="ck")
    # Output abstract field must be ≤ ~4002 chars (4000 + ellipsis)
    # Just check the ellipsis got appended.
    assert "…" in out


def test_papers_to_bibtex_text_round_trip_two_papers():
    papers = [
        {
            "id": "p1",
            "title": "First",
            "authors": ["Smith, J."],
            "published_date": "2024",
            "doi": "10.1/a",
        },
        {
            "id": "p2",
            "title": "Second",
            "authors": ["Doe, A."],
            "published_date": "2023",
            "doi": "10.1/b",
        },
    ]
    out = papers_to_bibtex_text(papers)
    assert "@article{Smith2024" in out
    assert "@article{Doe2023" in out
    # Header comment is present
    assert out.startswith("% BibTeX export generated by SWP Paper Hub")


def test_papers_to_bibtex_text_empty_list_returns_header_only():
    out = papers_to_bibtex_text([])
    assert "% BibTeX export" in out
    assert "@article{" not in out


# ----------------------------------------------------------------------
# _safe_archive_name (traversal-rejection)
# ----------------------------------------------------------------------


def test_safe_archive_name_rejects_dotdot_traversal():
    assert _safe_archive_name("../../etc/passwd") == "etc/passwd"
    assert _safe_archive_name("../foo.pdf") == "foo.pdf"


def test_safe_archive_name_strips_control_chars():
    assert "\x00" not in _safe_archive_name("foo\x00bar.pdf")
    assert "\x01" not in _safe_archive_name("f\x01o.pdf")


def test_safe_archive_name_replaces_backslashes_with_underscores():
    out = _safe_archive_name("foo\\bar.pdf")
    assert "\\" not in out
    assert "_" in out


def test_safe_archive_name_preserves_subdirs():
    assert _safe_archive_name("pdfs/2024/foo.pdf") == "pdfs/2024/foo.pdf"


def test_safe_archive_name_returns_fallback_when_empty():
    assert _safe_archive_name("..") == "untitled.pdf"
    assert _safe_archive_name("./") == "untitled.pdf"
    assert _safe_archive_name("") == "untitled.pdf"


# ----------------------------------------------------------------------
# stream_pdfs_zip
# ----------------------------------------------------------------------


def test_stream_pdfs_zip_empty_papers_yields_valid_empty_zip(tmp_path):
    """A papers=[] export should still produce a parseable ZIP (just the
    central directory)."""
    chunks = list(stream_pdfs_zip([], tmp_path))
    blob = b"".join(chunks)
    # Empty ZIP marker bytes — ZipFile must accept it without raising
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert zf.namelist() == []


def test_stream_pdfs_zip_skips_papers_without_has_pdf(tmp_path):
    papers = [
        {"id": "p1", "title": "no pdf", "has_pdf": False, "pdf_url": ""},
        {"id": "p2", "title": "also no", "has_pdf": False, "pdf_url": "x.pdf"},
    ]
    chunks = list(stream_pdfs_zip(papers, tmp_path))
    blob = b"".join(chunks)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert zf.namelist() == []


def test_stream_pdfs_zip_includes_existing_files(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "alpha.pdf").write_bytes(b"%PDF-1.4 fake")
    (downloads / "beta.pdf").write_bytes(b"%PDF-1.4 also")
    papers = [
        {"id": "a", "has_pdf": True, "pdf_url": "/static/alpha.pdf"},
        {"id": "b", "has_pdf": True, "pdf_url": "beta.pdf"},
        # has_pdf=True but the file does not exist → silently skipped
        {"id": "c", "has_pdf": True, "pdf_url": "missing.pdf"},
    ]
    chunks = list(stream_pdfs_zip(papers, downloads))
    blob = b"".join(chunks)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    assert "alpha.pdf" in names
    assert "beta.pdf" in names
    assert "missing.pdf" not in names


def test_stream_pdfs_zip_dedups_collision_filenames(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "shared.pdf").write_bytes(b"%PDF-1.4 a")
    # Two papers reference the same filename — second one gets _2 suffix
    papers = [
        {"id": "a", "has_pdf": True, "pdf_url": "shared.pdf"},
        {"id": "b", "has_pdf": True, "pdf_url": "shared.pdf"},
    ]
    chunks = list(stream_pdfs_zip(papers, downloads))
    blob = b"".join(chunks)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = sorted(zf.namelist())
    assert "shared.pdf" in names
    assert any(n != "shared.pdf" and "shared" in n for n in names)


# ----------------------------------------------------------------------
# stream_all_zip
# ----------------------------------------------------------------------


def test_stream_all_zip_contains_bib_ris_csl_manifest(tmp_path):
    """The all-in-one export bundles bib + ris + csl + manifest at the
    root, even when no PDFs are present."""
    papers = [
        {
            "id": "p1",
            "title": "Test",
            "authors": ["Smith, A."],
            "published_date": "2024",
            "doi": "10.1/test",
            "has_pdf": False,
        }
    ]
    chunks = list(stream_all_zip(papers, tmp_path / "noop"))
    blob = b"".join(chunks)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    assert "papers.bib" in names
    assert "papers.ris" in names
    assert "papers.csl.json" in names
    assert "manifest.json" in names
    # Manifest is well-formed JSON with the expected keys
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["paper_count"] == 1
    assert "exported_at" in manifest


def test_stream_all_zip_includes_pdf_subdir(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "x.pdf").write_bytes(b"%PDF-1.4 stub")
    papers = [{"id": "p", "title": "x", "authors": ["A"], "has_pdf": True, "pdf_url": "x.pdf"}]
    chunks = list(stream_all_zip(papers, downloads))
    blob = b"".join(chunks)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    assert "pdfs/x.pdf" in names
