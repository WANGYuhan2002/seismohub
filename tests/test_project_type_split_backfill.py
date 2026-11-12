"""TC-301..307 — backfill of (kind, scope, cadence, default_sort_by) from
legacy `Project.type`. Covers the canonical map, config overrides, and
idempotency. Tests are pure-DB (no FastAPI client) so they run in <1 s.

Each test uses the `isolated_db` fixture from conftest.py so the suite
stays parallel-safe and never touches the developer's real DB. The
migration is invoked directly via `db._backfill_project_taxonomy` to keep
the failure mode tight; the integration-level "init_db twice is a no-op"
assertion lives in the broader Q3 migration test file.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text


def _seed_legacy_project(
    connection, *, legacy_type: str, config: dict | None = None
) -> str:
    """Insert a row that looks like a pre-migration project (kind=NULL)."""
    pid = str(uuid.uuid4())
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        text(
            "INSERT INTO projects "
            "(id, name, type, config, sort_order, is_default, created_at, updated_at) "
            "VALUES (:id, :name, :type, :cfg, 0, 0, :ca, :ua)"
        ),
        {
            "id": pid,
            "name": f"legacy-{legacy_type}",
            "type": legacy_type,
            "cfg": json.dumps(config or {}, ensure_ascii=False),
            "ca": now,
            "ua": now,
        },
    )
    return pid


def _fetch(connection, pid: str):
    row = connection.execute(
        text(
            "SELECT kind, scope, cadence, default_sort_by, table_mode "
            "FROM projects WHERE id = :id"
        ),
        {"id": pid},
    ).fetchone()
    return tuple(row) if row else None


@pytest.fixture()
def migrated_engine(isolated_db):
    """Run init_db() once so the new columns exist; return the engine."""
    import db as db_module

    db_module.init_db()
    return db_module.engine


def test_TC301_monthly_maps_to_personal_monthly_tracker(migrated_engine):
    """2026-05-25 reclassification: legacy type='monthly' now maps to
    kind='tracker' (auto-buckets every paper by YYYY-MM folder), not
    kind='collection' as the original PR-02 default had it. The Monthly
    Literature Review IS a tracker per BRIEF §1.1 semantics.
    """
    import db as db_module

    with migrated_engine.begin() as conn:
        # Nuke the kind value the init_db backfill already set so we can
        # re-exercise the function on a controlled row.
        pid = _seed_legacy_project(conn, legacy_type="monthly")
        conn.execute(text("UPDATE projects SET kind = NULL WHERE id = :id"), {"id": pid})
        db_module._backfill_project_taxonomy(conn)
        assert _fetch(conn, pid) == ("tracker", "personal", "monthly", "published_date", "compact")


def test_TC302_topic_monitor_maps_to_personal_weekly_tracker(migrated_engine):
    import db as db_module

    with migrated_engine.begin() as conn:
        pid = _seed_legacy_project(conn, legacy_type="topic_monitor")
        conn.execute(text("UPDATE projects SET kind = NULL WHERE id = :id"), {"id": pid})
        db_module._backfill_project_taxonomy(conn)
        assert _fetch(conn, pid) == (
            "tracker", "personal", "weekly", "published_date", "compact",
        )


def test_TC303_static_maps_to_custom_sort_collection(migrated_engine):
    import db as db_module

    with migrated_engine.begin() as conn:
        pid = _seed_legacy_project(conn, legacy_type="static")
        conn.execute(text("UPDATE projects SET kind = NULL WHERE id = :id"), {"id": pid})
        db_module._backfill_project_taxonomy(conn)
        assert _fetch(conn, pid) == ("collection", None, None, "custom", "compact")


def test_TC304_config_cadence_override_wins_for_tracker(migrated_engine):
    import db as db_module

    with migrated_engine.begin() as conn:
        pid = _seed_legacy_project(
            conn, legacy_type="topic_monitor", config={"cadence": "daily"}
        )
        conn.execute(text("UPDATE projects SET kind = NULL WHERE id = :id"), {"id": pid})
        db_module._backfill_project_taxonomy(conn)
        kind, scope, cadence, _, _ = _fetch(conn, pid)
        assert (kind, scope, cadence) == ("tracker", "personal", "daily")


def test_TC305_config_scope_override_wins_for_tracker(migrated_engine):
    import db as db_module

    with migrated_engine.begin() as conn:
        pid = _seed_legacy_project(
            conn, legacy_type="topic_monitor",
            config={"scope": "group", "cadence": "monthly"},
        )
        conn.execute(text("UPDATE projects SET kind = NULL WHERE id = :id"), {"id": pid})
        db_module._backfill_project_taxonomy(conn)
        kind, scope, cadence, _, _ = _fetch(conn, pid)
        assert (kind, scope, cadence) == ("tracker", "group", "monthly")


def test_TC305b_unknown_override_values_fall_back_to_default(migrated_engine):
    import db as db_module

    with migrated_engine.begin() as conn:
        pid = _seed_legacy_project(
            conn, legacy_type="topic_monitor",
            config={"scope": "WORLD", "cadence": "hourly"},
        )
        conn.execute(text("UPDATE projects SET kind = NULL WHERE id = :id"), {"id": pid})
        db_module._backfill_project_taxonomy(conn)
        # Bogus override values → fall back to the canonical
        # topic_monitor → (tracker, personal, weekly) tuple.
        kind, scope, cadence, _, _ = _fetch(conn, pid)
        assert (kind, scope, cadence) == ("tracker", "personal", "weekly")


def test_TC306_second_run_is_a_no_op(migrated_engine):
    import db as db_module

    with migrated_engine.begin() as conn:
        pid = _seed_legacy_project(conn, legacy_type="topic_monitor")
        conn.execute(text("UPDATE projects SET kind = NULL WHERE id = :id"), {"id": pid})
        db_module._backfill_project_taxonomy(conn)
        before = _fetch(conn, pid)
        # Hand-edit a column so we can prove the second pass does NOT
        # overwrite it — once kind is set, the row is skipped entirely.
        conn.execute(
            text("UPDATE projects SET default_sort_by = 'author' WHERE id = :id"),
            {"id": pid},
        )
        db_module._backfill_project_taxonomy(conn)
        after = _fetch(conn, pid)
        assert after[3] == "author"  # user edit preserved
        assert before[0:3] == after[0:3]  # kind/scope/cadence untouched


def test_TC307_null_legacy_type_falls_back_to_monthly_tracker_shape(migrated_engine):
    """Bogus / empty type column falls through to the LEGACY_PROJECT_TYPE_MAP
    "monthly" default, which since 2026-05-25 is the personal monthly
    tracker shape (was: collection, NULL, NULL).
    """
    import db as db_module

    with migrated_engine.begin() as conn:
        # Bypass NOT NULL via direct SQL — older DBs occasionally have NULL here.
        pid = str(uuid.uuid4())
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            text(
                "INSERT INTO projects (id, name, type, config, sort_order, "
                "is_default, created_at, updated_at) "
                "VALUES (:id, 'nulltype', '', '{}', 0, 0, :ca, :ua)"
            ),
            {"id": pid, "ca": now, "ua": now},
        )
        conn.execute(text("UPDATE projects SET kind = NULL WHERE id = :id"), {"id": pid})
        db_module._backfill_project_taxonomy(conn)
        assert _fetch(conn, pid) == ("tracker", "personal", "monthly", "published_date", "compact")
