"""Tests for the ❤️ "interested" signal (2026-06-16).

Covers the new ``POST /api/papers/{id}/like`` toggle, the ``is_liked`` /
``liked_at`` surface on ``_paper_to_dict``, the orthogonality of ❤️ to the
read/archive/dismiss flags, and — crucially — the ALTER-TABLE migration that
adds ``user_paper_states.liked_at`` to a *pre-existing* DB (the path the real
``seismo_hub.db`` hits on the next boot; the fresh-install path uses ORM
CREATE which already includes the column).

❤️ is a persistent taste marker, distinct from ★ follow_up (to-read queue).
See ``计划_爱心与兴趣信号_2026-06-16.md`` §1.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _cols(db_path: Path, table: str) -> set[str]:
    """PRAGMA table_info via stdlib sqlite3 — reads the disk the way an
    operator would, bypassing any SQLAlchemy pool caching that could mask a
    missing ALTER."""
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# Endpoint behaviour
# ---------------------------------------------------------------------------

def test_like_toggle_via_api(client, make_paper):
    """POST /like flips liked_at on/off; is_liked + liked_at surface in GET."""
    pid = make_paper("like-001")

    # Initial state — not liked, fields present in _paper_to_dict.
    body = client.get(f"/api/papers/{pid}").json()
    assert body["is_liked"] is False
    assert body["liked_at"] is None

    # Like it.
    data = client.post(f"/api/papers/{pid}/like").json()
    assert data["paper_id"] == pid
    assert data["is_liked"] is True
    assert data["liked_at"] is not None

    # GET reflects the new state.
    body = client.get(f"/api/papers/{pid}").json()
    assert body["is_liked"] is True
    assert body["liked_at"] is not None

    # Toggle off — re-POST clears the flag.
    data = client.post(f"/api/papers/{pid}/like").json()
    assert data["is_liked"] is False
    assert data["liked_at"] is None
    assert client.get(f"/api/papers/{pid}").json()["is_liked"] is False


def test_like_unknown_paper_404(client):
    r = client.post("/api/papers/does-not-exist/like")
    assert r.status_code == 404


def test_like_is_orthogonal_to_read_archive_dismiss(client, make_paper):
    """Liking a paper must not touch read/archive/dismiss — ❤️ answers WHAT,
    the others answer WHEN/handled. They share a row but not a meaning."""
    pid = make_paper("like-002")
    client.post(f"/api/papers/{pid}/like")
    body = client.get(f"/api/papers/{pid}").json()
    assert body["is_liked"] is True
    assert body["is_read"] is False
    assert body["is_archived"] is False
    assert body["is_dismissed"] is False


# ---------------------------------------------------------------------------
# Migration — ALTER path on a legacy user_paper_states table
# ---------------------------------------------------------------------------

def test_liked_at_migration_adds_column_to_legacy_table(isolated_db: Path):
    """A DB whose user_paper_states predates liked_at must gain the column on
    the next init_db(), without losing existing rows, idempotently."""
    import main  # late import so conftest's monkeypatch wins

    # First boot materialises the modern schema.
    main.init_db()

    # Rewind to the pre-2026-06-16 shape: drop & recreate user_paper_states
    # WITHOUT liked_at, then seed a legacy state row.
    with sqlite3.connect(isolated_db) as c:
        c.execute("DROP TABLE user_paper_states")
        c.execute(
            "CREATE TABLE user_paper_states ("
            "paper_id VARCHAR(64) PRIMARY KEY, "
            "read_at DATETIME, archived_at DATETIME, dismissed_at DATETIME, "
            "updated_at DATETIME NOT NULL)"
        )
        c.execute(
            "INSERT INTO user_paper_states "
            "(paper_id, read_at, updated_at) VALUES "
            "('legacy-1', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        c.commit()
    assert "liked_at" not in _cols(isolated_db, "user_paper_states")

    # Second boot runs the ALTER path.
    main.init_db()
    assert "liked_at" in _cols(isolated_db, "user_paper_states"), (
        "init_db() did not add liked_at to a pre-existing user_paper_states"
    )

    # Legacy row preserved; liked_at defaults to NULL.
    with sqlite3.connect(isolated_db) as c:
        row = c.execute(
            "SELECT read_at, liked_at FROM user_paper_states "
            "WHERE paper_id = 'legacy-1'"
        ).fetchone()
    assert row is not None, "legacy row vanished across the migration"
    assert row[0] == "2026-01-01 00:00:00"
    assert row[1] is None

    # Idempotent — a third boot neither errors nor duplicates the column.
    main.init_db()
    assert "liked_at" in _cols(isolated_db, "user_paper_states")
