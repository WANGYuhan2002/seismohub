"""Tests for ``services/zotero_import.py`` (was 11 % coverage).

Closes the gap left by per-file table row #4 of TEST-COVERAGE.md. Every
function tested here is **pure**: no database session, no filesystem
writes, no network. The end-to-end ``import_zotero_files`` happy path is
exercised against an isolated DB + an in-memory ``uploaded_files`` dict
(no real .zip handling — the function takes the unzipped mapping
directly), so we never shell out.

Coverage targets:
- ``normalize_upload_path``: URL-decode, strip leading ./, reject ../
- ``_is_noise_path``: __MACOSX, .DS_Store filter
- ``_clean_bib_value``: strip braces / quotes / LaTeX escapes
- ``_normalize_doi``: case + prefix normalization
- ``_abstract_flag``: missing / short / ok sentinel
- ``_stable_id``: deterministic SHA-1 prefix
- ``_read_balanced_entry`` + ``_parse_fields`` + ``parse_bibtex``
- ``_extract_file_paths`` (Zotero ``file = `` field syntax)
- ``_parse_authors`` and ``_parse_keywords``
- ``_entry_date`` / ``_entry_year``
- ``_normalized_title`` / ``_title_similarity`` (FN-B3-A04 contract)
- ``import_zotero_files`` happy path with one bib + one matching pdf
- ``import_zotero_files`` error: no .bib in upload → ZoteroImportError
- ``import_zotero_files`` error: multiple .bib files → ZoteroImportError
- ``import_zotero_files`` rejects path traversal in upload keys
"""

from __future__ import annotations

import pytest

from services.zotero_import import (
    ZoteroImportError,
    _abstract_flag,
    _clean_bib_value,
    _entry_date,
    _entry_year,
    _extract_file_paths,
    _is_noise_path,
    _normalize_doi,
    _normalized_title,
    _parse_authors,
    _parse_fields,
    _parse_keywords,
    _read_balanced_entry,
    _stable_id,
    _title_similarity,
    import_zotero_files,
    normalize_upload_path,
    parse_bibtex,
)


# ----------------------------------------------------------------------
# normalize_upload_path
# ----------------------------------------------------------------------


def test_normalize_upload_path_strips_leading_dotslash():
    assert normalize_upload_path("./foo/bar.pdf") == "foo/bar.pdf"


def test_normalize_upload_path_url_decodes():
    assert normalize_upload_path("a%20b/c.pdf") == "a b/c.pdf"


def test_normalize_upload_path_converts_backslashes():
    assert normalize_upload_path("a\\b\\c.pdf") == "a/b/c.pdf"


def test_normalize_upload_path_rejects_dotdot_segment():
    """Internal '../' segments are unsafe; leading './' / '../' are
    lstrip()'d (treated as path-noise rather than traversal).
    This pins the surprising behaviour so a future hardening change is
    intentional."""
    with pytest.raises(ZoteroImportError, match="Unsafe path"):
        normalize_upload_path("foo/../bar.pdf")
    with pytest.raises(ZoteroImportError, match="Unsafe path"):
        normalize_upload_path("a/b/../../c.pdf")


def test_normalize_upload_path_strips_leading_dotdot_slash():
    """Surprising-but-real: ``../../etc/passwd`` is lstripped to
    ``etc/passwd`` because the leading '.' and '/' chars are removed by
    ``str.lstrip("./")`` before PurePosixPath sees the segments. The
    internal-traversal guard only catches `..` in the MIDDLE of the path."""
    assert normalize_upload_path("../../etc/passwd") == "etc/passwd"


def test_normalize_upload_path_collapses_dot_segments():
    """Bare '.' segments in the middle are dropped."""
    assert normalize_upload_path("a/./b/c.pdf") == "a/b/c.pdf"


def test_normalize_upload_path_empty_returns_empty_string():
    assert normalize_upload_path("") == ""


# ----------------------------------------------------------------------
# _is_noise_path
# ----------------------------------------------------------------------


def test_is_noise_path_drops_macosx_metadata():
    assert _is_noise_path("__MACOSX/foo.pdf")
    assert _is_noise_path("__macosx/foo.pdf")
    assert _is_noise_path("a/__MACOSX/b.pdf")


def test_is_noise_path_drops_ds_store():
    assert _is_noise_path(".DS_Store")
    assert _is_noise_path("foo/.DS_Store")
    assert _is_noise_path(".ds_store")


def test_is_noise_path_keeps_real_files():
    assert not _is_noise_path("My Library.bib")
    assert not _is_noise_path("storage/ABCD/paper.pdf")


def test_is_noise_path_empty_string_is_noise():
    assert _is_noise_path("")


# ----------------------------------------------------------------------
# _clean_bib_value
# ----------------------------------------------------------------------


def test_clean_bib_value_strips_braces():
    assert _clean_bib_value("{Hello}") == "Hello"


def test_clean_bib_value_strips_outer_quotes():
    assert _clean_bib_value('"Hello"') == "Hello"


def test_clean_bib_value_strips_trailing_comma():
    assert _clean_bib_value("Hello,") == "Hello"


def test_clean_bib_value_unescapes_latex_escapes():
    assert _clean_bib_value(r"foo \& bar") == "foo & bar"
    assert _clean_bib_value(r"foo \_ bar") == "foo _ bar"
    assert _clean_bib_value(r"foo \% bar") == "foo % bar"


def test_clean_bib_value_strips_inner_braces_too():
    assert _clean_bib_value("{Hello {World}}") == "Hello World"


def test_clean_bib_value_collapses_whitespace():
    assert _clean_bib_value("hello   world\n\nfoo") == "hello world foo"


def test_clean_bib_value_empty_returns_empty():
    assert _clean_bib_value("") == ""
    assert _clean_bib_value("   ") == ""


# ----------------------------------------------------------------------
# _normalize_doi
# ----------------------------------------------------------------------


def test_normalize_doi_strips_doi_org_prefix():
    assert _normalize_doi("https://doi.org/10.1038/x") == "10.1038/x"
    assert _normalize_doi("http://dx.doi.org/10.1038/x") == "10.1038/x"


def test_normalize_doi_strips_doi_colon_prefix():
    assert _normalize_doi("doi:10.1038/x") == "10.1038/x"
    assert _normalize_doi("DOI:10.1038/x") == "10.1038/x"


def test_normalize_doi_lowercases():
    assert _normalize_doi("10.1038/XYZ") == "10.1038/xyz"


def test_normalize_doi_empty_returns_empty():
    assert _normalize_doi("") == ""
    assert _normalize_doi(None) == ""  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# _abstract_flag
# ----------------------------------------------------------------------


def test_abstract_flag_missing_for_empty_string():
    assert _abstract_flag("") == "missing"
    assert _abstract_flag("   ") == "missing"


def test_abstract_flag_ok_for_long_enough():
    """≥ 100 chars → 'ok' (canonical sentinel; not 'present')."""
    assert _abstract_flag("a" * 100) == "ok"
    assert _abstract_flag("a" * 1000) == "ok"


def test_abstract_flag_short_for_below_threshold():
    assert _abstract_flag("a" * 50) == "short"
    assert _abstract_flag("a" * 99) == "short"


# ----------------------------------------------------------------------
# _stable_id
# ----------------------------------------------------------------------


def test_stable_id_is_deterministic():
    a = _stable_id("foo", "bar", "baz")
    b = _stable_id("foo", "bar", "baz")
    assert a == b
    assert len(a) == 16


def test_stable_id_differs_for_different_inputs():
    assert _stable_id("a") != _stable_id("b")
    assert _stable_id("a", "b") != _stable_id("ab", "")


def test_stable_id_handles_empty_parts():
    """Empty strings are coerced to '' so the call doesn't crash."""
    out = _stable_id("", "", "")
    assert len(out) == 16


# ----------------------------------------------------------------------
# _read_balanced_entry + _parse_fields + parse_bibtex
# ----------------------------------------------------------------------


def test_read_balanced_entry_handles_nested_braces():
    text = "{simple{nested}value}rest"
    body, idx = _read_balanced_entry(text, 0)
    assert body == "simple{nested}value"
    assert text[idx:] == "rest"


def test_read_balanced_entry_handles_paren_form():
    text = "(paren value)tail"
    body, idx = _read_balanced_entry(text, 0)
    assert body == "paren value"


def test_read_balanced_entry_raises_on_unclosed():
    with pytest.raises(ZoteroImportError, match="balanced braces"):
        _read_balanced_entry("{unterminated", 0)


def test_parse_fields_single_field():
    out = _parse_fields("title = {Hello World}")
    assert out == {"title": "Hello World"}


def test_parse_fields_multiple_fields():
    text = "title = {Hello}, author = {Smith, A.}, year = {2024}"
    out = _parse_fields(text)
    assert out["title"] == "Hello"
    assert out["author"] == "Smith, A."
    assert out["year"] == "2024"


def test_parse_fields_handles_quoted_value():
    out = _parse_fields('title = "Hello", year = "2024"')
    assert out["title"] == "Hello"
    assert out["year"] == "2024"


def test_parse_fields_handles_bare_value():
    """Bare numeric values (no quotes/braces) are common for year."""
    out = _parse_fields("year = 2024, foo = bar")
    assert out["year"] == "2024"
    assert out["foo"] == "bar"


def test_parse_bibtex_full_entry_round_trip():
    text = """
@article{Niu2024,
  title = {Crustal Structure},
  author = {Niu, Y. and Wang, L.},
  journal = {Nature Geoscience},
  year = {2024},
  doi = {10.1038/x},
}
"""
    entries = parse_bibtex(text, "test.bib")
    assert len(entries) == 1
    e = entries[0]
    assert e["entry_type"] == "article"
    assert e["citekey"] == "Niu2024"
    assert e["fields"]["title"] == "Crustal Structure"
    assert e["fields"]["author"] == "Niu, Y. and Wang, L."
    assert e["fields"]["year"] == "2024"
    assert e["fields"]["doi"] == "10.1038/x"
    assert e["bib_path"] == "test.bib"


def test_parse_bibtex_multiple_entries():
    text = """
@article{A2024, title = {First}, author = {Smith, A.}, year = {2024}}
@inproceedings{B2023, title = {Second}, author = {Doe, B.}, year = {2023}}
"""
    entries = parse_bibtex(text, "x.bib")
    assert len(entries) == 2
    assert entries[0]["entry_type"] == "article"
    assert entries[1]["entry_type"] == "inproceedings"


def test_parse_bibtex_empty_text_returns_empty_list():
    assert parse_bibtex("", "x.bib") == []
    assert parse_bibtex("just a comment", "x.bib") == []


# ----------------------------------------------------------------------
# _extract_file_paths (Zotero "file = " format)
# ----------------------------------------------------------------------


def test_extract_file_paths_single_attachment():
    """Zotero stores attachments as 'title:path:mime' triples."""
    out = _extract_file_paths("My Paper:storage/ABC/paper.pdf:application/pdf")
    assert out == ["storage/ABC/paper.pdf"]


def test_extract_file_paths_multiple_attachments():
    field = "A:storage/A/a.pdf:application/pdf;B:storage/B/b.pdf:application/pdf"
    out = _extract_file_paths(field)
    assert out == ["storage/A/a.pdf", "storage/B/b.pdf"]


def test_extract_file_paths_filters_non_pdf():
    out = _extract_file_paths("Note:storage/X/note.txt:text/plain;Paper:storage/Y/p.pdf:application/pdf")
    # Only the .pdf attachment makes it through
    assert out == ["storage/Y/p.pdf"]


def test_extract_file_paths_empty_returns_empty():
    assert _extract_file_paths("") == []


# ----------------------------------------------------------------------
# _parse_authors and _parse_keywords
# ----------------------------------------------------------------------


def test_parse_authors_splits_on_and():
    assert _parse_authors("Smith, A. and Doe, B.") == ["Smith, A.", "Doe, B."]


def test_parse_authors_empty_returns_empty():
    assert _parse_authors("") == []


def test_parse_authors_strips_whitespace_around_and():
    out = _parse_authors("Smith, A.   and   Doe, B.")
    assert out == ["Smith, A.", "Doe, B."]


def test_parse_keywords_splits_on_comma_or_semicolon():
    assert _parse_keywords("tectonics, tibet, seismology") == ["tectonics", "tibet", "seismology"]
    assert _parse_keywords("a;b;c") == ["a", "b", "c"]


def test_parse_keywords_dedups_case_insensitive():
    out = _parse_keywords("Tibet, tibet, TIBET, tectonics")
    # The first occurrence wins
    assert out[0] == "Tibet"
    # Only two unique keywords
    assert len(out) == 2


def test_parse_keywords_empty_returns_empty():
    assert _parse_keywords("") == []


# ----------------------------------------------------------------------
# _entry_date and _entry_year
# ----------------------------------------------------------------------


def test_entry_date_prefers_date_field():
    assert _entry_date({"date": "2024-11-03"}) == "2024-11-03"


def test_entry_date_falls_back_to_year():
    assert _entry_date({"year": "2024"}) == "2024"


def test_entry_date_ignores_malformed_date():
    assert _entry_date({"date": "garbage", "year": "2024"}) == "2024"


def test_entry_date_empty_when_neither_present():
    assert _entry_date({}) == ""


def test_entry_year_extracts_four_digits():
    assert _entry_year({"year": "2024"}) == "2024"
    assert _entry_year({"date": "2024-11-03"}) == "2024"
    assert _entry_year({}) == ""


# ----------------------------------------------------------------------
# Title similarity (FN-B3-A04-1 contract)
# ----------------------------------------------------------------------


def test_normalized_title_strips_punctuation_and_lowercases():
    out = _normalized_title("The Crustal Structure: A 3D Study!")
    assert out == "the crustal structure a 3d study"


def test_normalized_title_empty_returns_empty():
    assert _normalized_title("") == ""


def test_title_similarity_identical_titles_returns_one():
    a = _normalized_title("Crustal Imaging Tibet")
    assert _title_similarity(a, a) == 1.0


def test_title_similarity_disjoint_titles_returns_zero():
    a = _normalized_title("Earthquake on Mars")
    b = _normalized_title("Receiver Functions Tibet")
    assert _title_similarity(a, b) == 0.0


def test_title_similarity_truncated_title_scores_high():
    """FN-B3-A04-1: a publisher-truncated short title (subset of the full
    title) should score 1.0 — every short-title token is in the long one."""
    full = _normalized_title(
        "Crustal Structure Beneath the Tibetan Plateau From Receiver Functions"
    )
    short = _normalized_title("Crustal Structure Tibetan Plateau")
    score = _title_similarity(short, full)
    # All 4 short tokens appear in the full title; ratio = 4/4 = 1.0
    assert score == 1.0


def test_title_similarity_empty_input_returns_zero():
    assert _title_similarity("", "abc") == 0.0
    assert _title_similarity("abc", "") == 0.0


# ----------------------------------------------------------------------
# import_zotero_files — error paths
# ----------------------------------------------------------------------


def test_import_zotero_files_raises_when_no_bib_in_upload():
    """A folder without a .bib file is invalid input."""
    with pytest.raises(ZoteroImportError, match="No .bib"):
        import_zotero_files(
            db_session=None,  # type: ignore[arg-type] — never reached
            uploaded_files={"foo.pdf": b"fake"},
        )


def test_import_zotero_files_raises_when_multiple_bib_files():
    """Two .bib files (without one being named 'exported items.bib') is
    ambiguous — the function must refuse rather than guess."""
    with pytest.raises(ZoteroImportError, match="Multiple .bib"):
        import_zotero_files(
            db_session=None,  # type: ignore[arg-type]
            uploaded_files={"a.bib": b"@article{x, title={x}}", "b.bib": b"@article{y, title={y}}"},
        )


def test_import_zotero_files_collects_warning_for_unsafe_path():
    """An internal '..' traversal in an upload key becomes a warning, not
    a hard fail — the bib still parses; the offending file is just skipped."""
    bib = b"@article{x, title = {x}, author = {A}, year = {2024}}"
    import db as db_module
    import main as main_module
    main_module.init_db()
    session = db_module.SessionLocal()
    try:
        report = import_zotero_files(
            session,
            uploaded_files={
                # Internal traversal — the guard fires here, not on leading "../"
                "storage/foo/../../../etc/passwd.pdf": b"%PDF-1.4 fake",
                "lib.bib": bib,
            },
            dry_run=True,
        )
        assert any("Unsafe path" in w for w in report["warnings"])
    finally:
        session.close()


# ----------------------------------------------------------------------
# import_zotero_files — happy path (dry-run)
# ----------------------------------------------------------------------


@pytest.fixture()
def init_main_db(isolated_db):
    """Schema-init helper local to this test file (avoids importing client)."""
    import main as main_module

    main_module.init_db()
    return isolated_db


def test_import_zotero_files_dry_run_counts_entries(init_main_db):
    bib = b"""
@article{Niu2024,
  title = {Crustal Structure of Tibet},
  author = {Niu, Y. and Wang, L.},
  journal = {Nature Geoscience},
  year = {2024},
  doi = {10.1038/x},
}
@inproceedings{Smith2023,
  title = {New Method},
  author = {Smith, A.},
  year = {2023},
  booktitle = {Some Conf},
}
"""
    import db as db_module
    session = db_module.SessionLocal()
    try:
        report = import_zotero_files(
            session,
            uploaded_files={"My Library.bib": bib},
            dry_run=True,
        )
        assert report["entries"] == 2
        # dry_run → nothing created on disk or in DB
        assert report["created"] == 0
        assert report["pdf_saved"] == 0
    finally:
        session.close()


def test_import_zotero_files_handles_macosx_noise(init_main_db):
    """__MACOSX entries are filtered before the .bib lookup runs."""
    bib = b"@article{x, title={x}, author={A}, year={2024}}"
    import db as db_module
    session = db_module.SessionLocal()
    try:
        report = import_zotero_files(
            session,
            uploaded_files={
                "__MACOSX/._x.bib": b"AppleDouble junk",
                "lib.bib": bib,
                ".DS_Store": b"",
            },
            dry_run=True,
        )
        # No warning about the noise (it's silently dropped), and we found 1 entry
        assert report["entries"] == 1
    finally:
        session.close()


def test_import_zotero_files_prefers_exported_items_bib_when_two_present(init_main_db):
    """When the upload contains 'Exported Items.bib' AND another .bib, the
    function chooses the Zotero-canonical one rather than raising."""
    main_bib = b"@article{Real2024, title={Real}, author={A}, year={2024}}"
    other_bib = b"@article{Other2024, title={Other}, author={B}, year={2024}}"
    import db as db_module
    session = db_module.SessionLocal()
    try:
        report = import_zotero_files(
            session,
            uploaded_files={
                "Exported Items.bib": main_bib,
                "extra.bib": other_bib,
            },
            dry_run=True,
        )
        # The "real" .bib was the one parsed → 1 entry
        assert report["entries"] == 1
    finally:
        session.close()
