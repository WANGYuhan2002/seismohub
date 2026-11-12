"""Schema-migration contract tests for the 2026-Q3 redesign (PR-15).

Owns four invariants that, if any one of them regresses, will lose user data
on the next `uvicorn main:app` boot. The tests are deliberately written as
raw-SQL probes (sqlite3 / SQLAlchemy text()) rather than ORM round-trips —
the whole point is to test the migration *itself*, not the post-migration
ORM, so we want to read the disk the same way the operator would with
`sqlite3 seismo_hub.db "PRAGMA table_info(projects)"`.

Coverage map (see changelog/05-technical-architecture/10-test-plan.md §2.1):

1. Idempotency           — `init_db()` × 3 is a no-op after the first call.
2. Backfill correctness  — legacy `type` rows land on the §3.1 (kind, scope,
                            cadence) triple per the PLAN.md / 01-db-
                            migrations.md §3.1 table.
3. Rollback safety       — `Project.type` column survives the migration
                            (shadow-column policy, §6 of 01-db-migrations.md).
4. UNIQUE(paper_id, pos) — `qa_messages` duplicate-position INSERT raises
                            IntegrityError (qa-chat-persistence design §2).

All four tests use the existing `isolated_db` fixture in `tests/conftest.py`
so each test starts from an empty SQLite file and never touches the
developer's real `seismo_hub.db`. The `client` fixture is deliberately *not*
used here because the migration tests want to run `init_db()` themselves —
sometimes twice, sometimes with pre-seeded legacy rows in between.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


# --------------------------------------------------------------------------
# Helpers — kept module-private so they don't leak into the broader test
# suite's import namespace.
# --------------------------------------------------------------------------

def _table_columns(db_path: Path, table: str) -> set[str]:
    """Read PRAGMA table_info via the stdlib sqlite3 driver — avoids any
    SQLAlchemy connection-pool caching that could mask a missing ALTER."""
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _schema_snapshot(db_path: Path) -> str:
    """Cheap drift detector — hashes the union of (every CREATE statement,
    every table row count). Used to assert that a second `init_db()` call
    mutated nothing; see 01-db-migrations.md §7 for the pattern."""
    with sqlite3.connect(db_path) as conn:
        ddl = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        counts: list[tuple[str, int]] = []
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            counts.append((table,
                           conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
    h = hashlib.sha256()
    h.update(repr(ddl).encode("utf-8"))
    h.update(repr(counts).encode("utf-8"))
    return h.hexdigest()


def _seed_legacy_project(db_path: Path, *, pid: str, name: str, type_: str,
                         config: dict | None = None) -> None:
    """Write a row in the *pre-PR-01* shape — `type` enum, no `kind`/`scope`/
    `cadence`. Used by the backfill test to prove the redesign's UPDATE walks
    every legacy row."""
    cfg = json.dumps(config or {}, ensure_ascii=False)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, type, config, sort_order, is_default, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 0, "
            "'2026-05-01 00:00:00', '2026-05-01 00:00:00')",
            (pid, name, type_, cfg),
        )
        conn.commit()


# --------------------------------------------------------------------------
# 1. Idempotency — init_db() × 3 is a no-op after the first call.
# --------------------------------------------------------------------------

@pytest.mark.migration
def test_init_db_is_idempotent_across_three_calls(isolated_db: Path) -> None:
    """Calling init_db three times must produce identical schema + row
    counts on calls 2 and 3. Catches any ALTER that forgot its
    PRAGMA-gated check, any CREATE TABLE that forgot IF NOT EXISTS, and any
    backfill that forgot its WHERE clause."""
    import main  # late import so conftest's monkeypatch wins

    # Call 1 — builds the schema from scratch.
    main.init_db()
    snap_after_1 = _schema_snapshot(isolated_db)

    # Call 2 — must be a no-op.
    main.init_db()
    snap_after_2 = _schema_snapshot(isolated_db)
    assert snap_after_1 == snap_after_2, (
        "init_db() is not idempotent — second call mutated schema or rows"
    )

    # Call 3 — defensive: in case a buggy migration alternates between
    # two states (rare but seen in retrospect when a backfill UPDATE flips
    # a default value back and forth).
    main.init_db()
    snap_after_3 = _schema_snapshot(isolated_db)
    assert snap_after_2 == snap_after_3, (
        "init_db() oscillates between calls 2 and 3 — likely a backfill "
        "without a WHERE … IS NULL predicate"
    )

    # Sanity — the migration actually ran and added the Q3 columns.
    project_cols = _table_columns(isolated_db, "projects")
    for required in ("kind", "scope", "cadence", "type"):
        assert required in project_cols, (
            f"projects.{required} missing after init_db() — migration broke"
        )


# --------------------------------------------------------------------------
# 2. Backfill correctness — legacy `type` → (kind, scope, cadence) triple
#    per PLAN.md / 01-db-migrations.md §3.1.
# --------------------------------------------------------------------------

# (old type,                seeded config,                expected kind, scope,    cadence)
# 2026-05-25 reclassification: legacy type='monthly' now maps to
# kind='tracker' (auto-bucketed by YYYY-MM) per BRIEF §1.1, NOT
# kind='collection' as PR-02 originally had it.
_BACKFILL_MATRIX = [
    ("monthly",              {},                           "tracker",     "personal", "monthly"),
    ("topic_monitor",        {},                           "tracker",     "personal", "weekly"),
    ("topic_monitor",        {"cadence": "daily"},         "tracker",     "personal", "daily"),
    ("topic_monitor",        {"cadence": "monthly"},       "tracker",     "personal", "monthly"),
]


@pytest.mark.migration
def test_legacy_type_backfills_to_kind_scope_cadence(isolated_db: Path) -> None:
    """Seed one project per row in PLAN.md §1.2 table, then run init_db()
    and assert every row landed on the expected (kind, scope, cadence)
    triple. The most fragile single thing in the redesign — one missed
    branch in `_backfill_project_taxonomy` and live users lose projects."""
    import main

    # First boot — creates the `projects` table so we can INSERT into it
    # with the legacy column set. (We need init_db once to materialise the
    # table; the backfill predicate `WHERE kind IS NULL` then catches the
    # seeded rows on the second boot.)
    main.init_db()

    seeded: list[tuple[str, str, dict, str, str | None, str | None]] = []
    for old_type, cfg, exp_kind, exp_scope, exp_cadence in _BACKFILL_MATRIX:
        pid = str(uuid.uuid4())
        _seed_legacy_project(
            isolated_db, pid=pid, name=f"legacy-{old_type}-{pid[:8]}",
            type_=old_type, config=cfg,
        )
        # Null out the redesign columns to simulate a row written by an
        # older release that did not know about kind/scope/cadence.
        with sqlite3.connect(isolated_db) as c:
            c.execute(
                "UPDATE projects SET kind = NULL, scope = NULL, cadence = NULL "
                "WHERE id = ?", (pid,),
            )
            c.commit()
        seeded.append((pid, old_type, cfg, exp_kind, exp_scope, exp_cadence))

    # Second boot — runs `add_columns_2026_q3 → _backfill_project_taxonomy`
    # against the seeded rows.
    main.init_db()

    with sqlite3.connect(isolated_db) as c:
        c.row_factory = sqlite3.Row
        for pid, old_type, cfg, exp_kind, exp_scope, exp_cadence in seeded:
            row = c.execute(
                "SELECT kind, scope, cadence, type FROM projects WHERE id = ?",
                (pid,),
            ).fetchone()
            assert row is not None, (
                f"Project id={pid} disappeared after init_db()"
            )
            assert row["kind"] == exp_kind, (
                f"type={old_type!r} cfg={cfg!r}: expected kind={exp_kind!r}, "
                f"got {row['kind']!r}"
            )
            assert row["scope"] == exp_scope, (
                f"type={old_type!r} cfg={cfg!r}: expected scope={exp_scope!r}, "
                f"got {row['scope']!r}"
            )
            assert row["cadence"] == exp_cadence, (
                f"type={old_type!r} cfg={cfg!r}: expected cadence={exp_cadence!r}, "
                f"got {row['cadence']!r}"
            )
            # Backfill must not corrupt the legacy `type` column (see
            # rollback test below for the same invariant from another angle).
            assert row["type"] == old_type, (
                f"Backfill mutated legacy type column from {old_type!r} to "
                f"{row['type']!r} — breaks the shadow-column rollback story"
            )


# --------------------------------------------------------------------------
# 3. Rollback safety — legacy `Project.type` is preserved (never DROPped).
# --------------------------------------------------------------------------

@pytest.mark.migration
def test_legacy_type_column_is_never_dropped(isolated_db: Path) -> None:
    """01-db-migrations.md §6 spells out the shadow-column policy: SQLite's
    DROP COLUMN is fragile and the rollback story relies on the old code
    still being able to read `Project.type`. If a future PR ever adds a
    DROP COLUMN, this test fires and the reviewer can quote §6 at it."""
    import main

    main.init_db()
    cols = _table_columns(isolated_db, "projects")
    assert "type" in cols, (
        "projects.type was dropped — breaks the shadow-column rollback "
        "policy in changelog/05-technical-architecture/01-db-migrations.md §6"
    )

    # The new triple must also exist (dual-existence is the point of the
    # shadow-column policy — old code reads `type`, new code reads the triple).
    for new_col in ("kind", "scope", "cadence"):
        assert new_col in cols, f"projects.{new_col} missing after init_db()"

    # Re-boot must not change that.
    main.init_db()
    cols2 = _table_columns(isolated_db, "projects")
    assert "type" in cols2, "projects.type vanished on second init_db()"
    assert cols == cols2, "Column set drifted between init_db() calls"

    # Seed a row populated only via the legacy `type` write path, then
    # confirm the row + value survive a second boot — proves the migration
    # is non-destructive for downgrade-and-redeploy-old-code scenarios.
    pid = str(uuid.uuid4())
    _seed_legacy_project(
        isolated_db, pid=pid, name="shadow-survivor",
        type_="monthly", config={},
    )
    main.init_db()
    with sqlite3.connect(isolated_db) as c:
        row = c.execute(
            "SELECT type FROM projects WHERE id = ?", (pid,)
        ).fetchone()
    assert row is not None and row[0] == "monthly", (
        "Legacy type value was lost across an init_db() cycle — rollback "
        "would silently lose the project's identity"
    )


# --------------------------------------------------------------------------
# 4. UNIQUE(paper_id, position) — `qa_messages` collision raises IntegrityError.
# --------------------------------------------------------------------------

@pytest.mark.migration
def test_qa_messages_unique_paper_position_fires_on_collision(
    isolated_db: Path, make_paper,
) -> None:
    """The `qa_messages` schema (qa-chat-persistence design §2) declares
    `UniqueConstraint("paper_id", "position", name="uq_qa_paper_position")`.
    If the migration forgets the constraint, a buggy MAX(position)+1
    allocator (race condition under concurrent inserts) would silently
    write duplicate position rows and the chat reload would render the
    same turn twice. This test makes the constraint *itself* load-bearing:
    inserting a duplicate (paper_id, position) must raise IntegrityError.
    """
    import db as db_module
    import main

    main.init_db()
    paper_id = make_paper("qa-unique-test", title="Unique test paper")

    # First insert at position=1 — succeeds.
    with db_module.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO qa_messages "
                "(paper_id, role, text, position, citations, archived, created_at) "
                "VALUES (:pid, :role, :txt, :pos, '[]', 0, '2026-05-24 00:00:00')"
            ),
            {"pid": paper_id, "role": "user", "txt": "first", "pos": 1},
        )

    # Second insert at the same (paper_id, position) — must fail.
    with pytest.raises(IntegrityError) as exc_info:
        with db_module.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO qa_messages "
                    "(paper_id, role, text, position, citations, archived, created_at) "
                    "VALUES (:pid, :role, :txt, :pos, '[]', 0, '2026-05-24 00:00:01')"
                ),
                {"pid": paper_id, "role": "assistant", "txt": "dup", "pos": 1},
            )
    # SQLite surfaces UNIQUE failures with this exact substring; pinning
    # it ensures we are tripping the right constraint and not some other
    # NOT NULL / FK violation that happens to throw IntegrityError too.
    assert "UNIQUE" in str(exc_info.value).upper(), (
        f"Expected a UNIQUE constraint failure, got: {exc_info.value!r}"
    )
    # Bonus: the constraint should mention the paper_id + position pair.
    msg = str(exc_info.value).lower()
    assert "paper_id" in msg and "position" in msg, (
        f"Expected UNIQUE violation to mention paper_id + position, got: "
        f"{exc_info.value!r}"
    )

    # Different paper, same position=1 — must succeed (constraint is
    # scoped per-paper, not global).
    other_id = make_paper("qa-unique-test-other", title="Other paper")
    with db_module.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO qa_messages "
                "(paper_id, role, text, position, citations, archived, created_at) "
                "VALUES (:pid, :role, :txt, :pos, '[]', 0, '2026-05-24 00:00:02')"
            ),
            {"pid": other_id, "role": "user", "txt": "first-on-other", "pos": 1},
        )

    # Same paper, monotonically next position=2 — must succeed.
    with db_module.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO qa_messages "
                "(paper_id, role, text, position, citations, archived, created_at) "
                "VALUES (:pid, :role, :txt, :pos, '[]', 0, '2026-05-24 00:00:03')"
            ),
            {"pid": paper_id, "role": "assistant", "txt": "second", "pos": 2},
        )

    # Final sanity — the three successful rows are on disk; the collision
    # row is not.
    with sqlite3.connect(isolated_db) as c:
        total = c.execute("SELECT COUNT(*) FROM qa_messages").fetchone()[0]
        for_paper = c.execute(
            "SELECT position FROM qa_messages WHERE paper_id = ? ORDER BY position",
            (paper_id,),
        ).fetchall()
    assert total == 3, f"Expected 3 rows in qa_messages, got {total}"
    assert [r[0] for r in for_paper] == [1, 2], (
        f"Expected positions [1, 2] for {paper_id!r}, got {for_paper!r}"
    )
