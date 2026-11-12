"""Inbox endpoint contract tests (top-level nav §1.1).

Covers the three invariants for ``GET /api/inbox`` +
``POST /api/inbox/mark_viewed`` documented in
``changelog/04-feature-design/04-top-level-nav.md`` §1.1 and §2:

  1. Inbox is the union of papers across **kind='tracker'** projects
     only. Papers added to a collection do NOT leak into Inbox.
  2. POST /api/inbox/mark_viewed stamps the watermark forward, so the
     next GET returns a watermark string and unread_count drops to 0.
  3. Empty DB → ``{papers: [], tracker_count: 0}`` (zero-state).

Reuses the ``client`` / ``isolated_db`` / ``make_paper`` fixtures from
``tests/conftest.py``. No PDFs, no LLM calls, no network — pure CRUD
+ /api/inbox round-trip.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_tracker(client, *, name: str = "DAS tracker", topics=None) -> str:
    """Insert a kind='tracker' Project row and return its id.

    Goes through the ORM directly because POST /api/projects on this
    branch still writes legacy `type='topic_monitor'` without
    populating the new `kind/scope/cadence` triple — the redesign
    backfill only fires at boot. We need explicit kind='tracker' for
    /api/inbox to see the project.
    """
    import db as db_module
    from models import Project
    session = db_module.SessionLocal()
    try:
        proj = Project(
            id=str(uuid.uuid4()),
            name=name,
            type="topic_monitor",
            kind="tracker",
            scope="personal",
            cadence="weekly",
            config={"topics": list(topics or [])},
        )
        session.add(proj)
        session.commit()
        return proj.id
    finally:
        session.close()


def _create_collection(client, *, name: str = "My collection") -> str:
    """Insert a kind='collection' project and return its id.

    Goes through the ORM directly so this helper doesn't depend on the
    /api/projects POST handler accepting the new ``kind`` field
    explicitly (legacy POST sets kind via the type→triple backfill).
    """
    import db as db_module
    from models import Project
    session = db_module.SessionLocal()
    try:
        proj = Project(
            id=str(uuid.uuid4()),
            name=name,
            type="static",
            kind="collection",
            scope=None,
            cadence=None,
            default_sort_by="custom",
            config={},
        )
        session.add(proj)
        session.commit()
        return proj.id
    finally:
        session.close()


def _attach_paper(project_id: str, paper_id: str, folder: str = "", source: str = "topic_monitor") -> None:
    """Insert a ProjectPaper row directly so we control added_at exactly.

    Uses source='topic_monitor' by default because the Inbox endpoint now
    excludes manually-imported rows (source in {manual, upload, watch_folder,
    import}) to keep the daily-discovery feed clean.
    """
    import db as db_module
    from models import ProjectPaper
    session = db_module.SessionLocal()
    try:
        session.add(ProjectPaper(
            project_id=project_id,
            paper_id=paper_id,
            folder=folder,
            source=source,
        ))
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1. Inbox returns only kind='tracker' papers.
# ---------------------------------------------------------------------------

def test_inbox_endpoint_returns_only_tracker_papers(client, isolated_db: Path, make_paper) -> None:
    """Create one tracker + one collection; add one paper to each.

    GET /api/inbox must return exactly the tracker paper, with a
    ``source_tracker`` envelope pointing at the right project, and the
    collection paper must NOT appear in the response.
    """
    tracker_id = _create_tracker(client, name="DAS tracker", topics=["DAS"])
    collection_id = _create_collection(client, name="My static set")

    pid_tracker = make_paper("p-tracker-1", title="Tracker paper")
    pid_collection = make_paper("p-collection-1", title="Collection paper")
    _attach_paper(tracker_id, pid_tracker)
    _attach_paper(collection_id, pid_collection)

    r = client.get("/api/inbox")
    assert r.status_code == 200, r.text
    body = r.json()

    # Shape check — top-level keys per §1.1.
    assert set(body.keys()) >= {"papers", "watermark", "unread_count", "tracker_count"}, (
        f"Inbox response missing required keys. Got: {sorted(body.keys())}"
    )
    # 2026-05-25 reclassification: monthly Lit Review is now also kind=tracker
    # (it auto-buckets every paper by YYYY-MM). So a fresh DB seeds 1 tracker
    # (the default), and this test creates +1 more = 2 trackers total. The
    # COLLECTION the test creates is still NOT in the count.
    assert body["tracker_count"] == 2, (
        f"Expected tracker_count=2 (the seeded default Monthly Lit Review "
        f"+ this test's tracker), got {body['tracker_count']!r}"
    )

    # Tracker-paper present, collection-paper absent.
    ids = [p["id"] for p in body["papers"]]
    assert pid_tracker in ids, (
        f"Tracker paper {pid_tracker!r} missing from /api/inbox. "
        f"Returned ids: {ids!r}"
    )
    assert pid_collection not in ids, (
        f"Collection paper {pid_collection!r} leaked into Inbox. "
        f"Inbox must filter to kind='tracker' projects only."
    )

    # source_tracker envelope is correct (id + name).
    tracker_paper = next(p for p in body["papers"] if p["id"] == pid_tracker)
    assert tracker_paper.get("source_tracker") is not None, (
        f"Tracker paper missing source_tracker envelope: {tracker_paper!r}"
    )
    assert tracker_paper["source_tracker"]["id"] == tracker_id
    assert tracker_paper["source_tracker"]["name"] == "DAS tracker"

    # Manually-imported papers in a tracker must NOT appear in Inbox.
    pid_manual = make_paper("p-manual-1", title="Manually imported paper")
    _attach_paper(tracker_id, pid_manual, source="manual")
    r2 = client.get("/api/inbox")
    ids2 = [p["id"] for p in r2.json()["papers"]]
    assert pid_manual not in ids2, (
        f"Manually-imported paper leaked into Inbox — "
        f"Inbox must only show auto-tracked content (source=topic_monitor/backfill)."
    )


# ---------------------------------------------------------------------------
# 2. POST /mark_viewed persists a watermark; subsequent GET reflects it.
# ---------------------------------------------------------------------------

def test_inbox_mark_viewed_persists_watermark(client, isolated_db: Path, make_paper) -> None:
    """Round-trip: with one unread paper, unread_count == 1. After POST
    /api/inbox/mark_viewed, watermark is set and unread_count drops to 0.

    Re-issuing GET /api/inbox must return the same watermark string in
    the top-level field so the frontend can draw the divider.
    """
    tracker_id = _create_tracker(client, name="Marked-viewed tracker")
    pid = make_paper("p-mark-1", title="Pre-watermark paper")
    _attach_paper(tracker_id, pid)

    # Initially: watermark is None, every paper counts as unread.
    r0 = client.get("/api/inbox")
    assert r0.status_code == 200, r0.text
    body0 = r0.json()
    assert body0["watermark"] is None, (
        f"Fresh DB should have NULL watermark, got {body0['watermark']!r}"
    )
    assert body0["unread_count"] == 1, (
        f"Expected unread_count=1 with NULL watermark + 1 tracker paper, "
        f"got {body0['unread_count']!r}"
    )

    # POST mark_viewed → returns {ok, last_viewed_at}.
    r1 = client.post("/api/inbox/mark_viewed")
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1.get("ok") is True
    assert isinstance(body1.get("last_viewed_at"), str) and body1["last_viewed_at"]
    stamped = body1["last_viewed_at"]

    # GET /api/inbox now surfaces the watermark and unread_count == 0
    # (the existing paper was added BEFORE the stamp).
    r2 = client.get("/api/inbox")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["watermark"] == stamped, (
        f"Watermark mismatch after mark_viewed. POST returned "
        f"{stamped!r}, GET returned {body2['watermark']!r}"
    )
    assert body2["unread_count"] == 0, (
        f"After mark_viewed all existing papers should be 'seen' — "
        f"got unread_count={body2['unread_count']!r}"
    )

    # A second POST stamps a fresh value (idempotent but monotonic).
    time.sleep(0.005)  # ensure datetime.utcnow advances
    r3 = client.post("/api/inbox/mark_viewed")
    assert r3.status_code == 200
    assert r3.json()["last_viewed_at"] >= stamped


# ---------------------------------------------------------------------------
# 3. Empty DB → tracker_count == 0, papers == [].
# ---------------------------------------------------------------------------

def test_inbox_empty_when_no_papers_in_trackers(client, isolated_db: Path) -> None:
    """Fresh DB seeds the default 'Monthly Literature Review' which since
    2026-05-25 is now kind='tracker' (not 'collection') — but with zero
    papers attached. /api/inbox should return tracker_count=1 (the seed)
    AND papers=[] (no papers anywhere yet), so the frontend can render
    the §4.2 'All caught up' soft-empty state.
    """
    r = client.get("/api/inbox")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tracker_count"] == 1, (
        f"Expected tracker_count=1 on fresh DB (seeded Monthly Lit Review "
        f"is now kind=tracker). Got {body['tracker_count']!r}. "
        f"Full body: {body!r}"
    )
    assert body["papers"] == [], (
        f"Expected papers=[] on fresh DB. Got {body['papers']!r}"
    )
    assert body["unread_count"] == 0
