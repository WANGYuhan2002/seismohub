import json
import logging
import os
import threading
from pathlib import Path
from datetime import datetime

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from models import Base


logger = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent
# Storage path is env-overridable so tests / throwaway instances never pollute
# the real DB. SEISMO_DB_PATH wins when set (absolute, or relative to CWD, ~ ok);
# otherwise the canonical DB lives next to the code.
# See _audit_2026-06-11/02-ACTION-PLAN.md P2-1 / 05-CLEANUP-EXECUTED.md.
_env_db_path = (os.environ.get("SEISMO_DB_PATH") or "").strip()
DATABASE_PATH = Path(_env_db_path).expanduser() if _env_db_path else BASE_DIR / "seismo_hub.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"


# FN-B1-A13-1 — Default SQLAlchemy JSON serializer uses json.dumps with
# ensure_ascii=True, which escapes every non-ASCII codepoint (CJK, emoji,
# etc.) to \uXXXX in the on-disk JSON columns. The HTTP round-trip is still
# correct, but debugging is painful: `sqlite3 ... "SELECT config FROM
# projects WHERE config LIKE '%长白山%';"` returns zero rows even when the
# data is there. Swap in a UTF-8-preserving encoder so columns store literal
# CJK/emoji bytes, shrinking storage for CJK-heavy configs ~2x and making
# the DB greppable by humans.
def _json_dumps_utf8(obj, **kwargs):
    kwargs["ensure_ascii"] = False
    return json.dumps(obj, **kwargs)


# M5 fix (2026-06-30) — how long a blocked writer waits for the single SQLite
# write lock before giving up. Was effectively 0ms (no busy_timeout set), so the
# fetch daemon, tracker daemon and request threads writing the shared papers
# table at the same instant hit SQLITE_BUSY ("database is locked") immediately:
# a request 500s, a background pass silently aborts. With a non-zero timeout the
# loser simply waits (WAL allows readers throughout); a real single-user app
# never holds the writer for seconds. Env-overridable.
_SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("SEISMO_SQLITE_BUSY_TIMEOUT_MS") or "10000")

# SQLite is shared by FastAPI requests and the background scheduler thread.
engine = create_engine(
    DATABASE_URL,
    # ``timeout`` is the sqlite3-driver busy wait (seconds); the PRAGMA below is
    # the authoritative per-connection setting. Both point the same way.
    connect_args={
        "check_same_thread": False,
        "timeout": max(1.0, _SQLITE_BUSY_TIMEOUT_MS / 1000.0),
    },
    future=True,
    json_serializer=_json_dumps_utf8,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    """Enable WAL mode (concurrent readers + 1 writer), FK enforcement,
    NORMAL sync, and a non-zero busy_timeout so concurrent writers WAIT for the
    lock instead of failing instantly. Fixes FN-B1-A12-1 + FN-B6-A07-1 (+ M5)."""
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


# New Paper columns added by the PDF acquisition pipeline. Each entry is
# (column_name, SQL type + default/constraint). Kept as a list so the ad-hoc
# migration in init_db() stays readable and easy to extend.
_PAPER_RETRIEVAL_COLUMNS = [
    ("retrieval_status", "VARCHAR(32) NOT NULL DEFAULT 'not_attempted'"),
    ("retrieval_source", "VARCHAR(64)"),
    ("retrieval_source_url", "TEXT"),
    ("retrieval_last_stage", "VARCHAR(16)"),
    ("retrieval_error_reason", "TEXT"),
    ("retrieval_attempts_count", "INTEGER NOT NULL DEFAULT 0"),
    ("retrieval_attempted_at", "DATETIME"),
    ("retrieval_succeeded_at", "DATETIME"),
    ("retrieval_next_retry_at", "DATETIME"),
]

# Extra Paper columns added by post-roadmap M1 features (upload dedup, last-read).
# Same list-of-tuples shape as above so the migration loop stays trivial.
_PAPER_M1_COLUMNS = [
    ("pdf_sha256", "VARCHAR(64)"),
    ("last_opened_at", "DATETIME"),
    # Roadmap V2 item 41 — Why Strip externalized intent (8-word "why I opened
    # this") sticky to the paper detail view; persists across re-opens.
    ("reading_intent", "TEXT"),
    ("reading_intent_at", "DATETIME"),
    # Roadmap V2 item 1 — Forced two-sentence takeaway captured at "mark as
    # read" / modal-close. `what` = "what did the paper argue?"; `why` = "why
    # does it matter to me?". `at` = when the user filled it (drives "papers
    # closed this week" counter).
    ("user_takeaway_what", "TEXT"),
    ("user_takeaway_why", "TEXT"),
    ("user_takeaway_at", "DATETIME"),
    # Roadmap V2 item 16-prime — Save-for-follow-up personal queue. Replaces
    # the dropped "Forward to student" item (no collaborators yet).
    ("follow_up", "BOOLEAN DEFAULT 0"),
    ("follow_up_at", "DATETIME"),
    # 2026-05-19 — Structured 7-question reading notes (replaces the older
    # `reading_intent` + `user_takeaway_what/why` 1-line workflow with a
    # 七问读书法 template). Stored as JSON {q1, q2, ..., q7, personal}.
    ("reading_notes", "TEXT"),
    ("reading_notes_at", "DATETIME"),
]

# BRIEF v2 §3 — cheap title+abstract semantic-match embedding columns. Computed
# at ingest by the ingest-pipeline package (services/embedding_service.py); the
# semantic tracker predicate scores cosine(query, paper) >= threshold over these.
# Same list-of-tuples shape as the migrations above so the init_db loop stays
# trivial. All three nullable so a paper persists fine when embedding is skipped
# (no API key / quota / network failure) — it just won't match a semantic
# predicate until a re-embed/backfill pass recovers it.
#   - embedding       : JSON-encoded float list (encode_vector output) in TEXT.
#   - embedding_model : (model, dim) stability tag — vectors only comparable
#                       within one model id; consumers skip cosine when it differs.
#   - embedded_at     : wall-clock when the vector was last (re)computed.
_PAPER_EMBEDDING_COLUMNS = [
    ("embedding", "TEXT"),
    ("embedding_model", "VARCHAR(64)"),
    ("embedded_at", "DATETIME"),
]

# Project columns added post-M2. Same list-of-tuples shape as the paper
# migrations above so the init_db loop stays trivial.
# FN-B1-A04-1 / FN-B1-A08-1: is_default flag prevents deletion of the seeded
# "Monthly Literature Review" project no matter how many other projects exist.
_PROJECT_COLUMNS = [
    ("is_default", "BOOLEAN DEFAULT 0"),
    # 2026-Q3 redesign (PR-01) — split the legacy `type` enum into a
    # `kind`/`scope`/`cadence` triple per BRIEF §5, and add the columns
    # that the redesigned sidebar (items E + F) needs to persist server-
    # side: sort/table presets, optional description + color tag, and the
    # "unread since last visit" telemetry for tracker projects. Backfill
    # of legacy `type` → new triple lives in PR-02 (`_backfill_project_taxonomy`)
    # so this PR stays a pure additive schema change; every column is
    # nullable (or has a safe default) so existing rows keep working
    # untouched until PR-02 lands.
    ("kind", "VARCHAR(16)"),                # "tracker" | "collection" | NULL until PR-02 backfill
    ("scope", "VARCHAR(16)"),               # "group" | "personal" (tracker-only) | NULL
    ("cadence", "VARCHAR(16)"),             # "daily" | "weekly" | "monthly" (tracker-only) | NULL
    ("default_sort_by", "VARCHAR(16)"),     # "author" | "title" | "journal" | "year" | "custom" (item E)
    ("table_mode", "VARCHAR(16)"),          # "compact" | "expanded" (item F sidebar density)
    ("description", "TEXT"),                # optional free-text blurb shown in sidebar tooltip
    ("color_tag", "VARCHAR(16)"),           # optional color chip (e.g. "amber", "slate")
    # Sidebar persistence — list of column keys in display order, and the
    # active sort tuple. Stored as JSON text (SQLite has no JSON type, so
    # TEXT is the conventional shape — matches papers.reading_notes etc.).
    ("table_columns", "TEXT"),              # JSON list[str], e.g. ["author","journal","year"]
    ("table_sort", "TEXT"),                 # JSON {"key":"year","dir":"desc"}
    # Tracker telemetry — when did this tracker last accept a new auto-
    # assigned paper, and when did the user last open the project view.
    # Drives the "N new since last visit" badge on the sidebar (BRIEF
    # §1.1 — trackers stream in live, the badge is how the user notices).
    # Both nullable: collection projects never write these; brand-new
    # trackers write last_match_at on first hit, last_viewed_at on first
    # open.
    ("last_match_at", "DATETIME"),
    ("last_viewed_at", "DATETIME"),
    ("unread_count", "INTEGER NOT NULL DEFAULT 0"),
]


# 2026-05-25 hotfix — project_papers grew two columns that models.py
# declares but db.py never migrated:
#   - position (PR-32): in-folder ordering for collections with
#     default_sort_by="custom". NOT NULL DEFAULT 0 keeps legacy rows
#     "no opinion" → ties broken by added_at DESC.
#   - seen_in_project_at (PR-01): "when did this paper first appear in
#     this project's view?" — drives tracker unread badge. Nullable; the
#     backfill UPDATE below seeds it from added_at for existing rows.
#
# Without these ALTER TABLE statements, ANY query that hits
# project_papers via the ORM crashes with "no such column: position"
# — including `GET /api/papers?project_id=...` which the workspace
# sidebar fires on every click, breaking paper rendering entirely.
_PROJECT_PAPER_COLUMNS = [
    ("position", "INTEGER NOT NULL DEFAULT 0"),
    ("seen_in_project_at", "DATETIME"),
    # BRIEF v2 §1 — best predicate score (0..1) for this paper within this
    # tracker, written by the predicate-based matcher (typically the best
    # semantic cosine). Tracker views sort by this DESC (BRIEF v2 §6). Nullable
    # so legacy rows / collection memberships migrate cleanly and the ALTER
    # needs no server_default; sort coalesces NULL to the bottom.
    ("match_score", "FLOAT"),
]


# Canonical map from the legacy single-enum `Project.type` to the new
# taxonomy triple plus a per-kind default sort key. Single source of truth
# for this migration and for the inverse `Project.legacy_type`
# @hybrid_property added by PR-03. Mirrors `PLAN.md §1.2`:
#
#   | legacy type      | kind        | scope     | cadence | default_sort_by   |
#   | ---------------- | ----------- | --------- | ------- | ----------------- |
#   | topic_monitor    | tracker     | personal  | weekly  | published_date    |
#   | monthly          | tracker     | personal  | monthly | published_date    |
#   | static           | collection  | NULL      | NULL    | custom            |
#
# Unknown legacy values fall through to the `monthly` shape (the original
# default in models.py:158); _backfill_project_taxonomy never raises on a
# weird row, it just normalises it to the `monthly` tracker shape (a benign
# auto-bucketing default) so the read side can carry on.
LEGACY_PROJECT_TYPE_MAP = {
    # legacy_type     -> (kind,         scope,      cadence,    default_sort_by)
    "topic_monitor":     ("tracker",    "personal", "weekly",   "published_date"),
    # 2026-05-25 reclassification: "monthly" projects ARE trackers in the
    # BRIEF §1.1 sense — they auto-receive every new paper bucketed by
    # YYYY-MM folder, no manual curation. Mapping monthly → collection was
    # the original PR-02 default but it puts the Monthly Lit Review under
    # the wrong nav tab. The default project's auto-add path
    # (_add_paper_to_default_project) is independent of the per-tracker
    # scheduler, so this row never triggers a query-based re-match — and
    # the scheduler now early-returns on is_default=true (see
    # services/tracker_scheduler.py:_run_one_tracker) as belt-and-braces.
    "monthly":           ("tracker",    "personal", "monthly",  "published_date"),
    "static":            ("collection", None,       None,       "custom"),
}

# Per-tracker overrides we honour from the JSON `Project.config` blob.
# A handful of hand-stored rows have `config = {"cadence": "daily"}` or
# `{"scope": "group"}` from early-2026 experiments; the backfill must not
# clobber those. Allowed value sets are tight on purpose — anything off the
# allowlist is dropped silently so a typo doesn't poison the column.
_ALLOWED_CADENCES = {"daily", "weekly", "monthly"}
_ALLOWED_SCOPES = {"group", "personal"}


def _migrate_cos_topic_renames(connection) -> None:
    """Rewrite legacy COS topic labels stored in JSON fields.

    Re-runnable / idempotent: only rows that still contain a legacy label get
    touched. Used at startup to keep the live DB in sync with COS_TOPIC_RENAMES
    in services/analyze_service.py without forcing a manual SQL operation.
    """
    # Local import to avoid services package import at module load time.
    from services.cos_topics import (
        COS_TOPIC_RENAMES,
        PRIMARY_TOPIC_RENAMES,
        PRIMARY_TOPIC_DROPS,
    )

    # Stage D: legacy typo fixes + the v3 old→new vocab map, plus labels with no
    # v3 home (e.g. "Medical ultrasound imaging") which are dropped outright.
    renames = {**COS_TOPIC_RENAMES, **PRIMARY_TOPIC_RENAMES}
    drops = set(PRIMARY_TOPIC_DROPS)
    if not renames and not drops:
        return

    def _remap(labels):
        """Drop retired labels, rename the rest, dedupe preserving order."""
        out = []
        for t in labels:
            if t in drops:
                continue
            nt = renames.get(t, t)
            if nt not in out:
                out.append(nt)
        return out

    # papers.topics (JSON list[str])
    rows = list(
        connection.execute(text("SELECT id, topics FROM papers WHERE topics IS NOT NULL"))
    )
    for paper_id, topics_raw in rows:
        try:
            topics = json.loads(topics_raw) if isinstance(topics_raw, str) else topics_raw
        except (TypeError, ValueError):
            continue
        if not isinstance(topics, list):
            continue
        new_topics = _remap(topics)
        if new_topics != topics:
            connection.execute(
                text("UPDATE papers SET topics = :t WHERE id = :id"),
                {"t": json.dumps(new_topics, ensure_ascii=False), "id": paper_id},
            )

    # ai_analyses.topic_matches (JSON list[dict] with "topic" or "name" keys, or list[str])
    rows = list(
        connection.execute(
            text("SELECT id, topic_matches FROM ai_analyses WHERE topic_matches IS NOT NULL")
        )
    )
    for analysis_id, matches_raw in rows:
        try:
            matches = json.loads(matches_raw) if isinstance(matches_raw, str) else matches_raw
        except (TypeError, ValueError):
            continue
        if not isinstance(matches, list):
            continue
        changed = False
        new_matches = []
        for item in matches:
            if isinstance(item, str):
                if item in drops:
                    changed = True
                    continue
                renamed = renames.get(item, item)
                if renamed != item:
                    changed = True
                new_matches.append(renamed)
            elif isinstance(item, dict):
                label = item.get("topic") or item.get("name") or item.get("label")
                if label in drops:
                    changed = True
                    continue
                new_item = dict(item)
                for key in ("topic", "name", "label"):
                    if key in new_item and new_item[key] in renames:
                        new_item[key] = renames[new_item[key]]
                        changed = True
                new_matches.append(new_item)
            else:
                new_matches.append(item)
        if changed:
            connection.execute(
                text("UPDATE ai_analyses SET topic_matches = :m WHERE id = :id"),
                {"m": json.dumps(new_matches, ensure_ascii=False), "id": analysis_id},
            )


def _fix_iso_datetimes(connection) -> None:
    """Replace T-separator datetimes with space-separator in the M2 project tables.

    SQLAlchemy 1.4 DateTime columns on SQLite expect "YYYY-MM-DD HH:MM:SS" but
    the first-run backfill stored Python's datetime.isoformat() which uses 'T'.
    This migration is idempotent and cheap (indexed WHERE clause with LIKE).
    """
    from datetime import datetime as _dt
    now_str = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for tbl, col in [
        ("projects", "created_at"),
        ("projects", "updated_at"),
        ("projects", "archived_at"),
        ("project_papers", "added_at"),
        ("project_imports", "imported_at"),
    ]:
        try:
            connection.execute(
                text(f"UPDATE {tbl} SET {col} = REPLACE({col}, 'T', ' ') WHERE {col} LIKE '%T%'")
            )
        except Exception:
            pass  # table may not exist yet; create_all handles that above


def _backfill_project_taxonomy(connection) -> None:
    """Map legacy `Project.type` to the new `(kind, scope, cadence,
    default_sort_by)` triple per LEGACY_PROJECT_TYPE_MAP.

    Idempotent: only touches rows where `kind IS NULL OR kind = ''`. Once a
    row has been backfilled (or hand-edited by a user via the redesign UI),
    re-running this function is a no-op for that row. Calling it on a
    fully-migrated DB costs one indexed SELECT that returns zero rows.

    Honours per-tracker `config.cadence` and `config.scope` overrides: a
    handful of early-2026 rows carry these in the JSON blob and the user
    must not lose them. Override values are validated against the tight
    allowlists `_ALLOWED_CADENCES` / `_ALLOWED_SCOPES`; unknown values fall
    back to the LEGACY_PROJECT_TYPE_MAP default. `default_sort_by` is set
    via COALESCE so a row that already has a sort key (e.g. a freshly
    inserted row from the new write path) wins over our seed.

    Pre-condition: the `kind`, `scope`, `cadence`, `default_sort_by`, and
    `table_mode` columns must already exist on `projects` — PR-01 owns the
    DDL and `init_db()` calls it before this function. If the column is
    missing, the SELECT below will raise OperationalError and the boot
    aborts cleanly, which is the correct loud failure.
    """
    # Empty projects table on a fresh install → nothing to backfill.
    rows = list(
        connection.execute(
            text(
                "SELECT id, type, config FROM projects "
                "WHERE kind IS NULL OR kind = ''"
            )
        )
    )
    if not rows:
        return

    for proj_id, legacy_type, config_raw in rows:
        legacy = (legacy_type or "monthly").strip().lower()
        kind, scope, cadence, sort_by = LEGACY_PROJECT_TYPE_MAP.get(
            legacy, LEGACY_PROJECT_TYPE_MAP["monthly"]
        )

        # Honour config.cadence / config.scope overrides (rare but real:
        # group-scope or daily-cadence trackers stored by hand pre-redesign).
        # Trackers only — overrides on collections are meaningless.
        if kind == "tracker":
            try:
                cfg = (
                    json.loads(config_raw)
                    if isinstance(config_raw, str)
                    else (config_raw or {})
                )
            except (TypeError, ValueError):
                cfg = {}
            if isinstance(cfg, dict):
                c = (cfg.get("cadence") or "").strip().lower()
                if c in _ALLOWED_CADENCES:
                    cadence = c
                s = (cfg.get("scope") or "").strip().lower()
                if s in _ALLOWED_SCOPES:
                    scope = s

        connection.execute(
            text(
                "UPDATE projects SET "
                "  kind = :k, "
                "  scope = :s, "
                "  cadence = :c, "
                "  default_sort_by = COALESCE(default_sort_by, :sb), "
                "  table_mode = COALESCE(table_mode, 'compact') "
                "WHERE id = :id"
            ),
            {"k": kind, "s": scope, "c": cadence, "sb": sort_by, "id": proj_id},
        )


def _fixup_monthly_kind_to_tracker(connection) -> None:
    """2026-05-25 hotfix: re-classify legacy `type=monthly` rows that the
    original PR-02 backfill marked as `kind='collection'`. Per BRIEF §1.1
    these projects ARE trackers — they auto-receive every new paper, bucket
    by YYYY-MM folder, no manual curation. Misclassifying them put the
    Monthly Literature Review under the wrong nav tab in v0.X+1.

    Idempotent: only updates rows where type='monthly' AND kind='collection'.
    After this migration, the row has (kind='tracker', scope='personal',
    cadence='monthly') matching the updated LEGACY_PROJECT_TYPE_MAP entry.
    Re-running is a zero-row UPDATE.

    The per-tracker scheduler (services/tracker_scheduler.py) early-returns
    on is_default=true projects, so this reclassification doesn't change
    the auto-add behaviour — papers still flow via
    `_add_paper_to_default_project()` in main.py, independent of the
    query-based matcher.
    """
    connection.execute(
        text(
            "UPDATE projects SET "
            "  kind = 'tracker', "
            "  scope = COALESCE(scope, 'personal'), "
            "  cadence = COALESCE(cadence, 'monthly') "
            "WHERE type = 'monthly' AND kind = 'collection'"
        )
    )


def _seed_preset_trackers(connection) -> None:
    """On a BRAND-NEW database, seed the owner-curated starter set of trackers
    from seed_presets.json (topic + journal + star-group trackers). Most users
    are lazy — a sensible preset set matches expectations on first run.

    One-shot + idempotent via the 'preset_trackers_seeded' app_state flag:
      - flag already set          → return (handled for this DB);
      - any topic_monitor exists  → existing library: set the flag, do NOT seed;
      - truly fresh (no trackers) → INSERT the presets, then set the flag.

    Placed BEFORE _backfill_project_taxonomy in init_db so the seeded rows pick up
    their kind/scope/cadence triple on the same boot. Never blocks init_db.
    """
    import json as _json
    import uuid as _uuid
    from datetime import datetime

    FLAG = "preset_trackers_seeded"
    try:
        # Tests disable seeding (conftest sets this) so isolated DBs stay clean.
        if os.environ.get("SEISMO_NO_PRESET_SEED") == "1":
            return
        if list(connection.execute(
            text("SELECT 1 FROM app_state WHERE key = :k"), {"k": FLAG}
        )):
            return
        has_tracker = list(connection.execute(
            text("SELECT 1 FROM projects WHERE type = 'topic_monitor' LIMIT 1")
        ))
        if not has_tracker:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_presets.json")
            try:
                presets = (_json.loads(open(path, encoding="utf-8").read()).get("presets")) or []
            except Exception:
                presets = []
            now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            for i, p in enumerate(presets):
                # sort_order starts at 1 — the default Monthly (created later by
                # _backfill_default_project) takes sort_order 0.
                connection.execute(text(
                    "INSERT INTO projects (id, name, type, config, sort_order, is_default, unread_count, created_at, updated_at) "
                    "VALUES (:id, :name, :type, :cfg, :so, 0, 0, :ca, :ua)"
                ), {
                    "id": str(_uuid.uuid4()),
                    "name": p.get("name") or "Tracker",
                    "type": p.get("type") or "topic_monitor",
                    "cfg": _json.dumps(p.get("config") or {}, ensure_ascii=False),
                    "so": i + 1,
                    "ca": now_str,
                    "ua": now_str,
                })
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        connection.execute(text(
            "INSERT INTO app_state (key, value, updated_at) VALUES (:k, '1', :ts) "
            "ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = :ts"
        ), {"k": FLAG, "ts": ts})
    except Exception:
        pass  # seeding is best-effort; never block startup


def _backfill_default_project(connection) -> None:
    """Create the default "Monthly Literature Review" project and add all
    existing papers to it if they are not already members.

    Safe to run repeatedly (idempotent).
    """
    import json
    from datetime import datetime

    # 1. Ensure the default project exists.
    rows = list(connection.execute(
        text("SELECT id FROM projects WHERE type = 'monthly' AND name = 'Monthly Literature Review' LIMIT 1")
    ))
    if rows:
        default_project_id = rows[0][0]
    else:
        import uuid
        default_project_id = str(uuid.uuid4())
        # SQLite stores DateTime as strings; SQLAlchemy expects space-separated
        # format ("YYYY-MM-DD HH:MM:SS"), not the ISO T-format.
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        # PR-15 defensive: supply `is_default=0` explicitly so a fresh DB
        # (where create_all marked `is_default NOT NULL` per models.py)
        # doesn't trip on the raw INSERT. The follow-up UPDATE below flips
        # this row to is_default=1. Pre-PR-15 code relied on `is_default`
        # being added by the ALTER loop AFTER this INSERT — which works on
        # an old migrated DB but fails on a fresh ORM-built table.
        #
        # PR-46 defensive: same pattern for `unread_count` (Project ORM now
        # declares it NOT NULL DEFAULT 0 so the column survives the
        # migration tail-rewrite). The raw INSERT must include it on fresh
        # DBs where create_all already enforced NOT NULL before the ALTER
        # loop adds the column to legacy DBs.
        connection.execute(text(
            "INSERT INTO projects (id, name, type, config, sort_order, is_default, unread_count, created_at, updated_at) "
            "VALUES (:id, :name, :type, :cfg, :so, 0, 0, :ca, :ua)"
        ), {
            "id": default_project_id,
            "name": "Monthly Literature Review",
            "type": "monthly",
            "cfg": "{}",
            "so": 0,
            "ca": now_str,
            "ua": now_str,
        })

    # FN-B1-A04-1 / FN-B1-A08-1 — Mark this row as the un-deletable default.
    # Idempotent: re-running just rewrites is_default=1 on the same row.
    # Wrapped in try/except so first-boot (before _PROJECT_COLUMNS migration
    # runs ALTER TABLE) does not crash init_db; the column will exist on the
    # very next iteration of init_db()'s migration loop.
    try:
        connection.execute(
            text("UPDATE projects SET is_default = 1 WHERE id = :id"),
            {"id": default_project_id},
        )
    except Exception:
        pass

    # P0-3 (2026-06-11): the orphan dump in step 2 below contradicts the
    # 2026-06-03 product decision that broad-fetched papers in no project stay
    # backstage (see main.py "candidate pool ... NOT surfaced" comment). Guard it
    # behind a one-time flag so it runs at most once (legacy-DB placement) and
    # NEVER re-pollutes the curated Monthly Literature Review on subsequent boots
    # / re-fetches. Delete the 'mlr_orphan_backfill_done' app_state row to re-arm
    # a one-time pass. See _audit_2026-06-11/05-CLEANUP-EXECUTED.md.
    _orphan_flag = list(connection.execute(
        text("SELECT value FROM app_state WHERE key = 'mlr_orphan_backfill_done'")
    ))
    if _orphan_flag and (_orphan_flag[0][0] == "1"):
        return  # already ran once; candidate pool stays backstage
    _orphan_flag_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        text(
            "INSERT INTO app_state (key, value, updated_at) "
            "VALUES ('mlr_orphan_backfill_done', '1', :ts) "
            "ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = :ts"
        ),
        {"ts": _orphan_flag_ts},
    )

    # 2. Collect ORPHAN papers (papers with NO project membership at all).
    #    Previously this added every paper to Monthly regardless of whether
    #    it was already a member of another project (e.g. a Static collection
    #    like "Bent_GJI_literatures"). That caused cross-project pollution
    #    on every server restart. Fix: only backfill truly orphan papers.
    orphan_paper_ids = [
        row[0]
        for row in connection.execute(
            text(
                "SELECT p.id FROM papers p "
                "WHERE NOT EXISTS (SELECT 1 FROM project_papers pp "
                "                  WHERE pp.paper_id = p.id) "
                "ORDER BY p.created_at"
            )
        )
    ]
    if not orphan_paper_ids:
        return  # Nothing to backfill — all papers already have a home.

    papers = list(connection.execute(
        text("SELECT id, created_at, published_date FROM papers "
             "WHERE id IN (" + ",".join(["?"] * len(orphan_paper_ids)) + ")"),
        orphan_paper_ids,
    )) if False else list(connection.execute(
        # SQLAlchemy 1.4 bound-param style — convert to :p0,:p1,...
        text("SELECT id, created_at, published_date FROM papers WHERE id IN ("
             + ",".join(f":p{i}" for i in range(len(orphan_paper_ids))) + ")"),
        {f"p{i}": pid for i, pid in enumerate(orphan_paper_ids)},
    ))

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for paper_id, created_at_raw, pub_date_raw in papers:

        # Determine the YYYY-MM folder.
        folder = ""
        # Try published_date first ("2024-11-22", "2024-11", "2024", etc.)
        for raw in (pub_date_raw, created_at_raw):
            if not raw:
                continue
            s = str(raw)[:7]  # "YYYY-MM"
            if len(s) == 7 and s[4] == "-":
                folder = s
                break
        if not folder:
            folder = now_str[:7]  # "YYYY-MM" — falls through here when both pub_date_raw and created_at_raw are empty/malformed (pre-Wave-9 latent bug; was `now_iso` which is undefined)

        connection.execute(text(
            "INSERT OR IGNORE INTO project_papers "
            "(project_id, paper_id, folder, source, added_at) "
            "VALUES (:proj, :paper, :folder, 'backfill', :added)"
        ), {
            "proj": default_project_id,
            "paper": paper_id,
            "folder": folder,
            "added": now_str,
        })


# ---------------------------------------------------------------------------
# M-05 (BRIEF item H foundation) — paragraph-anchor backfill
# ---------------------------------------------------------------------------
PARAGRAPH_BACKFILL_FLAG = "paragraph_backfill_done"


def _backfill_paragraph_anchors() -> None:
    """Walk every paper with pdf_path set and add paragraphs[] to its
    PaperPageCache.pages. Idempotent, safe to crash and resume.

    Designed to be called inside a daemon thread from init_db(); never
    raises out to the caller — a single failing PDF logs and continues.
    """
    # Local imports so the thread function does not pull services/* into
    # module-load order.
    from models import AppState, PaperPageCache
    from services.pdf_page_cache import (
        extract_paragraph_anchors,
        _needs_paragraph_refresh,
    )

    session = SessionLocal()
    started = datetime.utcnow()
    upgraded = skipped = failed = 0
    try:
        flag = session.get(AppState, PARAGRAPH_BACKFILL_FLAG)
        if flag and flag.value == "1":
            logger.info("paragraph backfill already done; skipping")
            return

        paper_ids = [
            pid
            for (pid,) in session.execute(
                text(
                    "SELECT id FROM papers "
                    "WHERE pdf_path IS NOT NULL AND pdf_path <> '' "
                    "ORDER BY id"
                )
            )
        ]
        logger.info("paragraph backfill: %d candidate papers", len(paper_ids))

        for paper_id in paper_ids:
            try:
                cache = session.get(PaperPageCache, paper_id)
                if cache is None or not cache.pages:
                    skipped += 1
                    continue
                if not _needs_paragraph_refresh(cache):
                    skipped += 1
                    continue

                new_pages = []
                for page in cache.pages:
                    page_text = page.get("text") or ""
                    anchors = extract_paragraph_anchors(page_text)
                    merged = dict(page)
                    merged["paragraphs"] = anchors
                    new_pages.append(merged)

                cache.pages = new_pages
                cache.extracted_at = datetime.utcnow()
                session.add(cache)
                session.commit()
                upgraded += 1
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                failed += 1
                logger.warning(
                    "paragraph backfill failed for paper_id=%s: %s",
                    paper_id, exc,
                )

        flag = session.get(AppState, PARAGRAPH_BACKFILL_FLAG)
        if flag is None:
            session.add(AppState(key=PARAGRAPH_BACKFILL_FLAG, value="1"))
        else:
            flag.value = "1"
        session.commit()
        elapsed = (datetime.utcnow() - started).total_seconds()
        logger.info(
            "paragraph backfill done in %.1fs: upgraded=%d skipped=%d failed=%d",
            elapsed, upgraded, skipped, failed,
        )
    finally:
        session.close()


def _migrate_project_imports_unique(connection) -> None:
    """Rewrite project_imports so UNIQUE is on (project_id, source_hash), not
    on source_hash alone. Fixes FN-B3-A04-2 + FN-B3-A13-1 — same .bib must be
    importable into multiple projects.

    Idempotent: only rebuilds the table if the current schema still has the
    bare UNIQUE on source_hash. SQLite cannot ALTER away a column constraint,
    so we use the standard create-new + copy + drop + rename dance.
    """
    row = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='project_imports'")
    ).fetchone()
    if not row or not row[0]:
        # Table doesn't exist yet; create_all (above) will produce the new
        # schema directly.
        return
    schema_sql = row[0]
    if "UNIQUE (project_id, source_hash)" in schema_sql or "uq_project_import_hash" in schema_sql:
        # Already migrated.
        return
    # Belt-and-braces: only rebuild if we can see the legacy column-level UNIQUE
    # on source_hash.
    if "source_hash" not in schema_sql:
        return

    connection.execute(text(
        """
        CREATE TABLE project_imports_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_hash VARCHAR(64) NOT NULL,
            source_filename VARCHAR(255),
            imported_at DATETIME NOT NULL,
            paper_count INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT uq_project_import_hash UNIQUE (project_id, source_hash)
        )
        """
    ))
    connection.execute(text(
        "INSERT INTO project_imports_new (id, project_id, source_hash, source_filename, imported_at, paper_count) "
        "SELECT id, project_id, source_hash, source_filename, imported_at, paper_count FROM project_imports"
    ))
    connection.execute(text("DROP TABLE project_imports"))
    connection.execute(text("ALTER TABLE project_imports_new RENAME TO project_imports"))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_project_imports_project_id ON project_imports(project_id)"
    ))


def _create_qa_messages(connection) -> None:
    """W8-PR05 — per-paper AI Q&A chat persistence (BRIEF item D).

    Creates the `qa_messages` table from PR-04's `QAMessage` ORM model
    (see changelog/04-feature-design/12-qa-chat-persistence.md §2) plus
    the composite read index from 05-technical-architecture/18 §2.3.

    Idempotent: `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT
    EXISTS` mean re-running `init_db()` after the first boot is a no-op
    (one PRAGMA-equivalent lookup per statement, well under 1 ms).

    Note on FK type: `papers.id` is VARCHAR(64) (see models.py:14), not
    INTEGER as one of the early architecture sketches suggested. The
    SQL below matches the live column type so the FK enforces.

    Column shape follows PR-04's ORM model (model_id/model_label split,
    citations JSON envelope) — the §3 reconciliation note in the PR-05
    spec says PR-05 follows PR-04 (i.e. 12-qa-chat-persistence.md).
    """
    connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS qa_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id    VARCHAR(64) NOT NULL
                        REFERENCES papers(id) ON DELETE CASCADE,
            role        VARCHAR(16) NOT NULL,
            text        TEXT NOT NULL,
            model_id    VARCHAR(64),
            model_label VARCHAR(64),
            citations   JSON NOT NULL DEFAULT '[]',
            position    INTEGER NOT NULL,
            archived    BOOLEAN NOT NULL DEFAULT 0,
            created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_qa_paper_position UNIQUE (paper_id, position)
        )
        """
    ))

    # Composite read index — covers the hot query
    # `SELECT … WHERE paper_id = ? AND archived = 0 ORDER BY position`
    # used by GET /api/papers/{paper_id}/qa/history and the in-route
    # `_load_visible_history` helper. Including `archived` in the key
    # prefix lets SQLite skip both the filter scan and the ORDER BY.
    # Name aligns with PR-04's __table_args__ Index name.
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_qa_paper_archived_pos "
        "ON qa_messages (paper_id, archived, position)"
    ))

    # Secondary single-column index on paper_id — required by the
    # `index=True` flag on the QAMessage.paper_id ORM column (PR-04).
    # SQLite would create one implicitly on first ORM use; making it
    # explicit here keeps the schema deterministic across fresh-install
    # vs. migrated DBs.
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_qa_messages_paper_id "
        "ON qa_messages (paper_id)"
    ))


def _migrate_folders_to_monthly(connection) -> None:
    """Re-bucket legacy tracker sub-folders to monthly (``YYYY-MM``).

    _derive_folder() in services/tracker_scheduler.py used to bucket weekly
    (``YYYY-Www``) or daily (``YYYY-MM-DD``) depending on a tracker's cadence;
    as of 2026-06-13 all tracker sub-folders are monthly. This rewrites any
    existing weekly/daily ``project_papers.folder`` value to the month of the
    linked paper's published_date (falling back to the folder string's own
    month for daily values). Idempotent — rows already in ``YYYY-MM`` form,
    or with no derivable month, are left untouched.
    """
    import re as _re

    weekly_or_daily = _re.compile(r"^\d{4}-W\d{2}$|^\d{4}-\d{2}-\d{2}$")
    rows = list(connection.execute(text(
        "SELECT pp.rowid, pp.folder, p.published_date "
        "FROM project_papers pp JOIN papers p ON p.id = pp.paper_id "
        "WHERE pp.folder IS NOT NULL AND pp.folder != ''"
    )))
    updated = 0
    for rowid, folder, pub in rows:
        folder_s = str(folder)
        if not weekly_or_daily.match(folder_s):
            continue
        month = None
        pub_s = str(pub) if pub else ""
        if len(pub_s) >= 7 and pub_s[4] == "-":
            month = pub_s[:7]                     # YYYY-MM from published_date
        elif len(folder_s) == 10 and folder_s[4] == "-" and folder_s[7] == "-":
            month = folder_s[:7]                  # daily folder already carries the month
        if not month:
            continue                              # weekly folder w/ no usable date — leave it
        connection.execute(
            text("UPDATE project_papers SET folder = :f WHERE rowid = :rid"),
            {"f": month, "rid": rowid},
        )
        updated += 1
    if updated:
        print(f"[db] _migrate_folders_to_monthly: re-bucketed {updated} tracker folder(s) → YYYY-MM")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(papers)"))}
        if "institutions" not in columns:
            connection.execute(text("ALTER TABLE papers ADD COLUMN institutions JSON"))
        # v2 — fetch-time keywords from OpenAlex concepts (cheap; no Gemini call).
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(papers)"))}
        if "keywords" not in columns:
            connection.execute(text("ALTER TABLE papers ADD COLUMN keywords JSON"))

        # PDF acquisition pipeline migration: add any retrieval_* columns that
        # aren't present yet. We refresh the column set on each iteration so
        # successive ALTERs see each other — SQLite is fine with serial ALTERs.
        for column_name, column_ddl in _PAPER_RETRIEVAL_COLUMNS:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(papers)"))}
            if column_name not in columns:
                connection.execute(text(f"ALTER TABLE papers ADD COLUMN {column_name} {column_ddl}"))

        # M1 upload-dedup + reader-telemetry columns.
        for column_name, column_ddl in _PAPER_M1_COLUMNS:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(papers)"))}
            if column_name not in columns:
                connection.execute(text(f"ALTER TABLE papers ADD COLUMN {column_name} {column_ddl}"))

        # BRIEF v2 §3 — cheap title+abstract semantic-match embedding columns.
        # Refresh the column set each iteration so serial ALTERs see each other.
        for column_name, column_ddl in _PAPER_EMBEDDING_COLUMNS:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(papers)"))}
            if column_name not in columns:
                connection.execute(text(f"ALTER TABLE papers ADD COLUMN {column_name} {column_ddl}"))

        # Unique index on pdf_sha256 — NULLs are allowed and don't collide in
        # SQLite UNIQUE indexes, so existing rows with NULL hash are fine.
        existing_indexes = {
            row[1] for row in connection.execute(text("PRAGMA index_list(papers)"))
        }
        if "idx_papers_pdf_sha256_unique" not in existing_indexes:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_pdf_sha256_unique "
                    "ON papers(pdf_sha256) WHERE pdf_sha256 IS NOT NULL"
                )
            )

        ai_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(ai_analyses)"))}
        if "keywords" not in ai_columns:
            connection.execute(text("ALTER TABLE ai_analyses ADD COLUMN keywords JSON DEFAULT '[]'"))
        # PR-14 / BRIEF item C — model identity columns for the analysis-card
        # pill. PRAGMA-then-ALTER guard: re-running init_db is a no-op once
        # the columns exist. Both nullable so existing rows migrate cleanly.
        ai_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(ai_analyses)"))}
        if "analysis_model" not in ai_columns:
            connection.execute(text("ALTER TABLE ai_analyses ADD COLUMN analysis_model VARCHAR(64)"))
        if "analysis_model_label" not in ai_columns:
            connection.execute(text("ALTER TABLE ai_analyses ADD COLUMN analysis_model_label VARCHAR(64)"))
        # v3 faceted taxonomy (Stage B) — nullable JSON; old rows migrate cleanly.
        if "facets" not in ai_columns:
            connection.execute(text("ALTER TABLE ai_analyses ADD COLUMN facets JSON"))

        # Custom per-project emoji icon (2026-06-06). Nullable; falls back to the
        # type default in the UI when NULL.
        proj_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(projects)"))}
        if "icon" not in proj_columns:
            connection.execute(text("ALTER TABLE projects ADD COLUMN icon VARCHAR(16)"))

        # retrieval_attempts is a brand-new table so Base.metadata.create_all
        # above handles it. Nothing else needed here, but we leave a sanity
        # check just in case create_all was skipped (e.g. if a user hand-edited
        # the schema).
        attempts_info = list(connection.execute(text("PRAGMA table_info(retrieval_attempts)")))
        if not attempts_info:
            # Should never happen because create_all runs first; keep the
            # fallback so a half-migrated DB can still be recovered without
            # manual intervention.
            from models import RetrievalAttempt  # local import to avoid circular
            RetrievalAttempt.__table__.create(bind=connection)

        # Rewrite any legacy COS topic labels stored in JSON columns.
        # (e.g. "Cyrosphere Research" -> "Cryosphere Research")
        _migrate_cos_topic_renames(connection)

        # M2: create G6 page-cache table + G1 Projects tables.
        for tbl_check in ("paper_page_caches", "projects", "project_papers", "project_imports"):
            info = list(connection.execute(text(f"PRAGMA table_info({tbl_check})")))
            if not info:
                # Table missing — run create_all for this table only.
                from models import PaperPageCache, Project, ProjectPaper, ProjectImport  # noqa: F401
                Base.metadata.tables[tbl_check].create(bind=connection)

        # 2026-06-13: re-bucket legacy weekly/daily tracker sub-folders to monthly
        # (YYYY-MM), matching the new always-monthly _derive_folder() in
        # services/tracker_scheduler.py. Runs after project_papers is guaranteed.
        _migrate_folders_to_monthly(connection)

        # Wave-9 — user_paper_states table for the read/archive/dismiss UI
        # surface. Single row per paper, all three timestamps nullable so the
        # toggle endpoints can clear individual flags without deleting the
        # row. Created via the ORM (table is brand-new — no ALTER path needed).
        ups_info = list(connection.execute(text("PRAGMA table_info(user_paper_states)")))
        if not ups_info:
            from models import UserPaperState  # noqa: F401
            Base.metadata.tables["user_paper_states"].create(bind=connection)
        else:
            # 2026-06-16 — add liked_at for the ❤️ "interested" signal on
            # pre-existing tables (the CREATE above covers brand-new DBs).
            ups_cols = {row[1] for row in ups_info}
            if "liked_at" not in ups_cols:
                connection.execute(text("ALTER TABLE user_paper_states ADD COLUMN liked_at DATETIME"))

        # FN-B3-A04-2 + FN-B3-A13-1: scope project_imports.source_hash UNIQUE
        # to (project_id, source_hash). Must run before _backfill_default_project
        # touches project_imports (it doesn't currently, but keep the ordering
        # conservative so any future backfill sees the new shape).
        _migrate_project_imports_unique(connection)

        # PR-05 — qa_messages table for BRIEF item D (per-paper chat
        # persistence). Lands after the project-tables guard so the FK
        # target `papers` is guaranteed present, and before the default-
        # project backfill so no cascade can race the table creation.
        _create_qa_messages(connection)

        # FN-B1-A04-1 / FN-B1-A08-1: add is_default column to projects so
        # _backfill_default_project below can flag the seed row. Must run
        # BEFORE _backfill_default_project so its UPDATE sees the column.
        for column_name, column_ddl in _PROJECT_COLUMNS:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(projects)"))}
            if column_name not in columns:
                connection.execute(text(f"ALTER TABLE projects ADD COLUMN {column_name} {column_ddl}"))

        # 2026-05-25 hotfix — backfill the two project_papers columns that
        # the ORM declares (PR-01 / PR-32) but db.py never migrated. Without
        # these, every `GET /api/papers?project_id=...` query crashes with
        # "no such column: position" and returns empty via the new global
        # 500 handler, making the UI workspace sidebar render zero papers.
        for column_name, column_ddl in _PROJECT_PAPER_COLUMNS:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(project_papers)"))}
            if column_name not in columns:
                connection.execute(text(f"ALTER TABLE project_papers ADD COLUMN {column_name} {column_ddl}"))
        # Seed `seen_in_project_at` from `added_at` for historical rows so
        # the tracker unread badge does not flash N for every old paper on
        # first boot after this migration.
        connection.execute(text(
            "UPDATE project_papers SET seen_in_project_at = added_at "
            "WHERE seen_in_project_at IS NULL"
        ))

        # 2026-06-15: seed the fresh-install preset trackers (owner-curated set)
        # BEFORE the taxonomy backfill so the seeded rows pick up kind/scope/cadence
        # on the same boot. One-shot; no-op for existing libraries.
        _seed_preset_trackers(connection)

        # PR-02: derive the new taxonomy triple from legacy `Project.type`.
        # Runs BEFORE _backfill_default_project so the just-seeded default
        # row also picks up kind='tracker' on the same boot it is created.
        _backfill_project_taxonomy(connection)

        # 2026-05-25 hotfix: re-classify any legacy type=monthly row that
        # was previously backfilled as kind=collection (pre-update map).
        # Idempotent — zero-row UPDATE on a fresh boot.
        _fixup_monthly_kind_to_tracker(connection)

        # Seed default project + backfill all papers into it.
        _backfill_default_project(connection)

        # One-time fix: the initial backfill stored datetimes as ISO T-format
        # ("2026-05-12T08:48:43") but SQLAlchemy's DateTime column expects
        # space-separated format ("2026-05-12 08:48:43"). Repair any rows that
        # slipped through with the wrong format.
        _fix_iso_datetimes(connection)

    # M-05 — paragraph-anchor backfill. Daemon thread; never blocks startup.
    # Disabled when DISABLE_PARAGRAPH_BACKFILL=1 (set by tests so the
    # idempotency check in test_init_db_is_idempotent does not race with
    # the daemon thread writing AppState mid-snapshot).
    import os as _os
    if _os.environ.get("DISABLE_PARAGRAPH_BACKFILL") == "1":
        return
    try:
        with SessionLocal() as session:
            from models import AppState  # local import
            flag = session.get(AppState, PARAGRAPH_BACKFILL_FLAG)
            if not flag or flag.value != "1":
                t = threading.Thread(
                    target=_backfill_paragraph_anchors,
                    name="paragraph-backfill",
                    daemon=True,
                )
                t.start()
    except Exception as exc:  # noqa: BLE001 — never block startup on backfill spawn
        logger.warning("paragraph backfill thread did not start: %s", exc)
