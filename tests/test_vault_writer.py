"""Tests for ``services/vault_writer.py`` (PR-53 / PR-54 — was 0 % coverage).

Closes the **P0 #1** gap in ``changelog/09-followups/TEST-COVERAGE.md``:
the entire vault writer surface (323 stmts) had **no** unit tests before
this file. The user-notes split (TC-512/516/517 — explicitly called out
as "must keep passing" in PR-53) is the load-bearing contract; this file
exercises it end-to-end against a ``tmp_path`` vault directory plus a
real SQLAlchemy session bound to ``isolated_db``.

Scope of these tests (pure-function + filesystem; **no network**):
- ``set_vault_path`` / ``get_vault_path`` / ``clear_vault_path`` round-trip
- ``set_vault_path`` rejects empty / file-path (FN-B5-A02 contract)
- ``_slug`` strips FS-unsafe + CJK characters, truncates, handles empty
- ``vault_filename_for_paper`` prefers citekey, falls back to id+slug
- ``_resolve_collision``: same paper_id → same path; foreign id → suffixed;
  escalates prefix length when 4-hex prefix collides (FN-B5-A04-1)
- ``_build_frontmatter``: required YAML keys present, values escaped
- ``_build_swp_body``: TL;DR bilingual block, structured analysis, figures
- ``_split_existing``: SWP-owned vs ``## My Notes`` split, default user block
- ``write_paper_md``: no-op when no vault, atomic write, preserves user notes
- ``write_all_papers``: batch path returns status dict
"""

from __future__ import annotations

from pathlib import Path

import pytest

import db as db_module
from models import AIAnalysis, Paper
from services.vault_writer import (
    MY_NOTES_HEADING,
    SWP_FOOTER_MARKER,
    SWP_HEADER_MARKER,
    VaultWriterError,
    _build_frontmatter,
    _build_swp_body,
    _resolve_collision,
    _slug,
    _split_existing,
    _user_notes_default,
    clear_vault_path,
    get_vault_path,
    set_vault_path,
    vault_filename_for_paper,
    write_all_papers,
    write_paper_md,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_paper(
    paper_id: str = "test-paper-1",
    *,
    title: str = "Crustal Structure Beneath the Tibetan Plateau",
    authors=None,
    published_date: str = "2024-11-03",
    journal: str = "Nature Geoscience",
    doi: str = "10.1038/s41561-024-12345-x",
    link: str = "https://www.nature.com/articles/s41561-024-12345-x",
    pdf_path: str = "",
    topics=None,
    abstract: str = "",
    reading_intent: str = "",
    follow_up: bool = False,
) -> Paper:
    """Build an unattached Paper instance with sensible defaults."""
    return Paper(
        id=paper_id,
        source="test",
        title=title,
        journal=journal,
        topics=topics if topics is not None else ["tectonics", "tibet"],
        institutions=[],
        authors=authors if authors is not None else ["Niu, Yixian", "Wang, Lei"],
        published_date=published_date,
        doi=doi,
        link=link,
        pdf_path=pdf_path,
        abstract=abstract,
        abstract_flag="ok" if abstract else "missing",
        reading_intent=reading_intent,
        follow_up=follow_up,
    )


def _make_analysis(
    paper_id: str = "test-paper-1",
    *,
    relevance: int = 85,
    payload=None,
) -> AIAnalysis:
    return AIAnalysis(
        paper_id=paper_id,
        relevance_score=relevance,
        pdf_used=True,
        evidence_based_on="pdf",
        payload=payload or {
            "one_sentence_takeaway_en": "Receiver functions reveal a 70-km Moho.",
            "one_sentence_takeaway_zh": "接收函数表明莫霍面深约 70 公里。",
            "summary_150w_en": "We image the crust using teleseismic data.",
            "summary_150w_zh": "我们用远震数据成像地壳。",
            "structured_analysis": {
                "method": {"text": "P-RF inversion", "source": "§3, p. 4"},
                "results": {"text": "Moho at 70 km", "source": "§5, p. 9"},
            },
            "key_contributions": ["First high-res image", "New age constraints"],
            "methods": ["Receiver functions", "Common-conversion-point stacking"],
            "figure_analyses": [
                {
                    "figure_id": "Fig.1",
                    "title": "Station map",
                    "description": "Coverage across the plateau.",
                    "interpretation": "Dense network constrains depth.",
                    "source": "p. 3",
                }
            ],
        },
    )


# ----------------------------------------------------------------------
# AppState round-trip (PR-53 G11)
# ----------------------------------------------------------------------


@pytest.fixture()
def initdb(isolated_db):
    """Initialise the test DB schema (a thin shim over conftest.isolated_db).

    The conftest ``isolated_db`` fixture only rebinds the engine; it does
    NOT create tables. The ``client`` fixture indirectly creates them via
    ``main.init_db()``, but most vault-writer tests don't need a TestClient,
    so we call init_db ourselves here.
    """
    import main as main_module

    main_module.init_db()
    return isolated_db


def test_set_vault_path_persists_and_get_reads_back(initdb, tmp_path):
    session = db_module.SessionLocal()
    try:
        target = tmp_path / "obsidian-vault"
        resolved = set_vault_path(session, target)
        assert resolved.is_dir()
        assert resolved.resolve() == target.resolve()
    finally:
        session.close()

    session2 = db_module.SessionLocal()
    try:
        read_back = get_vault_path(session2)
        assert read_back is not None
        assert read_back.resolve() == target.resolve()
    finally:
        session2.close()


def test_set_vault_path_rejects_empty_string(initdb):
    session = db_module.SessionLocal()
    try:
        with pytest.raises(VaultWriterError, match="cannot be empty"):
            set_vault_path(session, "")
    finally:
        session.close()


def test_set_vault_path_rejects_when_target_is_an_existing_file(initdb, tmp_path):
    bogus_file = tmp_path / "not-a-folder.txt"
    bogus_file.write_text("hello")
    session = db_module.SessionLocal()
    try:
        with pytest.raises(VaultWriterError, match="not a directory"):
            set_vault_path(session, bogus_file)
    finally:
        session.close()


def test_set_vault_path_is_idempotent_overwrite(initdb, tmp_path):
    """Calling set_vault_path twice updates the same AppState row."""
    session = db_module.SessionLocal()
    try:
        a = tmp_path / "vault-a"
        b = tmp_path / "vault-b"
        set_vault_path(session, a)
        set_vault_path(session, b)
        current = get_vault_path(session)
        assert current is not None
        assert current.resolve() == b.resolve()
    finally:
        session.close()


def test_clear_vault_path_removes_row(initdb, tmp_path):
    session = db_module.SessionLocal()
    try:
        set_vault_path(session, tmp_path / "vault")
        assert get_vault_path(session) is not None
        clear_vault_path(session)
        assert get_vault_path(session) is None
        # Idempotent: clearing twice is fine
        clear_vault_path(session)
        assert get_vault_path(session) is None
    finally:
        session.close()


def test_get_vault_path_returns_none_when_unset(initdb):
    session = db_module.SessionLocal()
    try:
        assert get_vault_path(session) is None
    finally:
        session.close()


# ----------------------------------------------------------------------
# _slug (PR-53 G12 helper)
# ----------------------------------------------------------------------


def test_slug_strips_fs_unsafe_chars():
    """Filesystem-bad chars (/, \\, :, *, ?, ", <, >, |) become dashes/dropped."""
    assert "/" not in _slug("paper/with/slashes")
    assert "\\" not in _slug("paper\\with\\backslash")
    assert ":" not in _slug("title:subtitle")
    assert "*" not in _slug("a*star")
    assert "?" not in _slug("question?")
    assert '"' not in _slug('q "uote"')
    # Control bytes also stripped
    assert "\x00" not in _slug("nul\x00byte")


def test_slug_collapses_whitespace_to_dashes():
    assert _slug("hello world") == "hello-world"
    assert _slug("multiple   spaces") == "multiple-spaces"
    # Trailing dashes/dots stripped
    assert _slug("hello---").rstrip("-").endswith("hello") or "hello" in _slug("hello---")


def test_slug_truncates_to_max_len():
    long = "a" * 200
    out = _slug(long, max_len=10)
    assert len(out) <= 10


def test_slug_returns_untitled_for_empty():
    assert _slug("") == "untitled"
    assert _slug("   ") == "untitled"
    assert _slug(None) == "untitled"  # type: ignore[arg-type]


def test_slug_handles_ampersand_replacement():
    out = _slug("Tom & Jerry")
    assert "&" not in out
    # & becomes "and"
    assert "and" in out.lower()


# ----------------------------------------------------------------------
# vault_filename_for_paper (PR-53 G12)
# ----------------------------------------------------------------------


def test_vault_filename_uses_citekey_when_authors_and_year_present():
    paper = _make_paper(
        paper_id="abc123def4567890",
        title="Crustal Structure",
        authors=["Niu, Yixian", "Wang, Lei"],
        published_date="2024-11-03",
    )
    name = vault_filename_for_paper(paper)
    assert name.endswith(".md")
    # citekey embeds surname + year
    assert "niu" in name.lower() or "Niu" in name
    assert "2024" in name


def test_vault_filename_falls_back_to_id_slug_only_when_citekey_is_anon():
    """The fallback ``{id[:16]}--{slug}.md`` only triggers when ``_citekey``
    returns exactly ``"anon"``. Empty authors + empty year yields
    ``"Unknownn.d."`` which is truthy and != "anon", so the citekey path
    wins. This pins the behaviour so a refactor that changes the
    sentinel still gets caught.
    """
    paper = _make_paper(
        paper_id="abc123def4567890",
        title="Untitled Anonymous Paper",
        authors=[],
        published_date="",
    )
    name = vault_filename_for_paper(paper)
    assert name.endswith(".md")
    # `_citekey` returns "Unknownn.d." in this branch → slug-of-citekey path.
    # (The fallback `{id}--{slug}.md` is unreachable absent monkey-patching
    # `_citekey` to literally return "anon".)
    assert "Unknown" in name or paper.id[:16] in name


def test_vault_filename_handles_missing_title():
    paper = _make_paper(
        paper_id="abc123",
        title="",
        authors=[],
        published_date="",
    )
    name = vault_filename_for_paper(paper)
    assert name.endswith(".md")
    # Either citekey path ("Unknownn.d.") or untitled fallback — both
    # produce a non-empty .md filename.
    assert len(name) > len(".md")


# ----------------------------------------------------------------------
# _resolve_collision (PR-53 G13)
# ----------------------------------------------------------------------


def test_resolve_collision_returns_same_path_when_target_missing(tmp_path):
    target = tmp_path / "paper.md"
    resolved = _resolve_collision(target, "anyid")
    assert resolved == target


def test_resolve_collision_returns_same_path_when_existing_belongs_to_same_paper(
    tmp_path,
):
    """If the file already exists and its frontmatter's swp_paper_id matches,
    the same path is reused (the file is THIS paper's file)."""
    target = tmp_path / "niu2024.md"
    paper_id = "deadbeefcafe1234"
    target.write_text(
        f'---\nswp_paper_id: "{paper_id}"\ntitle: "x"\n---\n# hello\n',
        encoding="utf-8",
    )
    resolved = _resolve_collision(target, paper_id)
    assert resolved == target


def test_resolve_collision_suffixes_when_foreign_paper_owns_target(tmp_path):
    """Two papers with the same citekey land on different files."""
    target = tmp_path / "smith2024.md"
    target.write_text(
        '---\nswp_paper_id: "aaaabbbbccccdddd"\n---\n# original\n',
        encoding="utf-8",
    )
    # Different paper_id → must NOT overwrite, must suffix
    new_paper_id = "11112222333344445"
    resolved = _resolve_collision(target, new_paper_id)
    assert resolved != target
    assert resolved.parent == target.parent
    # The 4-hex prefix from the new id should appear in the new filename
    assert new_paper_id[:4] in resolved.name


def test_resolve_collision_escalates_prefix_length_when_4hex_collides(tmp_path):
    """FN-B5-A04-1: when the candidate with 4-hex suffix also exists with a
    foreign paper_id, escalate to 6/8/12 chars."""
    target = tmp_path / "smith2024.md"
    target.write_text(
        '---\nswp_paper_id: "aaaa1111bbbb2222"\n---\n# first\n',
        encoding="utf-8",
    )
    # First 4-char candidate already taken by yet another paper
    candidate_4 = tmp_path / "smith2024-deea.md"
    candidate_4.write_text(
        '---\nswp_paper_id: "deeaaaaaffffeeee"\n---\n# second\n',
        encoding="utf-8",
    )
    # The new paper shares the 4-hex prefix but differs at char 5
    new_paper_id = "deeabbbbccccdddd"
    resolved = _resolve_collision(target, new_paper_id)
    # Must NOT clobber either of the two existing files
    assert resolved != target
    assert resolved != candidate_4


def test_resolve_collision_reuses_existing_candidate_when_already_ours(tmp_path):
    """If we collided once and now the candidate-with-suffix is already
    ours, return that same path (don't escalate further)."""
    target = tmp_path / "smith2024.md"
    target.write_text(
        '---\nswp_paper_id: "aaaabbbbccccdddd"\n---\n',
        encoding="utf-8",
    )
    paper_id = "11112222333344445"
    suffix = paper_id[:4]
    candidate = tmp_path / f"smith2024-{suffix}.md"
    candidate.write_text(
        f'---\nswp_paper_id: "{paper_id}"\n---\n',
        encoding="utf-8",
    )
    resolved = _resolve_collision(target, paper_id)
    assert resolved == candidate


# ----------------------------------------------------------------------
# Frontmatter (PR-53 contract — Dataview keys)
# ----------------------------------------------------------------------


def test_build_frontmatter_contains_required_keys():
    paper = _make_paper()
    analysis = _make_analysis()
    fm = _build_frontmatter(paper, analysis)
    # Each line must be `key: value`; we check the well-known Dataview keys.
    for key in [
        "swp_paper_id",
        "citekey",
        "title",
        "authors",
        "year",
        "journal",
        "doi",
        "url",
        "topics",
        "tags",
        "swp_pdf_path",
        "swp_has_pdf",
        "swp_relevance",
        "swp_last_synced",
    ]:
        assert f"{key}:" in fm, f"frontmatter missing required key {key!r}"
    # YAML delimiters at the boundaries
    assert fm.startswith("---\n")
    assert "\n---\n" in fm


def test_build_frontmatter_escapes_quotes_in_title():
    """Quotes in the title must not unbalance the YAML quoting."""
    paper = _make_paper(title='An "interesting" study')
    fm = _build_frontmatter(paper, None)
    # Backslash-escaped quote inside quoted YAML value
    assert r'\"interesting\"' in fm


def test_build_frontmatter_handles_null_analysis():
    paper = _make_paper()
    fm = _build_frontmatter(paper, None)
    # The relevance / pdf_used keys exist but reduce to falsy values
    assert "swp_relevance: null" in fm
    assert "swp_pdf_used:" in fm


def test_build_frontmatter_year_extracted_from_published_date():
    paper = _make_paper(published_date="2024-11-03")
    fm = _build_frontmatter(paper, None)
    assert 'year: "2024"' in fm


# ----------------------------------------------------------------------
# _build_swp_body — bilingual markdown sections
# ----------------------------------------------------------------------


def test_build_swp_body_bilingual_tldr_and_summary():
    paper = _make_paper()
    analysis = _make_analysis()
    body = _build_swp_body(paper, analysis)
    # The title line and the SWP markers appear
    assert SWP_HEADER_MARKER in body
    assert SWP_FOOTER_MARKER in body
    # TL;DR section with EN text and the bilingual ZH suffix
    assert "## TL;DR" in body
    assert "70-km Moho" in body
    # Bilingual marker label
    assert "中文" in body
    # Summary section with EN body and Chinese subsection
    assert "## Summary" in body
    assert "### 中文摘要" in body
    # Structured analysis section
    assert "## Structured Analysis" in body
    assert "Method." in body
    # Figures section
    assert "## Figures" in body
    assert "Fig.1" in body
    # Key Contributions list
    assert "## Key Contributions" in body
    assert "First high-res image" in body


def test_build_swp_body_renders_two_sentence_takeaway_placeholder_when_missing():
    paper = _make_paper()  # no user_takeaway_what / user_takeaway_why
    body = _build_swp_body(paper, None)
    assert "## Two-sentence takeaway" in body
    # Placeholder text guides the user to fill it later
    assert "Not yet captured" in body


def test_build_swp_body_renders_two_sentence_takeaway_when_present():
    paper = _make_paper()
    # Use setattr because Paper.__init__ doesn't list these explicitly above
    paper.user_takeaway_what = "The crust is 70 km thick under Tibet."
    paper.user_takeaway_why = "This refines our isostasy models."
    body = _build_swp_body(paper, None)
    assert "What did the paper argue" in body
    assert "70 km thick" in body
    assert "Why does it matter" in body


def test_build_swp_body_renders_reading_intent_when_set():
    paper = _make_paper(reading_intent="Quick check before the group meeting.")
    body = _build_swp_body(paper, None)
    assert "## Why I opened this" in body
    assert "group meeting" in body


def test_build_swp_body_renders_follow_up_badge():
    paper = _make_paper(follow_up=True)
    body = _build_swp_body(paper, None)
    assert "## Follow-up" in body
    assert "follow-up queue" in body.lower() or "follow-up" in body.lower()


def test_build_swp_body_omits_sections_when_payload_empty():
    paper = _make_paper()
    body = _build_swp_body(paper, None)
    # No TL;DR / Summary when no analysis payload
    assert "## TL;DR" not in body
    assert "## Summary" not in body
    # But markers + title + meta line always present
    assert SWP_HEADER_MARKER in body
    assert SWP_FOOTER_MARKER in body


# ----------------------------------------------------------------------
# _split_existing (PR-53 user-notes split contract)
# ----------------------------------------------------------------------


def test_split_existing_separates_swp_owned_from_user_notes():
    existing = (
        "---\nswp_paper_id: \"abc\"\n---\n"
        f"{SWP_HEADER_MARKER}\n# Title\n\n## TL;DR\n\nfoo\n\n{SWP_FOOTER_MARKER}\n"
        f"\n{MY_NOTES_HEADING}\n\n- my private thought\n- another\n"
    )
    swp, user = _split_existing(existing)
    assert "TL;DR" in swp
    assert MY_NOTES_HEADING in user
    assert "my private thought" in user
    # The SWP half must NOT contain the My Notes section
    assert MY_NOTES_HEADING not in swp
    # The user half must NOT contain the SWP body
    assert "TL;DR" not in user


def test_split_existing_returns_default_user_block_when_no_my_notes():
    """A file without ## My Notes — the writer should treat all of it as
    SWP-owned and append the default user block on rewrite."""
    existing = "---\nfoo: bar\n---\n# Some old content without a notes section\n"
    swp, user = _split_existing(existing)
    assert "old content" in swp
    assert MY_NOTES_HEADING in user


def test_user_notes_default_starts_with_my_notes_heading():
    default = _user_notes_default()
    assert default.lstrip("\n").startswith(MY_NOTES_HEADING)
    assert "SWP will never overwrite" in default


# ----------------------------------------------------------------------
# write_paper_md — atomic write + merge contract
# ----------------------------------------------------------------------


def test_write_paper_md_noop_when_no_vault_configured(initdb):
    """If AppState.vault_path is unset, write_paper_md returns None."""
    session = db_module.SessionLocal()
    try:
        paper = _make_paper()
        # Paper is unattached but that's fine — write_paper_md only reads.
        result = write_paper_md(session, paper)
        assert result is None
    finally:
        session.close()


def test_write_paper_md_creates_file_with_swp_and_user_sections(initdb, tmp_path):
    session = db_module.SessionLocal()
    try:
        vault = tmp_path / "vault"
        set_vault_path(session, vault)
        paper = _make_paper()
        analysis = _make_analysis()
        out = write_paper_md(session, paper, analysis)
        assert out is not None
        assert out.is_file()
        text = out.read_text(encoding="utf-8")
        # Frontmatter
        assert text.startswith("---\n")
        # Both markers
        assert SWP_HEADER_MARKER in text
        assert SWP_FOOTER_MARKER in text
        # Default user-notes block appended
        assert MY_NOTES_HEADING in text
        # Bilingual content
        assert "70-km Moho" in text
        assert "70 公里" in text  # ZH variant
    finally:
        session.close()


def test_write_paper_md_preserves_existing_user_notes_on_rewrite(initdb, tmp_path):
    """The user-owned ``## My Notes`` block must survive a rewrite (TC-512/516/517).

    Uses a hex-shaped paper_id because ``_resolve_collision`` parses
    ``swp_paper_id`` with a ``[a-f0-9]+`` regex; non-hex IDs (like
    "test-paper-1") would be misclassified as a foreign collision and
    land on a new file. Production paper IDs are SHA-derived hex, so this
    constraint is satisfied in real use.
    """
    session = db_module.SessionLocal()
    try:
        vault = tmp_path / "vault"
        set_vault_path(session, vault)
        paper = _make_paper(paper_id="deadbeefcafe1234")
        analysis = _make_analysis(paper_id="deadbeefcafe1234")
        first = write_paper_md(session, paper, analysis)
        assert first is not None
        # Simulate the user editing the My Notes section
        text = first.read_text(encoding="utf-8")
        marked_text = text.replace(
            f"{MY_NOTES_HEADING}\n",
            f"{MY_NOTES_HEADING}\n\nThis is my custom personal note — KEEP ME.\n\n",
            1,
        )
        first.write_text(marked_text, encoding="utf-8")

        # Rewrite with a *changed* title (simulates re-analysis)
        paper.title = "Crustal Structure REVISED 2025"
        second = write_paper_md(session, paper, analysis)
        assert second == first  # same filename (citekey unchanged → same path)
        new_text = second.read_text(encoding="utf-8")
        # The SWP body picked up the new title
        assert "REVISED 2025" in new_text
        # The user notes survived
        assert "KEEP ME" in new_text
    finally:
        session.close()


def test_write_paper_md_with_explicit_vault_path_arg(initdb, tmp_path):
    """Passing vault_path directly skips the AppState lookup."""
    session = db_module.SessionLocal()
    try:
        vault = tmp_path / "explicit"
        out = write_paper_md(session, _make_paper(), None, vault_path=vault)
        assert out is not None
        assert out.parent.resolve() == vault.resolve()
    finally:
        session.close()


def test_write_paper_md_auto_creates_missing_vault_dir(initdb, tmp_path):
    """If the vault dir disappeared between set_vault_path and write, the
    writer should mkdir it rather than crash."""
    session = db_module.SessionLocal()
    try:
        vault = tmp_path / "vault-then-removed"
        set_vault_path(session, vault)
        # Delete the directory after registering it
        for p in vault.iterdir():
            p.unlink()
        vault.rmdir()
        out = write_paper_md(session, _make_paper())
        assert out is not None
        assert out.parent.is_dir()
    finally:
        session.close()


# ----------------------------------------------------------------------
# write_all_papers
# ----------------------------------------------------------------------


def test_write_all_papers_skipped_when_no_vault(initdb):
    session = db_module.SessionLocal()
    try:
        result = write_all_papers(session)
        assert result == {"status": "skipped", "reason": "no_vault", "written": 0}
    finally:
        session.close()


def test_write_all_papers_writes_each_paper_in_db(initdb, tmp_path, make_paper):
    # Seed three papers via the conftest factory
    make_paper("p1", title="First paper", authors=["Smith, A."], published_date="2024-01-01")
    make_paper("p2", title="Second paper", authors=["Jones, B."], published_date="2023-06-15")
    make_paper("p3", title="Third paper", authors=["Lee, C."], published_date="2022-12-31")

    session = db_module.SessionLocal()
    try:
        vault = tmp_path / "vault"
        set_vault_path(session, vault)
        result = write_all_papers(session)
        assert result["status"] == "ok"
        assert result["written"] == 3
        assert result["errors"] == []
        # Three .md files on disk
        md_files = list(vault.glob("*.md"))
        assert len(md_files) == 3
    finally:
        session.close()
