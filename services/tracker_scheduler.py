"""Per-tracker background scheduler — re-evaluate trackers on their own cadence.

Today the global fetch scheduler (`main._scheduler_loop`) refreshes the paper
pool every few hours. Trackers, however, do not independently re-run their
query against newly arrived papers between fetches. This module fills that
gap: a single daemon thread wakes every ``TRACKER_SCHEDULER_INTERVAL_SEC``
seconds (default 300), iterates every active ``kind='tracker'`` project, and
for each tracker whose ``last_match_at`` is older than its cadence (daily,
weekly, monthly) re-runs the matcher against recent papers.

The actual match logic is delegated to the canonical matcher in
``services.analyze_service._auto_assign_to_topic_monitors`` — we do NOT
duplicate predicate code; we just feed paper-by-paper into it. That matcher
delegates to the predicate registry (``services.predicates.match_tracker``),
which scores the paper over the EMBEDDED pool (the semantic predicate compares
the tracker's cached query embedding against ``Paper.embedding`` via numpy
cosine) and WRITES the best score to ``ProjectPaper.match_score`` (BRIEF v2
§1/§6 — tracker views sort by it). The matcher is already idempotent
(UniqueConstraint on (project_id, paper_id)), so calling it again for an
already-linked paper is a cheap no-op that only refreshes match_score upward.

BRIEF v2 §6 (lazy deep analysis): a fresh tracker match enqueues the full
Gemini Pro analysis as a hint (``analyze_service.enqueue_analysis``); the
scheduler itself never calls Gemini, keeping each pass cheap and network-free
(important for the ``DISABLE_TRACKER_SCHEDULER`` test contract). A separate
worker / route drains ``analyze_service.drain_analysis_queue`` to do the real
analysis on demand.

Cadence semantics — see ``changelog/04-feature-design/01-tracker-data-model.md``:

  daily   → re-check every 24h
  weekly  → re-check every 7d
  monthly → re-check every 30d

If ``Project.last_match_at`` is NULL the tracker is treated as "never run"
and is always due.

Folder derivation (see ``_derive_folder``):

  author-follow → "Institution / Author"
  all others    → "YYYY-MM"

(cadence-independent since 2026-06-13; co-author first-author folding 2026-06-14.)

Public API:

  ``start_tracker_scheduler()`` — idempotent daemon launcher.
  ``run_tracker_pass_once()`` — synchronous single-pass; returns stats.
  ``run_tracker_pass_for_project(project_id)`` — force-run one tracker.
  ``cadence_to_delta(cadence)`` — pure helper for cadence math.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Module-level singletons — keep the daemon idempotent across hot-reloads.    #
# --------------------------------------------------------------------------- #

_scheduler_lock = threading.Lock()
_scheduler_thread: Optional[threading.Thread] = None
_PASS_LOOKBACK_DAYS_FIRST_RUN = 90  # see §4 of plan: catch-up window on first run.


def _default_interval_seconds() -> int:
    """Read TRACKER_SCHEDULER_INTERVAL_SEC each call so tests can monkey-patch."""
    raw = os.environ.get("TRACKER_SCHEDULER_INTERVAL_SEC", "")
    try:
        v = int(raw) if raw else 300
    except ValueError:
        v = 300
    return max(5, v)  # floor at 5s to avoid CPU thrash if someone sets 0


# --------------------------------------------------------------------------- #
# Cadence math.                                                               #
# --------------------------------------------------------------------------- #

_CADENCE_DELTAS = {
    "daily":   timedelta(days=1),
    "weekly":  timedelta(days=7),
    "monthly": timedelta(days=30),
}


def cadence_to_delta(cadence: Optional[str]) -> timedelta:
    """daily → 1d, weekly → 7d, monthly → 30d. Unknown/None → 30d (safe default).

    The default of 30d matches the "monthly" bucket so an accidentally-empty
    cadence field doesn't accidentally spam matches on every pass.
    """
    if not cadence:
        return _CADENCE_DELTAS["monthly"]
    return _CADENCE_DELTAS.get(cadence.strip().lower(), _CADENCE_DELTAS["monthly"])


def _is_due(now: datetime, last_match_at: Optional[datetime], cadence: Optional[str]) -> bool:
    """True if a tracker should run on this pass.

    `last_match_at is None` → "never run yet" → always due (first-run catch-up).
    """
    if last_match_at is None:
        return True
    return now >= last_match_at + cadence_to_delta(cadence)


# --------------------------------------------------------------------------- #
# Folder derivation.                                                          #
# --------------------------------------------------------------------------- #

def _parse_published_date(raw: Any) -> Optional[date]:
    """Best-effort: ``Paper.published_date`` is stored as a free string.

    The fetcher writes ISO ("YYYY-MM-DD"); legacy rows sometimes have just
    "YYYY-MM" or junk. Return ``None`` on failure — caller falls back to now().
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    return None


_FOLDER_OTHER = "Other"


def _author_meta_for(project):
    """Return {author_id: {"name","institution"}} if `project` is an author-follow
    tracker carrying author_meta, else None. (Set by the follow-author flow /
    the watchlist seed.)"""
    cfg = getattr(project, "config", None)
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            return None
    if not isinstance(cfg, dict):
        return None
    for p in cfg.get("predicates") or []:
        if isinstance(p, dict) and p.get("type") == "author" and p.get("author_meta"):
            return p["author_meta"]
    return None


def _first_matched_author(meta, paper):
    """First paper author (in listed order) who is a watchlist member → (institution,
    author_name), else None. Same case-insensitive full-name substring rule as
    AuthorPredicate, so the folder author matches why the paper was admitted."""
    authors = [a for a in (getattr(paper, "authors", None) or []) if a]
    if not authors:
        return None
    items = [(m.get("name", ""), m.get("institution") or _FOLDER_OTHER)
             for m in meta.values() if m.get("name")]
    for au in authors:
        al = str(au).lower()
        for name, inst in items:
            if name and name.lower() in al:
                return (inst, name)
    return None


def _derive_folder(project, paper) -> str:
    """Bucket key for a paper inside a tracker.

    - Author-follow trackers (predicate carries ``author_meta``): a two-level
      ``"<Institution> / <Author>"`` key for the first matched watchlist author.
      Co-authored papers fold under the first listed match (design decision
      2026-06-14); the column view splits on " / " to render institution → author.
      Falls back to month when no configured name string matched (rare outbound
      name-format variants) so the paper still lands somewhere.
    - All other trackers: month (``YYYY-MM``).
    """
    meta = _author_meta_for(project)
    if meta:
        hit = _first_matched_author(meta, paper)
        if hit:
            inst, name = hit
            return f"{inst} / {name}"
    d = _parse_published_date(getattr(paper, "published_date", None)) or datetime.utcnow().date()
    return d.strftime("%Y-%m")


# --------------------------------------------------------------------------- #
# Single-pass logic.                                                          #
# --------------------------------------------------------------------------- #

# Trackers and Collections are independent (user directive 2026-06-06): a
# tracker's retroactive match scans only the TRACKER library = papers from the
# automated intake pipeline (openalex/crossref/rss/eth_rc/arxiv…). A paper whose
# `source` carries one of these markers is a manual import that belongs to a
# Collection and is excluded, so a new tracker never pulls in collection-only
# papers. (`source.like('%zotero%')` also catches the "v1_migration:zotero" form.)
_IMPORT_SOURCE_MARKERS = ("zotero", "upload", "manual", "import")


def _papers_to_evaluate(db_session, since: Optional[datetime]):
    """Yield Paper rows that could be candidates for `since` cutoff.

    `since` None → use a 90-day rolling window (first-run catch-up).
    Manually-imported (Collection) papers are excluded — see
    :data:`_IMPORT_SOURCE_MARKERS`.
    """
    from models import Paper
    from sqlalchemy import not_

    if since is None:
        cutoff = datetime.utcnow() - timedelta(days=_PASS_LOOKBACK_DAYS_FIRST_RUN)
    else:
        cutoff = since
    q = db_session.query(Paper).filter(Paper.created_at >= cutoff)
    for marker in _IMPORT_SOURCE_MARKERS:
        q = q.filter(not_(Paper.source.like(f"%{marker}%")))
    return q.order_by(Paper.created_at.asc()).all()


def _run_one_tracker(db_session, project) -> int:
    """Re-match `project` against recent papers. Returns # of new links written.

    Reuses the canonical matcher in analyze_service to avoid duplicating the
    predicate. We feed it papers one at a time; the matcher's own
    UniqueConstraint / "already a member?" check makes the call idempotent.

    Early-returns 0 for `is_default=true` projects: the seeded Monthly
    Literature Review (now kind='tracker', cadence='monthly') has no
    config.query because every paper auto-flows in via
    `_add_paper_to_default_project()` in main.py. Letting it traverse
    the query matcher here would just no-op (empty query → zero matches)
    and waste a DB pass; explicit early-return documents the design.
    """
    from services.analyze_service import (
        _auto_assign_to_topic_monitors,
        _has_analysis,
        enqueue_analysis,
    )
    from models import ProjectPaper

    if getattr(project, "is_default", False):
        # Belt-and-braces: bump last_match_at so we don't recompute due-ness
        # on every wake. Other code paths own this project's paper flow.
        project.last_match_at = datetime.utcnow()
        db_session.commit()
        return 0

    candidates = _papers_to_evaluate(db_session, project.last_match_at)
    if not candidates:
        # Still bump last_match_at so we don't loop on empty-DB trackers.
        project.last_match_at = datetime.utcnow()
        db_session.commit()
        return 0

    before = (
        db_session.query(ProjectPaper)
        .filter(ProjectPaper.project_id == project.id)
        .count()
    )

    for paper in candidates:
        # Build a topic_matches payload from the paper's stored topics so the
        # matcher's "any project topic overlaps with matched_names" branch
        # fires correctly without re-running Gemini.
        topic_list = []
        try:
            for t in (paper.topics or []):
                if t:
                    topic_list.append({"topic": str(t), "score": 1.0})
        except Exception:
            topic_list = []
        try:
            _auto_assign_to_topic_monitors(db_session, paper, topic_list)
        except Exception as exc:  # noqa: BLE001 — one bad paper must not stop the pass
            logger.warning(
                "tracker_scheduler: matcher failed for paper %s in project %s: %s",
                getattr(paper, "id", "?"), project.id, exc,
            )

    after = (
        db_session.query(ProjectPaper)
        .filter(ProjectPaper.project_id == project.id)
        .count()
    )

    # Patch the freshly-inserted rows to carry the derived folder.
    # The legacy matcher writes "YYYY-MM" for every tracker — we want
    # author-follow → Institution/Author; all others → YYYY-MM
    # (cadence-independent since 2026-06-13).
    new_rows = (
        db_session.query(ProjectPaper)
        .filter(
            ProjectPaper.project_id == project.id,
            ProjectPaper.source == "topic_monitor",
        )
        .all()
    )
    for row in new_rows:
        paper = next((p for p in candidates if p.id == row.paper_id), None)
        if paper is None:
            continue
        new_folder = _derive_folder(project, paper)
        if row.folder != new_folder:
            row.folder = new_folder
        # BRIEF v2 §6 (a): ensure a lazy full-analysis hint exists for every
        # matched paper that still lacks an AIAnalysis row. The matcher already
        # enqueues on first insert; this is defence-in-depth so a paper matched
        # via any path still gets analysed on the next drain. enqueue is a cheap
        # idempotent set-add — no Gemini call here.
        try:
            if not _has_analysis(db_session, row.paper_id):
                enqueue_analysis(row.paper_id)
        except Exception:
            pass

    project.last_match_at = datetime.utcnow()
    db_session.commit()
    return max(0, after - before)


def _is_tracker(project) -> bool:
    """Identify trackers via the canonical `kind` column, falling back to legacy.

    The migration to `kind` may not have run on every row; legacy rows
    where kind IS NULL but type='topic_monitor' should still count.
    """
    kind = getattr(project, "kind", None)
    if kind == "tracker":
        return True
    if kind is None and getattr(project, "type", None) == "topic_monitor":
        return True
    return False


def run_tracker_pass_once() -> Dict[str, int]:
    """Synchronous single-pass for tests and the manual 'Run now' button.

    Returns ``{"trackers_checked", "trackers_run", "papers_matched"}``.
    """
    from db import SessionLocal
    from models import Project

    db_session = SessionLocal()
    stats = {"trackers_checked": 0, "trackers_run": 0, "papers_matched": 0}
    try:
        now = datetime.utcnow()
        all_projects = db_session.query(Project).filter(Project.archived_at.is_(None)).all()
        # Exclude is_default trackers from "checked" — they own a separate
        # auto-add path (_add_paper_to_default_project) so the query-based
        # matcher has nothing to do for them. Counting them in
        # trackers_checked would inflate the stat the user sees on
        # `POST /api/trackers/run_now`.
        trackers = [p for p in all_projects if _is_tracker(p) and not getattr(p, "is_default", False)]
        stats["trackers_checked"] = len(trackers)

        for project in trackers:
            if not _is_due(now, project.last_match_at, project.cadence):
                continue
            stats["trackers_run"] += 1
            stats["papers_matched"] += _run_one_tracker(db_session, project)
    finally:
        db_session.close()
    return stats


def run_tracker_pass_for_project(project_id: str) -> Dict[str, Any]:
    """Force-run the matcher for one tracker, ignoring cadence.

    Used by ``POST /api/projects/{id}/run_now``.
    """
    from db import SessionLocal
    from models import Project

    db_session = SessionLocal()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            return {"ok": False, "error": "project_not_found", "papers_matched": 0}
        if not _is_tracker(project):
            return {"ok": False, "error": "not_a_tracker", "papers_matched": 0}
        if project.archived_at is not None:
            return {"ok": False, "error": "archived", "papers_matched": 0}
        matched = _run_one_tracker(db_session, project)
        return {"ok": True, "papers_matched": matched, "project_id": project_id}
    finally:
        db_session.close()


# --------------------------------------------------------------------------- #
# Daemon loop.                                                                #
# --------------------------------------------------------------------------- #

def _scheduler_loop() -> None:
    interval = _default_interval_seconds()
    while True:
        try:
            run_tracker_pass_once()
        except Exception as exc:  # noqa: BLE001 — never let the daemon die on a single pass.
            logger.warning("tracker_scheduler: pass failed: %s", exc)
        time.sleep(interval)


def start_tracker_scheduler() -> Optional[threading.Thread]:
    """Start the daemon thread. Safe to call multiple times — idempotent.

    Returns the running thread, or None if disabled via env or already running.
    """
    if os.environ.get("DISABLE_TRACKER_SCHEDULER") == "1":
        return None

    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return _scheduler_thread
        t = threading.Thread(
            target=_scheduler_loop,
            daemon=True,
            name="seismo-tracker-scheduler",
        )
        t.start()
        _scheduler_thread = t
        return t
