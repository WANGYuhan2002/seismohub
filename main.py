import hashlib
import io
import json
import logging
import os
import socket
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import defer, selectinload
from uvicorn import run as uvicorn_run

from db import DATABASE_PATH, SessionLocal, init_db
from models import AppState, Paper, PaperPageCache, Project, ProjectImport, ProjectPaper, QAMessage, RetrievalAttempt, UserPaperState
# PR-33: nine-key sort dispatch. Key extractors live in services/sort_keys.py
# (PR-34) so this file stays a thin builders dict; the `_neg_*` helpers convert
# ISO-date strings, integers, and None values into sortable tuples that achieve
# DESC order via Python's ascending sorted(). `_position` reads the per-project
# index PR-32 prefetches into get_filtered_papers().
from services.sort_keys import (
    author_sort_key, title_initial_key, journal_sort_key,
    _neg_date, _neg_year, _neg_updated, _neg_added, _neg_last_opened, _position,
)
from services.analyze_service import (
    _normalize_figure_analyses,
    analyze_paper_from_pdf,
    analyze_pending_papers,
    backfill_unembedded_papers,
    clean_text,
    drain_analysis_queue,
    extract_figure_insights_from_pdf,
    match_new_papers_to_trackers,
    normalize_doi,
)
from services.fetch_service import fetch_and_store_new_papers, normalize_institutions
from services.metadata_lookup import (
    detect_identifier_kind,            # PR-28
    lookup_metadata_by_identifier,     # PR-28
    lookup_metadata_by_doi,            # PR-24
    lookup_metadata_by_title_authors,  # T27 — title-fallback when no DOI
)
from services.pdf_naming import (
    build_pdf_filename,
    is_managed_download_path,
    pdf_target_path_for_paper,
    sha256_of_bytes,
    sha256_of_file,
    write_pdf_atomic,
)
from services.pdf_launcher import (
    PdfLauncherError,
    enumerate_readers,
    launch_pdf,
)
from services.export_service import (
    papers_to_bibtex_text,
    papers_to_csl_json,
    stream_all_zip,
    stream_pdfs_zip,
)
from services.journal_abbrev import abbreviate as journal_abbreviate
from services.journal_abbrev import journal_color
from services import watch_folder as watch_folder_service
# M1 F12: the 5-stage PDF acquisition pipeline is no longer wired into any
# active route. The module is kept on disk so the `RetrievalAttempt` audit
# table and historical metadata stay readable, but its imports here are
# limited to model-side helpers. The route stubs return `410 Gone` (see
# _ACQUIRE_GONE_BODY below).
from services.zotero_import import (
    ZoteroImportError,
    _allocate_zotero_id,
    find_duplicate_existing_pdfs,
    import_zotero_files,
    normalize_upload_path,
)
from services.analyze_service import (
    extract_doi_from_any as _extract_doi_from_any,
    extract_pdf_text as _extract_pdf_text_for_upload,
    normalize_doi as _normalize_doi_for_upload,
)
# PR-XX: per-tracker cadence-aware scheduler (BRIEF §1.1 mode 1).
# Daemon thread launched from startup_event; env-gated for tests via
# DISABLE_TRACKER_SCHEDULER=1 (mirrors the DISABLE_PARAGRAPH_BACKFILL
# pattern in db.py).
from services.tracker_scheduler import (
    run_tracker_pass_once,
    run_tracker_pass_for_project,
    start_tracker_scheduler,
)


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
DOWNLOADS_DIR = BASE_DIR / "downloads"
FETCH_THRESHOLD_SECONDS = int(os.getenv("SEISMO_FETCH_THRESHOLD_SECONDS", str(6 * 60 * 60)))
SCHEDULER_CHECK_SECONDS = int(os.getenv("SEISMO_SCHEDULER_CHECK_SECONDS", "300"))
# FN-B8-A02-1: hard cap on `/api/papers/upload` body size. Reject before
# we hash, dedup, or allocate a Paper row.
MAX_UPLOAD_BYTES = int(os.getenv("SWP_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))  # 100 MB default


def _env_bool(name: str, default: bool) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def _port_is_available(host: str, port: int) -> Optional[bool]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except PermissionError:
            return None
        except OSError:
            return False
    return True


def _project_default_port(base_dir: Path) -> int:
    digest = hashlib.sha1(str(base_dir).encode("utf-8", errors="ignore")).hexdigest()
    return 20000 + (int(digest[:8], 16) % 20000)


def _resolve_server_port(host: str, base_dir: Path) -> int:
    explicit_port = (os.getenv("SEISMO_PORT") or "").strip()
    if explicit_port:
        return int(explicit_port)

    preferred_port = _project_default_port(base_dir)
    preferred_status = _port_is_available(host, preferred_port)
    if preferred_status is not False:
        return preferred_port

    for offset in range(1, 64):
        candidate = preferred_port + offset
        if candidate > 65535:
            break
        candidate_status = _port_is_available(host, candidate)
        if candidate_status is not False:
            return candidate

    return preferred_port


SERVER_HOST = "127.0.0.1"
SERVER_PORT = _resolve_server_port(SERVER_HOST, BASE_DIR)
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/"
OPEN_BROWSER = _env_bool("SEISMO_OPEN_BROWSER", True)
# Set SEISMO_NO_AUTOFETCH=1 for a static instance (testing / clean baseline):
# disables the background fetch scheduler and the on-startup stale-data refresh
# so the DB is never auto-mutated. Pair with DISABLE_TRACKER_SCHEDULER=1 +
# DISABLE_PARAGRAPH_BACKFILL=1 for a fully frozen instance.
# See _audit_2026-06-11/02-ACTION-PLAN.md C / 05-CLEANUP-EXECUTED.md.
AUTO_FETCH_ENABLED = not _env_bool("SEISMO_NO_AUTOFETCH", False)

# Eager AI analysis (2026-07-05): analyse EVERY paper proactively in the
# background so it is ready BEFORE the user opens it (owner: API cost is not a
# concern). Replaces the lazy-only model for the analysis side. Bounded per
# pass; backs off when a pass makes no progress (e.g. missing key) so it
# never hot-loops. Disable for tests/CI with DISABLE_EAGER_ANALYSIS=1.
# 2026-07-06: each pass now fans out to a bounded thread pool (workers=1 is
# the serial legacy path and the runtime rollback switch).
EAGER_ANALYSIS_ENABLED = not _env_bool("DISABLE_EAGER_ANALYSIS", False)
EAGER_ANALYSIS_WORKERS = max(1, min(8, int(os.getenv("SEISMO_EAGER_ANALYSIS_WORKERS", "5"))))
# Unset batch ⇒ four pool-waves of work per pass (workers × 4) so per-paper
# stragglers amortize; an explicit env value always wins.
EAGER_ANALYSIS_BATCH = int(os.getenv("SEISMO_EAGER_ANALYSIS_BATCH") or EAGER_ANALYSIS_WORKERS * 4)
EAGER_ANALYSIS_IDLE_SEC = int(os.getenv("SEISMO_EAGER_ANALYSIS_IDLE_SEC", "60"))
EAGER_ANALYSIS_BACKOFF_SEC = int(os.getenv("SEISMO_EAGER_ANALYSIS_BACKOFF_SEC", "600"))
EAGER_ANALYSIS_GAP_SEC = float(os.getenv("SEISMO_EAGER_ANALYSIS_GAP_SEC", "1.0"))

DOWNLOADS_DIR.mkdir(exist_ok=True)

KNOWN_PUBLISHERS = {
    "american geophysical union",
    "oxford university press",
    "elsevier",
    "elsevier b.v.",
    "elsevier bv",
    "elsevier ltd",
    "springer",
    "springer nature",
    "springer-verlag",
    "wiley",
    "john wiley & sons",
    "john wiley and sons",
    "society of exploration geophysicists",
    "seismological society of america",
    "cambridge university press",
    "taylor & francis",
    "ieee",
    "sage publications",
    "copernicus publications",
    "mdpi",
    "academic press",
    "pergamon",
    "nature publishing group",
}

app = FastAPI(title="Seismo-Hub: Local AI Reader")
app.mount("/downloads", StaticFiles(directory=str(DOWNLOADS_DIR)), name="downloads")

# ---------------------------------------------------------------------------
# sec(M1): central 500-error handler. Stops leaking exception details (file
# paths, library names, occasional SQL fragments) to clients. Per-handler
# try/except blocks that did cleanup (rollback, file unlink, runtime stage
# transitions) still wrap their critical sections — they now return the
# generic envelope below instead of `str(exc)`. Handlers that simply echoed
# the error were removed, since this catch-all handles the rest.
# ---------------------------------------------------------------------------
_logger = logging.getLogger("seismohub")


def _error_envelope(message: str) -> Dict[str, Any]:
    """Shape used by both the global handler and per-handler 500 responses."""
    return {
        "error": "internal_server_error",
        "message": message,
        "error_id": uuid.uuid4().hex[:12],
    }


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all: log the full traceback server-side, return a generic
    error envelope with a 12-char error_id the user can quote when reporting
    bugs. Local-first single-user app — low real-world risk, cleaner contract."""
    envelope = _error_envelope(
        "Something went wrong on the server. Check the server log for details.",
    )
    _logger.error(
        "Unhandled exception [error_id=%s] on %s %s:\n%s",
        envelope["error_id"], request.method, request.url.path,
        traceback.format_exc(),
    )
    return JSONResponse(status_code=500, content=envelope)


_fetch_lock = threading.Lock()
# _acquire_batch_lock retained as a no-op so any unloaded references (e.g.
# from tests) don't crash. The PDF acquisition pipeline routes return 410.
_acquire_batch_lock = threading.Lock()
_pdf_normalize_lock = threading.Lock()
_zotero_import_lock = threading.Lock()
_runtime_lock = threading.Lock()
_scheduler_started = False
_eager_analysis_started = False
_browser_opened = False
_runtime_logs = deque(maxlen=200)
_acquisition_config: Any = None  # legacy holder; PDF pipeline removed in M1 F12
_acquisition_config_error: Optional[str] = None
_runtime_state: Dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "message": "Idle",
    "trigger": None,
    "started_at": None,
    "completed_at": None,
    "stats": {},
}


def _db_session():
    return SessionLocal()


def _get_acquisition_config():  # legacy stub; PDF pipeline removed in M1 F12
    raise HTTPException(status_code=410, detail=_ACQUIRE_GONE_BODY)


def _get_state(db_session, key: str, default: Optional[str] = None) -> Optional[str]:
    state = db_session.get(AppState, key)
    return state.value if state else default


def _set_state(db_session, key: str, value: str) -> None:
    state = db_session.get(AppState, key)
    if state is None:
        state = AppState(key=key, value=value)
        db_session.add(state)
    else:
        state.value = value
    db_session.commit()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _runtime_snapshot() -> Dict[str, Any]:
    with _runtime_lock:
        return {
            "running": _runtime_state["running"],
            "stage": _runtime_state["stage"],
            "message": _runtime_state["message"],
            "trigger": _runtime_state["trigger"],
            "started_at": _runtime_state["started_at"],
            "completed_at": _runtime_state["completed_at"],
            "stats": dict(_runtime_state.get("stats", {})),
            "logs": list(_runtime_logs),
        }


def _print_runtime_line(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def _push_runtime_log(message: str, *, stage: Optional[str] = None, **stats: Any) -> None:
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "stage": stage or _runtime_state.get("stage") or "idle",
        "message": message,
    }
    with _runtime_lock:
        if stage:
            _runtime_state["stage"] = stage
        _runtime_state["message"] = message
        if stats:
            merged = dict(_runtime_state.get("stats", {}))
            merged.update({key: value for key, value in stats.items() if value is not None})
            _runtime_state["stats"] = merged
            entry["stats"] = {key: value for key, value in stats.items() if value is not None}
        _runtime_logs.append(entry)
    _print_runtime_line(message)


def _begin_runtime(trigger: str) -> None:
    started_at = datetime.utcnow().isoformat()
    label = trigger.replace("_", " ")
    with _runtime_lock:
        _runtime_logs.clear()
        _runtime_state.update(
            {
                "running": True,
                "stage": "starting",
                "message": f"Starting {label}",
                "trigger": trigger,
                "started_at": started_at,
                "completed_at": None,
                "stats": {},
            }
        )
    _push_runtime_log(f"Starting {label}; trigger={trigger}", stage="starting")


def _finish_runtime(message: str, *, stage: str, **stats: Any) -> None:
    completed_at = datetime.utcnow().isoformat()
    with _runtime_lock:
        _runtime_state["running"] = False
        _runtime_state["stage"] = stage
        _runtime_state["message"] = message
        _runtime_state["completed_at"] = completed_at
        merged = dict(_runtime_state.get("stats", {}))
        merged.update({key: value for key, value in stats.items() if value is not None})
        _runtime_state["stats"] = merged
        _runtime_logs.append(
            {
                "timestamp": completed_at,
                "stage": stage,
                "message": message,
                "stats": {key: value for key, value in stats.items() if value is not None},
            }
        )
    _print_runtime_line(message)


def _paper_figure_insights(paper: Paper, payload: Dict[str, Any], *, include_fallback: bool) -> List[Dict[str, Any]]:
    stored = payload.get("figure_analyses") if isinstance(payload, dict) else None
    if isinstance(stored, list) and stored:
        # FN-B4-A01-1 (VERIFY-M, 2026-05-19): apply read-time normalization
        # so payloads written before FIX-ANALYZE landed still expose the
        # canonical `caption_summary` / `key_finding` aliases at the API
        # surface. The normalizer is idempotent and side-effect-free; if it
        # raises we fall back to the raw stored list rather than 500-ing.
        try:
            return _normalize_figure_analyses(stored)
        except Exception:
            return stored

    if include_fallback and paper.pdf_path:
        return extract_figure_insights_from_pdf(paper.pdf_path)

    return []


def _pdf_analysis_state(paper: Paper, analysis: Any) -> str:
    """2026-11-12 — why the detail panel's PDF badge says what it says.

    The badge used to compare the LIVE ``has_pdf`` with the STORED
    ``pdf_used``, so every paper whose PDF the harvester attached after its
    analysis showed a bare "AI analysis did not use PDF" (harvest_pdfs.py never
    re-analyses). This returns one of:

    ``no_pdf``             no PDF attached
    ``not_analyzed``       PDF attached, no AI analysis yet
    ``used``               the stored analysis read the PDF
    ``licence_restricted`` the TDM guard keeps this PDF away from hosted AI,
                           so a Reanalyze would not use it either
    ``added_after``        the analysis was written before the PDF was attached
                           (its payload recorded has_pdf=False); Reanalyze uses it
    ``not_used``           anything else (e.g. the PDF was unreadable back then)

    Detail view only: the licence check opens the PDF's first two pages.
    """
    if not paper.pdf_path:
        return "no_pdf"
    if analysis is None:
        return "not_analyzed"
    if analysis.pdf_used:
        return "used"
    try:
        from services.pdf_page_cache import FITZ_LOCK
        from services.tdm_guard_and_xml_renderer import is_tdm_restricted_paper
        with FITZ_LOCK:
            if is_tdm_restricted_paper(paper, pdf_path=paper.pdf_path)[0]:
                return "licence_restricted"
    except Exception:
        pass  # a guard error must never break the detail view
    payload = analysis.payload if isinstance(analysis.payload, dict) else {}
    had_pdf = payload.get("has_pdf")
    if had_pdf is False:
        return "added_after"
    if had_pdf is None:
        try:
            attached = paper.retrieval_succeeded_at
            analysed = analysis.updated_at
            if attached and analysed and analysed.replace(tzinfo=None) < attached.replace(tzinfo=None):
                return "added_after"
        except Exception:
            pass
    return "not_used"


def _format_author_initial(name: str) -> str:
    """`Wang, Xiaoming` / `Xiaoming Wang` / `王小明` → `Wang X.` / `王小明`.

    Keeps CJK names intact (no initial-collapsing). For Latin names we
    keep the family name in full and abbreviate every given name to a
    single initial — this is the convention seismology papers use.
    """
    raw = (name or "").strip()
    if not raw:
        return "?"
    # CJK passthrough
    if re.search(r"[一-鿿぀-ヿ]", raw):
        return raw
    # "Last, First" form
    if "," in raw:
        family, _, given = raw.partition(",")
        family = family.strip()
        given_parts = [p.strip() for p in given.split() if p.strip()]
        initials = " ".join(p[0].upper() + "." for p in given_parts if p)
        return f"{family} {initials}".strip() if initials else family
    # "First Last" form
    tokens = raw.split()
    if len(tokens) == 1:
        return tokens[0]
    family = tokens[-1]
    initials = " ".join(p[0].upper() + "." for p in tokens[:-1] if p)
    return f"{family} {initials}".strip() if initials else family


def _build_nav_label(authors: List[str], journal: str) -> str:
    """Format the left-nav entry text for one paper.

    Multi-author rule (DEEPER spec from D11.D / P9):
      1 author     → "Wang Y."
      2 authors    → "Wang Y. & Smith J."
      3+ authors   → "Wang Y. et al."
    Then append " · {journal_abbrev}".
    """
    import re  # local — `re` is already imported at top, this is defensive

    names = [a for a in (authors or []) if a]
    abbrev = journal_abbreviate(journal or "Others")
    if not names:
        return f"Unknown · {abbrev}"
    if len(names) == 1:
        return f"{_format_author_initial(names[0])} · {abbrev}"
    if len(names) == 2:
        return f"{_format_author_initial(names[0])} & {_format_author_initial(names[1])} · {abbrev}"
    return f"{_format_author_initial(names[0])} et al. · {abbrev}"


# `re` was already imported at the top of main.py via fetch_service / pdf_naming
# chains but we ensure it for `_format_author_initial` (defensive — keeps the
# function self-contained).
import re  # noqa: E402  -- intentionally placed after the helpers above


def _safe_json_load(raw: Optional[str]) -> Dict[str, Any]:
    """Best-effort JSON-string → dict. Returns {} for null/empty/malformed."""
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _paper_to_dict(paper: Paper, *, include_detail: bool = False, project_folder: Optional[str] = None) -> Dict[str, Any]:
    analysis = paper.ai_analysis
    payload = analysis.payload if analysis else {}
    # FN-B4-A01-1 (VERIFY-M, 2026-05-19): apply read-time normalization to
    # `figure_analyses` so payloads written before FIX-ANALYZE landed still
    # expose the canonical `caption_summary` / `key_finding` aliases at the
    # API surface. Shallow-copy first so we never mutate the SQLAlchemy
    # session-attached dict (would dirty the row).
    if isinstance(payload, dict) and isinstance(payload.get("figure_analyses"), list):
        try:
            normalized_figs = _normalize_figure_analyses(payload["figure_analyses"])
            payload = {**payload, "figure_analyses": normalized_figs}
        except Exception:
            pass  # never let normalize errors break the response
    pdf_url = None
    if paper.pdf_path:
        pdf_name = Path(paper.pdf_path).name
        # Add file-mtime query param so browser re-fetches after local annotation edits.
        try:
            mtime = int(Path(paper.pdf_path).stat().st_mtime)
        except OSError:
            mtime = 0
        pdf_url = f"/downloads/{quote(pdf_name)}?v={mtime}"

    authors = paper.authors or []
    if not isinstance(authors, list):
        authors = [str(authors)]

    topics = paper.topics or ["Others"]
    if not isinstance(topics, list):
        topics = [str(topics)]

    institutions_raw = paper.institutions or []
    if not isinstance(institutions_raw, list):
        institutions_raw = [str(institutions_raw)]
    institutions = normalize_institutions(institutions_raw)
    institutions = [
        value for value in institutions
        if value and value != "Others" and value.lower() not in KNOWN_PUBLISHERS
    ]

    # Wave-9 — UserPaperState surface (T8 era; PR-93 UI). `user_state` is
    # a 1:1 backref so missing row (the common case for legacy papers)
    # means all three flags are off.
    ups = getattr(paper, "user_state", None)
    is_read       = bool(ups and ups.read_at)
    is_archived   = bool(ups and ups.archived_at)
    is_dismissed  = bool(ups and ups.dismissed_at)
    is_liked      = bool(ups and ups.liked_at)
    read_at_iso       = ups.read_at.isoformat()      if ups and ups.read_at      else None
    archived_at_iso   = ups.archived_at.isoformat()  if ups and ups.archived_at  else None
    dismissed_at_iso  = ups.dismissed_at.isoformat() if ups and ups.dismissed_at else None
    liked_at_iso      = ups.liked_at.isoformat()     if ups and ups.liked_at     else None

    return {
        "id": paper.id,
        "source": paper.source,
        "title": paper.title,
        "journal": paper.journal or "Others",
        "topics": topics or ["Others"],
        "institutions": institutions,
        "authors": authors,
        "author_line": ", ".join(authors) if authors else "Unknown authors",
        "published_date": paper.published_date or "",
        "datetime_utc": paper.datetime_utc or "",
        "link": paper.link or "",
        "doi": paper.doi or "",
        "abstract": paper.abstract or "",
        "abstract_flag": paper.abstract_flag or "missing",
        "has_pdf": bool(paper.pdf_path),
        "pdf_url": pdf_url,
        "pdf_sha256": paper.pdf_sha256,
        "last_opened_at": paper.last_opened_at.isoformat() if paper.last_opened_at else None,
        # Roadmap V2 reading-state fields (items 1, 41, 16-prime).
        "reading_intent": paper.reading_intent or "",
        "reading_intent_at": paper.reading_intent_at.isoformat() if paper.reading_intent_at else None,
        "user_takeaway_what": paper.user_takeaway_what or "",
        "user_takeaway_why": paper.user_takeaway_why or "",
        "user_takeaway_at": paper.user_takeaway_at.isoformat() if paper.user_takeaway_at else None,
        "has_takeaway": bool(paper.user_takeaway_what and paper.user_takeaway_why),
        "follow_up": bool(paper.follow_up),
        "follow_up_at": paper.follow_up_at.isoformat() if paper.follow_up_at else None,
        # 2026-05-19 — Structured 7-question reading notes (七问读书法).
        # Stored as JSON text in `papers.reading_notes`; parsed back to dict.
        "reading_notes": _safe_json_load(paper.reading_notes),
        "reading_notes_at": (
            paper.reading_notes_at.isoformat() if paper.reading_notes_at else None
        ),
        # First author + journal abbreviation, formatted for the left nav.
        # Multi-author rule: 1 → "Wang Y.", 2 → "Wang Y. & Smith J.", 3+ → "Wang Y. et al."
        "nav_label": _build_nav_label(authors, paper.journal or "Others"),
        "journal_abbrev": journal_abbreviate(paper.journal or "Others"),
        "journal_color": journal_color(paper.journal or "Others"),
        "first_author": authors[0] if authors else "Unknown",
        "relevance_score": analysis.relevance_score if analysis else 0,
        "pdf_used": analysis.pdf_used if analysis else False,
        "analysis_summary": (payload.get("summary_150w_en") if payload else "") or "",
        "analysis_summary_zh": (payload.get("summary_150w_zh") if payload else "") or "",
        "analysis_takeaway": (payload.get("one_sentence_takeaway_en") if payload else "") or "",
        "analysis_takeaway_zh": (payload.get("one_sentence_takeaway_zh") if payload else "") or "",
        # G4: structured 5-field analysis (empty dict → no G4 data yet for legacy papers)
        "structured_analysis": (payload.get("structured_analysis") or {}) if payload else {},
        "structured_analysis_zh": (payload.get("structured_analysis_zh") or {}) if payload else {},
        # Keywords: prefer the AI-analysis keywords; fall back to the cheap
        # fetch-time OpenAlex keywords on the paper so every fetched paper shows
        # keywords even before (or without) a deep Gemini analysis.
        "keywords": (
            (analysis.keywords if analysis else None)
            or (paper.keywords or [])
            or (analysis.recommended_tags if analysis else None)
            or []
        ),
        "used_sources": analysis.used_sources if analysis else [],
        "cos_topic_match": analysis.topic_matches if analysis else [],
        "facets": (analysis.facets if analysis else None) or {},
        "evidence": {
            "based_on": analysis.evidence_based_on if analysis else "",
            "notes": analysis.evidence_notes if analysis else "",
        },
        "figure_insights": _paper_figure_insights(paper, payload, include_fallback=include_detail),
        # The full raw Gemini payload is detail-only: the only consumer is the
        # detail modal (openPaperDetail), which re-fetches GET /api/papers/{id}
        # (include_detail=True). List/Browse/Inbox cards read the flattened
        # top-level fields (analysis_summary, structured_analysis,
        # figure_insights, cos_topic_match, …), never analysis_payload — so
        # shipping it on every card was redundant weight. Gate it on detail.
        **({"analysis_payload": payload} if include_detail else {}),
        # 2026-11-12: detail-only reason behind the PDF badge (see _pdf_analysis_state).
        **({"pdf_analysis_state": _pdf_analysis_state(paper, analysis)} if include_detail else {}),
        "retrieval": {
            "status": paper.retrieval_status or "not_attempted",
            "source": paper.retrieval_source,
            "source_url": paper.retrieval_source_url,
            "last_stage": paper.retrieval_last_stage,
            "error_reason": paper.retrieval_error_reason,
            "attempts_count": paper.retrieval_attempts_count or 0,
            "attempted_at": paper.retrieval_attempted_at.isoformat() if paper.retrieval_attempted_at else None,
            "succeeded_at": paper.retrieval_succeeded_at.isoformat() if paper.retrieval_succeeded_at else None,
            "next_retry_at": paper.retrieval_next_retry_at.isoformat() if paper.retrieval_next_retry_at else None,
        },
        "updated_at": paper.updated_at.isoformat() if paper.updated_at else "",
        # Project workspace metadata (None when called outside a project scope)
        "project_folder": project_folder,
        # Wave-9 — UserPaperState surface (read / archive / dismiss UI).
        "is_read": is_read,
        "is_archived": is_archived,
        "is_dismissed": is_dismissed,
        "is_liked": is_liked,
        "read_at": read_at_iso,
        "archived_at": archived_at_iso,
        "dismissed_at": dismissed_at_iso,
        "liked_at": liked_at_iso,
    }


def _ris_escape(value: str) -> str:
    value = (value or "").replace("\r", " ").replace("\n", " ")
    return " ".join(value.split()).strip()


def _build_ris_text(papers: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for paper in papers:
        lines.append("TY  - JOUR")
        lines.append(f"TI  - {_ris_escape(paper.get('title', ''))}")
        lines.append(f"JO  - {_ris_escape(paper.get('journal', ''))}")

        date_value = (paper.get("published_date") or "").strip()
        ris_date = ""
        if len(date_value) == 10:
            ris_date = date_value.replace("-", "/")
        elif len(date_value) == 7:
            ris_date = f"{date_value}-01".replace("-", "/")
        elif len(date_value) == 4:
            ris_date = f"{date_value}-01-01".replace("-", "/")
        if ris_date:
            lines.append(f"DA  - {ris_date}")

        if paper.get("link"):
            lines.append(f"UR  - {_ris_escape(paper['link'])}")
        if paper.get("doi"):
            lines.append(f"DO  - {_ris_escape(paper['doi'])}")
        if paper.get("abstract"):
            lines.append(f"AB  - {_ris_escape(paper['abstract'])}")

        for author in paper.get("authors", []) or []:
            author_value = _ris_escape(str(author))
            if author_value:
                lines.append(f"AU  - {author_value}")

        for topic in paper.get("topics", []) or []:
            topic_value = _ris_escape(str(topic))
            if topic_value:
                lines.append(f"KW  - {topic_value}")

        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)


def _run_refresh_pipeline(
    trigger: str = "manual",
    *,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> Dict[str, Any]:
    if not _fetch_lock.acquire(blocking=False):
        snapshot = _runtime_snapshot()
        return {
            "status": "busy",
            "message": "Refresh already running.",
            "runtime": snapshot,
        }

    db_session = _db_session()
    started_at = datetime.utcnow().isoformat()
    try:
        _begin_runtime(trigger)
        _set_state(db_session, "last_fetch_started_at", started_at)
        _set_state(db_session, "last_fetch_trigger", trigger)

        fetch_result = fetch_and_store_new_papers(
            db_session,
            date_from=date_from,
            date_to=date_to,
            progress_callback=_push_runtime_log,
        )

        # H1 fix (2026-06-30) — retry papers an earlier intake left vector-less.
        # fetch_and_store_new_papers only embeds the freshly-touched set, so a
        # transient embedding failure (quota/network/key blip) left a paper at
        # embedding=NULL forever => invisible to every semantic tracker, with no
        # backfill anywhere in the codebase. Re-embed a bounded batch of
        # unembedded/stale-model papers BEFORE the match stage so recovered
        # vectors can immediately participate in matching. Best-effort.
        try:
            fetch_result["embed_backfill"] = backfill_unembedded_papers(
                db_session, progress_callback=_push_runtime_log
            )
        except Exception as exc:  # never fail the refresh on a backfill error
            _push_runtime_log(f"Embedding backfill raised: {exc}", stage="embed_backfill_error")

        analysis_result = {"analyzed": 0}
        analysis_warning = None

        # BRIEF v2 §3/§6 — LAZY ingest. fetch_and_store_new_papers already
        # computed the cheap title+abstract embedding at intake. Here we (1) match
        # the not-yet-analysed papers against every tracker (writes
        # ProjectPaper.match_score via the semantic + deterministic predicates and
        # enqueues each match), then (2) drain the lazy queue so the expensive
        # Gemini Pro analysis runs ONLY for tracker-matched papers — NOT for the
        # whole intake (the retired eager analyze_pending_papers model).
        try:
            from models import AIAnalysis

            pending = (
                db_session.query(Paper)
                .outerjoin(AIAnalysis)
                .filter(AIAnalysis.id.is_(None))
                .order_by(Paper.datetime_utc.desc())
                .all()
            )
            match_stats = match_new_papers_to_trackers(
                pending, db_session, progress_callback=_push_runtime_log
            )
            drain_stats = drain_analysis_queue(
                db_session, progress_callback=_push_runtime_log
            )
            analysis_result = {
                "analyzed": drain_stats.get("analyzed", 0),
                "skipped": drain_stats.get("skipped", 0),
                "failed": drain_stats.get("failed", 0),
                "matched": match_stats.get("papers_matched", 0),
                "links_created": match_stats.get("links_created", 0),
            }
        except Exception as exc:
            analysis_warning = str(exc)
            _push_runtime_log(f"Analysis stage raised an exception: {analysis_warning}", stage="analysis_error")

        completed_at = datetime.utcnow().isoformat()
        _set_state(db_session, "last_fetch_completed_at", completed_at)
        _set_state(db_session, "last_fetch_status", "ok")
        if analysis_warning:
            _set_state(db_session, "last_fetch_analysis_warning", analysis_warning)
        else:
            _set_state(db_session, "last_fetch_analysis_warning", "")

        _finish_runtime(
            "Refresh completed",
            stage="done",
            fetched=fetch_result.get("fetched", 0),
            created=fetch_result.get("created", 0),
            updated=fetch_result.get("updated", 0),
            analyzed=analysis_result.get("analyzed", 0),
        )

        return {
            "status": "ok",
            "trigger": trigger,
            "fetch": fetch_result,
            "analysis": analysis_result,
            "warning": analysis_warning,
            "completed_at": completed_at,
            "runtime": _runtime_snapshot(),
        }
    except Exception as exc:
        _set_state(db_session, "last_fetch_status", f"error: {exc}")
        _finish_runtime(f"Refresh failed: {exc}", stage="error")
        raise
    finally:
        db_session.close()
        _fetch_lock.release()


def _parse_iso_date(value: Optional[str], *, field_name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}. Expected YYYY-MM-DD.") from exc


# 04-feature-design/08 §1 — single source-of-truth whitelist.  Keep
# this set in sync with the sidebar dropdown in templates/index.html
# (PR-35) and the `_sort_papers()` dispatch table (PR-33); the helper
# `_mode_default()` and the resolution order below are the only places
# in main.py that should literal-string-compare against these values.
SORT_KEYS = frozenset({
    "relevance",             # default for tracker mode
    "published_date",        # legacy date-DESC mode
    "author",                # PR-34: first-author family name A-Z (pinyin for CJK)
    "title_initial",         # PR-34: first non-article word A-Z
    "journal",               # PR-34: canonical journal name A-Z
    "year",                  # PR-33: publication year DESC
    "added_to_project_at",   # PR-33: ProjectPaper.created_at DESC
    "last_opened",           # PR-33: Paper.last_opened_at DESC
    "custom",                # PR-33+36: ProjectPaper.position ASC, collection-only
})


def _mode_default(project: Optional["Project"]) -> str:
    """Mode-default sort: collection → author; everything else → published_date (newest-first).

    2026-06-15: tracker default changed from "relevance" → "published_date". The
    LLM relevance_score is under-specified (returns ~0-5) and covers only the few
    AI-analysed papers, so date-DESC is the honest default. Relevance stays a
    selectable sort option.
    """
    if project is not None and getattr(project, "kind", None) == "collection":
        return "author"
    return "published_date"


def _normalize_sort_by(
    sort_by: Optional[str],
    project: Optional["Project"] = None,
) -> str:
    """Resolve a user-supplied sort key to a value in SORT_KEYS.

    Resolution order (§6 of the design doc):
      1. Explicit `sort_by` if it's in the whitelist (custom requires a
         collection project — falls back to mode-default otherwise).
      2. `project.default_sort_by` if set and in the whitelist.
      3. Mode-default per `_mode_default()`.
    """
    value = (sort_by or "").strip().lower()
    if value in SORT_KEYS:
        if value == "custom" and (project is None or getattr(project, "kind", None) != "collection"):
            # Reject silently — the UI gates the dropdown option, but a stale
            # bookmark or curl call shouldn't 400. Fall back per design §7.
            return _mode_default(project)
        return value
    if project is not None and getattr(project, "default_sort_by", None) in SORT_KEYS:
        return project.default_sort_by
    return _mode_default(project)


def _sort_papers(
    papers: List[Dict[str, Any]],
    sort_by: Optional[str],
    *,
    project: Optional[Project] = None,
    project_paper_index: Optional[Dict[str, ProjectPaper]] = None,
) -> List[Dict[str, Any]]:
    """Sort *papers* by *sort_by* using the SORT_BUILDERS dispatch dict.

    Each builder returns a tuple whose lexicographic order encodes the full
    tie-break chain from design §1. There is no `reverse=True`; DESC ordering
    is achieved by `_neg_*` helpers (PR-34) that negate ints / invert ISO date
    strings so a single ascending `sorted()` call handles every key.
    """
    key = _normalize_sort_by(sort_by, project)

    def _rel_sort_key(p: Dict[str, Any]):
        # 相关度 MVP: scored papers (per-tracker context_relevance present) rank
        # ABOVE unscored ones; within each group higher score first. The legacy
        # 0–100 relevance_score (÷100) is only the unscored / Browse fallback, so
        # a stale LLM score can't outrank a real per-tracker cosine inside a
        # tracker view. Returns (group, -score): group 0 = scored, 1 = unscored.
        cr = p.get("context_relevance")
        if cr is not None:
            try:
                return (0, -float(cr))
            except (TypeError, ValueError):
                pass
        return (1, -(int(p.get("relevance_score") or 0) / 100.0))

    # Dispatch dict — one row per SORT_KEYS entry. Tuples encode the tie-break
    # chain from design §1 verbatim (left = primary, right = final id stabiliser).
    # NOTE: `p["id"]` is the universal final tie-break (the IntersectionObserver
    # at templates/index.html:160 relies on stable order across refreshes).
    SORT_BUILDERS: Dict[str, Any] = {
        "relevance":           lambda p: (_rel_sort_key(p), _neg_date(p), _neg_updated(p), p["id"]),
        "published_date":      lambda p: (_neg_date(p), -int(p.get("relevance_score") or 0), _neg_updated(p), p["id"]),
        "author":              lambda p: (author_sort_key(p.get("authors") or []), _neg_date(p), title_initial_key(p.get("title") or ""), p["id"]),
        "title_initial":       lambda p: (title_initial_key(p.get("title") or ""), author_sort_key(p.get("authors") or []), _neg_date(p), p["id"]),
        "journal":             lambda p: (journal_sort_key(p.get("journal") or ""), _neg_date(p), author_sort_key(p.get("authors") or []), p["id"]),
        "year":                lambda p: (_neg_year(p), -int(p.get("relevance_score") or 0), author_sort_key(p.get("authors") or []), p["id"]),
        "added_to_project_at": lambda p: (_neg_added(p, project_paper_index), _neg_date(p), p["id"]),
        "last_opened":         lambda p: (_neg_last_opened(p), _neg_date(p), p["id"]),
        "custom":              lambda p: (_position(p, project_paper_index), _neg_added(p, project_paper_index), p["id"]),
    }
    builder = SORT_BUILDERS.get(key) or SORT_BUILDERS["relevance"]
    return sorted(papers, key=builder)


def _as_filter_list(value: Optional[Any]) -> List[str]:
    """Normalize a facet filter param to a list of non-empty strings.

    Accepts the legacy single string, a list (multi-select, 2026-07-06), or
    None/"" (no filter). Values within one category are OR-ed by the callers;
    categories are AND-ed. Blank entries are dropped so an empty selection
    means "don't filter this category".
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v) for v in value if v is not None and str(v).strip()]


def _matches_filters(
    paper: Dict[str, Any],
    *,
    topic: Optional[Any] = None,
    journal: Optional[Any] = None,
    institution: Optional[Any] = None,
    has_pdf: Optional[bool] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> bool:
    # Multi-select (2026-07-06): each category OR-s its selected values; the
    # categories still AND together. An empty list means the category is off.
    topics = _as_filter_list(topic)
    journals = _as_filter_list(journal)
    institutions = _as_filter_list(institution)
    if topics and not any(t in paper["topics"] for t in topics):
        return False
    if journals and paper["journal"] not in journals:
        return False
    if institutions:
        paper_institutions = paper.get("institutions", []) or []
        if not any(i in paper_institutions for i in institutions):
            return False
    if has_pdf is not None and paper["has_pdf"] != has_pdf:
        return False

    published_date = paper.get("published_date") or ""
    if (date_from or date_to) and not published_date:
        return False
    if date_from and published_date and published_date < date_from:
        return False
    if date_to and published_date and published_date > date_to:
        return False
    return True


def _matches_q(paper: Dict[str, Any], q_lower: str) -> bool:
    """Return True if *q_lower* appears in any searchable text field of *paper*.

    Searches: title, author_line, abstract, journal, DOI, keywords.
    ``q_lower`` must already be lowercased by the caller.
    """
    haystack_parts = [
        paper.get("title") or "",
        paper.get("author_line") or "",
        paper.get("abstract") or "",
        paper.get("journal") or "",
        paper.get("doi") or "",
    ]
    # Flatten keyword list
    for kw in (paper.get("keywords") or []):
        haystack_parts.append(str(kw))
    haystack = " ".join(haystack_parts).lower()
    return q_lower in haystack


def _count_multivalue(papers: List[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for paper in papers:
        seen = set()
        for value in paper.get(field, []) or []:
            if value and value not in seen:
                counts[value] = counts.get(value, 0) + 1
                seen.add(value)
    return [{"value": key, "count": counts[key]} for key in sorted(counts)]


def _count_scalar(papers: List[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for paper in papers:
        value = paper.get(field)
        if value:
            counts[value] = counts.get(value, 0) + 1
    return [{"value": key, "count": counts[key]} for key in sorted(counts)]


def get_filtered_papers(
    db_session,
    *,
    topic: Optional[Any] = None,
    journal: Optional[Any] = None,
    institution: Optional[Any] = None,
    has_pdf: Optional[bool] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    include_detail: bool = False,
    project_id: Optional[str] = None,
    folder: Optional[str] = None,
    q: Optional[str] = None,
    scope_kind: Optional[str] = None,
    unmatched: bool = False,
) -> Dict[str, Any]:
    """Shared filter pipeline reused by `/api/papers`, `/api/export/*` and `/api/health`.

    When `project_id` is provided the paper set is first scoped to members of
    that project.  An additional `folder` param further narrows to one folder
    (e.g. "2024-11" in a monthly project).

    Returns a dict with:
      - `serialized`: every paper in scope (used for faceted count rollups)
      - `filtered`: the subset that matched all active filters, sorted
      - `sort_by`: the normalized sort mode
    """
    # --- project scoping ------------------------------------------------
    if project_id:
        # Build the set of paper_ids that belong to this project/folder.
        pp_q = db_session.query(ProjectPaper).filter(
            ProjectPaper.project_id == project_id
        )
        if folder:
            pp_q = pp_q.filter(ProjectPaper.folder == folder)
        # PR-33: also build the {paper_id → ProjectPaper} index used by the
        # custom / added_to_project_at / last_opened builders. PR-32 owns the
        # `position` and `created_at` columns the index reads; this PR only
        # consumes them. Single .all() pass — no extra query.
        pp_rows = pp_q.all()
        member_map: Dict[str, str] = {pp.paper_id: pp.folder for pp in pp_rows}
        project_paper_index: Optional[Dict[str, ProjectPaper]] = {pp.paper_id: pp for pp in pp_rows}
        project_obj: Optional[Project] = db_session.query(Project).filter(Project.id == project_id).first()
        if not member_map:
            return {
                "serialized": [],
                "filtered": [],
                "sort_by": _normalize_sort_by(sort_by, project_obj),
            }
        papers = (
            db_session.query(Paper)
            .options(selectinload(Paper.ai_analysis), selectinload(Paper.user_state))
            .filter(Paper.id.in_(list(member_map.keys())))
            .order_by(Paper.datetime_utc.desc(), Paper.updated_at.desc())
            .all()
        )
        serialized = [
            _paper_to_dict(paper, include_detail=include_detail, project_folder=member_map.get(paper.id))
            for paper in papers
        ]
        # 相关度 MVP (2026-06-16): per-tracker context_relevance, computed on
        # demand here (no DB column / migration). Trackers only — collections
        # are manual curation. Semantic trackers reuse match_score; keyword/
        # journal/author trackers score each paper by cosine to the member
        # centroid. Papers with no usable signal stay unscored (None) → they
        # fall back to date ordering. dismiss is NOT a signal (hide-only).
        if project_obj is not None and getattr(project_obj, "kind", None) == "tracker":
            try:
                from services.relevance_service import score_tracker_context_relevance
                _cr = score_tracker_context_relevance(papers, project_obj, project_paper_index)
                for _d in serialized:
                    _v = _cr.get(_d["id"])
                    if _v is not None:
                        _d["context_relevance"] = float(_v)
            except Exception as _rel_exc:
                _logger.warning(
                    "context_relevance injection skipped for project %s: %s",
                    getattr(project_obj, "id", "?"), _rel_exc,
                )
    else:
        # Browse mode: "All papers" = the user's LIBRARY = the union of papers
        # in ANY tracker/collection. The broad-fetched candidate pool (papers in
        # no project) stays backstage as the semantic-matching pond and is NOT
        # surfaced here (user decision 2026-06-03).
        # SEARCH mode (q given) deliberately queries the WHOLE pool so the user
        # can discover — and then collect — papers not yet in any project.
        base_q = db_session.query(Paper)
        if unmatched:
            # Orphan candidate pool (2026-06-15): papers in NO project — the
            # broad-fetched pond that matched no tracker/collection. Surfaced via
            # the Browse "unmatched candidates" toggle so the ~37% that was
            # backstage is now visible/controllable. Ignores scope_kind.
            member_all = db_session.query(ProjectPaper.paper_id).distinct()
            base_q = base_q.filter(~Paper.id.in_(member_all))
        elif not (q or "").strip():
            # Trackers and Collections are independent: "All Trackers" /
            # "All Collections" scope this unscoped view to papers in a project
            # of that kind. Without scope_kind it's the union of ANY project
            # (legacy "All papers").
            if scope_kind in ("tracker", "collection"):
                member_q = (
                    db_session.query(ProjectPaper.paper_id)
                    .join(Project, Project.id == ProjectPaper.project_id)
                    .filter(Project.kind == scope_kind)
                    .distinct()
                )
            else:
                member_q = db_session.query(ProjectPaper.paper_id).distinct()
            base_q = base_q.filter(Paper.id.in_(member_q))
        # M7 fix (2026-06-30) — defer the ~8KB-per-row `embedding` TEXT column on
        # the Browse path. Browse loads the WHOLE library (union of all projects,
        # unbounded, no pagination) and serialises it every request, but the list
        # serializer never reads `embedding` and this branch never computes
        # context_relevance — so eagerly pulling every row's 768-float JSON was
        # pure waste (~2.3MB/req at 286 rows, grows linearly). defer() loads it
        # only if some attribute access asks for it, which nothing on this path
        # does. (The tracker/project path above does NOT defer — its
        # context_relevance reads .embedding, so deferring there would N+1.)
        papers = base_q.options(
            selectinload(Paper.ai_analysis),
            selectinload(Paper.user_state),
            defer(Paper.embedding),
        ).order_by(
            Paper.datetime_utc.desc(), Paper.updated_at.desc()
        ).all()
        serialized = [_paper_to_dict(paper, include_detail=include_detail) for paper in papers]
        project_paper_index = None
        project_obj = None
    # --- content filters ------------------------------------------------
    filtered = [
        paper
        for paper in serialized
        if _matches_filters(
            paper,
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
        )
    ]
    # Keyword search (applied after facet filters so facet counts stay accurate)
    q_stripped = (q or "").strip()
    if q_stripped:
        q_lower = q_stripped.lower()
        filtered = [p for p in filtered if _matches_q(p, q_lower)]

    filtered = _sort_papers(filtered, sort_by, project=project_obj, project_paper_index=project_paper_index)
    return {
        "serialized": serialized,
        "filtered": filtered,
        "sort_by": _normalize_sort_by(sort_by, project_obj),
        "q": q_stripped,
    }


def _build_filter_payload(
    papers: List[Dict[str, Any]],
    *,
    topic: Optional[Any],
    journal: Optional[Any],
    institution: Optional[Any],
    has_pdf: Optional[bool],
    date_from: Optional[str],
    date_to: Optional[str],
) -> Dict[str, Any]:
    topic_base = [paper for paper in papers if _matches_filters(paper, journal=journal, institution=institution, has_pdf=has_pdf, date_from=date_from, date_to=date_to)]
    journal_base = [paper for paper in papers if _matches_filters(paper, topic=topic, institution=institution, has_pdf=has_pdf, date_from=date_from, date_to=date_to)]
    institution_base = [paper for paper in papers if _matches_filters(paper, topic=topic, journal=journal, has_pdf=has_pdf, date_from=date_from, date_to=date_to)]
    pdf_base = [paper for paper in papers if _matches_filters(paper, topic=topic, journal=journal, institution=institution, date_from=date_from, date_to=date_to)]

    # 2026-06-13: lock the topic filter to the canonical vocabulary (+ the
    # "Others" uncategorized bucket) so legacy / drifted labels never pollute the
    # dropdown, even between rename migrations. Local import — cos_topics is
    # dependency-free (no google-genai).
    from services.cos_topics import COS_TOPICS as _CANON_TOPICS
    _allowed_topics = set(_CANON_TOPICS) | {"Others"}
    topics_facet = [t for t in _count_multivalue(topic_base, "topics") if t.get("value") in _allowed_topics]

    return {
        "topics": topics_facet,
        "journals": _count_scalar(journal_base, "journal"),
        "institutions": _count_multivalue(institution_base, "institutions"),
        "pdf": {
            "true": sum(1 for paper in pdf_base if paper["has_pdf"]),
            "false": sum(1 for paper in pdf_base if not paper["has_pdf"]),
        },
    }


def _add_paper_to_default_project(
    db_session,
    paper: Paper,
    *,
    source: str = "upload",
    project_id: Optional[str] = None,
) -> None:
    """Add `paper` to the project named by ``project_id``.

    2026-05-25 behaviour change (user-driven): when ``project_id`` is None
    this is now a NO-OP. Previously it fell back to the seeded Monthly
    Literature Review (``is_default=True``), turning MLR into a
    "junk drawer" that grew unbounded with every upload + analysis.
    Per user directive — MLR is now just a normal tracker that the user
    must explicitly target if they want a paper to land in it.

    The function name is kept for back-compat; the *callers* are
    responsible for passing a real ``project_id``. Callers that have
    nothing to pass (orphaned uploads, watch-folder ingest without a
    configured target) simply skip this call now and the paper lives in
    the "All papers" view only.

    Folder derivation when ``project_id`` IS given:
      - monthly / topic_monitor → ``YYYY-MM`` derived from the paper's
        ``published_date`` (or ``updated_at`` / now as fallback)
      - static → fixed ``"imported"`` bucket (matches existing static-project
        import behaviour)

    Safe to call repeatedly — silently skips when the row already exists.
    """
    from sqlalchemy.exc import IntegrityError

    if not project_id:
        return  # 2026-05-25: no implicit MLR fallback anymore.

    project = (
        db_session.query(Project)
        .filter(Project.id == project_id, Project.archived_at == None)  # noqa: E711
        .first()
    )
    if project is None:
        return  # Unknown / archived project id — paper stays unattached.

    # Already a member?
    existing = db_session.query(ProjectPaper).filter_by(
        project_id=project.id, paper_id=paper.id
    ).first()
    if existing:
        return

    project_type = (project.type or "monthly").strip().lower()
    if project_type == "static":
        folder = "imported"
    else:
        raw_date = paper.published_date or (
            paper.updated_at.strftime("%Y-%m-%d") if paper.updated_at else ""
        )
        folder = (
            str(raw_date)[:7]
            if raw_date and len(str(raw_date)) >= 7
            else datetime.utcnow().strftime("%Y-%m")
        )

    pp = ProjectPaper(
        project_id=project.id,
        paper_id=paper.id,
        folder=folder,
        source=source,
    )
    db_session.add(pp)
    try:
        db_session.flush()
    except IntegrityError:
        db_session.rollback()


def _ingest_pdf_bytes(
    *,
    path: Path,
    data: bytes,
    source: str = "watch_folder",
    doi_from_pdf: Optional[str] = None,
    doi_from_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Watch-folder ingest entry point.

    Steps:
      1. SHA-256 against existing rows → skip if already known.
      2. Match an existing Paper by DOI; if found, copy the PDF into the
         managed downloads/ folder and re-analyze.
      3. Otherwise, create a new Paper row with whatever metadata we can
         extract from the PDF + DOI, then run the analyze pipeline.

    Returns a status dict, see :func:`services.watch_folder.process_pdf_file`.
    """
    db_session = _db_session()
    try:
        new_sha256 = sha256_of_bytes(data)
        existing = db_session.query(Paper).filter(Paper.pdf_sha256 == new_sha256).first()
        if existing is not None:
            return {
                "status": "skipped_duplicate",
                "paper_id": existing.id,
                "reason": "pdf_sha256_match",
            }

        doi = (doi_from_pdf or doi_from_name or "").strip()
        clean_doi = doi
        if clean_doi.lower().startswith("arxiv:"):
            # arXiv ids aren't real DOIs — keep them as a `link` hint only.
            clean_doi = ""

        target_paper: Optional[Paper] = None
        if clean_doi:
            # H4 — DOIs are case-insensitive; match regardless of stored casing.
            target_paper = (
                db_session.query(Paper).filter(func.lower(Paper.doi) == clean_doi.lower()).first()
            )

        if target_paper is None:
            # Build a minimal Paper row. Title comes from PDF first-page
            # text when we have it; otherwise fall back to filename.
            title = _extract_pdf_title(path) or path.stem.replace("_", " ")[:300]
            paper_id = hashlib.sha1(
                (clean_doi or new_sha256).encode("utf-8")
            ).hexdigest()[:24]
            target_paper = Paper(
                id=paper_id,
                source=source,
                title=title,
                journal="Others",
                topics=[],
                institutions=[],
                authors=[],
                doi=clean_doi or None,
                link=f"https://doi.org/{clean_doi}" if clean_doi else "",
                abstract="",
                abstract_flag="missing",
            )
            db_session.add(target_paper)
            db_session.flush()
            outcome = "ingested_new"
        else:
            outcome = "ingested_existing"

        # Place the PDF inside the managed downloads/ folder.
        previous = (
            Path(target_paper.pdf_path).resolve()
            if target_paper.pdf_path
            else None
        )
        target_path = pdf_target_path_for_paper(
            target_paper,
            DOWNLOADS_DIR,
            current_path=previous if previous and is_managed_download_path(previous, DOWNLOADS_DIR) else None,
        )
        write_pdf_atomic(target_path, data)
        target_paper.pdf_path = str(target_path.resolve())
        target_paper.pdf_sha256 = new_sha256
        _add_paper_to_default_project(db_session, target_paper, source=source)
        db_session.commit()

        # Fire-and-forget AI analysis so the watcher returns quickly.
        def _analyze_async(paper_id: str, pdf_path: str) -> None:
            session = _db_session()
            try:
                _begin_runtime("watch_folder")
                _push_runtime_log(
                    f"Watch-folder ingested {pdf_path}; analyzing…",
                    stage="watch_folder",
                )
                analyze_paper_from_pdf(
                    paper_id, pdf_path, session, progress_callback=_push_runtime_log
                )
                _finish_runtime(
                    "Watch-folder analysis complete",
                    stage="done",
                    analyzed=1,
                    ingested_paper_id=paper_id,
                )
            except Exception as exc:
                _finish_runtime(
                    f"Watch-folder analysis failed: {exc}", stage="error"
                )
            finally:
                session.close()

        threading.Thread(
            target=_analyze_async,
            args=(target_paper.id, target_paper.pdf_path),
            daemon=True,
            name=f"watch-analyze-{target_paper.id[:8]}",
        ).start()

        return {"status": outcome, "paper_id": target_paper.id}
    finally:
        db_session.close()


# T27: heuristics for spotting mangled PDF-extracted titles. Springer
# running headers and many publisher PDFs put boilerplate ABOVE the
# title — DOI, journal abbreviation, year, volume/issue/page, ISSN —
# so PyMuPDF's first-text-block scrape returns that junk instead of
# the actual title. These regexes flag "this title looks scraped, not
# editorial".
_MANGLED_TITLE_PATTERNS = (
    re.compile(r"doi\.org/", re.I),
    re.compile(r"\b\(0123456789\)"),          # Springer running header
    re.compile(r"\b(?:volume|vol)\.?\s*:\s*", re.I),
    re.compile(r"\([12][0-9]{3}\)\s+\d+\s*:\s*\d+"),  # "(2025) 11:60"
    re.compile(r"\b(?:issn|eissn)\s*[:\s]\s*[0-9]{4}-[0-9]{4}", re.I),
    re.compile(r"^\s*(?:received|accepted|published)[:\s]", re.I),
)


def _looks_like_mangled_title(text: str) -> bool:
    """True when the auto-extracted "title" is obviously PDF boilerplate."""
    if not text or len(text) < 8:
        return True
    return any(p.search(text) for p in _MANGLED_TITLE_PATTERNS)


def _enrich_metadata_from_lookup(
    auto_doi: str,
    auto_title: str,
) -> Optional[Dict[str, Any]]:
    """T27 — turn a (DOI or title) hint into clean metadata via online lookup.

    Tries in order:
      1. ``lookup_metadata_by_identifier("doi", auto_doi)`` if DOI present.
      2. ``lookup_metadata_by_title_authors(auto_title)`` if the title isn't
         obviously mangled boilerplate.

    Returns the ``LookupResult.fields`` dict on a usable hit (status in
    ``{"ok", "partial"}``) or ``None`` if every avenue fails. Wraps every
    call in try/except so a network error never crashes the upload.

    Used by ``POST /api/papers/upload`` to prefer canonical CrossRef /
    OpenAlex / S2 metadata over the raw PyMuPDF first-page scrape.
    """
    # 1. DOI lookup — strongest signal, hits CrossRef + OpenAlex + S2 fan-out.
    doi = (auto_doi or "").strip()
    if doi:
        try:
            res = lookup_metadata_by_identifier("doi", doi)
        except Exception:
            res = None
        if res is not None and res.status in ("ok", "partial") and res.fields.get("title"):
            return dict(res.fields)

    # 2. Title fallback — only when the heuristic title looks like a real
    #    title, not Springer running-header junk. S2 fuzzy-matches by
    #    title similarity; we accept top-1 only above the library's
    #    threshold (currently 0.85).
    title_hint = (auto_title or "").strip()
    if title_hint and not _looks_like_mangled_title(title_hint):
        try:
            res = lookup_metadata_by_title_authors(title_hint)
        except Exception:
            res = None
        if res is not None and res.status in ("ok", "partial") and res.fields.get("title"):
            return dict(res.fields)

    return None


# T30: throttle between online lookups during the startup backfill so we
# don't pin CrossRef's polite-pool rate limit when a user has hundreds of
# mangled rows. 1.2 s ≈ 50 calls/min, well under any provider's threshold.
_METADATA_BACKFILL_DELAY_S = 1.2


def _paper_needs_metadata_refresh(paper: Paper) -> bool:
    """T30 — heuristic for "this row's metadata is probably wrong".

    Two predicates, either fires:

    1. Title looks like PDF boilerplate per ``_looks_like_mangled_title``
       (Springer running header, ISSN line, volume/issue/page banner,
       DOI URL in title, etc.).
    2. The row has a usable DOI but the authors array is empty. That's
       the pre-T27 signature: title was scraped from PDF, no online
       enrichment ran. Re-running it now fills authors / journal /
       abstract / published_date.

    A "clean" row that already has authors AND a non-mangled title is
    skipped — the backfill is idempotent and zero-cost on warm restarts.
    """
    title = (paper.title or "").strip()
    if _looks_like_mangled_title(title):
        return True
    has_doi = bool((paper.doi or "").strip())
    authors_empty = not (paper.authors or [])
    if has_doi and authors_empty:
        return True
    return False


def _backfill_mangled_metadata() -> None:
    """Startup daemon: walk every Paper, re-enrich the ones that look
    mangled, overwrite their fields, log per-row outcomes.

    Sleeps 5s on boot to let the request thread serve the initial UI
    paint without contention. Throttles 1.2s between online lookups.
    Logs a single summary at the end. NEVER raises out of the thread
    (one bad row must not stop the pass).
    """
    import time as _t  # local alias keeps the top-level import clean
    try:
        _t.sleep(5.0)  # let the UI finish first paint
        db_session = _db_session()
        try:
            papers = db_session.query(Paper).order_by(Paper.created_at.asc()).all()
        finally:
            db_session.close()
        if not papers:
            _logger.info("metadata-backfill: empty library, nothing to do.")
            return
        considered = 0
        refreshed = 0
        skipped_clean = 0
        skipped_no_signal = 0
        errors = 0
        for paper in papers:
            considered += 1
            try:
                if not _paper_needs_metadata_refresh(paper):
                    skipped_clean += 1
                    continue
                doi = (paper.doi or "").strip()
                title_hint = (paper.title or "").strip()
                # _enrich_metadata_from_lookup needs at least one signal
                # (DOI or a non-mangled title). If neither is usable, skip.
                if not doi and _looks_like_mangled_title(title_hint):
                    skipped_no_signal += 1
                    continue
                fields = _enrich_metadata_from_lookup(doi, title_hint)
                _t.sleep(_METADATA_BACKFILL_DELAY_S)  # throttle even on miss
                if not fields:
                    skipped_no_signal += 1
                    continue
                # Re-open a session per row so a long pass doesn't pin one
                # connection. Light cost, mirrors _backfill_pdf_sha256.
                inner = _db_session()
                try:
                    row = inner.query(Paper).filter_by(id=paper.id).first()
                    if row is None:
                        continue
                    new_title = (fields.get("title") or "").strip()
                    if new_title:
                        row.title = new_title
                    new_doi = (fields.get("doi") or "").strip()
                    if new_doi and not (row.doi or "").strip():
                        row.doi = new_doi
                    new_authors = fields.get("authors") or []
                    if new_authors:
                        row.authors = new_authors
                    new_journal = (fields.get("journal") or "").strip()
                    if new_journal:
                        row.journal = new_journal
                    new_pub = (fields.get("published_date") or "").strip()
                    if new_pub:
                        row.published_date = new_pub
                    new_abs = (fields.get("abstract") or "").strip()
                    if new_abs and not (row.abstract or "").strip():
                        row.abstract = new_abs
                        row.abstract_flag = "ok"
                    new_link = (fields.get("link") or "").strip()
                    if new_link and not (row.link or "").strip():
                        row.link = new_link
                    inner.commit()
                    refreshed += 1
                    _logger.info(
                        "metadata-backfill: refreshed %s — title=%r authors=%d journal=%r",
                        row.id[:8], new_title[:60], len(new_authors), new_journal[:30],
                    )
                except Exception as exc:  # noqa: BLE001
                    inner.rollback()
                    errors += 1
                    _logger.warning("metadata-backfill: write failed for %s: %s", paper.id[:8], exc)
                finally:
                    inner.close()
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the pass
                errors += 1
                _logger.warning("metadata-backfill: row %s threw: %s", paper.id[:8], exc)
        _logger.info(
            "metadata-backfill: done. considered=%d refreshed=%d clean=%d no-signal=%d errors=%d",
            considered, refreshed, skipped_clean, skipped_no_signal, errors,
        )
    except Exception as exc:  # noqa: BLE001 — last-ditch
        _logger.warning("metadata-backfill: aborted: %s", exc)


def _extract_pdf_title(path: Path, *, max_chars: int = 240) -> Optional[str]:
    """Best-effort grab of a paper title from a PDF's first page text.

    Uses PyMuPDF text extraction + a simple heuristic: the first 1-3 lines
    of meaningful text typically form the title block. Returns None when
    PyMuPDF is unavailable or extraction yields nothing.
    """
    try:
        import fitz  # type: ignore
    except ImportError:  # pragma: no cover
        return None
    try:
        with fitz.open(path) as doc:
            if not len(doc):
                return None
            text = doc[0].get_text() or ""
    except Exception:  # pragma: no cover
        return None

    # First non-trivial line, joined with subsequent short lines until we hit
    # something author-y (lots of commas / `et al.`) or hit max_chars.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    out = lines[0][:max_chars]
    for ln in lines[1:5]:
        if len(out) >= max_chars:
            break
        if "," in ln and ln.count(",") >= 2:
            break  # likely the author list
        if len(ln) < 80 and not ln.endswith(("." , "?", "!")):
            out = (out + " " + ln).strip()[:max_chars]
        else:
            break
    return out


def _backfill_pdf_sha256(batch_log: bool = False) -> int:
    """Compute pdf_sha256 for papers that have a local PDF but no hash yet.

    Run once at startup (cheap — typically 10s for a couple hundred PDFs)
    so the dedup index can detect collisions for legacy uploads. Idempotent:
    rows that already have a hash are skipped. Returns number of rows updated.
    """
    db_session = _db_session()
    updated = 0
    try:
        rows = (
            db_session.query(Paper)
            .filter(Paper.pdf_path.isnot(None))
            .filter((Paper.pdf_sha256.is_(None)) | (Paper.pdf_sha256 == ""))
            .all()
        )
        for paper in rows:
            try:
                path = Path(paper.pdf_path)
                # Try the DB path first; if missing (e.g. directory was
                # renamed), fall back to matching by basename inside the
                # current DOWNLOADS_DIR. Skip silently if neither exists.
                if not path.is_file():
                    fallback = DOWNLOADS_DIR / path.name
                    if fallback.is_file():
                        path = fallback
                        # Heal the stale path so future ops work cleanly.
                        paper.pdf_path = str(path.resolve())
                    else:
                        continue
                paper.pdf_sha256 = sha256_of_file(path)
                updated += 1
            except OSError:
                continue
            except Exception as exc:
                if batch_log:
                    _push_runtime_log(f"sha256 backfill skipped {paper.id}: {exc}", stage="backfill")
                continue
        if updated:
            db_session.commit()
        if batch_log and updated:
            _push_runtime_log(f"Computed pdf_sha256 for {updated} legacy PDFs.", stage="backfill")
    finally:
        db_session.close()
    return updated


def _scheduler_loop() -> None:
    while True:
        db_session = _db_session()
        try:
            last_completed_at = _parse_dt(_get_state(db_session, "last_fetch_completed_at"))
            now = datetime.utcnow()
            should_refresh = last_completed_at is None or (now - last_completed_at).total_seconds() > FETCH_THRESHOLD_SECONDS
        finally:
            db_session.close()

        if should_refresh:
            try:
                _run_refresh_pipeline(trigger="scheduler")
            except Exception:
                pass

        time.sleep(SCHEDULER_CHECK_SECONDS)


def _start_background_scheduler_once() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="seismo-fetch-scheduler")
    scheduler_thread.start()
    _scheduler_started = True


def _count_unanalyzed(db_session) -> int:
    from models import AIAnalysis
    return (
        db_session.query(Paper.id)
        .outerjoin(AIAnalysis)
        .filter(AIAnalysis.id.is_(None))
        .count()
    )


def _eager_analysis_pending_count() -> int:
    """Count un-analysed papers on a fresh, immediately-closed session.

    The eager loop MUST NOT reuse one long-lived session for the before/after
    counts: the pool workers commit on their OWN sessions, and under SQLite WAL
    a session that already ran a SELECT holds its read snapshot — it would keep
    reporting the stale pending count, fake a "no progress" pass, and park the
    daemon in the 600s backoff forever. A fresh session per count also can't be
    poisoned by a failed pass (the old shared-session code could die of
    PendingRollbackError here and silently kill the daemon thread)."""
    db_session = _db_session()
    try:
        return _count_unanalyzed(db_session)
    finally:
        try:
            db_session.rollback()
        except Exception:
            pass
        db_session.close()


def _eager_analysis_loop() -> None:
    """Background worker: proactively analyse EVERY un-analysed paper so it is
    ready before the user opens it (2026-07-05; parallelized 2026-07-06 with a
    bounded thread pool — EAGER_ANALYSIS_WORKERS, workers own their sessions).
    The progress check (did the pending count drop?) means a stuck state — no
    API key, quota exhausted — backs off instead of hot-looping; when nothing is
    pending it idles. Runs silently (no runtime-log spam / no clobber of a fetch
    run's status). analyze_pending_papers + the in-flight guard keep it from
    double-analysing a paper the open-trigger is already handling."""
    while True:
        try:
            pending_before = pending_after = 0
            pending_before = _eager_analysis_pending_count()
            if pending_before > 0:
                db_session = _db_session()
                try:
                    analyze_pending_papers(
                        db_session,
                        limit=EAGER_ANALYSIS_BATCH,
                        progress_callback=None,
                        workers=EAGER_ANALYSIS_WORKERS,
                        session_factory=_db_session,
                    )
                except Exception:
                    _logger.warning("eager analysis pass failed", exc_info=True)
                finally:
                    try:
                        db_session.rollback()
                    except Exception:
                        pass
                    db_session.close()
                pending_after = _eager_analysis_pending_count()
            if pending_before == 0:
                time.sleep(EAGER_ANALYSIS_IDLE_SEC)          # nothing to do — idle
            elif pending_after >= pending_before:
                time.sleep(EAGER_ANALYSIS_BACKOFF_SEC)       # no progress (key/quota?) — back off
            else:
                time.sleep(EAGER_ANALYSIS_GAP_SEC)           # made progress — brief polite gap
        except Exception:
            # The daemon must survive anything (e.g. a transient DB error in the
            # count itself) — treat it like a no-progress pass and back off.
            _logger.warning("eager analysis loop iteration failed", exc_info=True)
            time.sleep(EAGER_ANALYSIS_BACKOFF_SEC)


def _start_eager_analysis_once() -> None:
    global _eager_analysis_started
    if _eager_analysis_started:
        return
    threading.Thread(target=_eager_analysis_loop, daemon=True, name="seismo-eager-analysis").start()
    _eager_analysis_started = True


def _open_browser_once() -> None:
    global _browser_opened
    if _browser_opened or not OPEN_BROWSER:
        return

    def _open() -> None:
        time.sleep(1.0)
        webbrowser.open(SERVER_URL)

    threading.Thread(target=_open, daemon=True, name="seismo-browser-opener").start()
    _browser_opened = True


@app.on_event("startup")
def startup_event() -> None:
    init_db()
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    if AUTO_FETCH_ENABLED:
        _start_background_scheduler_once()
    # 2026-07-05: eager background AI analysis of every un-analysed paper (so
    # papers are analysed before they're opened). Env-gated (DISABLE_EAGER_ANALYSIS=1).
    if EAGER_ANALYSIS_ENABLED:
        _start_eager_analysis_once()
    # PR-XX: launch the per-tracker cadence-aware scheduler. Idempotent
    # and env-gated (DISABLE_TRACKER_SCHEDULER=1 disables for tests).
    start_tracker_scheduler()
    _open_browser_once()

    # One-shot backfill of pdf_sha256 for legacy rows. Runs in a side thread
    # so startup latency stays flat; idempotent on subsequent boots.
    threading.Thread(
        target=lambda: _backfill_pdf_sha256(batch_log=True),
        daemon=True,
        name="seismo-pdf-sha256-backfill",
    ).start()

    # T30: startup scan for papers with mangled metadata (Springer running-
    # header titles, missing authors despite a DOI, etc.) and re-enrich
    # via the same CrossRef/OpenAlex/S2 chain T27 uses on new uploads.
    # Throttled + env-gated. Idempotent: a clean row is skipped on every
    # subsequent boot.
    if os.environ.get("DISABLE_METADATA_BACKFILL") != "1":
        threading.Thread(
            target=lambda: _backfill_mangled_metadata(),
            daemon=True,
            name="seismo-metadata-backfill",
        ).start()

    # Watch-folder integration (M1 F10).
    watch_folder_service.configure_ingest_callbacks({
        "ingest_pdf_bytes": _ingest_pdf_bytes,
    })
    db_session = _db_session()
    try:
        folders = watch_folder_service.load_watch_folders(db_session)
    finally:
        db_session.close()
    if folders:
        threading.Thread(
            target=lambda: watch_folder_service.start_watcher(folders),
            daemon=True,
            name="seismo-watch-folder-start",
        ).start()

    db_session = _db_session()
    try:
        last_completed_at = _parse_dt(_get_state(db_session, "last_fetch_completed_at"))
        should_refresh = last_completed_at is None or (datetime.utcnow() - last_completed_at) > timedelta(seconds=FETCH_THRESHOLD_SECONDS)
    finally:
        db_session.close()

    # Startup stays responsive by launching the first stale-data refresh in a side thread.
    if should_refresh and AUTO_FETCH_ENABLED:
        threading.Thread(target=lambda: _run_refresh_pipeline(trigger="startup"), daemon=True, name="seismo-startup-refresh").start()


@app.get("/")
def read_index():
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/debug/papers")
def debug_papers_simple():
    """Minimal smoke test — pure HTML list of first 10 papers, no JS.
    If THIS shows data but / does not, the bug is purely client-side.

    Disabled by default: set SWP_ENABLE_DEBUG_ROUTES=1 to expose. Returns
    a normal 404 when the env var is unset so the endpoint is invisible
    to scanners.
    """
    import os as _os
    from fastapi.responses import HTMLResponse
    if _os.environ.get("SWP_ENABLE_DEBUG_ROUTES") != "1":
        raise HTTPException(status_code=404, detail="Not Found")
    db = _db_session()
    try:
        papers = db.query(Paper).order_by(Paper.datetime_utc.desc()).limit(10).all()
        rows = "".join(
            f"<li><b>{(p.title or '(no title)')[:140]}</b><br>"
            f"<small>{(p.authors or '?')[:160]} — {p.journal or '?'} — {p.published_date or '?'}</small></li>"
            for p in papers
        )
        total = db.query(Paper).count()
        proj_count = db.query(Project).count()
        pp_count = db.query(ProjectPaper).count()
        html = f"""<!doctype html><html><head><meta charset="utf-8"><title>SWP Debug</title>
<style>body{{font:14px/1.5 -apple-system,system-ui,sans-serif;padding:24px;max-width:900px;margin:auto}}
h1{{color:#0f172a}}li{{margin:8px 0;padding:8px;background:#f8fafc;border-radius:6px}}
.ok{{color:#059669;font-weight:bold}}.bad{{color:#dc2626;font-weight:bold}}</style></head><body>
<h1>SWP Debug — bypassing frontend JS</h1>
<p class="{'ok' if total else 'bad'}">DB has <b>{total}</b> papers, <b>{proj_count}</b> projects, <b>{pp_count}</b> project_papers links.</p>
<p>If you can see the list below, the backend + DB are healthy. The main page bug is purely in <code>templates/index.html</code> JavaScript.</p>
<h2>First 10 papers (by date desc):</h2>
<ol>{rows}</ol>
<hr><p><a href="/">← back to main app</a> · <a href="/api/papers?limit=2">raw JSON of first 2</a></p>
</body></html>"""
        return HTMLResponse(html)
    finally:
        db.close()


# ── PR-42: tiny serializer for the Cmd-K palette index ────────────────
# The full _paper_to_dict() returns 30+ fields per row, most of which
# the palette never reads (abstract, reading-notes, pdf_path, nested
# analysis payload, figure-insights JSON, etc.). At 5000 papers the
# full shape is ≈ 4 MB gzipped; the projection below is ≈ 350 KB.
def _paper_to_index_row(paper) -> Dict[str, Any]:
    """Six-field projection used by `GET /api/papers?fields=index`.

    Returns the minimum the JS scorer needs to build a haystack:
    id, title, authors[], journal, year, doi. No abstract, no analysis,
    no PDF metadata. ≈ 70 bytes per row vs. ≈ 800 for the full shape.
    """
    authors = paper.authors or []
    if not isinstance(authors, list):
        authors = [str(authors)]
    # `year` is derived from `published_date` (YYYY-MM-DD) for sort/display.
    # _paper_to_dict exposes `datetime_utc` and `published_date` separately;
    # we collapse to a 4-char `year` so the JS doesn't have to parse dates.
    year = ""
    if paper.published_date and len(paper.published_date) >= 4:
        year = paper.published_date[:4]
    return {
        "id": paper.id,
        "title": paper.title or "",
        "authors": authors,
        "journal": paper.journal or "",
        "year": year,
        "doi": paper.doi or "",
    }


@app.get("/api/papers")
def get_papers(
    topic: Optional[List[str]] = Query(default=None, description="Repeatable: ?topic=A&topic=B OR-s within the category. Single value still works."),
    journal: Optional[List[str]] = Query(default=None, description="Repeatable: multi-select journals are OR-ed. Single value still works."),
    institution: Optional[List[str]] = Query(default=None, description="Repeatable: multi-select institutions are OR-ed. Single value still works."),
    has_pdf: Optional[bool] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="relevance"),
    project_id: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
    scope_kind: Optional[str] = Query(default=None, description="When unscoped, limit to papers in any 'tracker' or 'collection' project (the All Trackers / All Collections views)."),
    unmatched: Optional[bool] = Query(default=False, description="Browse the orphan candidate pool — papers in NO project (matched no tracker/collection)."),
    q: Optional[str] = Query(default=None, description="Full-text keyword search across title, authors, abstract, journal, keywords."),
    fields: Optional[str] = Query(default=None, description="Set to 'index' for the Cmd-K palette projection (id, title, authors, journal, year, doi only)."),
    limit: Optional[int] = Query(default=None, ge=1, le=20000),
):
    db_session = _db_session()
    try:
        # ── PR-42: Cmd-K palette projection path ─────────────────────────
        # When `fields=index` is passed, return a flat array of trimmed
        # rows instead of the full {"papers": [...], "filters": {...}}
        # shape. The palette doesn't use filters / facets / the sort
        # picker, so skipping `_build_filter_payload` saves a second
        # full-pass over the result set. Bypasses `_matches_filters`
        # too — the palette uses its own fuzzy scorer client-side and
        # ignores every filter param.
        if (fields or "").strip().lower() == "index":
            cap = limit if isinstance(limit, int) and limit > 0 else 5000
            papers = (
                db_session.query(Paper)
                .order_by(Paper.datetime_utc.desc(), Paper.updated_at.desc())
                .limit(cap)
                .all()
            )
            return {"papers": [_paper_to_index_row(p) for p in papers], "total": len(papers)}

        result = get_filtered_papers(
            db_session,
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            project_id=project_id,
            folder=folder,
            q=q,
            scope_kind=scope_kind,
            unmatched=bool(unmatched),
        )
        filters = _build_filter_payload(
            result["serialized"],
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
        )
        # Orphan candidate-pool size — drives the Browse "unmatched (N)" toggle.
        # M7 fix (2026-06-30) — only the Browse view (no project_id) shows that
        # toggle, but this whole-table NOT IN count ran on EVERY /api/papers
        # request (every tracker/collection/detail-list load too). Gate it to the
        # Browse view; return None elsewhere (the frontend only updates its badge
        # when the value is a number, and the toggle is Browse-only, so a tracker
        # view simply keeps the last Browse count — never shown there anyway).
        unmatched_count = None
        if not project_id:
            unmatched_count = (
                db_session.query(Paper.id)
                .filter(~Paper.id.in_(db_session.query(ProjectPaper.paper_id).distinct()))
                .count()
            )
        return {
            "papers": result["filtered"],
            "filters": filters,
            "count": len(result["filtered"]),
            "sort_by": result["sort_by"],
            "q": result.get("q", ""),
            "project_id": project_id,
            "folder": folder,
            "unmatched": bool(unmatched),
            "unmatched_count": unmatched_count,
        }
    finally:
        db_session.close()


@app.get("/api/papers/{paper_id}")
def get_paper(paper_id: str):
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        payload = _paper_to_dict(paper, include_detail=True)
        # H3 fix (2026-06-30) — opening the in-app detail modal now triggers the
        # lazy deep analysis for an un-analysed paper. Previously the trigger was
        # ONLY on opening the PDF in an external reader, so a paper never opened
        # there (especially a metadata-only paper with NO PDF, whose Reanalyze
        # button is hidden) had no path to analysis and showed a wall of "Not
        # available." Fire-and-forget + idempotent (no-op once an AIAnalysis
        # exists); analyze_on_open routes metadata-only papers to Flash, so this
        # works without a PDF. Gated on ai_analysis being absent so an already-
        # analysed paper's detail fetch never spawns a thread.
        if paper.ai_analysis is None:
            _trigger_lazy_analysis_on_open(paper_id)
        return payload
    finally:
        db_session.close()


# ---------------------------------------------------------------------------
# Projects / Workspaces  (M2 — must-do #4)
# ---------------------------------------------------------------------------

def _project_to_dict(project: Project, *, db_session=None) -> Dict[str, Any]:
    """Serialise a Project row.

    When ``db_session`` is given:
      * folder counts are computed (legacy behaviour);
      * ``unread_count`` is computed for tracker projects via
        ``COUNT(project_papers WHERE added_at > project.last_viewed_at)``.

    The legacy ``"type"`` key is preserved (backed by ``project.legacy_type``
    from PR-03) so callers that still read ``r.json()["type"]`` keep working
    while PR-32+ flip the frontend over to ``kind`` / ``scope`` / ``cadence``.
    See ``changelog/04-feature-design/01-tracker-data-model.md`` §4 and
    ``changelog/08-pr-diffs/PR-03-models-project-hybrid.md`` §3.
    """
    # `legacy_type` is the PR-03 hybrid — it derives "monthly" |
    # "topic_monitor" | "static" from the new (kind, scope, cadence,
    # default_sort_by) tuple, falling back to the stored `type` column
    # for rows the PR-02 backfill has not yet seen. This keeps the
    # `"type"` key on the wire honest even after the column itself is
    # dormant.
    base: Dict[str, Any] = {
        "id": project.id,
        "name": project.name,
        "type": project.legacy_type,
        # New triple (PR-01 columns, PR-03 ORM). NULL on rows the PR-02
        # backfill hasn't seen — frontend treats NULL as "collection /
        # personal / weekly" defaults via the master plan §1.1 table.
        "kind": project.kind,
        "scope": project.scope,
        "cadence": project.cadence,
        # Sidebar persistence (BRIEF §1.3 items E + F). Server is the
        # source of truth so a refresh restores the user's sort + density.
        "default_sort_by": project.default_sort_by,
        "table_mode": project.table_mode,
        # Optional cosmetics (sidebar tooltip + color chip).
        "description": project.description,
        "color_tag": project.color_tag,
        "config": project.config or {},
        "icon": project.icon,
        "sort_order": project.sort_order,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        "archived_at": project.archived_at.isoformat() if project.archived_at else None,
        # Tracker telemetry (planned PR-01 columns; not yet on disk —
        # defensive getattr so this serializer keeps working on the
        # current Project model. When those columns land, the getattr
        # returns the real value and the isoformat() branch fires).
        "last_match_at": (
            getattr(project, "last_match_at", None).isoformat()
            if getattr(project, "last_match_at", None) is not None
            else None
        ),
        "last_viewed_at": (
            getattr(project, "last_viewed_at", None).isoformat()
            if getattr(project, "last_viewed_at", None) is not None
            else None
        ),
        # Populated below when (db_session is given) AND (kind == "tracker").
        # Defaulted to 0 here so the JSON key is always present — frontend
        # uses `> 0` truthiness for the badge so this is safe for non-
        # trackers too.
        "unread_count": 0,
        "paper_count": 0,
        "folders": [],
    }
    if db_session is not None:
        from sqlalchemy import func, text as _text
        rows = db_session.query(
            ProjectPaper.folder,
            func.count(ProjectPaper.paper_id).label("cnt"),
        ).filter(ProjectPaper.project_id == project.id).group_by(ProjectPaper.folder).all()
        # Union of (a) folders that actually have papers and (b) user-created
        # empty subfolders stored in config.custom_folders. Empty ones show
        # count=0 in the sidebar and accept new papers via move.
        custom_folders = []
        cfg = project.config or {}
        if isinstance(cfg.get("custom_folders"), list):
            custom_folders = [str(f).strip() for f in cfg["custom_folders"] if str(f).strip()]
        have_papers = {r.folder: r.cnt for r in rows}
        folder_set = {}
        for name, cnt in have_papers.items():
            folder_set[name] = cnt
        for name in custom_folders:
            folder_set.setdefault(name, 0)
        base["folders"] = sorted(
            [{"name": n, "count": c} for n, c in folder_set.items()],
            key=lambda x: x["name"],
            reverse=True,
        )
        base["paper_count"] = sum(have_papers.values())  # only count real papers

        # Unread count: only meaningful for trackers (papers stream in
        # live; the badge answers "did anything land since I last opened
        # this?"). Collections add papers manually so the "unread"
        # framing doesn't fit. Gating on kind also avoids a needless
        # COUNT(*) per collection in the list-projects payload.
        #
        # Predicate matches 01-tracker-data-model §4.2:
        #   COUNT(*) WHERE project_id=? AND added_at > last_viewed_at
        # When last_viewed_at IS NULL (brand-new tracker, never opened),
        # everything counts as unread — same fallback as the design doc.
        if project.kind == "tracker":
            q = db_session.query(func.count(ProjectPaper.paper_id)).filter(
                ProjectPaper.project_id == project.id,
            )
            # Defensive: last_viewed_at column not yet on disk; getattr
            # falls back to None so unread_count = total paper count
            # (correct first-open behaviour per design doc).
            _last_viewed = getattr(project, "last_viewed_at", None)
            if _last_viewed is not None:
                q = q.filter(ProjectPaper.added_at > _last_viewed)
            base["unread_count"] = int(q.scalar() or 0)
    return base


@app.get("/api/projects")
def list_projects():
    """Return all non-archived projects with folder breakdowns."""
    db_session = _db_session()
    try:
        projects = (
            db_session.query(Project)
            .filter(Project.archived_at == None)  # noqa: E711
            .order_by(Project.sort_order.asc(), Project.created_at.asc())
            .all()
        )
        return {"projects": [_project_to_dict(p, db_session=db_session) for p in projects]}
    finally:
        db_session.close()


# ---------------------------------------------------------------------------
# Top-level nav — Inbox  (changelog/04-feature-design/04-top-level-nav.md §1.1)
# ---------------------------------------------------------------------------

INBOX_WATERMARK_KEY = "inbox_last_viewed_at"


@app.get("/api/inbox")
def get_inbox(limit: int = Query(default=200, ge=1, le=1000)):
    """Union of ProjectPaper rows across all kind='tracker' projects.

    Returns ``{papers: [...], watermark: ISO, unread_count: N,
    tracker_count: N}``. Each paper carries a ``source_tracker``
    sub-object (``{id, name, color_tag}``) so the frontend can group
    rows by tracker and badge them.

    ``watermark`` is read from ``AppState[inbox_last_viewed_at]`` and
    treated as the epoch when unset (everything is unread on first
    open). ``unread_count`` is the number of rows in the response with
    ``added_at > watermark`` — computed server-side so the badge stays
    truthy without forcing the client to re-parse ISO strings.

    Ordering: ``ProjectPaper.added_at DESC``. Collections and other
    kinds are excluded — Inbox is the tracker-only feed per
    ``changelog/04-feature-design/04-top-level-nav.md`` §1.1.
    """
    db_session = _db_session()
    try:
        # All non-archived tracker projects. We need the lookup table to
        # tag each paper with its source_tracker without a JOIN per row.
        tracker_projects = (
            db_session.query(Project)
            .filter(Project.kind == "tracker")
            .filter(Project.archived_at == None)  # noqa: E711
            .all()
        )
        tracker_count = len(tracker_projects)
        tracker_lookup: Dict[str, Dict[str, Any]] = {
            p.id: {
                "id": p.id,
                "name": p.name,
                "color_tag": p.color_tag,
            }
            for p in tracker_projects
        }

        watermark_raw = _get_state(db_session, INBOX_WATERMARK_KEY)
        watermark_dt = _parse_dt(watermark_raw)  # None when unset → "epoch"

        papers_out: List[Dict[str, Any]] = []
        # Sources excluded from Inbox: papers manually added/imported into trackers
        # don't count as "new auto-tracked content". Only auto-matched and backfilled
        # papers (plus migration seeds) feed the daily-discovery session.
        _INBOX_EXCLUDED_SOURCES = frozenset({"manual", "upload", "watch_folder", "import"})

        if tracker_lookup:
            # Pull the newest project_paper rows across all trackers.
            # SQLAlchemy ORM gives us the underlying Paper via the
            # relationship; we serialize each paper once and stamp
            # `source_tracker` + `project_folder` + `added_at`.
            rows = (
                db_session.query(ProjectPaper)
                .filter(ProjectPaper.project_id.in_(list(tracker_lookup.keys())))
                .filter(~ProjectPaper.source.in_(_INBOX_EXCLUDED_SOURCES))
                .order_by(ProjectPaper.added_at.desc())
                .limit(limit)
                .all()
            )
            for row in rows:
                paper = row.paper
                if paper is None:
                    continue
                paper_dict = _paper_to_dict(
                    paper, include_detail=False, project_folder=row.folder or None,
                )
                paper_dict["source_tracker"] = tracker_lookup.get(row.project_id)
                paper_dict["added_at"] = (
                    row.added_at.isoformat() if row.added_at else None
                )
                papers_out.append(paper_dict)

        # unread_count: rows with added_at > watermark. When watermark is
        # NULL (first open / never marked viewed) every row counts.
        if watermark_dt is None:
            unread_count = len(papers_out)
        else:
            unread_count = 0
            for row in papers_out:
                added = _parse_dt(row.get("added_at"))
                if added is not None and added > watermark_dt:
                    unread_count += 1

        return {
            "papers": papers_out,
            "watermark": watermark_dt.isoformat() if watermark_dt else None,
            "unread_count": unread_count,
            "tracker_count": tracker_count,
        }
    finally:
        db_session.close()


@app.post("/api/inbox/mark_viewed")
def mark_inbox_viewed():
    """Stamp ``AppState[inbox_last_viewed_at] = utcnow()``.

    Called by the frontend on Inbox tab activation; resets the
    "── New since last visit ──" watermark divider so subsequent
    arrivals show as unread.

    Body: none. Response: ``{"ok": true, "last_viewed_at": ISO}``.
    """
    db_session = _db_session()
    try:
        now = datetime.utcnow()
        _set_state(db_session, INBOX_WATERMARK_KEY, now.isoformat())
        return {"ok": True, "last_viewed_at": now.isoformat()}
    finally:
        db_session.close()


@app.get("/api/nav_counts")
def get_nav_counts():
    """Top-level nav badge counts in a single round-trip.

    Returns ``{inbox: N_unread, trackers: N_tracker_projects,
    collections: N_collection_projects, all: N_total_papers}``. The
    frontend uses these to populate the four tab badges without
    fetching the full /api/inbox and /api/projects payloads upfront.
    """
    db_session = _db_session()
    try:
        from sqlalchemy import func

        tracker_count = int(
            db_session.query(func.count(Project.id))
            .filter(Project.kind == "tracker")
            .filter(Project.archived_at == None)  # noqa: E711
            .scalar() or 0
        )
        collection_count = int(
            db_session.query(func.count(Project.id))
            .filter(Project.kind == "collection")
            .filter(Project.archived_at == None)  # noqa: E711
            .scalar() or 0
        )
        # "All papers" badge = library size = distinct papers in ANY project
        # (tracker/collection), NOT the full broad-fetched candidate pool.
        all_papers = int(
            db_session.query(func.count(func.distinct(ProjectPaper.paper_id))).scalar() or 0
        )

        # Inbox unread: tracker ProjectPaper rows with added_at > watermark.
        watermark_dt = _parse_dt(_get_state(db_session, INBOX_WATERMARK_KEY))
        unread_q = (
            db_session.query(func.count(ProjectPaper.id))
            .join(Project, Project.id == ProjectPaper.project_id)
            .filter(Project.kind == "tracker")
            .filter(Project.archived_at == None)  # noqa: E711
        )
        if watermark_dt is not None:
            unread_q = unread_q.filter(ProjectPaper.added_at > watermark_dt)
        inbox_unread = int(unread_q.scalar() or 0)

        return {
            "inbox": inbox_unread,
            "trackers": tracker_count,
            "collections": collection_count,
            "all": all_papers,
        }
    finally:
        db_session.close()


@app.get("/api/facets")
def get_facets():
    """Return the v3 faceted vocabulary (primary_topic + method/data_type/target/paper_role).

    Single source of truth for the front-end so the index.html mirror can stop
    hard-coding the topic list — kills the long-standing COS_TOPICS drift (Stage C).
    """
    from services.cos_topics import FACETS, MAX_PRIMARY_TOPICS
    return {"facets": FACETS, "max_primary_topics": MAX_PRIMARY_TOPICS}


@app.get("/api/journals/canonical")
def list_canonical_journals():
    """Return the distinct journal abbreviations from services/journal_abbrev.py.

    Used by the Topic Monitor wizard to populate the journal picker. Each
    abbreviation is paired with the first hex color encountered for it in the
    raw mapping (duplicates collapse to the first occurrence). Wave-9 also
    captures the longest full-name string seen for each abbrev so the
    tracker creation dropdown can show "GRL — Geophysical Research Letters"
    style autocomplete suggestions.
    """
    from services.journal_abbrev import _JOURNAL_ABBREVS_RAW
    # Build {abbrev: (color, full_name)} map — for full_name pick the
    # longest non-abbreviation key (i.e. skip short self-referencing
    # entries like "gji"/"grl" that exist only for tolerance lookups).
    seen: dict = {}
    for full_name, (abbrev, color) in _JOURNAL_ABBREVS_RAW.items():
        # Skip self-referencing keys: "grl" → "GRL" with full_name "grl"
        # is uninformative; prefer the long-form title.
        is_self_ref = full_name.strip().lower() == abbrev.strip().lower()
        if abbrev not in seen:
            seen[abbrev] = {"color": color, "full_name": "" if is_self_ref else full_name}
        else:
            # Keep the longest full_name we've seen so far for this abbrev.
            if not is_self_ref and len(full_name) > len(seen[abbrev]["full_name"]):
                seen[abbrev]["full_name"] = full_name
    return {
        "journals": sorted(
            [
                {"abbrev": a, "color": v["color"], "full_name": v["full_name"].title()}
                for a, v in seen.items()
            ],
            key=lambda x: x["abbrev"].lower(),
        )
    }


@app.get("/api/authors/canonical")
def list_canonical_authors():
    """Return the top-200 first-author last names by paper count.

    Used by the Tracker creation modal (author scope) for autocomplete.
    Pulls ``Paper.authors[0]`` from the JSON column and counts the most-
    common surname tokens — surfaces senior PIs first. Cached server-side
    for 60s via a module-level memo so repeated modal opens don't re-scan
    the papers table.

    Response: ``{"authors": [{"name": str, "count": int}, ...]}``
    """
    cached = _authors_canonical_cache_get()
    if cached is not None:
        return cached

    db_session = _db_session()
    try:
        rows = db_session.query(Paper.authors).filter(Paper.authors.isnot(None)).all()
    finally:
        db_session.close()

    from collections import Counter
    counter: Counter = Counter()
    for (authors_raw,) in rows:
        authors = authors_raw if isinstance(authors_raw, list) else []
        if not authors:
            continue
        first = (authors[0] or "").strip()
        if not first:
            continue
        # Use the full "First Last" string as the canonical key — the
        # surname-only split is fragile across cultures (CJK names, Spanish
        # double surnames). The downstream tracker matcher does the same
        # case-insensitive substring check on the full author string.
        counter[first] += 1

    top = counter.most_common(200)
    payload = {
        "authors": [{"name": name, "count": count} for name, count in top]
    }
    _authors_canonical_cache_set(payload)
    return payload


# 60-second TTL cache for /api/authors/canonical — single global memo,
# guarded by a lock so concurrent requests don't both run the scan.
_AUTHORS_CACHE: Dict[str, Any] = {"data": None, "expires_at": 0.0}
_AUTHORS_CACHE_LOCK = threading.Lock()
_AUTHORS_CACHE_TTL = 60.0  # seconds


def _authors_canonical_cache_get() -> Optional[Dict[str, Any]]:
    with _AUTHORS_CACHE_LOCK:
        if _AUTHORS_CACHE["data"] is not None and time.time() < _AUTHORS_CACHE["expires_at"]:
            return _AUTHORS_CACHE["data"]
    return None


def _authors_canonical_cache_set(payload: Dict[str, Any]) -> None:
    with _AUTHORS_CACHE_LOCK:
        _AUTHORS_CACHE["data"] = payload
        _AUTHORS_CACHE["expires_at"] = time.time() + _AUTHORS_CACHE_TTL


# ── Follow-author flow (2026-06-14) ─────────────────────────────────────────
# OpenAlex search + add/remove for an author-follow tracker (e.g. 「明星课题组」).
# The picker resolves name → OpenAlex author ID (disambiguation by eye) and stores
# the current institution so the tracker folds papers under "Institution / Author".
@app.get("/api/authors/search")
def search_authors_openalex(q: str = Query(..., min_length=2), limit: int = Query(8, ge=1, le=20)):
    """Search OpenAlex for author candidates → [{id, name, institution, works_count, top_topic, orcid}]."""
    from services.fetch_service import http_get_json
    try:
        data = http_get_json("https://api.openalex.org/authors", {
            "search": q, "per-page": limit, "mailto": "seismohub@example.com",
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OpenAlex author search failed: {exc}")
    out = []
    for a in (data.get("results") or [])[:limit]:
        oa_id = str(a.get("id") or "").rsplit("/", 1)[-1]
        insts = a.get("last_known_institutions") or []
        if insts:
            inst = insts[0].get("display_name") or ""
        else:
            inst = ((a.get("last_known_institution") or {}) or {}).get("display_name") or ""
        topics = a.get("topics") or a.get("x_concepts") or []
        top_topic = (topics[0].get("display_name") if topics else "") or ""
        orcid = a.get("orcid") or ""
        out.append({
            "id": oa_id,
            "name": a.get("display_name") or "",
            "institution": inst,
            "works_count": int(a.get("works_count") or 0),
            "top_topic": top_topic,
            "orcid": orcid.rsplit("/", 1)[-1] if orcid else "",
        })
    return {"results": out}


def _author_predicate(cfg: dict):
    for p in (cfg.get("predicates") or []):
        if isinstance(p, dict) and p.get("type") == "author":
            return p
    return None


@app.get("/api/projects/{project_id}/authors")
def list_followed_authors(project_id: str):
    """The author-follow list for a tracker (for the manage-authors panel)."""
    db_session = _db_session()
    try:
        project = db_session.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        ap = _author_predicate(project.config or {}) or {}
        meta = ap.get("author_meta") or {}
        authors = []
        for aid in (ap.get("author_ids") or []):
            m = meta.get(aid) or {}
            authors.append({"id": aid, "name": m.get("name") or aid, "institution": m.get("institution") or ""})
        return {"authors": authors, "count": len(authors)}
    finally:
        db_session.close()


@app.post("/api/projects/{project_id}/authors")
def follow_author(project_id: str, body: dict = Body(...)):
    """Add an author (OpenAlex id + name + institution) to a tracker's author
    predicate, backfill the existing pool (inbound), then re-fold so the new
    author's papers land under "Institution / Author". Outbound OpenAlex-by-author
    pull runs on the next fetch/refresh."""
    aid = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    institution = (str(body.get("institution") or "").strip().replace(" / ", " · ")) or "Other"
    if not aid or not name:
        raise HTTPException(status_code=400, detail="id and name are required")
    db_session = _db_session()
    try:
        project = db_session.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        cfg = json.loads(json.dumps(project.config or {}))  # deep copy — a shallow
        # copy shares the nested predicate dicts, so in-place edits would mutate the
        # original too and SQLAlchemy would see old==new and skip the UPDATE.
        preds = list(cfg.get("predicates") or [])
        ap = _author_predicate(cfg)
        if ap is None:
            ap = {"type": "author", "any_of": [], "author_ids": [], "author_meta": {}}
            preds.append(ap)
        ap.setdefault("any_of", [])
        ap.setdefault("author_ids", [])
        ap.setdefault("author_meta", {})
        if name not in ap["any_of"]:
            ap["any_of"].append(name)
        if aid not in ap["author_ids"]:
            ap["author_ids"].append(aid)
        ap["author_meta"][aid] = {"name": name, "institution": institution}
        cfg["predicates"] = preds
        project.config = cfg
        db_session.commit()
        backfilled = 0
        try:
            backfilled = _backfill_predicate_tracker(db_session, project)
            db_session.commit()
            from services.tracker_scheduler import _derive_folder
            from models import ProjectPaper
            rows = db_session.query(ProjectPaper).filter(ProjectPaper.project_id == project.id).all()
            if rows:
                pmap = {p.id: p for p in db_session.query(Paper).filter(Paper.id.in_([r.paper_id for r in rows])).all()}
                for r in rows:
                    pp = pmap.get(r.paper_id)
                    if pp is None:
                        continue
                    nf = _derive_folder(project, pp)
                    if r.folder != nf:
                        r.folder = nf
                db_session.commit()
        except Exception:
            db_session.rollback()
        return {"ok": True, "author_count": len(ap["author_ids"]), "backfilled": backfilled,
                "added": {"id": aid, "name": name, "institution": institution}}
    finally:
        db_session.close()


@app.delete("/api/projects/{project_id}/authors/{author_id}")
def unfollow_author(project_id: str, author_id: str):
    """Remove an author from a tracker's author predicate. Existing matched papers
    stay (non-destructive); the author just stops being fetched/matched going forward."""
    db_session = _db_session()
    try:
        project = db_session.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        cfg = json.loads(json.dumps(project.config or {}))  # deep copy (see follow_author)
        ap = _author_predicate(cfg)
        if ap is not None:
            meta = ap.get("author_meta") or {}
            name = (meta.get(author_id) or {}).get("name")
            ap["author_ids"] = [x for x in (ap.get("author_ids") or []) if x != author_id]
            if name:
                ap["any_of"] = [n for n in (ap.get("any_of") or []) if n != name]
            if author_id in meta:
                del meta[author_id]
            ap["author_meta"] = meta
            cfg["predicates"] = list(cfg.get("predicates") or [])
            project.config = cfg
            db_session.commit()
        return {"ok": True, "author_count": len((ap or {}).get("author_ids", []))}
    finally:
        db_session.close()


# ---------------------------------------------------------------------------
# Predicate-based tracker config (BRIEF v2 §1, §3, §7)
# ---------------------------------------------------------------------------
#
# A tracker config grew from the flat legacy ``{topics, keywords, journals}``
# shape into the richer ``{match, predicates:[...], description}`` shape. The
# helpers below normalise the new shape on the way IN (create / update) and
# embed the free-text ``description`` ONCE into the semantic predicate's cached
# ``query_embedding`` so the matcher (services.predicates.SemanticPredicate) and
# the create-time preview share the exact same query vector. Everything degrades
# gracefully: with no API key the description is stored but carries no embedding
# (the semantic predicate then simply never matches — see embedding_service's
# graceful-degradation contract), and the legacy fields are always preserved so
# old trackers and the legacy matcher branch keep working untouched.

# Predicate ``type`` values the UI builder + matcher understand. ``semantic`` is
# special (carries the embedded description); the rest are deterministic.
_TRACKER_PREDICATE_TYPES = {
    "semantic",
    "keyword",
    "journal",
    "author",
    "cos_topic",
    "institution",
    "date_window",
    "arxiv_category",
}

# Default semantic cosine threshold — mirrors
# services.predicates.DEFAULT_SEMANTIC_THRESHOLD so the create-time preview and
# the live matcher agree on the cut-off when the UI omits one.
_DEFAULT_SEMANTIC_THRESHOLD = 0.62


def _clean_term_list(value: Any) -> List[str]:
    """Coerce a predicate value (list or scalar) into a clean, deduped term list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out: List[str] = []
    for item in items:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def _coerce_threshold(raw: Any, default: float = _DEFAULT_SEMANTIC_THRESHOLD) -> float:
    """Parse a UI threshold into a 0..1 float, falling back to ``default``."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _embed_tracker_description(description: str) -> Optional[List[float]]:
    """Embed a tracker's free-text description as a retrieval QUERY vector.

    Returns ``None`` on blank text, missing API key, or any embedding failure —
    ``services.embedding_service.embed_text`` swallows network/quota errors to
    ``None`` and NEVER raises, so a tracker still saves (just without semantic
    matching until a later re-embed). Reused by both the create/update config
    normaliser and the ``/api/trackers/preview`` endpoint so the cached query
    embedding and the preview ranking use one identical vector.
    """
    text = (description or "").strip()
    if not text:
        return None
    try:
        from services.embedding_service import embed_text
        return embed_text(text, is_query=True)
    except Exception:
        _logger.warning("tracker description embedding failed; storing without it", exc_info=True)
        return None


def _normalize_tracker_predicate(spec: Any, *, embed: bool = True) -> Optional[Dict[str, Any]]:
    """Sanitise ONE predicate dict from the UI builder into stored shape.

    - Drops unknown / empty predicates (returns ``None``).
    - ``semantic`` predicates carry a free-text ``description`` and a
      ``threshold``; when ``embed`` is true the description is embedded ONCE here
      and cached on the predicate as ``query_embedding`` + ``embedding_model`` so
      the live matcher never re-embeds. A semantic predicate with neither a
      description nor a pre-supplied embedding is dropped.
    - All other types normalise their terms into a deduped ``any_of`` list (or
      ``after`` / ``before`` for ``date_window``); an empty term set is dropped.
    """
    if not isinstance(spec, dict):
        return None
    ptype = str(spec.get("type") or "").strip().lower()
    if ptype not in _TRACKER_PREDICATE_TYPES:
        return None

    if ptype == "semantic":
        description = str(spec.get("description") or spec.get("query") or "").strip()
        threshold = _coerce_threshold(spec.get("threshold"))
        out: Dict[str, Any] = {
            "type": "semantic",
            "description": description,
            "threshold": threshold,
        }
        # Reuse a client-supplied embedding when present (e.g. the preview
        # already computed it); otherwise embed the description now.
        existing_vec = (
            spec.get("query_embedding")
            or spec.get("embedding")
            or spec.get("query_vector")
        )
        vec: Optional[List[float]] = None
        if isinstance(existing_vec, (list, tuple)) and existing_vec:
            try:
                vec = [float(x) for x in existing_vec]
            except (TypeError, ValueError):
                vec = None
        if vec is None and embed and description:
            vec = _embed_tracker_description(description)
        if vec is not None:
            out["query_embedding"] = vec
            try:
                from services.embedding_service import EMBED_MODEL
                out["embedding_model"] = EMBED_MODEL
            except Exception:
                pass
        elif not description:
            # No description AND no usable embedding — nothing to match on.
            return None
        return out

    if ptype == "date_window":
        after = spec.get("after") or spec.get("from") or spec.get("start")
        before = spec.get("before") or spec.get("to") or spec.get("end")
        after = str(after).strip() if after else ""
        before = str(before).strip() if before else ""
        if not after and not before:
            return None
        win: Dict[str, Any] = {"type": "date_window"}
        if after:
            win["after"] = after
        if before:
            win["before"] = before
        return win

    # Deterministic term-list predicates (keyword / journal / author /
    # cos_topic / institution / arxiv_category).
    raw_terms = (
        spec.get("any_of")
        if spec.get("any_of") is not None
        else spec.get("values", spec.get("value", spec.get("terms", spec.get("items"))))
    )
    terms = _clean_term_list(raw_terms)
    # Journal predicates store upper-cased abbreviations, mirroring the legacy
    # config.journals normalisation and JournalPredicate's comparison.
    if ptype == "journal":
        terms = [t.upper() for t in terms]
    if not terms:
        return None
    out: Dict[str, Any] = {"type": ptype, "any_of": terms}
    # Author predicates additionally carry the OpenAlex author IDs + per-id
    # metadata (name / institution). These drive the OUTBOUND by-author fetch
    # (fetch_service reads spec["author_ids"]) and the "Institution / Author"
    # folder folding (tracker_scheduler._derive_folder reads author_meta), so they
    # MUST survive normalisation. Both the follow-author endpoint and the
    # new-tracker wizard supply them; without this they'd be silently dropped on
    # create AND on any config-touching PATCH (update_project re-normalises).
    if ptype == "author":
        ids = spec.get("author_ids")
        if isinstance(ids, (list, tuple)):
            seen_ids: set = set()
            deduped_ids: List[str] = []
            for x in ids:
                s = str(x).strip()
                if not s or s in seen_ids:
                    continue
                seen_ids.add(s)
                deduped_ids.append(s)
            if deduped_ids:
                out["author_ids"] = deduped_ids
        meta = spec.get("author_meta")
        if isinstance(meta, dict) and meta:
            clean_meta: Dict[str, Any] = {}
            for k, v in meta.items():
                if not isinstance(v, dict):
                    continue
                clean_meta[str(k)] = {
                    "name": str(v.get("name") or "").strip(),
                    "institution": str(v.get("institution") or "").strip(),
                }
            if clean_meta:
                out["author_meta"] = clean_meta
    return out


def _normalize_tracker_config(raw_config: Any, *, embed: bool = True) -> Dict[str, Any]:
    """Normalise an inbound tracker config for create / update (BRIEF v2 §1).

    Accepts the NEW predicate shape (``match`` + ``predicates`` + free-text
    ``description``) AND keeps the LEGACY fields (``topics`` / ``keywords`` /
    ``journals``) intact so the legacy matcher branch and existing trackers are
    untouched (backward compatible).

    Behaviour:
    - ``description`` (the top-level NL "describe your interest" text) is kept on
      the config AND, when present and not already represented by a semantic
      predicate, is materialised into a ``semantic`` predicate so a tracker
      created purely from the textarea still matches semantically.
    - Each predicate is sanitised + (for semantic) embedded via
      :func:`_normalize_tracker_predicate`.
    - ``match`` is coerced to ``"all"`` / ``"any"`` (default ``"any"``).
    - Legacy ``topics`` / ``keywords`` / ``journals`` lists are cleaned the same
      way ``create_project`` historically cleaned them.
    - Non-recognised keys (e.g. ``min_relevance``, ``custom_folders``,
      ``query``) are passed through untouched.

    Never raises; a non-dict config yields an empty dict.
    """
    if not isinstance(raw_config, dict):
        return {}
    config = dict(raw_config)

    # ── Legacy term lists — keep them, cleaned, for the legacy matcher. ──
    if "topics" in config:
        config["topics"] = _clean_term_list(config.get("topics"))
    if "keywords" in config:
        config["keywords"] = _clean_term_list(config.get("keywords"))
    if "journals" in config:
        config["journals"] = [t.upper() for t in _clean_term_list(config.get("journals"))]

    # ── Free-text NL description (BRIEF v2 §7). ──
    description = str(config.get("description") or "").strip()
    if description:
        config["description"] = description
    else:
        config.pop("description", None)

    # ── Predicate list (BRIEF v2 §1). ──
    raw_preds = config.get("predicates")
    normalized_preds: List[Dict[str, Any]] = []
    has_semantic = False
    if isinstance(raw_preds, list):
        for spec in raw_preds:
            norm = _normalize_tracker_predicate(spec, embed=embed)
            if norm is None:
                continue
            if norm.get("type") == "semantic":
                has_semantic = True
            normalized_preds.append(norm)

    # If the user typed a top-level description but built no explicit semantic
    # predicate, synthesise one so the textarea alone is enough to match.
    if description and not has_semantic:
        synth = _normalize_tracker_predicate(
            {"type": "semantic", "description": description}, embed=embed
        )
        if synth is not None:
            normalized_preds.append(synth)
            has_semantic = True

    if isinstance(raw_preds, list) or normalized_preds:
        config["predicates"] = normalized_preds
        match = str(config.get("match") or "any").strip().lower()
        if match not in ("all", "any"):
            match = "any"
        config["match"] = match

    return config


def _tracker_config_has_selectors(config: Dict[str, Any]) -> bool:
    """True when a tracker config carries at least one usable selector.

    A selector is any non-empty predicate OR any non-empty legacy term list
    (topics / keywords / journals) OR a free-text description. Used to reject a
    create/update that would otherwise silently produce a tracker that can never
    match anything.
    """
    if not isinstance(config, dict):
        return False
    preds = config.get("predicates")
    if isinstance(preds, list) and any(isinstance(p, dict) for p in preds):
        return True
    if str(config.get("description") or "").strip():
        return True
    for key in ("topics", "keywords", "journals"):
        if _clean_term_list(config.get(key)):
            return True
    return False


def _tracker_pool_vectors(db_session, *, limit: Optional[int] = None) -> Dict[str, Any]:
    """Load ``{paper_id: numpy_vector}`` for every paper carrying an embedding.

    The semantic-preview / NL-tracker engine (BRIEF v2 §3, §7): a single
    numpy brute-force cosine over the whole local pool (NO sqlite-vec). Reads the
    ``Paper.embedding`` TEXT column written at ingest and decodes it via
    ``services.embedding_service.decode_vector`` (a corrupt cell decodes to
    ``None`` and is skipped, never crashing the scan). Papers without an
    embedding are omitted (they can never match semantically).
    """
    from services.embedding_service import decode_vector

    q = (
        db_session.query(Paper.id, Paper.embedding)
        .filter(Paper.embedding.isnot(None))
    )
    rows = q.all()
    id_to_vec: Dict[str, Any] = {}
    for pid, blob in rows:
        vec = decode_vector(blob)
        if vec is not None:
            id_to_vec[pid] = vec
        if limit is not None and len(id_to_vec) >= limit:
            break
    return id_to_vec


@app.post("/api/trackers/preview")
def preview_tracker_matches(body: dict = Body(...)):
    """Preview which existing papers a free-text tracker description would match.

    BRIEF v2 §7 — the NL-tracker create-time match preview. The frontend's
    "预览匹配 / Preview matches" button POSTs the textarea description (and an
    optional tunable threshold) BEFORE the tracker is saved; we embed the
    description once (reusing the exact ``is_query=True`` path the live matcher's
    cached query embedding uses), run a numpy brute-force cosine over the local
    pool (``services.embedding_service.cosine_topk`` — NO sqlite-vec), and return
    the match count plus the top-N papers with their cosine scores so the user
    can judge + tune the tracker before committing.

    Body::

        {
            "description": "slow slip events detected with DAS",  # required
            "threshold":   0.62,     # optional, 0..1 (default 0.62)
            "top_n":       20        # optional cap on returned papers (default 20)
        }

    Response::

        {
            "ok": true,
            "embedded": true,            # false when no API key / embed failed
            "threshold": 0.62,
            "pool_size": 184,            # papers carrying an embedding
            "match_count": 12,           # papers at/above the threshold
            "query_embedding": [...],    # so the client can reuse it on save
            "matches": [
                {"id": "...", "title": "...", "journal": "...",
                 "journal_abbrev": "GRL", "authors": [...], "author_line": "...",
                 "published_date": "2025-03", "score": 0.81, "has_pdf": true},
                ...
            ]
        }

    Degrades gracefully: with no embedding key (or an embed failure) it returns
    ``ok: true, embedded: false, matches: []`` and a 200 so the UI can show a
    bilingual "semantic preview unavailable" hint instead of erroring.
    """
    description = str(body.get("description") or "").strip()
    if not description:
        raise HTTPException(
            status_code=400,
            detail="description is required for a tracker preview.",
        )
    threshold = _coerce_threshold(body.get("threshold"))
    try:
        top_n = int(body.get("top_n") or 20)
    except (TypeError, ValueError):
        top_n = 20
    top_n = max(1, min(top_n, 100))

    query_vec = _embed_tracker_description(description)
    if query_vec is None:
        # No key / embedding failure — return a graceful, explicit "unavailable".
        return {
            "ok": True,
            "embedded": False,
            "threshold": threshold,
            "pool_size": 0,
            "match_count": 0,
            "query_embedding": None,
            "matches": [],
        }

    db_session = _db_session()
    try:
        import numpy as np
        from services.embedding_service import cosine_topk

        id_to_vec = _tracker_pool_vectors(db_session)
        pool_size = len(id_to_vec)
        query_arr = np.asarray(query_vec, dtype=np.float32)

        # All scores at/above threshold, ranked; cap the returned rows to top_n.
        ranked = cosine_topk(query_arr, id_to_vec, k=None, threshold=threshold)
        match_count = len(ranked)
        top = ranked[:top_n]

        # Hydrate the top matches into lightweight paper cards (one query).
        matches: List[Dict[str, Any]] = []
        if top:
            top_ids = [pid for pid, _ in top]
            score_by_id = {pid: score for pid, score in top}
            papers = (
                db_session.query(Paper)
                .filter(Paper.id.in_(top_ids))
                .all()
            )
            by_id = {p.id: p for p in papers}
            for pid, score in top:  # preserve descending-score order
                paper = by_id.get(pid)
                if paper is None:
                    continue
                authors = paper.authors if isinstance(paper.authors, list) else []
                matches.append(
                    {
                        "id": paper.id,
                        "title": paper.title or "",
                        "journal": paper.journal or "",
                        "journal_abbrev": journal_abbreviate(paper.journal or "Others"),
                        "authors": authors,
                        "author_line": ", ".join(authors) if authors else "Unknown authors",
                        "first_author": authors[0] if authors else "Unknown",
                        "published_date": paper.published_date or "",
                        "has_pdf": bool(paper.pdf_path),
                        "score": round(float(score), 4),
                    }
                )

        return {
            "ok": True,
            "embedded": True,
            "threshold": threshold,
            "pool_size": pool_size,
            "match_count": match_count,
            # Returned so the client can pass it straight back on save and skip a
            # second embedding round-trip (accepted by _normalize_tracker_predicate).
            "query_embedding": [round(float(x), 7) for x in query_vec],
            "matches": matches,
        }
    finally:
        db_session.close()


@app.get("/api/presets")
def list_presets():
    """List the built-in preset trackers (seed_presets.json) for the "Add from
    template" UI (2026-07-05). Read-only. Each item carries the tracker `config`
    so the client renders its conditions with the same chip renderer and POSTs it
    straight to /api/projects. `already_added` flags presets whose name already
    exists as a project, so the UI can mark them as added."""
    path = BASE_DIR / "seed_presets.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        presets = data.get("presets", []) if isinstance(data, dict) else []
    except Exception as exc:  # noqa: BLE001 — missing/corrupt catalog degrades to empty.
        _logger.warning("list_presets: could not read seed_presets.json: %s", exc)
        return {"presets": []}
    db_session = _db_session()
    try:
        existing = {str(n).strip() for (n,) in db_session.query(Project.name).all()}
    finally:
        db_session.close()
    out: List[Dict[str, Any]] = []
    for p in presets:
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            continue
        out.append({
            "name": p.get("name"),
            "config": p.get("config") or {},
            "sort_order": p.get("sort_order", 0),
            "already_added": str(p.get("name")).strip() in existing,
        })
    out.sort(key=lambda x: x.get("sort_order", 0))
    return {"presets": out}


@app.post("/api/trackers/preview_config")
def preview_tracker_config(body: dict = Body(...)):
    """B1 — map a live Browse SEARCH state into a tracker config, then HONESTLY
    preview how many library papers the resulting tracker would actually match.

    This backs the "Save this search as Tracker" affordance. The subtlety it
    exists to surface: a Browse search and a tracker predicate do NOT match the
    same way. `_matches_q` is a case-insensitive SUBSTRING over
    title+author+abstract+journal+DOI+keywords, whereas the `keyword` predicate
    is WORD-BOUNDARY (for ASCII terms) over title+abstract+authors+summary and
    does NOT scan journal/DOI. So "quake" finds "earthquake" in search but not in
    a keyword tracker. Rather than let the user save a search and silently get
    fewer hits, we dry-run the REAL matcher over the whole pool and report the
    true count so the confirm UI can say "will match N (search shows M)".

    Body (raw search state, NOT a pre-built config)::

        {
          "q": "slow slip",           # keyword box
          "topic": "",                # topic facet (canonical COS name)
          "journal": "",              # journal facet (full name as stored)
          "institution": "",          # institution facet
          "want_semantic": false      # also track by semantic similarity to q?
        }

    Response::

        {
          "ok": true,
          "config": { "match": "all", "predicates": [...] },  # pass straight to POST /api/projects
          "predicate_types": ["keyword", "journal"],
          "suggestions": [ {"kind": "journal_from_q", ...} ],
          "total_hits": 42,           # papers the tracker would match NOW
          "orphan_hits": 15,          # of those, ones in NO project yet (get collected on backfill)
          "pool_size": 180,
          "sample": [ {"id","title","score","is_orphan"} ]   # up to 5
        }

    READ-ONLY: never writes. The returned `config` is exactly what create_project
    would store (same _normalize_tracker_config path), so preview == reality.
    """
    q = str(body.get("q") or "").strip()
    # Multi-select (2026-07-06): the facets arrive as lists (a single string is
    # still accepted for old callers). Each facet becomes ONE predicate whose
    # any_of holds every selected value — same OR-within / AND-across semantics
    # the Browse filter now uses, so preview == the search the user just saw.
    topics = _as_filter_list(body.get("topic"))
    journals = _as_filter_list(body.get("journal"))
    institutions = _as_filter_list(body.get("institution"))
    want_semantic = bool(body.get("want_semantic"))

    # --- 1. search state -> raw predicates (the search->predicate mapping) ----
    # match="all": Browse ANDs its facets + keyword, so the tracker must too, or
    # the saved tracker would be looser than the search the user just saw.
    raw_preds: List[Dict[str, Any]] = []
    suggestions: List[Dict[str, Any]] = []
    if q:
        raw_preds.append({"type": "keyword", "any_of": [q]})
        # Smart suggestion: the user may have typed a JOURNAL name into the search
        # box (search scans the journal field; the keyword predicate does not), so
        # a keyword tracker would miss journal-only hits. If q is an exact known
        # registry title, offer a journal predicate instead/as-well. Best-effort.
        try:
            from services.journal_registry import abbrev_for
            j_abbrev = abbrev_for(q)
            if j_abbrev and not journals:
                suggestions.append({
                    "kind": "journal_from_q",
                    "reason": f"'{q}' is a known journal; the keyword box does not track by journal.",
                    "predicate": {"type": "journal", "any_of": [j_abbrev.upper()]},
                })
        except Exception:
            pass
    if journals:
        # Facet stores full names; the journal predicate compares abbreviations.
        # Multiple selected journals share one predicate → OR within the facet.
        raw_preds.append({"type": "journal", "any_of": [journal_abbreviate(j) for j in journals]})
    if topics:
        raw_preds.append({"type": "cos_topic", "any_of": topics})
    if institutions:
        raw_preds.append({"type": "institution", "any_of": institutions})
    if want_semantic and q:
        # Embed q up front so a missing key degrades to "no semantic predicate"
        # (+ a suggestion) instead of a DEAD semantic predicate that, under
        # match="all", would sink every hit to zero.
        _vec = _embed_tracker_description(q)
        if _vec is not None:
            raw_preds.append({
                "type": "semantic", "description": q,
                "threshold": _DEFAULT_SEMANTIC_THRESHOLD, "query_embedding": _vec,
            })
        else:
            suggestions.append({
                "kind": "semantic_unavailable",
                "reason": "Semantic tracking needs an embedding key; keyword tracking still works.",
            })

    # --- 2. normalise EXACTLY as create_project will (preview == reality) -----
    config = _normalize_tracker_config({"match": "all", "predicates": raw_preds}, embed=True)
    if not _tracker_config_has_selectors(config):
        raise HTTPException(
            status_code=400,
            detail="This search has no trackable condition (add a keyword or a facet).",
        )

    # --- 3. honest dry-run of the REAL matcher over the whole library --------
    from services.predicates import match_tracker

    db_session = _db_session()
    try:
        member_ids = {
            pid for (pid,) in db_session.query(ProjectPaper.paper_id).distinct().all()
        }
        pool = db_session.query(Paper).all()
        total_hits = 0
        orphan_hits = 0
        sample: List[Dict[str, Any]] = []
        for paper in pool:
            try:
                belongs, score = match_tracker(paper, config)
            except Exception:
                belongs, score = (False, 0.0)
            if not belongs:
                continue
            total_hits += 1
            is_orphan = paper.id not in member_ids
            if is_orphan:
                orphan_hits += 1
            if len(sample) < 5:
                sample.append({
                    "id": paper.id,
                    "title": paper.title or "",
                    "journal_abbrev": journal_abbreviate(paper.journal or "Others"),
                    "score": round(float(score), 4),
                    "is_orphan": is_orphan,
                })
        return {
            "ok": True,
            "config": config,
            "match": config.get("match", "all"),
            "predicate_types": [str(p.get("type")) for p in config.get("predicates", [])],
            "suggestions": suggestions,
            "total_hits": total_hits,
            "orphan_hits": orphan_hits,
            "pool_size": len(pool),
            "sample": sample,
        }
    finally:
        db_session.close()


@app.post("/api/projects")
def create_project(body: dict):
    """Create a new project.

    Body: {name: str, type?: "monthly"|"topic_monitor"|"static",
           kind?: "tracker"|"collection", config?: dict, backfill?: bool}

    When ``type`` is "monthly"/"static", ``kind="collection"`` overrides the
    type-derived taxonomy to create a collection.

    For ``topic_monitor`` projects, when ``backfill`` is true (default), every
    existing paper whose AI analysis has ``topic_matches`` overlapping with
    ``config.topics`` is automatically added to the new project.  This makes
    the project useful immediately instead of waiting for the next upload.

    Topic Monitor accepts EITHER ``config.topics`` (canonical COS_TOPIC names
    matched against ``cos_topic_match``) OR ``config.keywords`` (free-form
    phrases matched case-insensitively against title + abstract + AI summary
    + AI keywords + author names).  At least one of the two must be non-empty.
    """
    from sqlalchemy.exc import IntegrityError

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required.")
    proj_type = body.get("type", "monthly")
    if proj_type not in ("monthly", "topic_monitor", "static"):
        raise HTTPException(status_code=400, detail=f"Unknown project type: {proj_type!r}")
    config = body.get("config") or {}
    do_backfill = bool(body.get("backfill", True))

    # Normalise config for topic_monitor (= a tracker). BRIEF v2 §1/§3/§7: the
    # config may carry the NEW predicate shape (``match`` + ``predicates`` +
    # free-text ``description``) AND/OR the LEGACY ``{topics, keywords,
    # journals}`` fields. ``_normalize_tracker_config`` cleans both, embeds the
    # semantic description ONCE into the predicate's cached ``query_embedding``
    # (graceful no-op without an API key), and preserves legacy fields so the
    # legacy matcher branch + existing trackers stay byte-compatible.
    if proj_type == "topic_monitor":
        config = _normalize_tracker_config(config, embed=True)
        if not _tracker_config_has_selectors(config):
            raise HTTPException(
                status_code=400,
                detail=(
                    "topic_monitor projects require at least one selector — a "
                    "free-text description, a predicate, OR a topic / keyword / "
                    "journal in config."
                ),
            )

    db_session = _db_session()
    try:
        # Determine next sort_order.
        # FN-B1-A06-1 (LOW pass, 2026-05-19): Previously used `COUNT(*)`, which
        # collides on non-tail deletes (delete row N+2 of N+4, then create →
        # new row gets sort_order = N+4 = same as the prior tail). Use
        # `MAX(sort_order) + 1` so allocations are monotone and unique even
        # across mid-list deletions. A NULL MAX (empty table) coalesces to -1
        # so the very first project gets sort_order = 0.
        from sqlalchemy import func
        max_order_row = db_session.query(func.coalesce(func.max(Project.sort_order), -1)).scalar()
        sort_order = (max_order_row or -1) + 1
        # P3-2 (2026-06-13): write the (kind, scope, cadence, default_sort_by)
        # triple at creation so a brand-new tracker is first-class IMMEDIATELY.
        # Previously only the legacy `type` was set and kind stayed NULL until the
        # next restart's db.py backfill — so Inbox/badges/scope views (all of
        # which strictly test kind=="tracker") ignored the tracker until reboot.
        # See _audit_2026-06-11/02-ACTION-PLAN.md P3-2.
        from db import LEGACY_PROJECT_TYPE_MAP
        _kind, _scope, _cadence, _sort_by = LEGACY_PROJECT_TYPE_MAP.get(
            proj_type, LEGACY_PROJECT_TYPE_MAP["monthly"]
        )
        # Honor an explicit collection request. The New-Collection wizard sends
        # type="monthly"/"static" + kind="collection"; without this, kind is ignored
        # (taxonomy comes only from LEGACY_PROJECT_TYPE_MAP[type]) so an empty collection
        # lands as a tracker (monthly->tracker). Owner-confirmed 2026-06-15.
        if body.get("kind") == "collection" and proj_type in ("monthly", "static"):
            _kind, _scope, _cadence, _sort_by = LEGACY_PROJECT_TYPE_MAP["static"]
        # Honor an explicit, valid cadence from the wizard (daily/weekly/monthly).
        # Trackers only — collections have no cadence. Without this every
        # topic_monitor tracker was pinned to the MAP default ("weekly") and the
        # wizard's frequency control was a silent no-op. Owner-confirmed 2026-06-15.
        if _kind == "tracker":
            _req_cadence = str(body.get("cadence") or "").strip().lower()
            if _req_cadence in ("daily", "weekly", "monthly"):
                _cadence = _req_cadence
        project = Project(
            name=name,
            type=proj_type,
            kind=_kind,
            scope=_scope,
            cadence=_cadence,
            default_sort_by=_sort_by,
            config=config,
            sort_order=sort_order,
        )
        db_session.add(project)
        db_session.commit()
        db_session.refresh(project)

        # Topic Monitor: backfill historical matching papers
        backfilled = 0
        if proj_type == "topic_monitor" and do_backfill:
            backfilled = _backfill_topic_monitor(db_session, project)

        payload = _project_to_dict(project, db_session=db_session)
        if backfilled:
            payload["backfilled_papers"] = backfilled
        return payload
    finally:
        db_session.close()


def _topic_monitor_haystack(paper: "Paper", analysis_summary: str = "", analysis_keywords: Optional[List[str]] = None) -> str:
    """Build the lowercase text blob used for keyword matching.

    Combines title + abstract + author names + AI summary + AI-extracted
    keywords.  Returns a single lowercased string so callers can do simple
    word-boundary substring tests against it.
    """
    parts: List[str] = []
    if paper.title:
        parts.append(paper.title)
    if paper.abstract:
        parts.append(paper.abstract)
    if paper.authors:
        for a in paper.authors:
            if a:
                parts.append(str(a))
    if analysis_summary:
        parts.append(analysis_summary)
    if analysis_keywords:
        for k in analysis_keywords:
            if k:
                parts.append(str(k))
    return " ".join(parts).lower()


def _keyword_hits(haystack_lower: str, keywords: List[str]) -> bool:
    """Return True if any keyword appears in haystack_lower (word-boundary aware).

    Keywords are matched case-insensitively.  A 1-2 char keyword falls back to
    plain substring match to support codes like "DAS", "FWI", "Mw".  Longer
    keywords use word-boundary regex so "ice" doesn't match "police".

    FIX 2026-05-19 (FN-B2-A06-1): CJK keywords cannot rely on ``\\b`` because
    Python's ``re`` treats CJK chars as ``\\w``, so the boundary never fires
    between two CJK chars and the keyword never matches inside an
    all-CJK haystack. For CJK keywords we fall back to plain substring
    containment (no regex). ASCII keywords keep the word-boundary regex.
    """
    import re
    for kw in keywords:
        kw_l = (kw or "").strip().lower()
        if not kw_l:
            continue
        # Detect CJK (CJK Unified Ideographs, Hiragana/Katakana, Hangul).
        is_cjk = any(
            '一' <= c <= '鿿'
            or '぀' <= c <= 'ヿ'
            or '가' <= c <= '힯'
            for c in kw_l
        )
        if is_cjk:
            if kw_l in haystack_lower:
                return True
        elif len(kw_l) <= 3:
            # Short codes: require word boundaries via simple split
            if re.search(r"\b" + re.escape(kw_l) + r"\b", haystack_lower):
                return True
        else:
            # Longer phrases: word-boundary regex too. Phrases with spaces work
            # naturally because re.escape preserves the spaces.
            if re.search(r"(?:^|\W)" + re.escape(kw_l) + r"(?:$|\W)", haystack_lower):
                return True
    return False


def _backfill_predicate_tracker(db_session, project: Project) -> int:
    """Backfill a NEW-shape (predicate) tracker over the existing local pool.

    BRIEF v2 §1/§3/§6/§7: delegates to ``services.predicates.match_tracker`` so
    the create-time backfill uses the EXACT same matcher (and cached semantic
    query embedding) the live ingest/scheduler path uses — including the cheap-
    first tiered short-circuit. Writes the best predicate score (the semantic
    cosine when one matched) into ``ProjectPaper.match_score`` so the tracker
    view can sort by it. Idempotent: an existing membership has its
    ``match_score`` refreshed upward and is never duplicated.

    Returns the number of NEW memberships added. A fresh match also enqueues the
    lazy full analysis (BRIEF v2 §6) when the paper has none yet.
    """
    from sqlalchemy.exc import IntegrityError
    from services.predicates import match_tracker

    try:
        from services.analyze_service import enqueue_analysis as _enqueue_analysis
    except Exception:  # pragma: no cover — keep backfill working if import shifts.
        _enqueue_analysis = None

    added = 0
    papers = db_session.query(Paper).all()
    for paper in papers:
        # Stash the AI-matched canonical topic names so a cos_topic predicate
        # scores against the AI labels, mirroring the live matcher.
        analysis = getattr(paper, "ai_analysis", None)
        if analysis is not None:
            try:
                tm = analysis.topic_matches or []
                names = [
                    str(m.get("topic")).strip()
                    for m in tm
                    if isinstance(m, dict) and m.get("topic")
                ]
                setattr(paper, "_matched_topic_names", names)
            except Exception:
                pass

        try:
            belongs, score = match_tracker(paper, project)
        except Exception:
            belongs, score = (False, 0.0)
        if not belongs:
            continue

        raw_date = paper.published_date or ""
        folder = (
            str(raw_date)[:7]
            if len(str(raw_date)) >= 7 and str(raw_date)[4] == "-"
            else datetime.utcnow().strftime("%Y-%m")
        )

        existing = (
            db_session.query(ProjectPaper)
            .filter_by(project_id=project.id, paper_id=paper.id)
            .first()
        )
        if existing:
            if score is not None and (
                existing.match_score is None or score > existing.match_score
            ):
                existing.match_score = float(score)
            continue

        pp = ProjectPaper(
            project_id=project.id,
            paper_id=paper.id,
            folder=folder,
            source="tracker_backfill",
            match_score=float(score) if score is not None else None,
        )
        db_session.add(pp)
        try:
            db_session.flush()
            added += 1
        except IntegrityError:
            db_session.rollback()
            continue
        # BRIEF v2 §6 — a fresh match is the trigger for the lazy deep analysis.
        if _enqueue_analysis is not None and analysis is None:
            try:
                _enqueue_analysis(paper.id)
            except Exception:
                pass

    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
    return added


def _backfill_topic_monitor(db_session, project: Project) -> int:
    """Scan the existing pool and add matching papers to a fresh tracker.

    Routing (BRIEF v2 §1): when the config carries the NEW predicate shape
    (``config.predicates``) the work is delegated to
    :func:`_backfill_predicate_tracker` (the predicate matcher, which also writes
    ``match_score``). The LEGACY fast path below still runs for configs with only
    ``topics`` / ``keywords`` / ``journals`` — a paper joins if EITHER
      - its ai_analyses.topic_matches overlap with config.topics, OR
      - any config.keywords[i] appears (case-insensitive, word-boundary) in
        title + abstract + AI summary + AI keywords + author names, OR
      - its journal abbreviation is in config.journals.

    Returns the number of papers added.  Idempotent — uses UniqueConstraint to
    skip dupes silently.  Folder follows the same ``YYYY-MM`` convention as
    monthly projects.
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import text as _sa_text

    cfg = project.config or {}

    # NEW predicate shape -> delegate to the predicate-matcher backfill so the
    # semantic / composable predicates (and match_score) are honoured.
    preds = cfg.get("predicates")
    if isinstance(preds, list) and any(isinstance(p, dict) for p in preds):
        return _backfill_predicate_tracker(db_session, project)

    target_topics = {str(t).strip().lower() for t in (cfg.get("topics") or [])}
    target_keywords = [str(k).strip() for k in (cfg.get("keywords") or []) if str(k).strip()]
    # Phase 2: journal abbreviations live as upper-case strings (e.g. "NATURE")
    # and match against services.journal_abbrev.abbreviate(paper.journal).
    target_journal_abbrevs = {
        str(j).strip().upper()
        for j in (cfg.get("journals") or [])
        if str(j).strip()
    }
    if not target_topics and not target_keywords and not target_journal_abbrevs:
        return 0

    added = 0
    # Iterate every paper with an analysis
    papers = db_session.query(Paper).all()
    for paper in papers:
        row = db_session.execute(
            _sa_text(
                "SELECT topic_matches, summary, keywords FROM ai_analyses WHERE paper_id = :pid LIMIT 1"
            ),
            {"pid": paper.id},
        ).fetchone()
        if not row:
            continue
        topic_matches_raw, summary_text, kw_raw = row
        # ── topic name overlap ────────────────────────────────────────
        # FIX 2026-05-19 (FN-B9-A02-2 / FN-B7-A02-1): use case-insensitive
        # substring containment instead of exact equality so the canonical
        # AI label (e.g. "Fibre-optic seismology / DAS") still matches a
        # project topic configured with a shorter alias ("Fibre-optic
        # seismology") and vice versa. This makes the matcher resilient to
        # COS_TOPICS drift between analyze_service.py, index.html, and the
        # actual Gemini-emitted labels stored in ai_analyses.topic_matches.
        topic_hit = False
        if target_topics and topic_matches_raw:
            try:
                topic_matches = json.loads(topic_matches_raw) if isinstance(topic_matches_raw, str) else topic_matches_raw
            except (TypeError, ValueError):
                topic_matches = None
            if isinstance(topic_matches, list):
                matched = set()
                for item in topic_matches:
                    if isinstance(item, dict):
                        tname = item.get("topic") or item.get("name") or item.get("label") or ""
                        if tname:
                            matched.add(str(tname).strip().lower())
                    elif isinstance(item, str):
                        matched.add(item.strip().lower())
                for pt in target_topics:
                    if not pt:
                        continue
                    for mn in matched:
                        if not mn:
                            continue
                        if pt == mn or pt in mn or mn in pt:
                            topic_hit = True
                            break
                    if topic_hit:
                        break

        # ── keyword text match ────────────────────────────────────────
        keyword_hit = False
        if (not topic_hit) and target_keywords:
            try:
                ai_kw_list = json.loads(kw_raw) if isinstance(kw_raw, str) else (kw_raw or [])
            except (TypeError, ValueError):
                ai_kw_list = []
            if not isinstance(ai_kw_list, list):
                ai_kw_list = []
            haystack = _topic_monitor_haystack(
                paper,
                analysis_summary=str(summary_text or ""),
                analysis_keywords=[str(k) for k in ai_kw_list],
            )
            if _keyword_hits(haystack, target_keywords):
                keyword_hit = True

        # ── 3rd: journal abbreviation match (Phase 2) ─────────────────
        # Only evaluated when topic and keyword both miss — the three
        # hit types are independent OR-branches and any one is sufficient
        # to include the paper.
        journal_hit = False
        if (not topic_hit and not keyword_hit) and target_journal_abbrevs:
            from services.journal_abbrev import abbreviate as _abbrev
            paper_abbrev = _abbrev(paper.journal or "")
            if paper_abbrev and paper_abbrev.upper() in target_journal_abbrevs:
                journal_hit = True

        if not topic_hit and not keyword_hit and not journal_hit:
            continue

        # Compute folder (YYYY-MM)
        raw_date = paper.published_date or ""
        folder = str(raw_date)[:7] if len(str(raw_date)) >= 7 and str(raw_date)[4] == "-" else datetime.utcnow().strftime("%Y-%m")

        pp = ProjectPaper(
            project_id=project.id,
            paper_id=paper.id,
            folder=folder,
            source="topic_monitor_backfill",
        )
        db_session.add(pp)
        try:
            db_session.flush()
            added += 1
        except IntegrityError:
            db_session.rollback()
            # already a member; skip
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
    return added


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    db_session = _db_session()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        return _project_to_dict(project, db_session=db_session)
    finally:
        db_session.close()


@app.post("/api/projects/{project_id}/mark_viewed")
def mark_project_viewed(project_id: str):
    """Stamp ``project.last_viewed_at = utcnow()`` to clear the unread badge.

    Per ``changelog/04-feature-design/01-tracker-data-model.md`` §4 the
    tracker sidebar's "N new since last visit" badge is the count of
    ProjectPaper rows whose ``added_at`` is newer than the project's
    ``last_viewed_at`` (or every row when ``last_viewed_at IS NULL``).
    Opening the tracker view from the frontend POSTs here so the next
    ``GET /api/projects/{id}`` returns ``unread_count == 0``.

    Body: none. Response::

        {"ok": true, "last_viewed_at": "2026-05-25T12:34:56.789012"}

    The endpoint is a no-op-friendly idempotent stamp — collections also
    accept it (the column exists on every project) even though their
    ``unread_count`` is hard-coded to 0 in ``_project_to_dict``. Returning
    200 for collections keeps the frontend's "open project" hook trivial
    (no per-kind branching).
    """
    db_session = _db_session()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        now = datetime.utcnow()
        project.last_viewed_at = now
        # Touch updated_at so SSE / polling clients notice the change.
        project.updated_at = now
        db_session.commit()
        return {"ok": True, "last_viewed_at": now.isoformat()}
    finally:
        db_session.close()


@app.post("/api/trackers/run_now")
def trigger_tracker_pass():
    """Manually trigger one scheduler pass for all due trackers.

    Returns ``{"trackers_checked", "trackers_run", "papers_matched"}`` — the
    same stats dict the daemon writes to logs on each interval.

    Use cases:
      * Manual "Refresh trackers" button in the UI when the user can't wait
        the full cadence delta.
      * Smoke-testing the scheduler via ``curl`` (see PR description).
    """
    return run_tracker_pass_once()


@app.post("/api/projects/{project_id}/run_now")
def trigger_tracker_for_project(project_id: str):
    """Force-run the matcher for one tracker, ignoring its cadence.

    Equivalent to ``/api/trackers/run_now`` but scoped to a single project so
    the user can refresh just one tracker without paying for a global pass.
    Returns ``{"ok": true, "papers_matched": N, "project_id": "..."}`` on
    success; ``{"ok": false, "error": <reason>}`` if the project doesn't
    exist, isn't a tracker, or is archived.
    """
    result = run_tracker_pass_for_project(project_id)
    if not result.get("ok"):
        err = result.get("error", "unknown")
        # Surface the canonical 404 for the missing-project case so clients
        # can distinguish "no such tracker" from "not a tracker".
        if err == "project_not_found":
            raise HTTPException(status_code=404, detail="Project not found.")
        if err == "not_a_tracker":
            raise HTTPException(status_code=400, detail="Project is not a tracker.")
        if err == "archived":
            raise HTTPException(status_code=400, detail="Project is archived.")
    return result


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, body: dict):
    """Rename, reorder, archive, or update the config of a project.

    Body (all optional): {name, sort_order, config, archived}.

    - ``config``: PATCH SEMANTICS — wholesale REPLACES the existing config
      dict (no shallow or deep merge). Pass the full new dict, not a partial
      diff. Existing keys are not preserved. To update one key, the caller
      must read the current config first and resubmit the union.
    - ``type`` cannot be changed via PATCH (rejected with 400 — see
      ALLOWED_PATCH_FIELDS below).
    - ``is_default`` is an internal hard-flag and is rejected with 400.
    """
    # FN-B1-A04-1 / FN-B1-A08-1 — `is_default` is an internal hard-flag that
    # protects the seeded "Monthly Literature Review" project from deletion.
    # Reject any PATCH attempt to flip it (which would otherwise route the
    # client around the delete guard).
    if "is_default" in body:
        raise HTTPException(
            status_code=400,
            detail="is_default cannot be changed via PATCH.",
        )
    # FN-B1-A15-1 — Reject unknown fields explicitly. Previously body like
    # ``{"type": "static"}`` silently no-op'd because nothing in this handler
    # touched ``project.type``. The 200 response misled clients into believing
    # the project's type had changed. Whitelist allowed keys (the is_default
    # check above remains as an explicit 400 with its own message).
    ALLOWED_PATCH_FIELDS = {"name", "sort_order", "config", "archived", "icon"}
    unknown = set(body.keys()) - ALLOWED_PATCH_FIELDS - {"is_default"}
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported fields in PATCH body: {sorted(unknown)}. "
                f"Allowed: {sorted(ALLOWED_PATCH_FIELDS)}"
            ),
        )
    db_session = _db_session()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        if "name" in body:
            project.name = (body["name"] or "").strip() or project.name
        if "icon" in body:
            # Custom emoji prefix; empty string resets to the type default.
            project.icon = ((body["icon"] or "").strip()[:16]) or None
        if "sort_order" in body:
            project.sort_order = int(body["sort_order"])
        if "config" in body:
            new_config = body["config"]
            # FN-B2-A03-1 — When patching a tracker's config, enforce the same
            # "at least one selector" rule that POST enforces. Without this
            # guard, a client could PATCH config to empty term lists which
            # silently disables the monitor (the backfill / fetch hooks see an
            # empty selector set and produce no matches).
            #
            # BRIEF v2 §1/§3/§7 — a tracker (kind='tracker' OR legacy
            # type='topic_monitor') may carry the NEW predicate shape (match +
            # predicates + free-text description). Normalise it here so the
            # semantic description is (re-)embedded into the predicate's cached
            # query_embedding and legacy fields are cleaned/preserved, exactly
            # like create_project. Other project kinds keep their config as-is.
            is_tracker = (
                getattr(project, "kind", None) == "tracker"
                or project.type == "topic_monitor"
            )
            if is_tracker and isinstance(new_config, dict):
                new_config = _normalize_tracker_config(new_config, embed=True)
                if not _tracker_config_has_selectors(new_config):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "tracker projects require at least one selector — a "
                            "free-text description, a predicate, OR a topic / "
                            "keyword / journal in config; empty config would "
                            "silently disable the monitor."
                        ),
                    )
            project.config = new_config
        if "archived" in body:
            # FN-B1-A07-1 (LOW pass, 2026-05-19): Only stamp `archived_at` on
            # the *first* archive; re-archiving an already-archived project
            # used to overwrite the original timestamp, which broke the
            # "archived 30 days ago — auto-delete?" audit semantics.
            if body["archived"]:
                if project.archived_at is None:
                    project.archived_at = datetime.utcnow()
            else:
                project.archived_at = None
        project.updated_at = datetime.utcnow()
        db_session.commit()
        db_session.refresh(project)
        return _project_to_dict(project, db_session=db_session)
    finally:
        db_session.close()


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    """Delete a project (papers are NOT deleted, only their project membership).

    FN-B1-A04-1 / FN-B1-A08-1 — Two-layer guard:
      1. Hard-flag check on Project.is_default (the seeded "Monthly Literature
         Review" row carries is_default=1 and can NEVER be deleted regardless
         of how many other projects exist).
      2. Defence-in-depth: also keep the original "last non-archived project"
         check so even a brand-new DB before backfill stays safe.
    """
    db_session = _db_session()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        # Layer 1: never delete the default project.
        if getattr(project, "is_default", False):
            raise HTTPException(
                status_code=409,
                detail="Cannot delete the default project.",
            )
        # Layer 2 (defence-in-depth): prevent deleting the last non-archived
        # project. Survives the unlikely case where is_default was never set
        # (e.g. legacy DB whose _backfill_default_project didn't run yet).
        non_archived = db_session.query(Project).filter(
            Project.archived_at == None, Project.id != project_id  # noqa: E711
        ).count()
        if non_archived == 0:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete the last project. Create another project first.",
            )
        # FN-B1-A11-1 — Use a bulk DELETE with rowcount so concurrent callers
        # racing for the same project_id can be distinguished. Without this,
        # 5 simultaneous DELETEs all return 200 (last 4 are silent no-ops
        # because db_session.delete() on an already-evicted row is a no-op).
        result = db_session.query(Project).filter(Project.id == project_id).delete()
        db_session.commit()
        if result == 0:
            raise HTTPException(status_code=404, detail="Project not found (already deleted).")
        return {"deleted": project_id}
    finally:
        db_session.close()


@app.post("/api/projects/{project_id}/papers/{paper_id}")
def add_paper_to_project(project_id: str, paper_id: str, body: dict = {}):
    """Add a paper to a project.  Body: {folder?: str, source?: str}"""
    db_session = _db_session()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")

        # Derive default folder by project type.
        # FN-B10-A01-1 — Previously only ``monthly`` got an auto-derived
        # folder; static / topic_monitor projects fell through with
        # ``folder=""`` which polluted the UI's folder-grouped view.
        # Align with the BibTeX importer's ``"imported"`` convention for
        # static projects, and reuse the YYYY-MM derivation for
        # topic_monitor (matches _backfill_topic_monitor).
        folder = (body.get("folder") or "").strip()
        if not folder:
            if project.type == "monthly":
                raw_date = paper.published_date or (
                    paper.updated_at.strftime("%Y-%m") if paper.updated_at else ""
                )
                folder = str(raw_date)[:7] if raw_date else datetime.utcnow().strftime("%Y-%m")
            elif project.type == "static":
                folder = "imported"
            elif project.type == "topic_monitor":
                raw_date = paper.published_date or ""
                folder = (
                    str(raw_date)[:7]
                    if len(str(raw_date)) >= 7 and str(raw_date)[4] == "-"
                    else datetime.utcnow().strftime("%Y-%m")
                )

        from sqlalchemy.exc import IntegrityError
        pp = ProjectPaper(
            project_id=project_id,
            paper_id=paper_id,
            folder=folder,
            source=body.get("source", "manual"),
        )
        db_session.add(pp)
        try:
            db_session.commit()
        except IntegrityError:
            db_session.rollback()
            # Already a member — update folder/source if changed
            existing = db_session.query(ProjectPaper).filter_by(
                project_id=project_id, paper_id=paper_id
            ).first()
            if existing and folder:
                existing.folder = folder
                db_session.commit()
        return {"project_id": project_id, "paper_id": paper_id, "folder": folder}
    finally:
        db_session.close()


@app.post("/api/projects/{project_id}/folders")
def create_subfolder(project_id: str, body: dict = Body(...)):
    """Create an empty subfolder under a project.

    Body: ``{"folder": "Important"}``.

    Folder names are stored in ``Project.config.custom_folders`` (a list of
    strings). Empty folders appear in the sidebar with count=0 and can
    receive papers via the move endpoint below.
    """
    folder_name = (body or {}).get("folder") or ""
    folder_name = str(folder_name).strip()
    if not folder_name:
        raise HTTPException(status_code=400, detail="folder name required")
    if len(folder_name) > 80:
        raise HTTPException(status_code=400, detail="folder name too long (max 80 chars)")
    # Reject names that conflict with auto-derived YYYY-MM format on monthly projects
    # (would be visually confusing) — allow on other types.
    db_session = _db_session()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        cfg = dict(project.config or {})
        custom = list(cfg.get("custom_folders") or [])
        if folder_name in custom:
            return {"project_id": project_id, "folder": folder_name, "status": "already_exists"}
        # Also check it doesn't clash with an auto-derived folder name from existing papers
        existing_names = {
            r[0] for r in db_session.query(ProjectPaper.folder)
            .filter(ProjectPaper.project_id == project_id).distinct().all()
        }
        if folder_name in existing_names:
            return {"project_id": project_id, "folder": folder_name, "status": "already_in_use"}
        custom.append(folder_name)
        cfg["custom_folders"] = custom
        project.config = cfg
        project.updated_at = datetime.utcnow()
        db_session.commit()
        return {"project_id": project_id, "folder": folder_name, "status": "created"}
    finally:
        db_session.close()


@app.delete("/api/projects/{project_id}/folders/{folder_name}")
def delete_subfolder(project_id: str, folder_name: str):
    """Delete a subfolder. Papers inside it move to the project's default folder.

    For monthly/topic_monitor projects the default is the paper's YYYY-MM;
    for static projects it's "imported".
    """
    db_session = _db_session()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        # Move any papers OUT of this folder
        papers_to_move = db_session.query(ProjectPaper).filter_by(
            project_id=project_id, folder=folder_name
        ).all()
        moved = 0
        for pp in papers_to_move:
            paper = db_session.get(Paper, pp.paper_id)
            if not paper:
                continue
            if project.type == "monthly" or project.type == "topic_monitor":
                raw = paper.published_date or ""
                pp.folder = str(raw)[:7] if len(str(raw)) >= 7 and str(raw)[4] == "-" else datetime.utcnow().strftime("%Y-%m")
            else:  # static
                pp.folder = "imported"
            moved += 1
        # Remove from custom_folders list
        cfg = dict(project.config or {})
        custom = [f for f in (cfg.get("custom_folders") or []) if f != folder_name]
        cfg["custom_folders"] = custom
        project.config = cfg
        project.updated_at = datetime.utcnow()
        db_session.commit()
        return {"project_id": project_id, "deleted_folder": folder_name, "papers_moved": moved}
    finally:
        db_session.close()


@app.patch("/api/projects/{project_id}/papers/{paper_id}/folder")
def move_paper_to_folder(project_id: str, paper_id: str, body: dict = Body(...)):
    """Move a paper to a different subfolder within the same project.

    Body: ``{"folder": "Important"}``. If the folder doesn't exist as a
    custom folder yet, it's auto-created.
    """
    new_folder = (body or {}).get("folder") or ""
    new_folder = str(new_folder).strip()
    if not new_folder:
        raise HTTPException(status_code=400, detail="folder name required")
    db_session = _db_session()
    try:
        pp = db_session.query(ProjectPaper).filter_by(
            project_id=project_id, paper_id=paper_id
        ).first()
        if pp is None:
            raise HTTPException(status_code=404, detail="Paper not in this project.")
        pp.folder = new_folder
        # Auto-register in custom_folders so the folder persists even if all
        # papers get moved out later
        project = db_session.get(Project, project_id)
        if project is not None:
            cfg = dict(project.config or {})
            custom = list(cfg.get("custom_folders") or [])
            if new_folder not in custom:
                custom.append(new_folder)
                cfg["custom_folders"] = custom
                project.config = cfg
                project.updated_at = datetime.utcnow()
        db_session.commit()
        return {"project_id": project_id, "paper_id": paper_id, "folder": new_folder}
    finally:
        db_session.close()


@app.delete("/api/projects/{project_id}/papers/{paper_id}")
def remove_paper_from_project(project_id: str, paper_id: str):
    """Remove a paper from a project (does not delete the paper itself)."""
    db_session = _db_session()
    try:
        pp = db_session.query(ProjectPaper).filter_by(
            project_id=project_id, paper_id=paper_id
        ).first()
        if pp is None:
            raise HTTPException(status_code=404, detail="Paper is not a member of this project.")
        db_session.delete(pp)
        db_session.commit()
        return {"removed": paper_id, "project_id": project_id}
    finally:
        db_session.close()


@app.post("/api/projects/{project_id}/import_bibtex")
async def import_bibtex_into_project(
    project_id: str,
    file: UploadFile = File(...),
):
    """Import a ``.bib`` / ``.ris`` file into an existing project.

    Each entry is parsed and either matched to an existing Paper (by DOI or
    normalized title+year) — in which case it is added to the project — or a
    new metadata-only Paper is created (no PDF) and added.

    The file is hashed and recorded in ``project_imports`` so re-uploading the
    same file is a no-op.

    Returns a report:
      {
        "imported": <int>,             # newly added to project (matched or new)
        "created_papers": <int>,       # metadata-only papers we had to create
        "matched_existing": <int>,     # papers already in library, just linked
        "skipped_duplicate_file": <bool>,  # if same hash already imported
        "warnings": [...]
      }
    """
    import sys
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
    from services.zotero_import import (
        parse_bibtex,
        _find_existing_paper,
        _create_paper_from_entry,
        _fill_missing_metadata,
        _clean_bib_value,
        ZoteroImportError,
    )

    db_session = _db_session()
    try:
        project = db_session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        if project.type != "static":
            raise HTTPException(
                status_code=400,
                detail=f"import_bibtex only supported for static projects (got {project.type!r}).",
            )

        # Read and hash the upload
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file.")
        try:
            text_content = raw.decode("utf-8", errors="replace")
        except Exception as e:  # pragma: no cover
            raise HTTPException(status_code=400, detail=f"Could not decode file: {e}")

        source_hash = hashlib.sha256(raw).hexdigest()
        source_filename = file.filename or "import.bib"

        # Idempotency: same file already imported into THIS project?
        existing_import = db_session.query(ProjectImport).filter_by(
            project_id=project_id, source_hash=source_hash
        ).first()
        if existing_import is not None:
            return {
                "imported": 0,
                "created_papers": 0,
                "matched_existing": 0,
                "skipped_duplicate_file": True,
                "previous_import_at": existing_import.imported_at.isoformat() if existing_import.imported_at else None,
                "warnings": [
                    f"This file (sha256 {source_hash[:8]}…) was already imported into this project; nothing to do.",
                ],
            }

        # Decide RIS vs BibTeX. Minimal RIS support: convert to a synthetic
        # BibTeX-shaped entry list using a simple field-by-field reader.
        warnings: List[str] = []
        if source_filename.lower().endswith(".ris") or text_content.lstrip().startswith(("TY  -", "TY-")):
            try:
                entries = _ris_to_entries(text_content)
            except ZoteroImportError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not parse RIS: {e}. Check the file for malformed tags or other syntax errors.",
                )
            if not entries:
                warnings.append("RIS parser found no entries; check that the file uses standard `TY  - ` tags.")
        else:
            try:
                entries = parse_bibtex(text_content, bib_path=source_filename)
            except ZoteroImportError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not parse BibTeX: {e}. Check the file for unclosed braces or other syntax errors.",
                )
            if not entries:
                warnings.append("BibTeX parser found no @entries; check syntax.")

        if not entries:
            return {
                "imported": 0,
                "created_papers": 0,
                "matched_existing": 0,
                "skipped_duplicate_file": False,
                "warnings": warnings,
            }

        added_to_project = 0
        matched_existing = 0
        created_papers = 0
        folder_label = "imported"  # static project default folder

        # FN-B3-A14-1 — Batch ProjectPaper flushes every BATCH_SIZE rows
        # instead of one per entry. On 5000-entry imports per-row flush was
        # dominating wall time (~22 entries/s vs ~145 e/s for 500). The
        # per-row flush in `_create_paper_from_entry` (services/zotero_import.py)
        # must stay (Paper.id is read for the ProjectPaper FK below), but the
        # ProjectPaper flushes — the hot path that fires once per entry
        # regardless of dedup outcome — are now amortized.
        BATCH_SIZE = 100
        pending_pp_ids: List[str] = []  # paper_ids queued in the current chunk

        def _flush_pp_batch() -> None:
            nonlocal added_to_project
            if not pending_pp_ids:
                return
            try:
                db_session.flush()
                added_to_project += len(pending_pp_ids)
                pending_pp_ids.clear()
            except IntegrityError:
                # A duplicate (project_id, paper_id) pair collided somewhere
                # in the chunk. Roll back, replay row-by-row to attribute the
                # failure correctly, and keep the rest (matches pre-fix
                # contract — duplicates are silently skipped).
                db_session.rollback()
                replay = list(pending_pp_ids)
                pending_pp_ids.clear()
                for replay_paper_id in replay:
                    replay_pp = ProjectPaper(
                        project_id=project_id,
                        paper_id=replay_paper_id,
                        folder=folder_label,
                        source="import",
                    )
                    db_session.add(replay_pp)
                    try:
                        db_session.flush()
                        added_to_project += 1
                    except IntegrityError:
                        db_session.rollback()
                        # already a member; not counted as added

        for entry in entries:
            fields = entry.get("fields") or {}
            title = _clean_bib_value(fields.get("title") or "")
            # FN-B3-A02-1: surface parser-level per-entry warnings (e.g. missing
            # RIS `ER` terminator) into the API response so callers know the
            # source file was malformed.
            entry_warning = entry.get("_warning")
            if entry_warning:
                warnings.append(
                    f"{entry_warning} (citekey={entry.get('citekey')!r}, title={title!r})"
                )
            if not title:
                warnings.append(f"Skipped entry with no title (citekey={entry.get('citekey')!r})")
                continue

            existing = _find_existing_paper(db_session, fields)
            if existing is not None:
                # Fill in any missing metadata from the BibTeX entry without
                # overwriting existing values (idempotent merge).
                _fill_missing_metadata(existing, fields)
                paper_id = existing.id
                matched_existing += 1
            else:
                # Create a metadata-only Paper (no PDF, no abstract analysis yet)
                try:
                    paper = _create_paper_from_entry(db_session, entry)
                    paper_id = paper.id
                    created_papers += 1
                except Exception as e:
                    warnings.append(f"Failed to create paper for entry {entry.get('citekey')!r}: {e}")
                    continue

            # Link to project (folder = 'imported' for static projects).
            # Flush is batched — see _flush_pp_batch above.
            pp = ProjectPaper(
                project_id=project_id,
                paper_id=paper_id,
                folder=folder_label,
                source="import",
            )
            db_session.add(pp)
            pending_pp_ids.append(paper_id)
            if len(pending_pp_ids) >= BATCH_SIZE:
                _flush_pp_batch()

        # Flush the remainder of the last (partial) chunk.
        _flush_pp_batch()

        # Record the import for dedup
        import_row = ProjectImport(
            project_id=project_id,
            source_hash=source_hash,
            source_filename=source_filename,
            paper_count=added_to_project,
        )
        db_session.add(import_row)

        try:
            db_session.commit()
        except IntegrityError as e:
            # FN-B3-A13-2 — Don't leak raw SQL / bound parameters to clients.
            # Log full traceback for server-side debugging.
            db_session.rollback()
            print(f"[import_bibtex] IntegrityError: {e!r}", file=sys.stderr)
            raise HTTPException(
                status_code=409,
                detail="Constraint violation; check file for duplicate imports.",
            )
        except SQLAlchemyError as e:
            db_session.rollback()
            print(f"[import_bibtex] SQLAlchemyError: {e!r}", file=sys.stderr)
            raise HTTPException(
                status_code=500,
                detail="Database error during import; check server logs.",
            )

        return {
            "imported": added_to_project,
            "created_papers": created_papers,
            "matched_existing": matched_existing,
            "skipped_duplicate_file": False,
            "warnings": warnings,
        }
    finally:
        db_session.close()


def _ris_to_entries(text_content: str) -> List[Dict[str, Any]]:
    """Minimal RIS → entries[] adapter.

    Maps RIS tags to BibTeX-shaped fields so the rest of the import pipeline
    can reuse ``_find_existing_paper`` etc.  Only the fields we care about
    (TI/T1, AU, DO, JO/T2/JF, PY/Y1, DA, AB/N2, UR, KW) are extracted.
    """
    entries: List[Dict[str, Any]] = []
    current: Dict[str, str] = {}
    authors: List[str] = []
    keywords: List[str] = []
    # FN-B3-A12-2: a record must begin with TY before any field is accepted.
    # FN-B3-A02-1: track whether ER terminated the in-flight record so we can
    # warn on trailing entries that lack an ER terminator.
    saw_ty = False
    saw_er = False

    def _commit():
        nonlocal current, authors, keywords, saw_ty, saw_er
        # FN-B3-A12-2: drop orphan records that never saw a TY tag.
        if saw_ty and current.get("title"):
            if authors:
                current["author"] = " and ".join(authors)
            if keywords:
                current["keywords"] = "; ".join(keywords)
            entry: Dict[str, Any] = {
                "entry_type": "article",
                "citekey": current.get("_citekey") or "",
                "fields": {k: v for k, v in current.items() if not k.startswith("_")},
                "bib_path": "ris-import",
                "bib_dir": "",
            }
            if current.get("_warning"):
                # Surface per-entry warning to the caller (FN-B3-A02-1).
                entry["_warning"] = current["_warning"]
            entries.append(entry)
        current = {}
        authors = []
        keywords = []
        saw_ty = False
        saw_er = False

    for raw_line in text_content.splitlines():
        line = raw_line.strip("\r\n")
        if not line.strip():
            continue
        # RIS line format: "TY  - JOUR" (tag, two spaces, dash, space, value)
        if len(line) < 6 or line[2:5] not in ("  -", " - "):
            continue
        tag = line[:2].strip().upper()
        value = line[5:].strip() if len(line) > 5 else ""

        if tag == "TY":
            _commit()
            saw_ty = True
            current["_citekey"] = ""
            continue
        if tag == "ER":
            saw_er = True
            _commit()
            continue
        # FN-B3-A12-2: ignore data lines that appear before a TY tag.
        if not saw_ty:
            continue
        if tag in ("T1", "TI"):
            current["title"] = value
        elif tag in ("AU", "A1"):
            authors.append(value)
        elif tag == "DO":
            current["doi"] = value
        elif tag in ("JO", "JF", "T2"):
            current["journal"] = value
        elif tag == "PY":
            current["date"] = value  # PY = primary publication date, wins over Y1
        elif tag in ("DA", "Y1"):
            current["year"] = value  # Y1/DA = secondary; only used if PY absent
        elif tag in ("AB", "N2"):
            current["abstract"] = (current.get("abstract", "") + " " + value).strip()
        elif tag == "UR":
            current["url"] = value
        elif tag == "KW":
            keywords.append(value)
        elif tag == "ID":
            current["_citekey"] = value

    # FN-B3-A02-1: still commit the trailing entry, but flag it so the import
    # endpoint can surface a per-entry warning to the caller.
    if saw_ty and current.get("title") and not saw_er:
        current["_warning"] = "RIS entry committed without ER terminator"
    _commit()
    return entries


@app.get("/api/export/ris")
def export_ris(
    topic: Optional[str] = Query(default=None),
    journal: Optional[str] = Query(default=None),
    institution: Optional[str] = Query(default=None),
    has_pdf: Optional[bool] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="relevance"),
    q: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
):
    db_session = _db_session()
    try:
        result = get_filtered_papers(
            db_session,
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            q=q,
            project_id=project_id,
            folder=folder,
        )
        ris_text = _build_ris_text(result["filtered"])
        return Response(
            content=ris_text,
            media_type="application/x-research-info-systems",
            headers={"Content-Disposition": 'attachment; filename="seismo_hub_export.ris"'},
        )
    finally:
        db_session.close()


def _export_filter_snapshot(
    topic, journal, institution, has_pdf, date_from, date_to, sort_by, q=None
) -> Dict[str, Any]:
    snap = {
        "topic": topic or None,
        "journal": journal or None,
        "institution": institution or None,
        "has_pdf": has_pdf,
        "date_from": date_from or None,
        "date_to": date_to or None,
        "sort_by": _normalize_sort_by(sort_by),  # export snapshot has no project context — mode-default fallback applies
    }
    if q:
        snap["q"] = q
    return snap


@app.get("/api/export/bibtex")
def export_bibtex(
    topic: Optional[str] = Query(default=None),
    journal: Optional[str] = Query(default=None),
    institution: Optional[str] = Query(default=None),
    has_pdf: Optional[bool] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="relevance"),
    q: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
):
    """Export the current filtered set as a BibTeX `.bib` file."""
    db_session = _db_session()
    try:
        result = get_filtered_papers(
            db_session,
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            q=q,
            project_id=project_id,
            folder=folder,
        )
        bibtex_text = papers_to_bibtex_text(result["filtered"])
        return Response(
            content=bibtex_text,
            media_type="application/x-bibtex",
            headers={"Content-Disposition": 'attachment; filename="seismo_hub_export.bib"'},
        )
    finally:
        db_session.close()


@app.get("/api/export/csl")
def export_csl_json(
    topic: Optional[str] = Query(default=None),
    journal: Optional[str] = Query(default=None),
    institution: Optional[str] = Query(default=None),
    has_pdf: Optional[bool] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="relevance"),
    q: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
):
    """Export as CSL-JSON — round-trippable with Zotero / Better BibTeX."""
    db_session = _db_session()
    try:
        result = get_filtered_papers(
            db_session,
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            q=q,
            project_id=project_id,
            folder=folder,
        )
        csl_payload = papers_to_csl_json(result["filtered"])
        return Response(
            content=json.dumps(csl_payload, ensure_ascii=False, indent=2),
            media_type="application/vnd.citationstyles.csl+json",
            headers={"Content-Disposition": 'attachment; filename="seismo_hub_export.csl.json"'},
        )
    finally:
        db_session.close()


@app.get("/api/export/pdfs.zip")
def export_pdfs_zip(
    topic: Optional[str] = Query(default=None),
    journal: Optional[str] = Query(default=None),
    institution: Optional[str] = Query(default=None),
    has_pdf: Optional[bool] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="relevance"),
    q: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
):
    """Stream a ZIP of every PDF in the current filtered set.

    Uses `services.export_service.stream_pdfs_zip` so memory stays flat
    regardless of library size.
    """
    from fastapi.responses import StreamingResponse

    db_session = _db_session()
    try:
        result = get_filtered_papers(
            db_session,
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            q=q,
            project_id=project_id,
            folder=folder,
        )
        # Capture filtered list now while the session is open; the streaming
        # generator below doesn't need the session anymore.
        papers = result["filtered"]
    finally:
        db_session.close()

    return StreamingResponse(
        stream_pdfs_zip(papers, DOWNLOADS_DIR),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="seismo_hub_pdfs.zip"'},
    )


@app.get("/api/export/all.zip")
def export_all_zip(
    topic: Optional[str] = Query(default=None),
    journal: Optional[str] = Query(default=None),
    institution: Optional[str] = Query(default=None),
    has_pdf: Optional[bool] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="relevance"),
    q: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
):
    """Stream `papers.bib` + `papers.ris` + `papers.csl.json` + `manifest.json`
    + every available PDF inside a single ZIP. Honours the current filters.
    """
    from fastapi.responses import StreamingResponse

    db_session = _db_session()
    try:
        result = get_filtered_papers(
            db_session,
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            q=q,
            project_id=project_id,
            folder=folder,
        )
        papers = result["filtered"]
    finally:
        db_session.close()

    snapshot = _export_filter_snapshot(
        topic, journal, institution, has_pdf, date_from, date_to, sort_by, q=q
    )
    if project_id:
        snapshot["project_id"] = project_id
    if folder:
        snapshot["folder"] = folder
    return StreamingResponse(
        stream_all_zip(papers, DOWNLOADS_DIR, filter_snapshot=snapshot),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="seismo_hub_export.zip"'},
    )


@app.get("/api/export/preview")
def export_preview(
    topic: Optional[str] = Query(default=None),
    journal: Optional[str] = Query(default=None),
    institution: Optional[str] = Query(default=None),
    has_pdf: Optional[bool] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default="relevance"),
    q: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
):
    """Live scope counter for the Export ▾ dropdown.

    Returns `{paper_count, papers_with_pdf}` so the dropdown header can show
    "Will export 47 papers (12 with PDFs)" before the user actually clicks.
    """
    db_session = _db_session()
    try:
        result = get_filtered_papers(
            db_session,
            topic=topic,
            journal=journal,
            institution=institution,
            has_pdf=has_pdf,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            q=q,
            project_id=project_id,
            folder=folder,
        )
        papers = result["filtered"]
    finally:
        db_session.close()

    snapshot = _export_filter_snapshot(
        topic, journal, institution, has_pdf, date_from, date_to, sort_by, q=q
    )
    if project_id:
        snapshot["project_id"] = project_id
    if folder:
        snapshot["folder"] = folder
    return {
        "paper_count": len(papers),
        "papers_with_pdf": sum(1 for p in papers if p.get("has_pdf")),
        "filters": snapshot,
    }


@app.get("/api/runtime")
def get_runtime():
    return _runtime_snapshot()


# ------------------------------------------------------------------
# External PDF reader integration (M1 must-do #1)
# ------------------------------------------------------------------

_DEFAULT_READER_STATE_KEY = "default_pdf_reader_id"


@app.get("/api/readers/available")
def list_available_readers(refresh: bool = Query(default=False)):
    """List PDF-capable apps on the host, plus the current default."""
    readers = enumerate_readers(force_refresh=refresh)
    db_session = _db_session()
    try:
        current_default = _get_state(db_session, _DEFAULT_READER_STATE_KEY)
    finally:
        db_session.close()
    return {
        "readers": [r.to_dict() for r in readers],
        "default_reader_id": current_default or "system_default",
        "platform": next((r.platform for r in readers), "Unknown"),
    }


@app.post("/api/readers/default")
def set_default_reader(payload: Dict[str, Any]):
    """Persist the user's preferred PDF reader.

    Body: `{"reader_id": "<id>"}`. Use "system_default" to clear the
    explicit preference and fall back to the OS default.
    """
    reader_id = (payload or {}).get("reader_id")
    if not reader_id or not isinstance(reader_id, str):
        raise HTTPException(status_code=400, detail="reader_id is required.")

    # Validate against enumerated readers (rejects command-injection attempts).
    if reader_id != "system_default":
        known = {r.id for r in enumerate_readers()}
        if reader_id not in known:
            raise HTTPException(status_code=400, detail=f"Unknown reader_id: {reader_id}")

    db_session = _db_session()
    try:
        _set_state(db_session, _DEFAULT_READER_STATE_KEY, reader_id)
        db_session.commit()
    finally:
        db_session.close()
    return {"status": "ok", "default_reader_id": reader_id}


@app.get("/api/watch-folders")
def list_watch_folders():
    """Return the user's watched folder list + observer status."""
    db_session = _db_session()
    try:
        folders = watch_folder_service.load_watch_folders(db_session)
    finally:
        db_session.close()
    return {
        "folders": folders,
        "observer": watch_folder_service.observer_status(),
    }


@app.post("/api/watch-folders")
def set_watch_folders(payload: Dict[str, Any]):
    """Replace the watched folder list. Body: `{"folders": ["/path", ...]}`."""
    raw = (payload or {}).get("folders")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="folders must be a list of strings.")
    db_session = _db_session()
    try:
        saved = watch_folder_service.save_watch_folders(
            db_session, [str(p) for p in raw]
        )
    finally:
        db_session.close()
    # Restart the observer with the new list (non-blocking).
    threading.Thread(
        target=lambda: watch_folder_service.start_watcher(saved),
        daemon=True,
        name="seismo-watch-folder-restart",
    ).start()
    return {"status": "ok", "folders": saved}


@app.post("/api/watch-folders/rescan")
def rescan_watch_folders():
    """Run a one-shot recursive scan over every configured folder.

    Useful after the app was offline or when the user added a new folder
    and wants to ingest everything already inside it.
    """
    db_session = _db_session()
    try:
        folders = watch_folder_service.load_watch_folders(db_session)
    finally:
        db_session.close()
    if not folders:
        return {"status": "ok", "scanned": [], "counts": {}}

    aggregate = {"seen": 0, "ingested_new": 0, "ingested_existing": 0, "duplicates": 0, "errors": 0}
    per_folder: List[Dict[str, Any]] = []
    for folder in folders:
        counts = watch_folder_service.scan_folder_once(Path(folder))
        per_folder.append({"folder": folder, "counts": counts})
        for k, v in counts.items():
            aggregate[k] = aggregate.get(k, 0) + v
    return {"status": "ok", "scanned": per_folder, "counts": aggregate}


@app.post("/api/papers/{paper_id}/open")
def open_paper_pdf(paper_id: str, payload: Optional[Dict[str, Any]] = None):
    """Launch this paper's PDF in an external reader.

    Body (optional): `{"reader_id": "<id>", "page": <int>}`. When `reader_id`
    is omitted, uses the user's persisted default; falling back to the OS
    default when none is set.
    """
    payload = payload or {}
    requested_reader = payload.get("reader_id")
    page = payload.get("page")
    if page is not None:
        try:
            page = int(page)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="page must be an integer.")
        if page < 1:
            raise HTTPException(status_code=400, detail="page must be >= 1.")

    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        if not paper.pdf_path:
            raise HTTPException(status_code=409, detail="This paper has no local PDF yet.")

        # Resolve reader_id: explicit override > persisted default > OS default.
        reader_id = requested_reader or _get_state(
            db_session, _DEFAULT_READER_STATE_KEY
        ) or "system_default"

        try:
            result = launch_pdf(Path(paper.pdf_path), reader_id=reader_id, page=page)
        except PdfLauncherError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Telemetry: bump last_opened_at so the UI can render "Read X ago".
        paper.last_opened_at = datetime.utcnow()
        # 2026-06-15: opening a PDF auto-marks the paper read (the manual read(✓)
        # toggle was removed from the card). Idempotent — only sets read_at once.
        _ups = _get_or_create_user_paper_state(db_session, paper_id)
        if _ups.read_at is None:
            _ups.read_at = datetime.utcnow()
        db_session.commit()

        # BRIEF v2 §6 — LAZY deep analysis on OPEN. Ingest only computes the
        # cheap title+abstract embedding; the full Gemini Pro analysis runs the
        # first time the user opens the paper. `analyze_on_open` is idempotent
        # (a no-op when an AIAnalysis row already exists) so re-opening an
        # already-analysed paper costs nothing. We run it fire-and-forget in a
        # daemon thread with its own session so the PDF launch response stays
        # fast and a model/quota error can never break opening the PDF.
        _trigger_lazy_analysis_on_open(paper_id)

        return {**result, "last_opened_at": paper.last_opened_at.isoformat()}
    finally:
        db_session.close()


def _trigger_lazy_analysis_on_open(paper_id: str) -> None:
    """Fire-and-forget the lazy deep analysis for an opened paper (BRIEF v2 §6).

    Spawns a daemon thread that calls
    ``services.analyze_service.analyze_on_open`` with its own DB session. The
    call is idempotent (skips when an analysis already exists), so this is a
    cheap no-op for already-analysed papers. Any error is logged and swallowed —
    opening the PDF must never fail because the deferred analysis could not run.
    """

    def _run() -> None:
        session = _db_session()
        try:
            from services.analyze_service import analyze_on_open
            _begin_runtime("analyze_on_open")
            _push_runtime_log(
                f"Paper {paper_id} opened; running lazy deep analysis if needed…",
                stage="analyze_on_open",
            )
            result = analyze_on_open(
                paper_id, session, progress_callback=_push_runtime_log
            )
            if result is None:
                _finish_runtime(
                    "Lazy analysis skipped (already analysed)",
                    stage="done",
                )
            else:
                _finish_runtime(
                    "Lazy analysis on open complete",
                    stage="done",
                    analyzed=1,
                    analyzed_paper_id=paper_id,
                )
        except Exception as exc:  # noqa: BLE001 — never break the open flow.
            try:
                _finish_runtime(
                    f"Lazy analysis on open failed: {exc}", stage="error"
                )
            except Exception:
                pass
            _logger.warning(
                "analyze_on_open failed for paper %s: %s", paper_id, exc
            )
        finally:
            session.close()

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"lazy-analyze-open-{paper_id[:8]}",
    ).start()


# ---------------------------------------------------------------------------
# Reading state — Roadmap V2 items 1 / 41 / 16-prime
# ---------------------------------------------------------------------------


def _maybe_refresh_vault(db_session, paper: Paper) -> None:
    """Best-effort vault `.md` refresh; never raise back to the request."""
    try:
        from services.vault_writer import write_paper_md
        write_paper_md(db_session, paper)
    except Exception:
        pass


@app.post("/api/papers/{paper_id}/intent")
def set_reading_intent(paper_id: str, body: dict = Body(...)):
    """Item 41 — Why Strip: set/clear the user's "why I opened this" intent.

    Body: ``{"intent": "compare H-κ stacking to Niu 2024"}`` — empty/null clears.
    Returns the persisted intent + timestamp.
    """
    intent = body.get("intent")
    if intent is not None and not isinstance(intent, str):
        raise HTTPException(status_code=422, detail="intent must be a string or null")
    text_in = (intent or "").strip()
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        # Cap to 200 chars — Why Strip is supposed to be a 8-word externalisation,
        # not a notebook. Soft cap keeps the UI sticky bar single-line.
        # FN-B6-A03-1 — surface the truncation in the response so the UI can
        # render a one-line toast instead of silently dropping characters.
        MAX_INTENT_CHARS = 200
        truncated = False
        if len(text_in) > MAX_INTENT_CHARS:
            text_in = text_in[:MAX_INTENT_CHARS].rstrip()
            truncated = True
        if text_in:
            paper.reading_intent = text_in
            paper.reading_intent_at = datetime.utcnow()
        else:
            paper.reading_intent = None
            paper.reading_intent_at = None
        db_session.commit()
        _maybe_refresh_vault(db_session, paper)
        # FN-B6-A03-2 / FN-B6-A08-1 — canonical keys match the GET shape
        # (`reading_intent` / `reading_intent_at`) so a UI write→read cycle
        # can reuse the same accessor. Old short keys (`intent` / `intent_at`)
        # kept as deprecated aliases for backwards compatibility.
        intent_iso = (
            paper.reading_intent_at.isoformat() if paper.reading_intent_at else None
        )
        return {
            "paper_id": paper.id,
            "reading_intent": paper.reading_intent,
            "reading_intent_at": intent_iso,
            # Deprecated aliases — kept so existing clients keep working.
            "intent": paper.reading_intent,
            "intent_at": intent_iso,
            "warning": (
                f"intent was truncated to {MAX_INTENT_CHARS} chars" if truncated else None
            ),
        }
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/reading_notes")
def set_reading_notes(paper_id: str, body: dict = Body(...)):
    """Structured 7-question reading notes (七问读书法) — replaces the old
    1-line intent + 2-sentence takeaway pair.

    Body: any subset of these 8 string keys (all optional, free text):
      - q1: 这篇论文要解决的问题是什么
      - q2: 为什么要解决这个问题
      - q3: 解决这个问题的难点是什么
      - q4: 前人怎么解决这个问题以及局限性
      - q5: 本文怎么解决这个问题
      - q6: 本文新方法的优势和技术细节
      - q7: 最后的结论
      - personal: 个人随便写的 note

    Empty / null clears the whole reading_notes row. Otherwise the JSON
    dict is stored verbatim. Per-field length cap: 5000 chars (silently
    truncated with `warning` in response).

    Returns the persisted dict + timestamp.
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    ALLOWED_KEYS = {"q1", "q2", "q3", "q4", "q5", "q6", "q7", "personal"}
    MAX_PER_FIELD = 5000
    cleaned: Dict[str, str] = {}
    truncated: List[str] = []
    for k in ALLOWED_KEYS:
        v = body.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            raise HTTPException(status_code=422, detail=f"{k} must be a string or null")
        v = v.strip()
        if not v:
            continue
        if len(v) > MAX_PER_FIELD:
            v = v[:MAX_PER_FIELD].rstrip()
            truncated.append(k)
        cleaned[k] = v

    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        if cleaned:
            paper.reading_notes = json.dumps(cleaned, ensure_ascii=False)
            paper.reading_notes_at = datetime.utcnow()
        else:
            # All fields blank → clear the row
            paper.reading_notes = None
            paper.reading_notes_at = None
        db_session.commit()
        _maybe_refresh_vault(db_session, paper)
        return {
            "paper_id": paper.id,
            "reading_notes": cleaned,
            "reading_notes_at": paper.reading_notes_at.isoformat() if paper.reading_notes_at else None,
            "warning": (
                f"Truncated to {MAX_PER_FIELD} chars: {sorted(truncated)}"
                if truncated else None
            ),
        }
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/takeaway")
def set_user_takeaway(paper_id: str, body: dict = Body(...)):
    """Item 1 — Forced two-sentence takeaway.

    Body: ``{"what": "...", "why": "..."}`` — either field may be empty;
    sending both empty clears the takeaway (the user changed their mind).
    Bumps ``user_takeaway_at`` whenever either field is non-empty — that
    timestamp drives the "papers closed this week" counter.
    """
    what = (body.get("what") or "").strip()
    why = (body.get("why") or "").strip()
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        # Cap each field to 600 chars — two sentences each, not paragraphs.
        # FN-B6-A04-2 — track per-field truncation so the response can warn
        # the user instead of silently dropping their text.
        MAX_TAKEAWAY_CHARS = 600
        what_truncated = False
        why_truncated = False
        if len(what) > MAX_TAKEAWAY_CHARS:
            what = what[:MAX_TAKEAWAY_CHARS].rstrip()
            what_truncated = True
        if len(why) > MAX_TAKEAWAY_CHARS:
            why = why[:MAX_TAKEAWAY_CHARS].rstrip()
            why_truncated = True
        paper.user_takeaway_what = what or None
        paper.user_takeaway_why = why or None
        if what or why:
            paper.user_takeaway_at = datetime.utcnow()
        else:
            paper.user_takeaway_at = None
        db_session.commit()
        _maybe_refresh_vault(db_session, paper)
        truncated_fields = []
        if what_truncated:
            truncated_fields.append("what")
        if why_truncated:
            truncated_fields.append("why")
        warning_msg = None
        if truncated_fields:
            warning_msg = (
                f"takeaway field(s) {truncated_fields} truncated to "
                f"{MAX_TAKEAWAY_CHARS} chars"
            )
        # FN-B6-A03-2 / FN-B6-A08-1 / FN-B6-A08-2 — match the GET shape so
        # the UI can `paper = { ...paper, ...response }` without remapping.
        # Adds `has_takeaway` (FN-B6-A08-2) — derived boolean used by the
        # dashboard to flag a paper as closed; previously had to be recomputed
        # or fetched via a second GET. Old short keys kept as deprecated
        # aliases for backwards compatibility.
        takeaway_iso = (
            paper.user_takeaway_at.isoformat() if paper.user_takeaway_at else None
        )
        return {
            "paper_id": paper.id,
            "user_takeaway_what": paper.user_takeaway_what,
            "user_takeaway_why": paper.user_takeaway_why,
            "user_takeaway_at": takeaway_iso,
            "has_takeaway": bool(paper.user_takeaway_what and paper.user_takeaway_why),
            # Deprecated aliases — kept so existing clients keep working.
            "what": paper.user_takeaway_what,
            "why": paper.user_takeaway_why,
            "takeaway_at": takeaway_iso,
            "warning": warning_msg,
        }
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/follow_up")
def toggle_follow_up(paper_id: str, body: Optional[dict] = Body(default=None)):
    """Item 16-prime — Save for follow-up personal queue.

    Body: ``{"follow_up": true|false}`` — when omitted, toggles current value.
    """
    body = body or {}
    if "follow_up" not in body:
        raise HTTPException(status_code=400, detail="follow_up boolean field is required")
    # FN-B6-A11-2 (LOW pass, 2026-05-19): Strict bool check — previously
    # `bool(body["follow_up"])` silently coerced any non-empty string (including
    # the literal "false") to True, which would surprise a JS client that
    # serialised a boolean as a string. Reject anything that is not a true bool.
    follow_up_val = body.get("follow_up")
    if not isinstance(follow_up_val, bool):
        raise HTTPException(status_code=422, detail="follow_up must be a boolean true or false")
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        new_value = follow_up_val
        paper.follow_up = new_value
        paper.follow_up_at = datetime.utcnow() if new_value else None
        db_session.commit()
        _maybe_refresh_vault(db_session, paper)
        return {
            "paper_id": paper.id,
            "follow_up": paper.follow_up,
            "follow_up_at": paper.follow_up_at.isoformat() if paper.follow_up_at else None,
        }
    finally:
        db_session.close()


# ---------------------------------------------------------------------------
# Wave-9 — UserPaperState: read / archive / dismiss toggles
# ---------------------------------------------------------------------------
# Three endpoints; each upserts a row in user_paper_states and toggles the
# corresponding timestamp. Response shape mirrors /follow_up so the
# frontend can swap implementations interchangeably.
#
# `read` and `archive` are toggles (re-POST clears the flag).
# `dismiss` is also a toggle (so a UI mistake can be undone) but the
# frontend defaults to filtering dismissed papers out, which makes the
# "Show N dismissed" reveal the only un-dismiss path most users will hit.
# ---------------------------------------------------------------------------


def _get_or_create_user_paper_state(db_session, paper_id: str) -> UserPaperState:
    """Lookup-or-insert the singleton state row for this paper."""
    ups = db_session.get(UserPaperState, paper_id)
    if ups is None:
        ups = UserPaperState(paper_id=paper_id)
        db_session.add(ups)
    return ups


@app.post("/api/papers/{paper_id}/read")
def toggle_paper_read(paper_id: str):
    """Toggle the "read" flag on a paper. Returns ``{is_read, read_at}``.

    No body needed — the endpoint flips whatever the current state is.
    Idempotent on re-POST in the sense that the column ends up at the
    opposite of where it started.
    """
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        ups = _get_or_create_user_paper_state(db_session, paper_id)
        if ups.read_at:
            ups.read_at = None
        else:
            ups.read_at = datetime.utcnow()
        db_session.commit()
        return {
            "paper_id": paper_id,
            "is_read": bool(ups.read_at),
            "read_at": ups.read_at.isoformat() if ups.read_at else None,
        }
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/archive")
def toggle_paper_archive(paper_id: str):
    """Toggle the "archived" flag on a paper. Returns ``{is_archived, archived_at}``.

    Archived papers stay visible everywhere (no filter applied) but dim
    in Inbox view so the user knows they've been handled.
    """
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        ups = _get_or_create_user_paper_state(db_session, paper_id)
        if ups.archived_at:
            ups.archived_at = None
        else:
            ups.archived_at = datetime.utcnow()
        db_session.commit()
        return {
            "paper_id": paper_id,
            "is_archived": bool(ups.archived_at),
            "archived_at": ups.archived_at.isoformat() if ups.archived_at else None,
        }
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/dismiss")
def toggle_paper_dismiss(paper_id: str):
    """Toggle the "dismissed" flag on a paper.

    Returns ``{is_dismissed, dismissed_at}``. Dismissed papers are hidden
    from the default Papers grid (UI applies the filter); the user can
    flip back via the "Show N dismissed" toggle to find and un-dismiss.
    """
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        ups = _get_or_create_user_paper_state(db_session, paper_id)
        if ups.dismissed_at:
            ups.dismissed_at = None
        else:
            ups.dismissed_at = datetime.utcnow()
        db_session.commit()
        return {
            "paper_id": paper_id,
            "is_dismissed": bool(ups.dismissed_at),
            "dismissed_at": ups.dismissed_at.isoformat() if ups.dismissed_at else None,
        }
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/like")
def toggle_paper_like(paper_id: str):
    """Toggle the ❤️ "interested" flag on a paper. Returns ``{is_liked, liked_at}``.

    2026-06-16 — ❤️ is the persistent *taste* signal (WHAT the user cares
    about), distinct from ★ follow_up (the transient *to-read* queue) and from
    read/archive/dismiss. It feeds the relevance interest profile as a positive
    example. No body needed — the endpoint flips whatever the current state is,
    mirroring /read.
    """
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        ups = _get_or_create_user_paper_state(db_session, paper_id)
        if ups.liked_at:
            ups.liked_at = None
        else:
            ups.liked_at = datetime.utcnow()
        db_session.commit()
        return {
            "paper_id": paper_id,
            "is_liked": bool(ups.liked_at),
            "liked_at": ups.liked_at.isoformat() if ups.liked_at else None,
        }
    finally:
        db_session.close()


@app.get("/api/follow_up/inbox")
def get_follow_up_inbox(project_id: Optional[str] = Query(default=None)):
    """Return papers marked for follow-up, newest pin first.

    When ``project_id`` is provided, results are scoped to papers belonging to
    that project (via the project_papers join table).
    """
    db_session = _db_session()
    try:
        base_query = db_session.query(Paper).filter(Paper.follow_up == True)  # noqa: E712
        if project_id:
            paper_ids = [
                pp.paper_id for pp in
                db_session.query(ProjectPaper).filter(ProjectPaper.project_id == project_id).all()
            ]
            base_query = base_query.filter(Paper.id.in_(paper_ids))
        rows = base_query.order_by(Paper.follow_up_at.desc()).all()
        return {
            "papers": [_paper_to_dict(p, include_detail=False) for p in rows],
            "count": len(rows),
        }
    finally:
        db_session.close()


@app.get("/api/reading_stats")
def get_reading_stats(project_id: Optional[str] = Query(default=None)):
    """Aggregate state for the header chip: papers closed in the last 7 / 30 days.

    "Closed" = user_takeaway_at is non-null. Drives the T1 reading-exit
    micro-win on the dashboard. When ``project_id`` is provided, all counts
    are scoped to papers belonging to that project.

    Note: ``closed_last_7_days`` and ``closed_last_30_days`` are rolling
    windows (now − 7d / now − 30d), not calendar week/month. The legacy
    ``closed_this_week`` / ``closed_this_month`` keys remain as deprecated
    aliases for backwards compatibility with existing UI consumers.
    """
    from datetime import timedelta

    db_session = _db_session()
    try:
        now = datetime.utcnow()
        week_start = now - timedelta(days=7)
        month_start = now - timedelta(days=30)

        scoped_paper_ids: Optional[List[str]] = None
        if project_id:
            scoped_paper_ids = [
                pp.paper_id for pp in
                db_session.query(ProjectPaper).filter(ProjectPaper.project_id == project_id).all()
            ]

        def _scoped(q):
            if scoped_paper_ids is not None:
                return q.filter(Paper.id.in_(scoped_paper_ids))
            return q

        total = _scoped(db_session.query(Paper)).count()
        closed_week = _scoped(
            db_session.query(Paper)
            .filter(Paper.user_takeaway_at != None, Paper.user_takeaway_at >= week_start)  # noqa: E711
        ).count()
        closed_month = _scoped(
            db_session.query(Paper)
            .filter(Paper.user_takeaway_at != None, Paper.user_takeaway_at >= month_start)  # noqa: E711
        ).count()
        closed_total = _scoped(
            db_session.query(Paper)
            .filter(Paper.user_takeaway_at != None)  # noqa: E711
        ).count()
        follow_up_count = _scoped(
            db_session.query(Paper)
            .filter(Paper.follow_up == True)  # noqa: E712
        ).count()
        # FN-B6-A05-2 — Surface lifetime takeaway count (distinct from the
        # rolling 7/30-day "closed" windows). The header chip's "takeaways"
        # tile previously had to call /api/papers and count user_takeaway_at
        # client-side; this exposes it directly.
        takeaway_count = _scoped(
            db_session.query(Paper)
            .filter(Paper.user_takeaway_at != None)  # noqa: E711
        ).count()
        return {
            "total_papers": total,
            # New canonical field names — honest about rolling-window semantics.
            "closed_last_7_days": closed_week,
            "closed_last_30_days": closed_month,
            "closed_total": closed_total,
            "follow_up_count": follow_up_count,
            "takeaway_count": takeaway_count,
            # Deprecated aliases — kept for backwards compatibility with
            # existing UI consumers (templates/index.html). Remove once all
            # callers migrate to the *_last_N_days names.
            "closed_this_week": closed_week,
            "closed_this_month": closed_month,
        }
    finally:
        db_session.close()


@app.get("/api/papers/{paper_id}/pages")
def get_paper_pages(paper_id: str, refresh: bool = Query(default=False)):
    """Return the per-page text cache for *paper_id*.

    On first call (or when `?refresh=true`), the PDF is extracted and cached.
    Returns ``{paper_id, page_count, pages: [{page, text, chars}]}``.
    """
    from services.pdf_page_cache import get_or_create_cache

    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")

        # Return existing cache if present and not forced
        cache = db_session.get(PaperPageCache, paper_id)
        if cache and not refresh:
            return {
                "paper_id": paper_id,
                "page_count": cache.page_count,
                "pages": cache.pages,
                "extracted_at": cache.extracted_at.isoformat() if cache.extracted_at else None,
            }

        if not paper.pdf_path:
            raise HTTPException(
                status_code=409,
                detail="No PDF available for this paper. Upload a PDF first.",
            )

        new_cache = get_or_create_cache(paper_id, paper.pdf_path, db_session, force_refresh=refresh)
        if new_cache is None:
            return {"paper_id": paper_id, "page_count": 0, "pages": [], "extracted_at": None}

        return {
            "paper_id": paper_id,
            "page_count": new_cache.page_count,
            "pages": new_cache.pages,
            "extracted_at": new_cache.extracted_at.isoformat() if new_cache.extracted_at else None,
        }
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/locate")
def locate_paper_anchor(paper_id: str, body: dict = Body(...)):
    """Resolve a seismohub:// anchor to a scroll position within a paper.

    Body (at least one form required):
      * ``{"uri": "seismohub://paper/{paper_id}/p{page}#sha1={sha1}"}``
      * ``{"page": int, "sha1": str}``
      * ``{"page": int, "sha1": str, "opening_sentence": str}`` (enables fuzzy fallback)

    Returns ``{found, page, char_offset, text_snippet, paragraph_index, status,
    drift_reason, recovered_via}``.  Never raises 404 — on cache-miss or
    drift the response has ``found=false`` and the caller can still open the
    PDF to the right page.
    """
    from services.anchor_resolver import parse_seismohub_uri, resolve_anchor

    # Accept either a full URI or page + sha1 components.
    uri = (body.get("uri") or "").strip()
    if uri:
        parsed = parse_seismohub_uri(uri)
        if parsed is None or parsed["paper_id"] != paper_id:
            # Mismatched paper_id or bad URI — still try page-only if available.
            page_raw = body.get("page")
            try:
                page = int(page_raw) if page_raw is not None else 1
            except (TypeError, ValueError):
                page = 1
            return {
                "found": False, "page": page, "char_offset": 0,
                "text_snippet": None, "paragraph_index": None,
                "status": "invalid_uri", "drift_reason": None, "recovered_via": None,
            }
        page = parsed["page"]
        sha1 = parsed["sha1"]
    else:
        page_raw = body.get("page")
        try:
            page = int(page_raw) if page_raw is not None else 1
        except (TypeError, ValueError):
            page = 1
        sha1 = (body.get("sha1") or body.get("sha") or "").strip() or None

    opening_sentence = (body.get("opening_sentence") or "").strip() or None

    db_session = _db_session()
    try:
        return resolve_anchor(db_session, paper_id, page, sha1,
                              opening_sentence=opening_sentence)
    finally:
        db_session.close()


@app.post("/api/citation/resolve")
def resolve_citation_uri(body: dict = Body(...)):
    """Resolve a seismohub:// URI to a scroll position (URI-centric endpoint).

    Body: ``{"uri": "seismohub://paper/{paper_id}/p{page}#sha1={sha1}",
             "opening_sentence": str (optional)}``

    This is the endpoint the JS click-interceptor in templates/index.html
    calls.  Delegates to the paper-scoped ``/api/papers/{paper_id}/locate``
    logic but accepts the URI as the primary input so callers do not need
    to parse the URI client-side.

    Never raises 404 — returns ``{status: "invalid_uri"}`` on bad input.
    """
    from services.anchor_resolver import parse_seismohub_uri, resolve_anchor

    uri = (body.get("uri") or "").strip()
    if not uri:
        return {
            "found": False, "page": None, "char_offset": 0,
            "text_snippet": None, "paragraph_index": None,
            "status": "invalid_uri", "drift_reason": None, "recovered_via": None,
        }

    parsed = parse_seismohub_uri(uri)
    if parsed is None:
        return {
            "found": False, "page": None, "char_offset": 0,
            "text_snippet": None, "paragraph_index": None,
            "status": "invalid_uri", "drift_reason": None, "recovered_via": None,
        }

    paper_id = parsed["paper_id"]
    page = parsed["page"]
    sha1 = parsed["sha1"]
    opening_sentence = (body.get("opening_sentence") or "").strip() or None

    db_session = _db_session()
    try:
        result = resolve_anchor(db_session, paper_id, page, sha1,
                                opening_sentence=opening_sentence)
        # Attach paper_id to the response so the JS handler can use it
        # without re-parsing the URI.
        result["paper_id"] = paper_id
        return result
    finally:
        db_session.close()


@app.get("/api/papers/{paper_id}/qa/history")
def get_qa_history(paper_id: str):
    """Return the persisted QA conversation for a paper.

    Returns the visible (``archived=False``) ``qa_messages`` rows in
    chronological order (``position`` ASC) as a JSON array. Each row
    carries the fields the chat-modal renderer needs to redraw the
    conversation faithfully on reload:

    Response shape (HTTP 200) — bare array, **not** an envelope::

        [
          {
            "role":        "user",
            "text":        "What seismic method is used?",
            "model_id":    null,
            "model_label": null,
            "citations":   [],
            "created_at":  "2026-05-24T09:11:02Z",
            "position":    1
          },
          {
            "role":        "assistant",
            "text":        "The paper uses DAS ...",
            "model_id":    "gemini-3.1-pro-preview",
            "model_label": "Gemini 3.1 Pro",
            "citations":   [{"page": 3, "paragraph_idx": 8, ...}],
            "created_at":  "2026-05-24T09:11:08Z",
            "position":    2
          }
        ]

    Returns ``[]`` (HTTP 200) when no conversation has happened yet —
    this is the *expected* shape for a brand-new paper and is **not**
    an error.  Returns **HTTP 404** only when the paper itself does
    not exist.  Archived rows (200-turn / 100 KB cap, per
    ``04-feature-design/12-qa-chat-persistence.md`` §6) are silently
    filtered out; a future ``?include_archived=true`` toggle can flip
    that without a contract break because the field set stays the
    same.
    """
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")

        rows = (
            db_session.query(QAMessage)
            .filter(QAMessage.paper_id == paper_id, QAMessage.archived == False)  # noqa: E712
            .order_by(QAMessage.position.asc())
            .all()
        )
        return [
            {
                "role":        row.role,
                "text":        row.text,
                "model_id":    row.model_id,
                "model_label": row.model_label,
                "citations":   row.citations or [],
                "created_at":  row.created_at.isoformat() + "Z" if row.created_at else None,
                "position":    row.position,
            }
            for row in rows
        ]
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/chat")
async def chat_with_paper(paper_id: str, body: dict = Body(...)):
    """PDF QA chat — stateless single-turn endpoint (M2 G5).

    Request body::

        {
          "message": "What seismic method is used?",
          "history": [                               // optional prior turns
            {"role": "user",  "text": "..."},
            {"role": "model", "text": "..."}
          ]
        }

    Response::

        {
          "reply":       "The paper uses DAS ...",  // clean reply text
          "citations":   [{...envelope...}, ...],   // structured anchors (PR-07)
          "model":       "gemini-3.1-pro-preview",  // actual wire ID used
          "model_label": "Gemini 3.1 Pro",          // human label, frozen at write-time
          "has_pages":   true,                      // false when no page cache exists
          "turn_id":     1742                       // qa_messages.id of the assistant row
        }

    Each successful call writes two rows to ``qa_messages`` — one
    ``role="user"`` row mirroring ``message`` and one ``role="assistant"``
    row carrying ``model``/``model_label``/``citations``. ``turn_id`` is
    the assistant row's primary key; the client uses it to anchor edits,
    re-runs, and per-turn delete (PR-09+). The two inserts share the
    same SQLAlchemy session and the same transaction, so a turn pair is
    atomic: a mid-call crash leaves zero rows on disk, never one.
    ``history`` from the body is still honoured for this PR; PR-09
    flips the frontend to omit it and the handler will then load
    server-side history from ``qa_messages``.
    Requires a Gemini API key (``GEMINI_API_KEY`` env var).  Returns 503 if
    the key is absent or the model call fails.
    """
    from services.qa_service import answer_question
    from sqlalchemy import func as _sa_func

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="'message' field is required and must be non-empty.")

    history = body.get("history") or []
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="'history' must be a list.")

    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")

        cache = db_session.get(PaperPageCache, paper_id)
        pages = (cache.pages or []) if cache else []

        # 2026-11-09 (owner: 「加上」): institution-licensed full text (TDM / subscription copies) must never reach
        # hosted AI. Refuse before any LLM call; the UI shows `detail` as a System message.
        if pages:
            from services.tdm_qa_guard import REFUSAL, qa_block_reason
            tdm_reason = qa_block_reason(paper, pages)
            if tdm_reason:
                raise HTTPException(status_code=403, detail=f"{REFUSAL} ({tdm_reason})")

        authors_raw = paper.authors or []
        author_str = (
            ", ".join(authors_raw) if isinstance(authors_raw, list) else str(authors_raw)
        )
        year = (paper.published_date or "")[:4]
        meta = {"title": paper.title or "", "authors": author_str, "year": year}

        api_key = (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("VERTEX_AI_API_KEY")
            or ""
        )
        if not api_key:
            raise HTTPException(
                status_code=503,
                detail="GEMINI_API_KEY not configured — QA chat is unavailable.",
            )

        # 2026-06-16: run the synchronous LLM call in a thread-pool executor so
        # the 5–30s round-trip no longer blocks the event loop (a single QA
        # request used to freeze the whole server for every other user/tab).
        # 120s ceiling guards a hung provider. Safe because answer_question is
        # pure LLM — it carries no SQLAlchemy session into the worker thread.
        import asyncio
        import functools
        try:
            result = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(
                        answer_question,
                        question=message,
                        history=history,
                        pages=pages,
                        paper_meta=meta,
                    ),
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="LLM call timed out after 120s — please try again.",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"LLM call failed: {exc}",
            ) from exc

        # ---- Persist the turn pair (BRIEF item D) ------------------------
        # Allocate the next position inside the same transaction. The
        # composite ``UNIQUE(paper_id, position)`` constraint (PR-04) is
        # what makes this race-safe on a single-writer SQLite: two
        # concurrent requests would collide on the assistant insert and
        # one would be retried by the caller, not write a duplicate row.
        next_pos = (
            db_session.query(
                _sa_func.coalesce(_sa_func.max(QAMessage.position), 0)
            )
            .filter(QAMessage.paper_id == paper_id)
            .scalar()
            or 0
        )

        # PR-07 contract: ``model`` is the *actually-used* wire ID (could
        # be FALLBACK_MODEL on primary failure); ``model_label`` is the
        # human label frozen at write-time so a row generated by
        # ``Gemini 3.1 Pro`` still reads ``Gemini 3.1 Pro`` years later
        # even after the preview is retired from the display dict.
        model_id = result.get("model") or ""
        model_label = result.get("model_label") or ""
        citations = result.get("citations") or []

        user_row = QAMessage(
            paper_id=paper_id,
            role="user",
            text=message,
            model_id=None,
            model_label=None,
            citations=[],
            position=next_pos + 1,
            archived=False,
        )
        assistant_row = QAMessage(
            paper_id=paper_id,
            role="assistant",
            text=result["reply"],
            model_id=model_id or None,
            model_label=model_label or None,
            citations=citations,
            position=next_pos + 2,
            archived=False,
        )
        db_session.add(user_row)
        db_session.add(assistant_row)
        try:
            db_session.commit()
        except Exception as exc:
            db_session.rollback()
            # The LLM reply is already paid-for; surfacing a 500 here
            # would lose it. Log and return the reply without ``turn_id``
            # so the client at least sees the answer; the next turn will
            # re-allocate a position and recover.
            _print_runtime_line(f"qa_messages persistence failed: {exc}")
            return {
                "reply": result["reply"],
                "citations": citations,
                "model": model_id,
                "model_label": model_label,
                "has_pages": bool(pages),
                "turn_id": None,
            }

        return {
            "reply": result["reply"],
            "citations": citations,
            "model": model_id,
            "model_label": model_label,
            "has_pages": bool(pages),
            "turn_id": assistant_row.id,
        }
    finally:
        db_session.close()


@app.delete("/api/papers/{paper_id}/qa/history")
def delete_qa_history(paper_id: str, hard: bool = Query(default=False)):
    """Clear the persisted QA conversation for a paper (BRIEF item D).

    Default behaviour is a *soft archive* — sets ``archived = TRUE`` on every
    visible row so the audit trail survives (the model badge from item C
    needs old rows to remain attributable). Passing ``?hard=true`` issues an
    actual ``DELETE`` and is reserved for the "user deleted this paper"
    cascade or for test fixtures; it is **not** wired into the default UI.

    Returns ``{paper_id, deleted, mode}`` where ``deleted`` is the count of
    rows touched (archived or hard-deleted). ``mode`` is ``"archive"`` or
    ``"hard"`` so a v2 client can decide whether to offer an undo.

    404 if the paper does not exist. A paper with no chat history yet
    returns ``{deleted: 0, mode: …}`` with HTTP 200 — clearing an empty
    history is a no-op, not an error.
    """
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")

        if hard:
            # Cascade / test-fixture path: actually delete the rows. Returns
            # the integer rowcount from SQLAlchemy bulk-delete.
            deleted = (
                db_session.query(QAMessage)
                .filter(QAMessage.paper_id == paper_id)
                .delete(synchronize_session=False)
            )
            db_session.commit()
            return {"paper_id": paper_id, "deleted": int(deleted), "mode": "hard"}

        # Soft path: archive every currently-visible row. Already-archived
        # rows are left alone so a double-click on Clear is idempotent and
        # does not lie about the count.
        deleted = (
            db_session.query(QAMessage)
            .filter(QAMessage.paper_id == paper_id, QAMessage.archived == False)  # noqa: E712
            .update({"archived": True}, synchronize_session=False)
        )
        db_session.commit()
        return {"paper_id": paper_id, "deleted": int(deleted), "mode": "archive"}
    finally:
        db_session.close()


# ---------------------------------------------------------------------------
# Vault (Obsidian Markdown sidecar) — Roadmap V2 item 4
# ---------------------------------------------------------------------------


@app.get("/api/vault/path")
def get_vault_path_route():
    """Return the currently configured Obsidian vault path, or null if unset."""
    from services.vault_writer import get_vault_path

    db_session = _db_session()
    try:
        path = get_vault_path(db_session)
        return {
            "path": str(path) if path else None,
            "exists": bool(path and path.exists()),
            "default_suggestion": str(Path.home() / "Documents" / "Obsidian" / "SWP-vault"),
        }
    finally:
        db_session.close()


@app.post("/api/vault/path")
def set_vault_path_route(body: dict = Body(...)):
    """Configure the Obsidian vault path. Creates the directory if needed.

    Body: ``{"path": "/home/me/Obsidian/SWP-vault"}`` — or ``null`` to unset.
    Returns the resolved path on success.
    """
    from services.vault_writer import (
        VaultWriterError,
        clear_vault_path,
        set_vault_path,
    )

    raw = body.get("path")
    db_session = _db_session()
    try:
        if raw in (None, ""):
            clear_vault_path(db_session)
            return {"status": "cleared", "path": None}
        try:
            resolved = set_vault_path(db_session, str(raw))
        except VaultWriterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "path": str(resolved)}
    finally:
        db_session.close()


@app.post("/api/vault/sync")
def sync_vault_route():
    """Backfill: write all papers in the library to the configured vault.

    No-op (returns ``status="skipped"``) if no vault path is set.
    """
    from services.vault_writer import write_all_papers

    db_session = _db_session()
    try:
        return write_all_papers(db_session)
    finally:
        db_session.close()


@app.post("/api/papers/{paper_id}/vault_sync")
def sync_one_paper_to_vault(paper_id: str):
    """Force-rewrite the `.md` file for a single paper.

    Useful for testing and for "I edited the title, push it to my vault now."
    """
    from services.vault_writer import VaultWriterError, write_paper_md

    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        try:
            path = write_paper_md(db_session, paper)
        except VaultWriterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if path is None:
            return {"status": "skipped", "reason": "no_vault_configured"}
        return {"status": "ok", "path": str(path)}
    finally:
        db_session.close()


@app.get("/api/health")
def get_health():
    """Operational snapshot used by the planned settings page health pill.

    Returns DB size, papers count, last analyze timestamp, Gemini API key
    presence (just a 'set'/'missing' flag — never leaks the key), disk free
    on the downloads volume, runtime state, and the resolved server URL.
    """
    import shutil

    health: Dict[str, Any] = {"status": "ok", "checks": {}}

    # SQLite file size
    try:
        db_bytes = DATABASE_PATH.stat().st_size if DATABASE_PATH.exists() else 0
        health["checks"]["db_size_bytes"] = db_bytes
        health["checks"]["db_size_mb"] = round(db_bytes / (1024 * 1024), 2)
    except OSError as exc:
        health["status"] = "degraded"
        health["checks"]["db_error"] = str(exc)

    # Papers + analysis counts + last analyze timestamp
    db_session = _db_session()
    try:
        from sqlalchemy import func

        from models import AIAnalysis

        papers_count = db_session.query(func.count(Paper.id)).scalar() or 0
        analyses_count = db_session.query(func.count(AIAnalysis.id)).scalar() or 0
        last_analyze = (
            db_session.query(func.max(AIAnalysis.updated_at)).scalar()
        )
        pdf_count = (
            db_session.query(func.count(Paper.id))
            .filter(Paper.pdf_path.isnot(None))
            .scalar()
            or 0
        )
        deduped = (
            db_session.query(func.count(Paper.id))
            .filter(Paper.pdf_sha256.isnot(None))
            .scalar()
            or 0
        )
        health["checks"]["papers_count"] = papers_count
        health["checks"]["analyses_count"] = analyses_count
        health["checks"]["pdf_count"] = pdf_count
        health["checks"]["papers_with_sha256"] = deduped
        health["checks"]["last_analyze_at"] = (
            last_analyze.isoformat() if last_analyze else None
        )
    except Exception as exc:  # pragma: no cover — never want /api/health to 500
        health["status"] = "degraded"
        health["checks"]["query_error"] = str(exc)
    finally:
        db_session.close()

    # Gemini / Vertex API key presence — just a boolean.
    health["checks"]["gemini_api_key_set"] = bool(
        os.getenv("VERTEX_AI_API_KEY") or os.getenv("GEMINI_API_KEY")
    )

    # Disk free on the downloads volume.
    try:
        usage = shutil.disk_usage(str(DOWNLOADS_DIR if DOWNLOADS_DIR.exists() else BASE_DIR))
        health["checks"]["disk_free_gb"] = round(usage.free / (1024 ** 3), 2)
        health["checks"]["disk_total_gb"] = round(usage.total / (1024 ** 3), 2)
        if usage.free < 500 * 1024 * 1024:  # <500MB
            health["status"] = "degraded"
            health["checks"]["disk_warning"] = "Less than 500MB free on downloads volume."
    except OSError as exc:
        health["checks"]["disk_error"] = str(exc)

    # Server info
    health["server"] = {
        "url": SERVER_URL,
        "host": SERVER_HOST,
        "port": SERVER_PORT,
        "downloads_dir": str(DOWNLOADS_DIR),
    }

    # Current runtime snapshot (so a single GET surfaces everything).
    health["runtime"] = _runtime_snapshot()

    return health


# ---------------------------------------------------------------------------
# Settings / BYOK (2026-06-16) — let the user manage their API key from a
# Settings page instead of hand-editing .env. Local single-user app: the key
# lives in .env (plaintext, same as today) and is mirrored into os.environ so
# it takes effect immediately, no restart. We never log or echo the full key.
# ---------------------------------------------------------------------------

def _upsert_env_var(name: str, value: str) -> None:
    """Insert-or-replace a single ``NAME=value`` line in the project .env.

    Preserves every other line and comment; creates .env if absent. Writes
    ATOMICALLY (temp file in the same dir + os.replace) at mode 0600, so:
      * a crash / disk-full mid-save can never leave a truncated or empty .env
        that wipes the user's other BYOK keys (was: in-place write_text);
      * the secrets file is never world-readable (was: 0644 — any other local
        user could `cat .env`). Caller validates that ``value`` has no newline.
    """
    env_path = BASE_DIR / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    new_line = f"{name}={value}"
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith(f"{name}=") or s.startswith(f"{name} ="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    payload = "\n".join(lines) + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=str(BASE_DIR), prefix=".env.", suffix=".tmp")
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(env_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # Ensure private perms even if .env pre-existed at 0644 before this save.
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def _mask_secret(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    v = str(v)
    return ("•" * max(0, len(v) - 4)) + v[-4:] if len(v) > 4 else "••••"


# Settings-page schema: only the .env vars that are (a) actually consumed by the
# LIVE app AND (b) something the user needs to configure. Per owner 2026-06-17:
# dropped GEMINI/GOOGLE fallbacks (user is on Vertex) and ALL the PDF-acquisition
# params (that pipeline is currently decoupled from the app — see
# 审核_env参数详解_2026-06-17.md). ETH_RC stays — it powers live metadata fetch
# (fetch_service), not the decoupled PDF pipeline. type drives widget + masking.
SETTINGS_FIELDS: List[Dict[str, Any]] = [
    {"env": "VERTEX_AI_API_KEY", "group": "AI 模型", "type": "secret", "label": "Vertex API Key（首选）"},
    {"env": "VERTEX_AI_PROJECT", "group": "AI 模型", "type": "text", "label": "Vertex Project（GCP 项目 ID）"},
    {"env": "VERTEX_AI_LOCATION", "group": "AI 模型", "type": "text", "label": "Vertex Location（区域）", "placeholder": "global"},
    {"env": "GOOGLE_CUSTOM_SEARCH_API_KEY", "group": "网络富化（搜索）", "type": "secret", "label": "Google CSE API Key"},
    {"env": "GOOGLE_CSE_CX", "group": "网络富化（搜索）", "type": "secret", "label": "Google CSE 引擎 ID (cx)"},
    {"env": "ETH_RC_API_KEY", "group": "文献元数据抓取", "type": "secret", "label": "ETH RC API Key（ETH 机构论文元数据）"},
]

# Back-compat aliases for the earlier vertex_*/gemini_* POST body field names.
_SETTINGS_ALIAS = {
    "vertex_api_key": "VERTEX_AI_API_KEY",
    "vertex_project": "VERTEX_AI_PROJECT",
    "vertex_location": "VERTEX_AI_LOCATION",
    "gemini_api_key": "GEMINI_API_KEY",
}


@app.get("/api/settings")
def get_settings():
    """Schema-driven settings: every managed .env var, grouped. Secrets are
    masked (never the full value); bool/number/text returned as-is. Keeps the
    older vertex/gemini keys for back-compat with the earlier UI/tests."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for f in SETTINGS_FIELDS:
        raw = os.environ.get(f["env"]) or ""
        item: Dict[str, Any] = {
            "env": f["env"], "type": f["type"], "label": f.get("label", f["env"]),
            "placeholder": f.get("placeholder", ""), "has_value": bool(raw),
        }
        if f["type"] == "secret":
            item["masked"] = _mask_secret(raw)
        elif f["type"] == "bool":
            item["value"] = str(raw).strip().lower() in ("true", "1", "yes", "on")
        else:
            item["value"] = raw
        groups.setdefault(f["group"], []).append(item)
    vkey = os.environ.get("VERTEX_AI_API_KEY") or ""
    gkey = os.environ.get("GEMINI_API_KEY") or ""
    vproj = os.environ.get("VERTEX_AI_PROJECT") or ""
    vloc = os.environ.get("VERTEX_AI_LOCATION") or ""
    provider = "vertex" if vkey else ("gemini" if gkey else None)
    return {
        "groups": [{"name": g, "fields": items} for g, items in groups.items()],
        "env_file": str(BASE_DIR / ".env"),
        "provider": provider,
        "vertex": {"api_key_set": bool(vkey), "api_key_masked": _mask_secret(vkey), "project": vproj, "location": vloc or "global"},
        "gemini": {"api_key_set": bool(gkey), "api_key_masked": _mask_secret(gkey)},
        "gemini_api_key_set": bool(vkey or gkey),
        "gemini_api_key_masked": _mask_secret(vkey or gkey),
        "key_source": "VERTEX_AI_API_KEY" if vkey else ("GEMINI_API_KEY" if gkey else None),
    }


@app.post("/api/settings")
def update_settings(body: dict = Body(...)):
    """Upsert any managed .env var(s) into .env + os.environ (live, no restart).
    Body is ``{env_or_alias: value}``. Bool fields always apply; text/secret are
    skipped when blank (so an empty field never clobbers an existing value).
    Only vars in SETTINGS_FIELDS are accepted; secrets are never logged/echoed."""
    by_env = {f["env"]: f for f in SETTINGS_FIELDS}
    updates: Dict[str, str] = {}
    for k, v in (body or {}).items():
        env_name = _SETTINGS_ALIAS.get(k, k)
        f = by_env.get(env_name)
        if f is None:
            continue  # ignore unknown keys
        if f["type"] == "bool":
            truthy = (v is True) or (str(v).strip().lower() in ("true", "1", "yes", "on"))
            updates[env_name] = "true" if truthy else "false"
        else:
            val = str(v if v is not None else "").strip()
            if not val:
                continue  # leave unchanged
            if "\n" in val or "\r" in val:
                # An embedded newline would inject extra NAME=... lines into .env.
                # Reject upfront (before any write) so one bad field can't corrupt
                # the file or silently shadow other env vars.
                raise HTTPException(status_code=422, detail=f"{env_name}: 值不能包含换行符。")
            if f["type"] == "secret" and len(val) < 8:
                raise HTTPException(status_code=422, detail=f"{env_name}: 值太短，看起来不像有效凭据。")
            updates[env_name] = val
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided.")
    try:
        for env_name, val in updates.items():
            _upsert_env_var(env_name, val)
            os.environ[env_name] = val
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write .env: {exc}")

    # M8 fix (2026-06-30) — if an LLM-affecting credential changed, drop the
    # cached genai clients so the NEXT call rebuilds from the new env. Without
    # this the analyze/chat/embedding clients keep a client constructed with the
    # OLD key (read only at construction) until a full process restart, so a
    # corrected key silently never takes effect.
    _LLM_ENV = {"VERTEX_AI_API_KEY", "VERTEX_AI_PROJECT", "VERTEX_AI_LOCATION", "GEMINI_API_KEY"}
    if _LLM_ENV & set(updates):
        try:
            from services.llm_client import reset_shared_clients
            from services.analyze_service import reset_vertex_client
            from services.embedding_service import reset_client as reset_embedding_client
            reset_shared_clients()
            reset_vertex_client()
            reset_embedding_client()
        except Exception:
            _logger.warning("update_settings: failed to reset cached LLM clients", exc_info=True)

    return {"ok": True, "updated": list(updates.keys())}


def _zip_upload_to_files(file_name: str, data: bytes) -> Dict[str, bytes]:
    if not file_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a Zotero .zip archive.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Uploaded file is not a readable ZIP archive.") from exc

    out: Dict[str, bytes] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        try:
            rel_path = normalize_upload_path(info.filename)
        except ZoteroImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        out[rel_path] = archive.read(info)
    return out


async def _collect_zotero_upload_files(
    *,
    archive: Optional[UploadFile],
    files: Optional[List[UploadFile]],
) -> Dict[str, bytes]:
    uploaded: Dict[str, bytes] = {}
    if archive is not None and archive.filename:
        uploaded.update(_zip_upload_to_files(archive.filename, await archive.read()))
    for file in files or []:
        if not file.filename:
            continue
        uploaded[normalize_upload_path(file.filename)] = await file.read()
    return uploaded


def _runtime_stats_from_zotero_report(report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entries": report.get("entries", 0),
        "matched_existing": report.get("matched_existing", 0),
        "created": report.get("created", 0),
        "pdf_saved": report.get("pdf_saved", 0),
        "orphan_pdfs": len(report.get("orphan_pdfs", []) or []),
        "duplicate_existing_pdf": len(report.get("duplicate_existing_pdf_items", []) or []),
        "queued_analysis": report.get("queued_analysis", 0),
    }


def _parse_replace_paper_ids(raw: str) -> set[str]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="replace_paper_ids must be a JSON array.") from exc
    if not isinstance(payload, list):
        raise HTTPException(status_code=400, detail="replace_paper_ids must be a JSON array.")
    return {str(item).strip() for item in payload if str(item).strip()}


def _analyze_zotero_import_async(paper_ids: List[str], report: Dict[str, Any]) -> None:
    analyzed = 0
    failed = 0
    db_session = _db_session()
    try:
        total = len(paper_ids)
        for index, paper_id in enumerate(paper_ids, start=1):
            paper = db_session.get(Paper, paper_id)
            if paper is None or not paper.pdf_path:
                failed += 1
                continue
            try:
                _push_runtime_log(
                    f"[{index}/{total}] Analyzing Zotero-imported PDF for {paper_id}",
                    stage="analysis_pdf",
                    current=index,
                    total=total,
                    paper_id=paper_id,
                )
                analyze_paper_from_pdf(
                    paper_id,
                    paper.pdf_path,
                    db_session,
                    progress_callback=_push_runtime_log,
                )
                analyzed += 1
            except Exception as exc:
                failed += 1
                _push_runtime_log(
                    f"Analysis failed for Zotero-imported paper {paper_id}: {exc}",
                    stage="analysis_error",
                    current=index,
                    total=total,
                    paper_id=paper_id,
                )
        _finish_runtime(
            f"Zotero import completed ({report.get('pdf_saved', 0)} PDFs saved, {analyzed} analyzed)",
            stage="done",
            analyzed=analyzed,
            analysis_failed=failed,
            **_runtime_stats_from_zotero_report(report),
        )
    finally:
        db_session.close()
        _zotero_import_lock.release()


@app.post("/api/import/zotero")
async def import_zotero_export(
    archive: Optional[UploadFile] = File(default=None),
    files: Optional[List[UploadFile]] = File(default=None),
    replace_paper_ids: str = Form(default="[]"),
    duplicate_review_complete: bool = Form(default=False),
    project_id: Optional[str] = Form(default=None),
):
    if not _zotero_import_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Zotero import is already running.")

    release_lock = True
    db_session = _db_session()
    try:
        _begin_runtime("zotero_import")
        uploaded = await _collect_zotero_upload_files(archive=archive, files=files)
        if not uploaded:
            raise HTTPException(status_code=400, detail="Please upload a Zotero ZIP or exported folder.")

        _push_runtime_log(
            f"Zotero import received {len(uploaded)} file(s)",
            stage="zotero_import",
            uploaded_files=len(uploaded),
        )
        replace_ids = _parse_replace_paper_ids(replace_paper_ids)
        if not duplicate_review_complete:
            duplicate_report = find_duplicate_existing_pdfs(db_session, uploaded)
            duplicate_items = duplicate_report.get("duplicate_existing_pdf_items", []) or []
            if duplicate_items:
                _finish_runtime(
                    f"Zotero import needs duplicate review ({len(duplicate_items)} existing PDFs)",
                    stage="done",
                    entries=duplicate_report.get("entries", 0),
                    duplicate_existing_pdf=len(duplicate_items),
                )
                return {
                    "status": "duplicate_review_needed",
                    "report": {
                        "entries": duplicate_report.get("entries", 0),
                        "duplicate_existing_pdf_items": duplicate_items,
                        "duplicate_existing_pdf": len(duplicate_items),
                    },
                    "runtime": _runtime_snapshot(),
                }

        report = import_zotero_files(
            db_session,
            uploaded,
            replace_existing=False,
            replace_paper_ids=replace_ids,
        )
        queued_ids = list(report.get("queued_analysis_paper_ids") or [])

        # When the caller targets a specific project, attach every imported
        # paper to it post-hoc. Without project_id we leave the existing
        # default-Monthly assignment (handled by the import service) alone
        # so backwards compatibility is preserved.
        if project_id:
            assigned_ids: List[str] = []
            assigned_ids.extend(queued_ids)
            for key in ("imported_paper_ids", "created_paper_ids", "saved_paper_ids"):
                extra = report.get(key) or []
                if isinstance(extra, list):
                    assigned_ids.extend(str(pid) for pid in extra)
            try:
                for pid in dict.fromkeys(assigned_ids):  # de-dup, preserve order
                    paper_obj = db_session.get(Paper, pid)
                    if paper_obj is not None:
                        _add_paper_to_default_project(
                            db_session,
                            paper_obj,
                            source="zotero_import",
                            project_id=project_id,
                        )
                db_session.commit()
            except Exception:
                db_session.rollback()

        if queued_ids:
            _push_runtime_log(
                f"Zotero import saved {report.get('pdf_saved', 0)} PDF(s); background analysis queued",
                stage="analysis_queue",
                current=0,
                total=len(queued_ids),
                **_runtime_stats_from_zotero_report(report),
            )
            threading.Thread(
                target=_analyze_zotero_import_async,
                args=(queued_ids, report),
                daemon=True,
                name="seismo-zotero-import-analysis",
            ).start()
            release_lock = False
            return {"status": "started", "report": report, "runtime": _runtime_snapshot()}

        _finish_runtime(
            f"Zotero import completed ({report.get('pdf_saved', 0)} PDFs saved)",
            stage="done",
            **_runtime_stats_from_zotero_report(report),
        )
        return {"status": "ok", "report": report, "runtime": _runtime_snapshot()}
    except HTTPException:
        _finish_runtime("Zotero import failed", stage="error")
        raise
    except ZoteroImportError as exc:
        _finish_runtime(f"Zotero import failed: {exc}", stage="error")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        # sec(M1): runtime cleanup must still happen on failure, but the
        # response now uses the generic envelope. Full traceback is logged
        # by _logger.exception so the error_id can be cross-referenced.
        envelope = _error_envelope(
            "Something went wrong during the Zotero import. Check the server log for details.",
        )
        _finish_runtime("Zotero import failed", stage="error")
        _logger.exception("error_id=%s in import_zotero", envelope["error_id"])
        return JSONResponse(status_code=500, content=envelope)
    finally:
        db_session.close()
        if release_lock:
            _zotero_import_lock.release()


@app.post("/api/pdf/normalize_names")
def normalize_pdf_names():
    if not _pdf_normalize_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="PDF filename normalization is already running.")

    report: Dict[str, Any] = {
        "renamed": 0,
        "unchanged": 0,
        "skipped_missing": 0,
        "skipped_external_path": 0,
        "conflicts_resolved": 0,
        "errors": [],
    }
    db_session = _db_session()
    try:
        papers = db_session.query(Paper).filter(Paper.pdf_path.isnot(None)).all()
        for paper in papers:
            try:
                current_path = Path(paper.pdf_path).resolve() if paper.pdf_path else None
                if not current_path or not current_path.exists():
                    report["skipped_missing"] += 1
                    continue
                if not is_managed_download_path(current_path, DOWNLOADS_DIR):
                    report["skipped_external_path"] += 1
                    continue
                target_path = pdf_target_path_for_paper(paper, DOWNLOADS_DIR, current_path=current_path)
                if target_path == current_path:
                    report["unchanged"] += 1
                    continue
                if target_path.name != build_pdf_filename(paper):
                    report["conflicts_resolved"] += 1
                os.replace(current_path, target_path)
                paper.pdf_path = str(target_path.resolve())
                paper.updated_at = datetime.utcnow()
                report["renamed"] += 1
            except Exception as exc:
                report["errors"].append({"paper_id": paper.id, "error": str(exc)})
        db_session.commit()
        return {"status": "ok", "report": report}
    finally:
        db_session.close()
        _pdf_normalize_lock.release()


# ── PR-29: Magic-wand unified resolver (BRIEF item G, W2) ───────────────
@app.post("/api/papers/from_identifier")
def add_paper_from_identifier(body: dict = Body(...)):
    """Resolve a pasted identifier into a draft Paper (or surface a duplicate).

    Body: ``{"input": "<doi|arxiv|pmcid|pmid|url|freetext>",
             "project_id": "<uuid-or-omit>",
             "kind_hint": "<doi|arxiv|...>"   # optional}``

    Returns one of three envelopes:
        draft:     {status, paper_draft, provenance}
        duplicate: {status, duplicate_of, deep_link, paper_draft, matched_by}
        not_found: {status, input, kind, manual_form, hint}
    """
    payload = body or {}
    raw_input = (payload.get("input") or payload.get("identifier") or "").strip()
    if not raw_input:
        raise HTTPException(
            status_code=400,
            detail="Provide an identifier (DOI, arXiv ID, PMC ID, PMID, URL, or title) in `input`.",
        )
    project_id = payload.get("project_id") or None
    kind_hint = (payload.get("kind_hint") or payload.get("kind") or "").strip().lower() or None

    kind = kind_hint or detect_identifier_kind(raw_input)
    db_session = _db_session()
    try:
        # Pre-lookup dedup for DOI fast-path.
        if kind in ("doi", "eartharxiv", "chemrxiv"):
            normalised = normalize_doi(raw_input)
            if normalised:
                existing = (
                    db_session.query(Paper)
                    .filter(Paper.doi.ilike(normalised))
                    .order_by(Paper.updated_at.desc())
                    .first()
                )
                if existing is not None:
                    return {
                        "status": "duplicate",
                        "duplicate_of": existing.id,
                        "matched_by": "doi",
                        "deep_link": f"/?paper={existing.id}",
                        "paper_draft": {
                            "title": existing.title,
                            "journal": existing.journal,
                            "published_date": existing.published_date,
                            "doi": existing.doi,
                            "authors": existing.authors or [],
                        },
                        "provenance": {"meta_source": {"trace": [], "short_circuit": "pre_lookup_dedup"}},
                    }

        result = lookup_metadata_by_identifier(kind, raw_input)
        if not result or not result.fields or not result.fields.get("title"):
            return {
                "status": "not_found",
                "input": raw_input,
                "kind": kind,
                "manual_form": True,
                "hint": "No metadata returned from Semantic Scholar / OpenAlex / CrossRef. "
                        "Try a different identifier, drop the PDF, or enter the basics manually.",
                "provenance": {
                    "meta_source": {"trace": list((result.errors if result else ()) or [])}
                },
            }

        # Post-lookup dedup: resolver may have normalised arXiv → DOI.
        canonical_doi = normalize_doi(result.fields.get("doi") or "")
        if canonical_doi:
            post_dup = (
                db_session.query(Paper)
                .filter(Paper.doi.ilike(canonical_doi))
                .order_by(Paper.updated_at.desc())
                .first()
            )
            if post_dup is not None:
                return {
                    "status": "duplicate",
                    "duplicate_of": post_dup.id,
                    "matched_by": "doi",
                    "deep_link": f"/?paper={post_dup.id}",
                    "paper_draft": {
                        "title": post_dup.title,
                        "journal": post_dup.journal,
                        "published_date": post_dup.published_date,
                        "doi": post_dup.doi,
                        "authors": post_dup.authors or [],
                    },
                    "provenance": {"meta_source": dict(result.meta_source or {})},
                }

        # Draft envelope.
        fields = result.fields or {}
        abstract = clean_text(fields.get("abstract") or "")
        abstract = re.sub(r"</?jats:[^>]+>", "", abstract)
        return {
            "status": "draft",
            "paper_draft": {
                "title":          fields.get("title", "").strip(),
                "authors":        [a for a in (fields.get("authors") or []) if a],
                "journal":        fields.get("journal") or "",
                "published_date": fields.get("published_date") or "",
                "year":           fields.get("year") or "",
                "doi":            normalize_doi(fields.get("doi") or ""),
                "abstract":       abstract,
                "link":           fields.get("link") or (
                    f"https://doi.org/{fields.get('doi')}" if fields.get("doi") else ""
                ),
                "pdf_url":        fields.get("pdf_url") or "",
                "_input":         {"kind": kind, "value": raw_input},
            },
            "provenance": {
                "meta_source": dict(result.meta_source or {}),
                "meta_conflict_log": list(result.meta_conflict_log or ()),
                "status": result.status,
                "source": result.source,
                "missing_fields": list(result.missing_fields or ()),
            },
        }
    finally:
        db_session.close()


@app.post("/api/papers/from_doi")
def add_paper_from_doi(body: dict = Body(...)):
    """Create a metadata-only Paper row from a DOI.

    Fetches metadata via Crossref `/works/{doi}` (primary) → falls back to
    OpenAlex `/works/https://doi.org/{doi}`. No PDF is uploaded — user can
    upload one later via /api/upload/{paper_id}. The new paper joins the
    specified project (or default Monthly if project_id omitted).

    Body: ``{"doi": "10.xxxx/...", "project_id": "uuid-or-omit"}``

    Returns: ``{paper_id, title, ...}``.
    """
    import requests, hashlib
    raw_doi = (body or {}).get("doi") or ""
    raw_doi = normalize_doi(raw_doi.strip())
    if not raw_doi or "/" not in raw_doi:
        raise HTTPException(status_code=400, detail="Provide a valid DOI like 10.1029/2025gc012821")
    project_id = (body or {}).get("project_id") or None

    db_session = _db_session()
    try:
        # Check if paper already exists (DOI match, case-insensitive)
        existing = (
            db_session.query(Paper)
            .filter(Paper.doi.ilike(raw_doi))
            .order_by(Paper.updated_at.desc())
            .first()
        )
        if existing is not None:
            # Already in library — just add to target project if specified
            if project_id:
                _add_paper_to_default_project(db_session, existing, source="doi_import", project_id=project_id)
                db_session.commit()
            return {
                "status": "already_exists",
                "paper_id": existing.id,
                "title": existing.title,
                "journal": existing.journal,
                "added_to_project": bool(project_id),
            }

        # ── Fetch metadata: Crossref first, OpenAlex fallback ──────────
        meta: Dict[str, Any] = {}
        try:
            cr = requests.get(
                f"https://api.crossref.org/works/{raw_doi}",
                headers={"User-Agent": f"SeismoHub/2.0 (mailto:{os.getenv('PIPELINE_CONTACT_EMAIL') or 'noreply@example.invalid'})"},
                timeout=15,
            )
            if cr.status_code == 200:
                work = cr.json().get("message") or {}
                title_list = work.get("title") or []
                authors_raw = work.get("author") or []
                date_parts = (work.get("issued") or {}).get("date-parts") or [[]]
                date_parts = (date_parts[0] if date_parts else []) + [None, None, None]
                year, month, day = date_parts[0], date_parts[1], date_parts[2]
                published_date = "-".join(
                    [f"{year:04d}" if year else "",
                     f"{month:02d}" if month else "",
                     f"{day:02d}" if day else ""]
                ).rstrip("-") if year else ""
                meta = {
                    "title": (title_list[0] if title_list else "").strip(),
                    "authors": [
                        ((a.get("given", "") + " " + a.get("family", "")).strip()
                         or a.get("name", ""))
                        for a in authors_raw if isinstance(a, dict)
                    ],
                    "journal": (work.get("container-title") or [""])[0],
                    "published_date": published_date,
                    "year": str(year) if year else "",
                    "abstract": (work.get("abstract") or "").strip(),
                    "link": work.get("URL") or f"https://doi.org/{raw_doi}",
                }
        except Exception:
            meta = {}

        # OpenAlex fallback
        if not meta.get("title"):
            try:
                oa = requests.get(
                    f"https://api.openalex.org/works/https://doi.org/{raw_doi}",
                    headers={"User-Agent": f"SeismoHub/2.0 (mailto:{os.getenv('PIPELINE_CONTACT_EMAIL') or 'noreply@example.invalid'})"},
                    timeout=15,
                )
                if oa.status_code == 200:
                    work = oa.json() or {}
                    auth_raw = work.get("authorships") or []
                    venue = ((work.get("primary_location") or {}).get("source") or {}) or work.get("host_venue") or {}
                    meta = {
                        "title": (work.get("title") or "").strip(),
                        "authors": [
                            (a.get("author") or {}).get("display_name", "")
                            for a in auth_raw if isinstance(a, dict)
                        ],
                        "journal": venue.get("display_name", ""),
                        "published_date": work.get("publication_date", ""),
                        "year": str(work.get("publication_year") or ""),
                        "abstract": "",  # OpenAlex returns inverted-index — skip for now
                        "link": (work.get("doi") or f"https://doi.org/{raw_doi}"),
                    }
            except Exception:
                pass

        if not meta.get("title"):
            raise HTTPException(
                status_code=404,
                detail=f"Could not fetch metadata for DOI {raw_doi} from Crossref or OpenAlex.",
            )

        # ── Create Paper row ──────────────────────────────────────────
        paper_id = "doi_" + hashlib.sha1(raw_doi.encode()).hexdigest()[:14]
        # Avoid collision
        if db_session.get(Paper, paper_id):
            paper_id = paper_id + "_" + hashlib.sha1(f"{raw_doi}::{datetime.utcnow()}".encode()).hexdigest()[:6]

        abstract = clean_text(meta.get("abstract") or "")
        # Strip jats:* tags Crossref sometimes returns
        abstract = re.sub(r"</?jats:[^>]+>", "", abstract)

        paper = Paper(
            id=paper_id,
            source="doi",
            title=meta["title"],
            journal=meta.get("journal") or "Others",
            topics=["Others"],
            institutions=[],
            authors=[a for a in (meta.get("authors") or []) if a],
            published_date=meta.get("published_date") or None,
            datetime_utc=meta.get("published_date") or None,
            link=meta.get("link") or f"https://doi.org/{raw_doi}",
            doi=raw_doi,
            abstract=abstract,
            abstract_flag="ok" if len(abstract) >= 100 else ("short" if abstract else "missing"),
        )
        db_session.add(paper)
        db_session.flush()

        # Add to target project (or default)
        _add_paper_to_default_project(db_session, paper, source="doi_import", project_id=project_id)
        db_session.commit()

        return {
            "status": "ok",
            "paper_id": paper.id,
            "title": paper.title,
            "journal": paper.journal,
            "published_date": paper.published_date,
            "authors": paper.authors,
            "doi": paper.doi,
        }
    finally:
        db_session.close()


# ──────────────────────────────────────────────────────────────────
# 「上传遗失 PDF」— 把上传的 PDF 匹配到 EXISTING 条目并补 PDF（不创建新条目）。
# 2026-06-16: 与 /api/papers/upload（创建新条目）分离。决策树（owner 拍板）:
#   ① sha256 命中 → 已存在(跳过)
#   ② DOI 精确：无 PDF → 补；有不同 PDF → conflict(暂存，前端逐篇决定)
#   ③ 标题模糊：≥0.92 且唯一 → 自动补；中置信/多候选 → needs_confirmation;
#      <0.75 → 未匹配
#   ④ 未匹配 → 跳过
# conflict / needs_confirmation 的 PDF 暂存到 downloads/_pending/，前端确认后
# 调 /api/papers/match_pdfs/confirm 挂载。DOI 是主力(精确)，标题只兜底且中
# 置信必转人工，绝不自动挂错。
# ──────────────────────────────────────────────────────────────────
_PENDING_PDF_DIR = DOWNLOADS_DIR / "_pending"


def _stash_pending_pdf(data: bytes, sha: str) -> None:
    _PENDING_PDF_DIR.mkdir(parents=True, exist_ok=True)
    write_pdf_atomic(_PENDING_PDF_DIR / f"{sha}.pdf", data)


def _attach_pdf_to_paper(db_session, paper: Paper, data: bytes, new_sha256: str) -> None:
    """Place PDF bytes onto an EXISTING paper (reuses the watch-folder layout)."""
    previous = Path(paper.pdf_path).resolve() if paper.pdf_path else None
    target_path = pdf_target_path_for_paper(
        paper,
        DOWNLOADS_DIR,
        current_path=previous if previous and is_managed_download_path(previous, DOWNLOADS_DIR) else None,
    )
    write_pdf_atomic(target_path, data)
    paper.pdf_path = str(target_path.resolve())
    paper.pdf_sha256 = new_sha256


# Bound match-triggered analyses to the same budget as the eager daemon so a
# 50-file bulk upload cannot spawn 50 concurrent Gemini-Pro runs.
_MATCH_ANALYSIS_SEM = threading.BoundedSemaphore(max(1, EAGER_ANALYSIS_WORKERS))


def _spawn_pdf_analysis(paper_id: str, pdf_path: str, source: str = "match_pdfs") -> None:
    """Fire-and-forget Gemini-Pro analysis for a freshly-attached PDF.

    Mirrors the watch-folder ``_analyze_async`` closure (see _ingest_pdf_bytes):
    its OWN db session (SQLAlchemy is not thread-safe), ``_claim_analysis`` to
    avoid double-running with the eager daemon, and the shared semaphore to cap
    fan-out. ``_run_analysis`` overwrites any prior metadata-only analysis, so
    this also performs the "supply the full-text PDF later" upgrade.
    """
    if not paper_id or not pdf_path:
        return

    def _work() -> None:
        from services.analyze_service import _claim_analysis, _release_analysis
        if not _claim_analysis(paper_id):
            return  # the eager daemon / another trigger already owns this paper
        session = None
        try:
            session = _db_session()  # inside try so a claim is ALWAYS released
            # Append-only log only — do NOT drive _begin_runtime/_finish_runtime.
            # Up to EAGER_ANALYSIS_WORKERS of these run concurrently and share the
            # single-producer /runtime channel with the eager daemon; begin()
            # clears logs and finish() flips running=False, so concurrent attaches
            # would clobber each other's status. Mirror the eager daemon: silent
            # (progress_callback=None), one breadcrumb via the append-only log.
            with _MATCH_ANALYSIS_SEM:
                _push_runtime_log(f"{source}: analyzing attached PDF {paper_id}", stage=source)
                analyze_paper_from_pdf(paper_id, pdf_path, session)
        except Exception as exc:
            _push_runtime_log(f"{source}: analysis failed for {paper_id}: {exc}", stage=source)
        finally:
            _release_analysis(paper_id)
            if session is not None:
                session.close()

    threading.Thread(target=_work, daemon=True, name=f"match-analyze-{paper_id[:8]}").start()


def _gc_pending_pdfs(max_age_days: int = 7) -> None:
    """Delete stale stashed PDFs so unconfirmed/unresolved uploads don't pile up."""
    try:
        if not _PENDING_PDF_DIR.exists():
            return
        import time
        cutoff = time.time() - max_age_days * 86400
        for p in _PENDING_PDF_DIR.glob("*.pdf"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _match_llm_client():
    """Vertex client for the optional match adjudicator (SEISMO_MATCH_LLM=1)."""
    try:
        from services.analyze_service import _get_vertex_client
        return _get_vertex_client()
    except Exception:
        return None


@app.post("/api/papers/match_pdfs")
async def match_pdfs(files: List[UploadFile] = File(...)):
    """Upload PDFs and match each to an EXISTING paper (补 PDF, never create).

    Uses the shared services.pdf_match_core ladder (sha256 → DOI → arXiv → exact
    title → fuzzy title). A high-confidence hit auto-attaches AND fires a Gemini
    analysis so the paper's AI columns fill in. Everything else — conflicts,
    mid-confidence, and no-match — is STASHED and returned so the frontend can
    surface it for a one-click manual attach; nothing is silently dropped. An
    existing PDF is never overwritten (→ conflict).
    """
    from services.pdf_match_core import LibraryIndex, extract_match_signals, match_signals

    _gc_pending_pdfs()
    match_llm = _match_llm_client() if os.getenv("SEISMO_MATCH_LLM", "0") == "1" else None
    db_session = _db_session()
    results: List[Dict[str, Any]] = []
    summary = {"attached": 0, "conflict": 0, "needs_confirmation": 0,
               "unresolved": 0, "skipped": 0}
    try:
        lib = LibraryIndex(session=db_session)  # built ONCE, reused for every file
        for f in files:
            fname = f.filename or "upload.pdf"
            raw = await f.read()
            if not raw or not raw.startswith(b"%PDF"):
                results.append({"filename": fname, "action": "skipped", "reason": "not_a_pdf"})
                summary["skipped"] += 1
                continue
            if len(raw) > MAX_UPLOAD_BYTES:
                results.append({"filename": fname, "action": "skipped", "reason": "too_large"})
                summary["skipped"] += 1
                continue

            # Per-file guard: one bad file (disk error, a paper deleted
            # mid-batch, a corrupt PDF) must never abort the batch and drop the
            # rest — stash what we can and keep going.
            try:
                sig = extract_match_signals(raw, fname)
                res = match_signals(sig, lib, llm_client=match_llm)
                sha = sig.sha256
                row = lib.by_id.get(res.paper_id) if res.paper_id else None
                top_doi = sig.dois[0] if sig.dois else ""
                # a resolved paper may have been deleted since the index was built
                paper = db_session.get(Paper, row.id) if row is not None else None

                if res.action == "attach" and paper is not None:
                    _attach_pdf_to_paper(db_session, paper, raw, sha)
                    db_session.commit()
                    lib.register_attached(row, sha)
                    _spawn_pdf_analysis(paper.id, paper.pdf_path, "match_pdfs")
                    results.append({"filename": fname, "action": "attached", "method": res.method,
                                    "paper_id": paper.id, "paper_title": paper.title,
                                    "score": res.score, "analysis": "queued"})
                    summary["attached"] += 1

                elif res.action == "already_attached" and row is not None:
                    results.append({"filename": fname, "action": "skipped", "reason": "already_attached",
                                    "paper_id": row.id, "paper_title": row.title})
                    summary["skipped"] += 1

                elif res.action == "conflict" and paper is not None:
                    _stash_pending_pdf(raw, sha)
                    results.append({"filename": fname, "action": "conflict", "method": res.method,
                                    "paper_id": paper.id, "paper_title": paper.title, "pending": sha})
                    summary["conflict"] += 1

                elif res.action == "needs_review":
                    _stash_pending_pdf(raw, sha)
                    results.append({"filename": fname, "action": "needs_confirmation",
                                    "candidates": res.candidates, "pending": sha,
                                    "title": sig.title, "doi": top_doi})
                    summary["needs_confirmation"] += 1

                else:  # 'unmatched' (or a resolved paper vanished) — stash so it is recoverable
                    _stash_pending_pdf(raw, sha)
                    results.append({"filename": fname, "action": "unresolved",
                                    "candidates": res.candidates, "pending": sha,
                                    "title": sig.title, "doi": top_doi})
                    summary["unresolved"] += 1
            except Exception as exc:
                db_session.rollback()
                try:
                    err_sha = sha256_of_bytes(raw)
                    _stash_pending_pdf(raw, err_sha)
                    results.append({"filename": fname, "action": "unresolved", "candidates": [],
                                    "pending": err_sha, "reason": f"error: {exc}"})
                    summary["unresolved"] += 1
                except Exception:
                    results.append({"filename": fname, "action": "skipped", "reason": "error"})
                    summary["skipped"] += 1
        return {"results": results, "summary": summary}
    finally:
        db_session.close()


@app.post("/api/papers/match_pdfs/confirm")
def confirm_match_pdf(body: dict = Body(default=None)):
    """Attach a stashed (needs_confirmation/conflict) PDF to a chosen paper.

    Body: ``{"pending": sha256, "paper_id": str, "replace": bool=false}``.
    """
    body = body or {}
    pending = str(body.get("pending") or "").strip()
    paper_id = str(body.get("paper_id") or "").strip()
    replace = bool(body.get("replace", False))
    if not pending or not paper_id:
        raise HTTPException(status_code=400, detail="pending(sha256) and paper_id are required")
    pending_path = _PENDING_PDF_DIR / f"{pending}.pdf"
    if not pending_path.exists():
        raise HTTPException(status_code=404, detail="pending PDF not found (may have expired)")
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")
        if paper.pdf_path and not replace:
            raise HTTPException(status_code=409, detail={
                "error": "has_pdf",
                "message": "paper already has a PDF; pass replace=true to overwrite",
            })
        try:
            data = pending_path.read_bytes()
        except FileNotFoundError:
            # gc sweep or a concurrent/double confirm removed it after the exists()
            # check above — return the same graceful 404, not a 500.
            raise HTTPException(status_code=404, detail="pending PDF not found (may have expired)")
        _attach_pdf_to_paper(db_session, paper, data, pending)
        db_session.commit()
        _spawn_pdf_analysis(paper.id, paper.pdf_path, "match_confirm")
        try:
            pending_path.unlink()
        except OSError:
            pass
        return {"status": "attached", "paper_id": paper.id, "paper_title": paper.title,
                "analysis": "queued"}
    finally:
        db_session.close()


@app.post("/api/papers/upload")
async def upload_paper(
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    doi: Optional[str] = Form(default=None),
    analyze: bool = Form(default=False),
    project_id: Optional[str] = Form(default=None),
    force_duplicate: bool = Form(default=False),
):
    """Create a new Paper row + store PDF in one shot.

    - SHA-256 dedup: if a paper already has the same pdf_sha256, return 409
      with the existing paper id.
    - Auto-add to the project named by ``project_id``; falls back to the
      seeded default Monthly project when none is supplied.
    - Title / DOI are auto-extracted from the PDF when the caller leaves
      them blank (PyMuPDF first-page heuristic + regex DOI sniff). Failures
      degrade silently to filename-based fallback.
    - Triggers Gemini analysis only when ``analyze=true`` (default False so
      QA / bulk imports don't burn quota).
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    db_session = _db_session()
    try:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file.")
        # FN-B8-A02-1: cap upload size so a malicious or accidental
        # multi-hundred-MB body cannot pin the heap or fill the disk.
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"PDF too large ({len(raw) / 1024 / 1024:.1f} MB); "
                    f"max is {MAX_UPLOAD_BYTES / 1024 / 1024:.0f} MB."
                ),
            )
        if not raw.startswith(b"%PDF"):
            raise HTTPException(
                status_code=400,
                detail="File does not look like a PDF (missing %PDF header).",
            )

        pdf_sha = sha256_of_bytes(raw)
        # SHA-256 dedup against any pre-existing paper row.
        existing = (
            db_session.query(Paper).filter(Paper.pdf_sha256 == pdf_sha).first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_pdf",
                    "message": "An identical PDF is already stored.",
                    "existing_paper_id": existing.id,
                    "existing_title": existing.title,
                },
            )

        # Persist PDF bytes to the managed downloads directory BEFORE auto-
        # extracting metadata so PyMuPDF / extract_pdf_text can read a real
        # file path. We need a temp path because Paper construction (which
        # decides the final filename) hasn't happened yet — write to a UUID
        # stub inside DOWNLOADS_DIR, then move once the paper id is known.
        tmp_path = DOWNLOADS_DIR / f"_upload_{pdf_sha[:16]}.pdf"
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        write_pdf_atomic(tmp_path, raw)

        # PDF auto-extract: when the user hasn't supplied title or DOI we
        # try to fish them out of the first page. Any failure (corrupted
        # PDF, missing PyMuPDF, weird encoding) silently falls through to
        # the filename-based fallback so the upload still succeeds.
        auto_title = ""
        auto_doi = ""
        title_blank = not (title or "").strip()
        doi_blank = not (doi or "").strip()
        if title_blank or doi_blank:
            try:
                auto_title = _extract_pdf_title(tmp_path) or ""
            except Exception:
                auto_title = ""
            try:
                page0_text = _extract_pdf_text_for_upload(
                    str(tmp_path), max_pages=1, max_chars=5000
                )
            except Exception:
                page0_text = ""
            try:
                auto_doi = _extract_doi_from_any(auto_title, page0_text) or ""
            except Exception:
                auto_doi = ""

        # T27: prefer online metadata (CrossRef → OpenAlex → S2 fan-out)
        # over the PDF heuristic when the user didn't supply title + DOI.
        # The heuristic title is often Springer running-header garbage
        # like "Vol.: (0123456789) Geomech. Geophys. Geo-energ. Geo-resour.
        # (2025) 11:60 https://doi.org/10.1007/s40948-025-00972-0 REVIEW
        # ..." — but the DOI sitting in that mess is a clean foreign key
        # we can use to fetch editorial-grade metadata.
        enriched: Optional[Dict[str, Any]] = None
        if title_blank or doi_blank:
            try:
                enriched = _enrich_metadata_from_lookup(auto_doi, auto_title)
            except Exception:
                enriched = None

        # Title resolution: explicit > online lookup > heuristic > filename
        final_title = (
            (title or "").strip()
            or (enriched and (enriched.get("title") or "").strip())
            or auto_title
            or (Path(file.filename).stem if file.filename else "")
            or "Untitled Upload"
        )
        # DOI resolution: explicit > online lookup > heuristic
        final_doi_input = (
            (doi or "").strip()
            or (enriched and (enriched.get("doi") or "").strip())
            or auto_doi
        )
        normalized_doi = _normalize_doi_for_upload(final_doi_input)

        # 创建侧 DOI 查重（2026-06-16）：sha256 去重在前（拦同一文件）；这里再按
        # DOI 拦"同一篇论文的不同文件/已被 tracker 抓过"的重复创建。命中已有且未
        # 显式 force_duplicate → 409，让前端三选（仍创建副本 / 补 PDF 到已有 / 取消）。
        if normalized_doi and not force_duplicate:
            # H4 — case-insensitive DOI match so an upload whose DOI casing differs
            # from a feed-stored row dedups onto it instead of creating a copy.
            existing_by_doi = (
                db_session.query(Paper).filter(func.lower(Paper.doi) == normalized_doi.lower()).first()
            )
            if existing_by_doi is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise HTTPException(status_code=409, detail={
                    "error": "exists_by_doi",
                    "message": "A paper with this DOI already exists.",
                    "existing_paper_id": existing_by_doi.id,
                    "existing_title": existing_by_doi.title,
                })

        # Other fields from the enriched payload (CrossRef shape: authors
        # list, journal, year/published_date, abstract). Empty default
        # mirrors the legacy behaviour when no online hit was had.
        enriched_authors = (enriched and enriched.get("authors")) or []
        enriched_journal = (enriched and (enriched.get("journal") or "").strip()) or ""
        enriched_pub_date = (enriched and (enriched.get("published_date") or "").strip()) or ""
        enriched_abstract = (enriched and (enriched.get("abstract") or "").strip()) or ""
        enriched_link = (enriched and (enriched.get("link") or "").strip()) or ""

        paper_id = _allocate_zotero_id(
            db_session,
            {
                "doi": normalized_doi,
                "title": final_title,
                "year": (enriched_pub_date[:4] if enriched_pub_date else ""),
            },
        )
        paper = Paper(
            id=paper_id,
            source="upload",
            title=final_title,
            doi=normalized_doi or None,
            pdf_sha256=pdf_sha,
            authors=enriched_authors,
            topics=[],
            institutions=[],
            # journal column is NOT NULL with default "Others"; never pass None.
            journal=enriched_journal or "Others",
            published_date=enriched_pub_date or None,
            abstract=enriched_abstract or None,
            abstract_flag=("ok" if enriched_abstract else "missing"),
            link=enriched_link or None,
        )
        db_session.add(paper)
        db_session.flush()

        # Move the temp PDF into the canonical paper-named path.
        target_path = pdf_target_path_for_paper(paper, DOWNLOADS_DIR)
        try:
            if tmp_path.resolve() != target_path.resolve():
                os.replace(tmp_path, target_path)
        except OSError:
            # If rename fails (cross-device, race), re-write the bytes to
            # the canonical path and drop the temp.
            write_pdf_atomic(target_path, raw)
            try:
                tmp_path.unlink()
            except OSError:
                pass
        paper.pdf_path = str(target_path.resolve())
        db_session.commit()

        # Auto-join the requested (or default) project for UI visibility.
        try:
            _add_paper_to_default_project(
                db_session, paper, source="upload", project_id=project_id
            )
            db_session.commit()
        except Exception:
            db_session.rollback()

        analysis_payload = None
        if analyze:
            try:
                analysis_payload = analyze_paper_from_pdf(
                    paper.id,
                    paper.pdf_path,
                    db_session,
                    progress_callback=_push_runtime_log,
                )
            except Exception:
                # Don't fail the upload on analysis error; the bytes are saved.
                analysis_payload = None

        return {
            "paper_id": paper.id,
            "pdf_sha256": pdf_sha,
            "filename": target_path.name,
            "analysis": analysis_payload,
        }
    except HTTPException:
        raise
    finally:
        db_session.close()


@app.post("/api/upload/{paper_id}")
async def upload_pdf(paper_id: str, file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")

        previous_pdf_path = Path(paper.pdf_path).resolve() if paper.pdf_path else None
        contents = await file.read()

        # PDF magic-byte sanity check (cheaper than full validate_pdf_bytes
        # and catches mislabelled uploads early).
        if not contents.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="File does not look like a PDF (missing %PDF header).")

        # Compute SHA-256 of the uploaded bytes for dedup + change detection.
        new_sha256 = sha256_of_bytes(contents)

        # Dedup check: if another paper already has the same PDF bytes (and
        # it's not THIS paper), refuse with a clear pointer to the existing
        # record. Skipped when the existing paper is the same one (re-upload
        # of an identical file is allowed and idempotent).
        duplicate = (
            db_session.query(Paper)
            .filter(Paper.pdf_sha256 == new_sha256, Paper.id != paper_id)
            .first()
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_pdf",
                    "message": "An identical PDF is already stored under another paper.",
                    "existing_paper_id": duplicate.id,
                    "existing_title": duplicate.title,
                },
            )

        target_path = pdf_target_path_for_paper(
            paper,
            DOWNLOADS_DIR,
            current_path=previous_pdf_path if previous_pdf_path and is_managed_download_path(previous_pdf_path, DOWNLOADS_DIR) else None,
        )
        write_pdf_atomic(target_path, contents)
        if (
            previous_pdf_path
            and previous_pdf_path != target_path.resolve()
            and previous_pdf_path.exists()
            and is_managed_download_path(previous_pdf_path, DOWNLOADS_DIR)
        ):
            try:
                previous_pdf_path.unlink()
            except OSError:
                pass

        # Persist the SHA-256 so future uploads can dedup against this row.
        paper.pdf_sha256 = new_sha256
        # Ensure the paper is a member of the default monthly project.
        _add_paper_to_default_project(db_session, paper, source="upload")
        db_session.flush()

        # Uploaded PDFs immediately replace the current local file association and re-run analysis.
        _begin_runtime("upload")
        _push_runtime_log(f"PDF upload received; re-analyzing paper {paper_id} with the uploaded PDF", stage="upload")
        payload = analyze_paper_from_pdf(paper_id, str(target_path), db_session, progress_callback=_push_runtime_log)
        _finish_runtime("PDF upload and re-analysis completed", stage="done", analyzed=1, uploaded_paper_id=paper_id)
        return {"status": "ok", "paper_id": paper_id, "analysis": payload, "pdf_sha256": new_sha256}
    except HTTPException:
        # Don't wrap explicit HTTPExceptions (e.g. 409 duplicate) into 500.
        raise
    except RuntimeError:
        # sec(M1): runtime cleanup happens here; response is now the generic
        # envelope. RuntimeError from analyze_paper_from_pdf often carries
        # internal library/file-path detail we don't want to echo to clients.
        envelope = _error_envelope(
            "Something went wrong while re-analyzing the uploaded PDF.",
        )
        _finish_runtime("PDF re-analysis failed", stage="error")
        _logger.exception("error_id=%s in upload_pdf", envelope["error_id"])
        return JSONResponse(status_code=500, content=envelope)
    except ValueError as exc:
        _finish_runtime(f"PDF re-analysis failed: {exc}", stage="error")
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        db_session.close()


@app.post("/api/reanalyze/{paper_id}")
def reanalyze_paper(paper_id: str):
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        if not paper.pdf_path:
            raise HTTPException(status_code=400, detail="No PDF is available for this paper yet.")

        _begin_runtime("reanalyze")
        _push_runtime_log(f"Manual re-analysis started for paper {paper_id} using the stored PDF", stage="upload")
        payload = analyze_paper_from_pdf(paper_id, paper.pdf_path, db_session, progress_callback=_push_runtime_log)
        _finish_runtime("Manual PDF re-analysis completed", stage="done", analyzed=1, reanalyzed_paper_id=paper_id)
        return {"status": "ok", "paper_id": paper_id, "analysis": payload}
    except RuntimeError:
        # sec(M1): runtime cleanup still runs, but client gets the generic
        # envelope rather than the raw RuntimeError message.
        envelope = _error_envelope(
            "Something went wrong while re-analyzing the PDF.",
        )
        _finish_runtime("Manual PDF re-analysis failed", stage="error")
        _logger.exception("error_id=%s in reanalyze_paper", envelope["error_id"])
        return JSONResponse(status_code=500, content=envelope)
    except ValueError as exc:
        _finish_runtime(f"Manual PDF re-analysis failed: {exc}", stage="error")
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        db_session.close()


@app.get("/api/reanalyze_batch/preview")
def reanalyze_batch_preview():
    """Return counts of papers eligible for batch re-analysis.

    A paper is eligible when it has a PDF but its ``structured_analysis``
    payload is empty (i.e. was analysed before G4 was introduced, or
    analysis failed silently).

    Response: ``{eligible, total_with_pdf}``
    """
    from models import AIAnalysis

    db_session = _db_session()
    try:
        papers_with_pdf = db_session.query(Paper).filter(Paper.pdf_path.isnot(None)).all()
        total_with_pdf = len(papers_with_pdf)
        eligible = 0
        for paper in papers_with_pdf:
            analysis = paper.ai_analysis
            payload = analysis.payload if analysis else None
            sa = (payload or {}).get("structured_analysis") or {}
            has_structured = any(v and v.get("text") for v in sa.values()) if isinstance(sa, dict) else False
            if not has_structured:
                eligible += 1
        return {"eligible": eligible, "total_with_pdf": total_with_pdf}
    finally:
        db_session.close()


@app.post("/api/reanalyze_batch")
def reanalyze_batch(dry_run: bool = Query(default=False)):
    """Re-analyse all papers that have a PDF but lack a ``structured_analysis``.

    Runs **synchronously** in a background thread (same as the single-paper
    reanalyze endpoint) so the caller gets a streaming runtime log.  For large
    libraries (>30 papers) prefer triggering this during off-hours.

    Query params
    ------------
    dry_run : bool
        If ``true``, return the list of eligible paper IDs without running any
        analysis.  Useful for a confirmation step in the UI.
    """
    db_session = _db_session()
    try:
        papers_with_pdf = (
            db_session.query(Paper)
            .filter(Paper.pdf_path.isnot(None))
            .order_by(Paper.published_date.desc())
            .all()
        )
        eligible_papers = []
        for paper in papers_with_pdf:
            analysis = paper.ai_analysis
            payload = analysis.payload if analysis else None
            sa = (payload or {}).get("structured_analysis") or {}
            has_structured = any(v and v.get("text") for v in sa.values()) if isinstance(sa, dict) else False
            if not has_structured:
                eligible_papers.append(paper)

        if dry_run:
            return {
                "dry_run": True,
                "eligible": len(eligible_papers),
                "paper_ids": [p.id for p in eligible_papers],
            }

        if not eligible_papers:
            return {"status": "ok", "analyzed": 0, "skipped": 0, "errors": []}

        _begin_runtime("reanalyze_batch")
        _push_runtime_log(
            f"Batch re-analysis started: {len(eligible_papers)} paper(s) without structured analysis",
            stage="upload",
        )

        analyzed = 0
        errors = []
        for paper in eligible_papers:
            try:
                _push_runtime_log(f"Re-analyzing {paper.id} — {paper.title[:60]}", stage="analyze")
                # Re-open a fresh session per paper to avoid long-lived transactions.
                inner_session = _db_session()
                try:
                    analyze_paper_from_pdf(paper.id, paper.pdf_path, inner_session, progress_callback=None)
                    analyzed += 1
                finally:
                    inner_session.close()
            except Exception as exc:
                errors.append({"paper_id": paper.id, "error": str(exc)})
                _push_runtime_log(f"Error re-analyzing {paper.id}: {exc}", stage="error")

        _finish_runtime(
            f"Batch re-analysis complete: {analyzed} analyzed, {len(errors)} error(s)",
            stage="done",
            analyzed=analyzed,
        )
        return {"status": "ok", "analyzed": analyzed, "skipped": 0, "errors": errors}

    except Exception:
        # sec(M1): runtime cleanup remains, but client gets the generic
        # envelope. Full traceback is logged for debugging.
        envelope = _error_envelope(
            "Something went wrong during batch re-analysis.",
        )
        _finish_runtime("Batch re-analysis failed", stage="error")
        _logger.exception("error_id=%s in reanalyze_batch", envelope["error_id"])
        return JSONResponse(status_code=500, content=envelope)
    finally:
        db_session.close()


@app.post("/api/refresh")
@app.post("/api/search")
def refresh_papers(
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
):
    # sec(M1): no try/except around the catch-all anymore — the global
    # exception handler logs the traceback and returns the generic envelope.
    # `_run_refresh_pipeline` still performs its own _finish_runtime cleanup
    # on error before re-raising (see line ~620), so observability is intact.
    resolved_date_from = _parse_iso_date(date_from, field_name="date_from")
    resolved_date_to = _parse_iso_date(date_to, field_name="date_to")
    if resolved_date_from and resolved_date_to and resolved_date_from > resolved_date_to:
        raise HTTPException(status_code=400, detail="date_from must be earlier than or equal to date_to.")
    return _run_refresh_pipeline(trigger="manual", date_from=resolved_date_from, date_to=resolved_date_to)


@app.delete("/api/papers/{paper_id}")
def delete_paper(paper_id: str):
    """Permanently delete a paper and all its references.

    Bulk-deletes ProjectPaper / AIAnalysis / PaperPageCache rows (explicit, to
    avoid ORM cascade NULLing the PaperPageCache PK — see FN-B10-A02-1), then
    deletes the Paper row; managed PDFs under DOWNLOADS_DIR are unlinked from
    disk. Does NOT touch vault markdown. Returns {status, paper_id, title}.
    """
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")

        pdf_path = Path(paper.pdf_path).resolve() if paper.pdf_path else None
        title = paper.title
        # FIX 2026-05-19 (FN-B10-A02-1): explicitly clean up child rows before
        # deleting the Paper row. Even with FK ON DELETE CASCADE declared
        # (FN-B1-A12-1), ORM-level cascade can leave stale session state when
        # a paper is referenced by multiple project_papers / ai_analyses rows,
        # producing a 500 on DELETE /api/papers/{id}. Issue bulk deletes first
        # so the final db_session.delete(paper) has no remaining dependents.
        from models import AIAnalysis
        db_session.query(ProjectPaper).filter(ProjectPaper.paper_id == paper_id).delete(synchronize_session=False)
        db_session.query(AIAnalysis).filter(AIAnalysis.paper_id == paper_id).delete(synchronize_session=False)
        # FIX 2026-05-19 (extends FN-B10-A02-1): also delete paper_page_caches
        # rows. The PaperPageCache.paper_id is both PK and FK; ORM cascade
        # tries to NULL it (which fails — PK can't be NULL), raising
        # `AssertionError: Dependency rule tried to blank-out primary key
        #  column 'paper_page_caches.paper_id'`. Explicit bulk delete avoids
        # the ORM cascade path and just removes the rows directly.
        db_session.query(PaperPageCache).filter(PaperPageCache.paper_id == paper_id).delete(synchronize_session=False)
        db_session.delete(paper)
        db_session.commit()

        downloads_root = DOWNLOADS_DIR.resolve()
        is_managed_pdf = bool(
            pdf_path
            and pdf_path.exists()
            and (
                getattr(pdf_path, "is_relative_to", None) and pdf_path.is_relative_to(downloads_root)
                or str(pdf_path).startswith(f"{downloads_root}{os.sep}")
            )
        )
        if is_managed_pdf:
            try:
                pdf_path.unlink()
            except OSError:
                pass

        return {"status": "ok", "paper_id": paper_id, "title": title}
    finally:
        db_session.close()


def _serialize_retrieval_attempt(row: RetrievalAttempt) -> Dict[str, Any]:
    return {
        "id": row.id,
        "stage": row.stage,
        "source": row.source,
        "url_attempted": row.url_attempted,
        "outcome": row.outcome,
        "http_status": row.http_status,
        "bytes_downloaded": row.bytes_downloaded,
        "error_reason": row.error_reason,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "duration_ms": row.duration_ms,
    }


# ------------------------------------------------------------------
# Deprecated PDF crawler endpoints (M1 F12).
# The 5-stage acquisition pipeline was removed in favour of upload-only
# ingestion. These routes are kept as `410 Gone` stubs so any cached
# frontend / bookmark still gets a clear migration hint pointing at the
# new flows: `/api/upload/{id}` for one-off uploads and
# `/api/watch-folders` for automatic ingestion.
# ------------------------------------------------------------------

_ACQUIRE_GONE_BODY = {
    "error": "endpoint_removed",
    "message": (
        "The PDF acquisition pipeline was removed. Upload PDFs via "
        "POST /api/upload/{paper_id}, or configure a watch folder via "
        "POST /api/watch-folders (then POST /api/watch-folders/rescan)."
    ),
    "alternatives": [
        "POST /api/upload/{paper_id}",
        "POST /api/watch-folders",
        "POST /api/watch-folders/rescan",
    ],
}


@app.post("/api/papers/{paper_id}/acquire_pdf")
def acquire_paper_pdf(paper_id: str):  # noqa: ARG001 — kept for route parity
    raise HTTPException(status_code=410, detail=_ACQUIRE_GONE_BODY)


@app.post("/api/acquire_pdf/batch")
def acquire_pdf_batch_endpoint(max: int = Query(default=5, ge=1, le=20)):  # noqa: ARG001
    raise HTTPException(status_code=410, detail=_ACQUIRE_GONE_BODY)


@app.get("/api/papers/{paper_id}/retrieval_history")
def get_retrieval_history(paper_id: str):
    db_session = _db_session()
    try:
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        rows = (
            db_session.query(RetrievalAttempt)
            .filter(RetrievalAttempt.paper_id == paper_id)
            .order_by(RetrievalAttempt.started_at.desc())
            .all()
        )
        return {
            "paper_id": paper_id,
            "retrieval": {
                "status": paper.retrieval_status,
                "source": paper.retrieval_source,
                "source_url": paper.retrieval_source_url,
                "last_stage": paper.retrieval_last_stage,
                "error_reason": paper.retrieval_error_reason,
                "attempts_count": paper.retrieval_attempts_count,
                "attempted_at": paper.retrieval_attempted_at.isoformat() if paper.retrieval_attempted_at else None,
                "succeeded_at": paper.retrieval_succeeded_at.isoformat() if paper.retrieval_succeeded_at else None,
                "next_retry_at": paper.retrieval_next_retry_at.isoformat() if paper.retrieval_next_retry_at else None,
            },
            "attempts": [_serialize_retrieval_attempt(row) for row in rows],
        }
    finally:
        db_session.close()


@app.post("/api/acquire_pdf/reset_session")
def reset_acquisition_session():
    raise HTTPException(status_code=410, detail=_ACQUIRE_GONE_BODY)


if __name__ == "__main__":
    uvicorn_run(app, host=SERVER_HOST, port=SERVER_PORT, reload=False)
