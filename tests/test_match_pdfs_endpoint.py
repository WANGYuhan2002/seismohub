"""End-to-end tests for POST /api/papers/match_pdfs (上传遗失 PDF, 2026-06-16).

Builds real one-page PDFs with PyMuPDF so the endpoint's DOI/title extraction
runs for real, against an isolated DB + downloads dir. Covers: DOI exact-match
auto-attach, not-a-PDF skip, unmatched skip, and DOI conflict → /confirm.
"""
import pytest

pytest.importorskip("fitz")


def _make_pdf(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), text, fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def iso_downloads(tmp_path, monkeypatch):
    """Reroute DOWNLOADS_DIR + the match _PENDING dir to a temp folder."""
    import main
    import services.pdf_naming as pdf_naming
    dl = tmp_path / "dl"
    dl.mkdir()
    monkeypatch.setattr(main, "DOWNLOADS_DIR", dl, raising=False)
    monkeypatch.setattr(pdf_naming, "DOWNLOADS_DIR", dl, raising=True)
    monkeypatch.setattr(main, "_PENDING_PDF_DIR", dl / "_pending", raising=False)
    return dl


@pytest.fixture(autouse=True)
def spawn_recorder(monkeypatch):
    """Replace the fire-and-forget analysis trigger with a recorder so tests
    never spawn real Gemini threads, and can assert the attach → analyze wiring."""
    import main
    calls = []
    monkeypatch.setattr(
        main, "_spawn_pdf_analysis",
        lambda paper_id, pdf_path, source="match_pdfs": calls.append((paper_id, source)),
        raising=True,
    )
    return calls


def _pdf_path_of(paper_id: str):
    import db as db_module
    from models import Paper
    s = db_module.SessionLocal()
    try:
        p = s.get(Paper, paper_id)
        return p.pdf_path if p else None
    finally:
        s.close()


def test_match_attaches_by_doi(client, make_paper, iso_downloads, spawn_recorder):
    make_paper("paperA", doi="10.1234/test.2026", title="Slow slip on the megathrust")
    pdf = _make_pdf("Slow slip on the megathrust\nhttps://doi.org/10.1234/test.2026")
    r = client.post("/api/papers/match_pdfs", files={"files": ("a.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"]["attached"] == 1
    res = d["results"][0]
    assert res["action"] == "attached"
    assert res["method"] == "doi"
    assert res["paper_id"] == "paperA"
    assert _pdf_path_of("paperA")  # PDF now attached
    assert res.get("analysis") == "queued"
    assert ("paperA", "match_pdfs") in spawn_recorder  # attach → analyze wired


def test_not_a_pdf_is_skipped(client, iso_downloads):
    r = client.post("/api/papers/match_pdfs", files={"files": ("x.txt", b"hello world", "text/plain")})
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["action"] == "skipped"
    assert res["reason"] == "not_a_pdf"


def test_unmatched_is_stashed_as_unresolved(client, iso_downloads):
    # Empty library + a PDF with no DOI and no title hit → must NOT be silently
    # dropped: it is stashed and surfaced as 'unresolved', recoverable via /confirm.
    pdf = _make_pdf("Completely Unrelated Treatise On Nothing In Particular\nno identifier present")
    r = client.post("/api/papers/match_pdfs", files={"files": ("u.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    d = r.json()
    res = d["results"][0]
    assert res["action"] == "unresolved"
    assert res.get("pending")
    assert d["summary"]["unresolved"] == 1
    # bytes were stashed → a later manual attach is possible
    assert (iso_downloads / "_pending" / f"{res['pending']}.pdf").exists()


def test_unresolved_can_be_manually_attached(client, make_paper, iso_downloads, spawn_recorder):
    make_paper("target", doi="10.1/notinpdf", title="Some Target Paper With A Distinct Name")
    pdf = _make_pdf("An Unrelated Heading With No Identifier\nbody text only")
    r = client.post("/api/papers/match_pdfs", files={"files": ("u.pdf", pdf, "application/pdf")})
    res = r.json()["results"][0]
    assert res["action"] == "unresolved"
    # user hand-picks the target paper via the existing confirm endpoint
    r2 = client.post("/api/papers/match_pdfs/confirm",
                     json={"pending": res["pending"], "paper_id": "target"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "attached"
    assert r2.json().get("analysis") == "queued"
    assert ("target", "match_confirm") in spawn_recorder  # confirm → analyze wired
    assert _pdf_path_of("target")


def test_doi_conflict_then_confirm_with_replace(client, make_paper, iso_downloads):
    # Existing paper already HAS a (fake) pdf → DOI hit is a conflict, not auto-attach.
    make_paper("paperB", doi="10.5555/conflict", title="A paper",
               pdf_path="/nonexistent/old.pdf", pdf_sha256="deadbeefdeadbeef")
    pdf = _make_pdf("A paper\nhttps://doi.org/10.5555/conflict")
    r = client.post("/api/papers/match_pdfs", files={"files": ("b.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["action"] == "conflict"
    assert res["paper_id"] == "paperB"
    assert res.get("pending")

    # Without replace → 409
    r409 = client.post("/api/papers/match_pdfs/confirm",
                       json={"pending": res["pending"], "paper_id": "paperB"})
    assert r409.status_code == 409

    # With replace → attached
    r2 = client.post("/api/papers/match_pdfs/confirm",
                     json={"pending": res["pending"], "paper_id": "paperB", "replace": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "attached"
    new_path = _pdf_path_of("paperB")
    assert new_path and new_path != "/nonexistent/old.pdf"


def test_upload_blocks_duplicate_doi_then_force_creates(client, make_paper, iso_downloads):
    make_paper("dupA", doi="10.9999/dup", title="Duplicate Detection Paper")
    pdf = _make_pdf("Duplicate Detection Paper\nhttps://doi.org/10.9999/dup")
    # Create-side DOI check: without force → 409 exists_by_doi (don't silently dup).
    r = client.post("/api/papers/upload",
                    files={"file": ("d.pdf", pdf, "application/pdf")},
                    data={"force_duplicate": "false"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "exists_by_doi"
    assert detail["existing_paper_id"] == "dupA"
    assert detail.get("existing_title")
    # With force_duplicate → creates a second row (user chose "duplicate").
    r2 = client.post("/api/papers/upload",
                     files={"file": ("d.pdf", pdf, "application/pdf")},
                     data={"force_duplicate": "true"})
    assert r2.status_code == 200, r2.text
