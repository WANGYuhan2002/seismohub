"""Pytest fixtures for the SWP Paper Hub test suite (M1 F13).

Provides a TestClient backed by an isolated SQLite file so tests never
mutate the developer's real ``seismo_hub.db``. Each test gets a fresh DB.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

# Make the repo root importable regardless of cwd
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 2026-06-15: disable the fresh-install preset-tracker seeder during tests — it
# would populate every isolated test DB with the 7 starter trackers and break
# fixtures that assert project counts / inbox contents. Set at module level so
# it lands before any `db` import / init_db() call.
os.environ.setdefault("SEISMO_NO_PRESET_SEED", "1")


@pytest.fixture(scope="session", autouse=True)
def _silence_browser_open() -> None:
    """Prevent the FastAPI startup hook from opening the user's browser."""
    os.environ.setdefault("SEISMO_OPEN_BROWSER", "0")
    # PR-19: suppress the paragraph-anchor backfill daemon thread during
    # tests — it races with schema-snapshot idempotency checks.
    os.environ.setdefault("DISABLE_PARAGRAPH_BACKFILL", "1")
    # PR-XX: suppress the per-tracker cadence scheduler during tests —
    # it would loop in the background and race with assertions that
    # check tracker last_match_at / ProjectPaper counts. The dedicated
    # tests call run_tracker_pass_once() directly.
    os.environ.setdefault("DISABLE_TRACKER_SCHEDULER", "1")
    # T30: suppress the startup metadata-backfill daemon during tests —
    # it would issue live HTTP calls to CrossRef/OpenAlex/S2 and mutate
    # paper rows mid-assertion. Tests that need to exercise the backfill
    # call _backfill_mangled_metadata() directly with mocks.
    os.environ.setdefault("DISABLE_METADATA_BACKFILL", "1")
    # 2026-07-05: suppress the eager background AI-analysis daemon during tests —
    # it would loop making live Gemini calls on every un-analysed paper. Tests
    # that exercise analysis call analyze_pending_papers()/analyze_on_open() directly.
    os.environ.setdefault("DISABLE_EAGER_ANALYSIS", "1")


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Reroute ``db.DATABASE_PATH`` / ``db.engine`` to a tmp SQLite file.

    Yields the temp DB path so tests can poke at it directly if needed.
    """
    tmp_db = tmp_path / "test_seismo.db"
    # Import lazily so monkeypatching wins over module-import caching
    import db as db_module  # type: ignore
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    new_engine = create_engine(
        f"sqlite:///{tmp_db}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    new_session = sessionmaker(bind=new_engine, autoflush=False, autocommit=False, expire_on_commit=False)

    monkeypatch.setattr(db_module, "DATABASE_PATH", tmp_db, raising=True)
    monkeypatch.setattr(db_module, "DATABASE_URL", f"sqlite:///{tmp_db}", raising=True)
    monkeypatch.setattr(db_module, "engine", new_engine, raising=True)
    monkeypatch.setattr(db_module, "SessionLocal", new_session, raising=True)

    # main.py captured a reference to the old DATABASE_PATH / SessionLocal at
    # import time — patch those too so /api/health and route handlers use the
    # temp DB. Only the symbol names exposed at the module top level matter.
    try:
        import main as main_module  # type: ignore

        monkeypatch.setattr(main_module, "DATABASE_PATH", tmp_db, raising=False)
        monkeypatch.setattr(main_module, "SessionLocal", new_session, raising=False)
    except Exception:
        pass

    yield tmp_db


@pytest.fixture()
def client(isolated_db: Path):
    """FastAPI TestClient using the isolated DB. init_db runs the migration."""
    from fastapi.testclient import TestClient

    import main  # type: ignore
    main.init_db()
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def downloads_with_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a fresh downloads/ folder containing a tiny valid PDF."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    sample = downloads / "sample.pdf"
    sample.write_bytes(_MINIMAL_PDF_BYTES)
    # Reroute DOWNLOADS_DIR everywhere it's read
    import services.pdf_naming as pdf_naming
    import services.pdf_launcher as pdf_launcher
    import main as main_module
    monkeypatch.setattr(pdf_naming, "DOWNLOADS_DIR", downloads, raising=True)
    monkeypatch.setattr(pdf_launcher, "DOWNLOADS_DIR", downloads, raising=True)
    monkeypatch.setattr(main_module, "DOWNLOADS_DIR", downloads, raising=False)
    return downloads


@pytest.fixture()
def make_paper(isolated_db):
    """Factory fixture: call make_paper(paper_id, **kwargs) to insert a Paper row.

    Returns a callable so tests can create multiple papers with different IDs.
    The Paper is inserted directly via SQLAlchemy so tests that need a paper
    without going through an HTTP import endpoint can use this.
    """
    created_ids: list = []

    def _factory(paper_id: str, **kwargs) -> str:
        import db as db_module
        from models import Paper

        defaults = dict(
            title=kwargs.pop("title", f"Test paper {paper_id}"),
            journal=kwargs.pop("journal", "Test Journal"),
            topics=kwargs.pop("topics", []),
            institutions=kwargs.pop("institutions", []),
            authors=kwargs.pop("authors", ["Test Author"]),
            abstract=kwargs.pop("abstract", ""),
            abstract_flag=kwargs.pop("abstract_flag", "missing"),
        )
        defaults.update(kwargs)
        session = db_module.SessionLocal()
        try:
            paper = Paper(id=paper_id, **defaults)
            session.add(paper)
            session.commit()
            created_ids.append(paper_id)
        finally:
            session.close()
        return paper_id

    return _factory


# Tiny 1-page PDF (valid header + minimal trailer). Used by ingest tests.
_MINIMAL_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n"
    b"0000000000 65535 f\n"
    b"0000000009 00000 n\n"
    b"0000000052 00000 n\n"
    b"0000000098 00000 n\n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n149\n%%EOF\n"
)
