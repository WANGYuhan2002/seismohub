"""Tests for the per-tracker cadence-aware scheduler (PR-XX).

Covers six invariants of services.tracker_scheduler:

1. cadence_to_delta maps daily/weekly/monthly to 1d/7d/30d (and unknown
   values fall back to 30d).
2. _is_due returns True when last_match_at is None (first-run catch-up).
3. _is_due respects the cadence delta — daily fired 1h ago is not due,
   fired 25h ago is due.
4. run_tracker_pass_once links newly matched papers to a freshly created
   tracker on its first pass.
5. run_tracker_pass_once is idempotent — a second run does not double-link
   papers it already linked.
6. start_tracker_scheduler returns None when DISABLE_TRACKER_SCHEDULER=1
   (no daemon thread starts).

Time-sensitive tests use freezegun (added to requirements for PR-58).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _seed_tracker(
    db_session,
    *,
    name: str,
    cadence: str = "weekly",
    topics: list[str] | None = None,
    last_match_at: datetime | None = None,
) -> str:
    """Insert a kind=tracker Project row and return its id."""
    from models import Project

    cfg: dict = {"query": {}}
    if topics:
        cfg["query"]["topics"] = topics
        cfg["topics"] = topics  # legacy mirror

    proj = Project(
        id=str(uuid.uuid4()),
        name=name,
        type="topic_monitor",
        kind="tracker",
        scope="personal",
        cadence=cadence,
        config=cfg,
        last_match_at=last_match_at,
    )
    db_session.add(proj)
    db_session.commit()
    return proj.id


# --------------------------------------------------------------------------- #
# 1. cadence_to_delta                                                         #
# --------------------------------------------------------------------------- #

def test_cadence_to_delta_known_values() -> None:
    """daily → 1d, weekly → 7d, monthly → 30d; unknown / None → 30d default."""
    from services.tracker_scheduler import cadence_to_delta

    assert cadence_to_delta("daily") == timedelta(days=1)
    assert cadence_to_delta("weekly") == timedelta(days=7)
    assert cadence_to_delta("monthly") == timedelta(days=30)
    # Defensive defaults — bad cadence must not crash the scheduler.
    assert cadence_to_delta(None) == timedelta(days=30)
    assert cadence_to_delta("") == timedelta(days=30)
    assert cadence_to_delta("yearly") == timedelta(days=30)
    # Case-insensitive.
    assert cadence_to_delta("DAILY") == timedelta(days=1)


# --------------------------------------------------------------------------- #
# 2-3. _is_due                                                                #
# --------------------------------------------------------------------------- #

def test_due_check_first_run_is_due() -> None:
    """A tracker that has never run (last_match_at is None) is always due."""
    from services.tracker_scheduler import _is_due

    now = datetime(2026, 5, 25, 12, 0, 0)
    assert _is_due(now, None, "daily") is True
    assert _is_due(now, None, "weekly") is True
    assert _is_due(now, None, "monthly") is True


def test_due_check_respects_cadence() -> None:
    """Daily tracker fired 1h ago is not due; fired 25h ago is due."""
    from services.tracker_scheduler import _is_due

    now = datetime(2026, 5, 25, 12, 0, 0)

    # 1h ago for a daily tracker — not due (next run is 23h away).
    one_hour_ago = now - timedelta(hours=1)
    assert _is_due(now, one_hour_ago, "daily") is False

    # 25h ago for a daily tracker — due (5h past the 24h cadence).
    twenty_five_hours_ago = now - timedelta(hours=25)
    assert _is_due(now, twenty_five_hours_ago, "daily") is True

    # 6d ago for a weekly tracker — not due.
    six_days_ago = now - timedelta(days=6)
    assert _is_due(now, six_days_ago, "weekly") is False

    # 8d ago for a weekly tracker — due.
    eight_days_ago = now - timedelta(days=8)
    assert _is_due(now, eight_days_ago, "weekly") is True


# --------------------------------------------------------------------------- #
# 4. run_tracker_pass_once links new matches                                  #
# --------------------------------------------------------------------------- #

def test_run_pass_links_newly_matched_papers(
    client, isolated_db: Path, make_paper,
) -> None:
    """A tracker with topic="DAS" and a paper with topics=["DAS"] should
    end up linked after run_tracker_pass_once.
    """
    import db as db_module
    from models import ProjectPaper
    from services.tracker_scheduler import run_tracker_pass_once

    session = db_module.SessionLocal()
    try:
        pid = make_paper(
            "p-sched-1",
            title="DAS-detected slow slip",
            topics=["DAS"],
            journal="GRL",
            published_date="2026-05-24",
        )
        project_id = _seed_tracker(
            session, name="DAS daily tracker", cadence="daily", topics=["DAS"],
        )

        # Sanity check: nothing linked before the pass.
        before = session.query(ProjectPaper).filter_by(project_id=project_id).count()
        assert before == 0
    finally:
        session.close()

    stats = run_tracker_pass_once()
    assert stats["trackers_checked"] == 1
    assert stats["trackers_run"] == 1
    assert stats["papers_matched"] == 1

    # Verify the link was actually written.
    session = db_module.SessionLocal()
    try:
        rows = session.query(ProjectPaper).filter_by(project_id=project_id).all()
        assert len(rows) == 1, f"Expected 1 ProjectPaper row, got {len(rows)}"
        assert rows[0].paper_id == pid
        # Cadence no longer determines folder format (monthly since
        # 2026-06-13): the folder is YYYY-MM regardless of cadence.
        assert rows[0].folder == "2026-05", (
            f"Expected monthly folder '2026-05', got {rows[0].folder!r}"
        )
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 5. Idempotent — second run does not double-link.                            #
# --------------------------------------------------------------------------- #

def test_run_pass_skips_already_linked_papers(
    client, isolated_db: Path, make_paper,
) -> None:
    """Running the scheduler twice in a row must not produce duplicate
    ProjectPaper rows. The UniqueConstraint + 'already a member?' check in
    the matcher protects against this.
    """
    import db as db_module
    from models import ProjectPaper
    from services.tracker_scheduler import run_tracker_pass_once

    session = db_module.SessionLocal()
    try:
        make_paper(
            "p-idem-sched",
            title="Idempotent DAS paper",
            topics=["DAS"],
            published_date="2026-05-20",
        )
        _seed_tracker(
            session, name="DAS idem tracker", cadence="weekly", topics=["DAS"],
        )
    finally:
        session.close()

    # First pass — should link.
    stats1 = run_tracker_pass_once()
    assert stats1["papers_matched"] == 1

    # Second pass — tracker.last_match_at is now ~now(), so cadence(weekly=7d)
    # gates it out. trackers_run should be 0 on the second call.
    stats2 = run_tracker_pass_once()
    assert stats2["trackers_checked"] == 1
    assert stats2["trackers_run"] == 0, (
        f"Second pass should be cadence-gated (weekly, just ran), got "
        f"trackers_run={stats2['trackers_run']}"
    )

    # No duplicates regardless.
    session = db_module.SessionLocal()
    try:
        total = session.query(ProjectPaper).count()
        assert total == 1, (
            f"Expected exactly 1 ProjectPaper row after idempotent runs, "
            f"got {total}"
        )
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 6. Disabled via env var.                                                    #
# --------------------------------------------------------------------------- #

def test_disabled_via_env_var(monkeypatch) -> None:
    """When DISABLE_TRACKER_SCHEDULER=1, start_tracker_scheduler returns
    None and no thread is started.
    """
    from services import tracker_scheduler

    # The conftest fixture sets this to "1" already, but be explicit.
    monkeypatch.setenv("DISABLE_TRACKER_SCHEDULER", "1")
    # Reset module singleton so we're not seeing a previously-started thread.
    monkeypatch.setattr(tracker_scheduler, "_scheduler_thread", None)

    result = tracker_scheduler.start_tracker_scheduler()
    assert result is None, "Expected None when disabled, got %r" % (result,)


def test_enabled_when_env_unset(monkeypatch) -> None:
    """Without DISABLE_TRACKER_SCHEDULER, start_tracker_scheduler launches
    a daemon thread. We verify a Thread object comes back without letting
    the loop actually do anything by stubbing _scheduler_loop.
    """
    from services import tracker_scheduler

    monkeypatch.delenv("DISABLE_TRACKER_SCHEDULER", raising=False)
    monkeypatch.setattr(tracker_scheduler, "_scheduler_thread", None)

    # No-op loop so the daemon doesn't spam DB queries during the test.
    monkeypatch.setattr(tracker_scheduler, "_scheduler_loop", lambda: None)

    t = tracker_scheduler.start_tracker_scheduler()
    assert t is not None
    assert t.daemon is True
    assert t.name == "seismo-tracker-scheduler"

    # Calling again is idempotent: returns the same thread (or a new one
    # if the first finished).
    t2 = tracker_scheduler.start_tracker_scheduler()
    assert t2 is not None


# --------------------------------------------------------------------------- #
# 7. HTTP endpoint smoke tests                                                #
# --------------------------------------------------------------------------- #

def test_post_trackers_run_now_returns_stats(client, isolated_db: Path) -> None:
    """POST /api/trackers/run_now returns the stats dict even when there are
    no trackers (zero-counts response).
    """
    r = client.post("/api/trackers/run_now")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "trackers_checked": 0,
        "trackers_run": 0,
        "papers_matched": 0,
    }, f"Unexpected empty-DB stats: {body!r}"


def test_post_project_run_now_404_for_unknown_id(client, isolated_db: Path) -> None:
    """POST /api/projects/{id}/run_now → 404 when the project doesn't exist."""
    r = client.post("/api/projects/does-not-exist-uuid/run_now")
    assert r.status_code == 404, r.text


def test_post_project_run_now_400_for_non_tracker(
    client, isolated_db: Path, make_paper,
) -> None:
    """POST /api/projects/{id}/run_now → 400 when the project is a
    collection, not a tracker.
    """
    import db as db_module
    from models import Project

    session = db_module.SessionLocal()
    try:
        proj = Project(
            id=str(uuid.uuid4()),
            name="A collection",
            type="static",
            kind="collection",
            config={},
        )
        session.add(proj)
        session.commit()
        pid = proj.id
    finally:
        session.close()

    r = client.post(f"/api/projects/{pid}/run_now")
    assert r.status_code == 400, r.text
    assert "tracker" in r.json().get("detail", "").lower()
