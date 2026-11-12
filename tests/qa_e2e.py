"""SWP Paper Hub — End-to-end QA harness.

Run all batches:
    pytest tests/qa_e2e.py -v

Run a single batch:
    pytest tests/qa_e2e.py::TestBatch01Projects -v

Each test case is named TC_NNN to match the QA plan numbering. Cases that
require Gemini quota or network are marked with `@pytest.mark.slow` and
skipped by default; run with `-m slow` to include them.

Created by the 10-batch QA suite.
This file is populated incrementally as each batch agent ships its cases.
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest


# ──────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
QA_DIR = REPO_ROOT / "qa"
SEED_DIR = QA_DIR / "seed"
DB_PATH = REPO_ROOT / "seismo_hub.db"
BASE_URL = os.environ.get("SWP_QA_BASE_URL", "http://127.0.0.1:31462")


@pytest.fixture(scope="session")
def client():
    """HTTP client pinned to the running uvicorn instance."""
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        # Healthcheck — fail loudly if server isn't up so the human sees it
        try:
            r = c.get("/api/runtime")
            r.raise_for_status()
        except Exception as e:
            pytest.exit(f"Server at {BASE_URL} not reachable: {e}")
        yield c


@pytest.fixture(scope="session")
def db():
    """Read-only SQLite connection for direct DB assertions."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def cleanup_qa_data(db, client):
    """Per-test cleanup hook — removes all QA-* projects and qa_* papers
    once a test finishes. Safe even if the test only created a subset."""
    yield
    # Cascade-delete QA-* projects (which removes project_papers rows)
    projs = client.get("/api/projects").json().get("projects", [])
    for p in projs:
        if p["name"].startswith("QA-"):
            client.delete(f"/api/projects/{p['id']}")
    # Manual SQL cleanup for orphan papers created via BibTeX import
    cur = db.cursor()
    cur.execute("DELETE FROM ai_analyses WHERE paper_id LIKE 'qa_%' OR paper_id LIKE 'zotero_%qa%'")
    cur.execute("DELETE FROM papers WHERE id LIKE 'qa_%' OR id LIKE 'zotero_%qa%'")
    db.commit()


# ──────────────────────────────────────────────────────────────────────────
# Utility helpers used by multiple batches
# ──────────────────────────────────────────────────────────────────────────

def create_project(client, name: str, type_: str = "monthly", config: Optional[Dict] = None,
                   backfill: bool = True) -> Dict:
    body = {"name": name, "type": type_, "config": config or {}, "backfill": backfill}
    r = client.post("/api/projects", json=body)
    assert r.status_code in (200, 201), f"Create failed: {r.status_code} {r.text}"
    return r.json()


def get_project(client, pid: str) -> Dict:
    return client.get(f"/api/projects/{pid}").json()


def list_project_papers(client, pid: str) -> List[Dict]:
    return client.get(f"/api/papers", params={"project_id": pid}).json().get("papers", [])


def import_bibtex(client, pid: str, bib_path: Path) -> Dict:
    with open(bib_path, "rb") as f:
        files = {"file": (bib_path.name, f, "application/x-bibtex")}
        r = client.post(f"/api/projects/{pid}/import_bibtex", files=files)
    return r.json()


# ──────────────────────────────────────────────────────────────────────────
# Batch classes — populated by per-batch QA agents
# ──────────────────────────────────────────────────────────────────────────

class TestBatch01Projects:
    """Batch 1 — Project CRUD & lifecycle. TC-101..150."""

    def test_tc_101_create_monthly_project(self, client, cleanup_qa_data):
        proj = create_project(client, "QA-B1-A01-T101", type_="monthly")
        assert proj["type"] == "monthly"
        assert proj["name"] == "QA-B1-A01-T101"
        assert proj["paper_count"] == 0

    def test_tc_102_create_topic_monitor_canonical(self, client, cleanup_qa_data):
        proj = create_project(
            client, "QA-B1-A01-T102", type_="topic_monitor",
            config={"topics": ["Fibre-optic seismology"]}, backfill=False,
        )
        assert proj["type"] == "topic_monitor"
        assert proj["config"]["topics"] == ["Fibre-optic seismology"]

    def test_tc_103_create_static(self, client, cleanup_qa_data):
        proj = create_project(client, "QA-B1-A01-T103", type_="static")
        assert proj["type"] == "static"

    # TC-104..150 to be filled by Batch 1 agents


class TestBatch02TopicMonitor:
    """Batch 2 — Topic Monitor matching. TC-201..250."""
    pass


class TestBatch03Import:
    """Batch 3 — BibTeX/RIS Import. TC-301..350."""
    pass


class TestBatch04AIAnalysis:
    """Batch 4 — AI Analysis pipeline. TC-401..450."""
    pass


class TestBatch05Vault:
    """Batch 5 — Vault Writer. TC-501..550."""
    pass


class TestBatch06Reading:
    """Batch 6 — Light Reading Workflow. TC-601..650."""
    pass


class TestBatch07Export:
    """Batch 7 — Export & Download. TC-701..750."""
    pass


class TestBatch08Upload:
    """Batch 8 — Upload, Watch, External Reader. TC-801..850."""
    pass


class TestBatch09UI:
    """Batch 9 — UI Flows (browser-driven, may require Playwright). TC-901..950."""
    pass


class TestBatch10Integration:
    """Batch 10 — Cross-cutting Integration. TC-1001..1050."""
    pass
