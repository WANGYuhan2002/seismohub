"""Watch-folder auto-ingest (M1 F10–F11).

Replaces the deprecated PDF crawler. The user picks a directory (typically
their Zotero ``storage/`` folder or ``~/Downloads``) and this service watches
for new ``.pdf`` files. Each newly observed file is:

1. SHA-256 hashed (dedup against ``papers.pdf_sha256``).
2. Probed for a DOI (first two pages of the PDF text, fallback to filename).
3. Enriched against OpenAlex / Crossref to fill title / authors / journal.
4. Inserted as a new ``Paper`` row + analyzed by ``analyze_paper_from_pdf``.

Public API:

- :func:`save_watch_folders` / :func:`load_watch_folders` — persist the
  user's folder list in ``AppState``.
- :func:`start_watcher` / :func:`stop_watcher` — manage the background
  ``watchdog.Observer``. Idempotent on repeated start.
- :func:`scan_folder_once` — run a one-shot sweep (called at startup and
  on explicit "Rescan" from the UI).
- :func:`process_pdf_file` — the per-file ingest routine; exposed so tests
  can drive it without spinning up a watcher.

The watcher uses :class:`watchdog.observers.polling.PollingObserver` on
macOS because FSEvents misses Zotero atomic-renames in practice (see the
risk register R2 in BRAINSTORM_ROADMAP).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:  # PyMuPDF is already a project dep; degrade gracefully if missing
    import fitz  # type: ignore
except ImportError:  # pragma: no cover
    fitz = None

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers.polling import PollingObserver
except ImportError:  # pragma: no cover — dep added in requirements.txt
    PollingObserver = None  # type: ignore
    FileSystemEventHandler = object  # type: ignore


logger = logging.getLogger(__name__)


WATCH_FOLDERS_KEY = "watch_folders_json"
_DEBOUNCE_SEC = 2.0
_RECENT_TS: Dict[str, float] = {}

# Module-level singleton observer (only one can be running at a time).
_observer: Optional["PollingObserver"] = None
_observer_lock = threading.Lock()
_observer_paths: List[Path] = []

# DOI regex (top of services/analyze_service.py already has this — re-stated
# here to keep the module self-contained for tests).
_DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Persisted watch-folder list (stored as JSON in AppState)
# ---------------------------------------------------------------------------

def load_watch_folders(db_session) -> List[str]:
    from models import AppState  # local import to avoid cycle

    state = db_session.get(AppState, WATCH_FOLDERS_KEY)
    if not state or not state.value:
        return []
    try:
        data = json.loads(state.value)
        if isinstance(data, list):
            return [str(p) for p in data if isinstance(p, str)]
    except (ValueError, TypeError):
        pass
    return []


def save_watch_folders(db_session, folders: List[str]) -> List[str]:
    from models import AppState

    # Normalize + de-dup
    cleaned: List[str] = []
    seen: set[str] = set()
    for raw in folders:
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        cleaned.append(resolved)

    state = db_session.get(AppState, WATCH_FOLDERS_KEY)
    payload = json.dumps(cleaned)
    if state is None:
        state = AppState(key=WATCH_FOLDERS_KEY, value=payload)
        db_session.add(state)
    else:
        state.value = payload
    db_session.commit()
    return cleaned


# ---------------------------------------------------------------------------
# DOI extraction + metadata
# ---------------------------------------------------------------------------

def extract_doi_from_pdf(path: Path, *, max_pages: int = 2) -> Optional[str]:
    """Return the first DOI found in the first ``max_pages`` of a PDF, or None."""
    if fitz is None or not path.is_file():
        return None
    try:
        with fitz.open(path) as doc:
            for page_index in range(min(max_pages, len(doc))):
                text = doc[page_index].get_text() or ""
                match = _DOI_RE.search(text)
                if match:
                    return match.group(1).rstrip(").,;:")
    except Exception as exc:  # pragma: no cover
        logger.warning("PDF DOI extraction failed for %s: %s", path, exc)
    return None


def extract_doi_from_name(path: Path) -> Optional[str]:
    """Look for a DOI substring or arXiv id inside the filename itself."""
    match = _DOI_RE.search(path.name)
    if match:
        return match.group(1).rstrip(").,;:")
    arxiv_match = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", path.stem)
    if arxiv_match:
        return f"arXiv:{arxiv_match.group(1)}"
    return None


# ---------------------------------------------------------------------------
# Per-file ingest
# ---------------------------------------------------------------------------

# We keep a callback contract instead of importing main.py to avoid a
# circular import. main.py registers its analyze + dedup helpers via
# ``configure_ingest_callbacks()``.

IngestCallbacks = Dict[str, Callable[..., Any]]
_callbacks: IngestCallbacks = {}


def configure_ingest_callbacks(callbacks: IngestCallbacks) -> None:
    """Wire up the integration points (called once at startup from main.py)."""
    _callbacks.update(callbacks)


def process_pdf_file(path: Path, *, source: str = "watch_folder") -> Dict[str, Any]:
    """Ingest a single PDF.

    Returns a status dict ``{status, paper_id?, reason?}`` describing what
    happened. Status values:

    - ``"skipped_not_pdf"``: file is not a PDF
    - ``"skipped_duplicate"``: SHA-256 already in DB
    - ``"ingested_existing"``: matched an existing paper by DOI and PDF copied
    - ``"ingested_new"``: created a brand-new paper row
    - ``"error"``: ingest failed, ``reason`` carries the message
    """
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        return {"status": "skipped_not_pdf"}
    if not _callbacks:
        return {"status": "error", "reason": "ingest_callbacks_not_configured"}

    try:
        data = path.read_bytes()
    except OSError as exc:
        return {"status": "error", "reason": f"read_failed: {exc}"}

    if not data.startswith(b"%PDF"):
        return {"status": "skipped_not_pdf"}

    # Hand off to the registered ingester. main.py owns the DB session
    # lifecycle and the analyze pipeline.
    try:
        return _callbacks["ingest_pdf_bytes"](
            path=path,
            data=data,
            source=source,
            doi_from_pdf=extract_doi_from_pdf(path),
            doi_from_name=extract_doi_from_name(path),
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("Watch-folder ingest failed for %s", path)
        return {"status": "error", "reason": str(exc)}


# ---------------------------------------------------------------------------
# Watchdog event handler + folder sweep
# ---------------------------------------------------------------------------

class _PdfHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Debounced PDF file handler."""

    def on_created(self, event):  # type: ignore[override]
        if event.is_directory:
            return
        self._maybe_handle(Path(event.src_path))

    def on_moved(self, event):  # type: ignore[override]
        if event.is_directory:
            return
        self._maybe_handle(Path(event.dest_path))

    def on_modified(self, event):  # type: ignore[override]
        # Zotero writes atomically by renaming a temp file; we still want
        # to handle plain copies that end with a modify event.
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".pdf":
            return
        self._maybe_handle(path)

    @staticmethod
    def _maybe_handle(path: Path) -> None:
        if path.suffix.lower() != ".pdf":
            return
        key = str(path.resolve())
        now = time.time()
        if (now - _RECENT_TS.get(key, 0.0)) < _DEBOUNCE_SEC:
            return
        _RECENT_TS[key] = now

        # Fire off the actual ingest in a worker thread so the watchdog
        # event loop stays snappy. Wait a beat first so atomic renames
        # finish flushing.
        def _delayed() -> None:
            time.sleep(_DEBOUNCE_SEC / 2)
            try:
                result = process_pdf_file(path, source="watch_folder")
                logger.info("watch_folder: %s -> %s", path.name, result.get("status"))
                if _callbacks.get("on_event"):
                    _callbacks["on_event"](path, result)
            except Exception:  # pragma: no cover
                logger.exception("watch_folder handler crashed")

        threading.Thread(target=_delayed, daemon=True).start()


def scan_folder_once(folder: Path) -> Dict[str, int]:
    """Walk `folder` recursively and ingest every PDF found (idempotent).

    Returns counts ``{seen, ingested_new, ingested_existing, duplicates, errors}``.
    """
    counts = {"seen": 0, "ingested_new": 0, "ingested_existing": 0, "duplicates": 0, "errors": 0}
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return counts
    for pdf in folder.rglob("*.pdf"):
        counts["seen"] += 1
        try:
            result = process_pdf_file(pdf, source="watch_folder_scan")
        except Exception:
            counts["errors"] += 1
            continue
        s = result.get("status", "error")
        if s == "ingested_new":
            counts["ingested_new"] += 1
        elif s == "ingested_existing":
            counts["ingested_existing"] += 1
        elif s == "skipped_duplicate":
            counts["duplicates"] += 1
        elif s == "error":
            counts["errors"] += 1
    return counts


# ---------------------------------------------------------------------------
# Observer lifecycle
# ---------------------------------------------------------------------------

def start_watcher(paths: List[str]) -> bool:
    """(Re-)start the polling observer over ``paths``. Returns True on success."""
    if PollingObserver is None:
        logger.warning("watchdog not installed; watch_folder disabled")
        return False

    global _observer, _observer_paths
    with _observer_lock:
        stop_watcher()
        observer = PollingObserver(timeout=1.0)
        handler = _PdfHandler()
        valid: List[Path] = []
        for raw in paths:
            path = Path(raw).expanduser()
            if not path.is_dir():
                logger.warning("watch_folder skip (not a dir): %s", path)
                continue
            try:
                observer.schedule(handler, str(path), recursive=True)
                valid.append(path)
            except Exception:
                logger.exception("watch_folder schedule failed for %s", path)
        if not valid:
            return False
        observer.daemon = True
        observer.start()
        _observer = observer
        _observer_paths = valid
        logger.info("watch_folder observing: %s", [str(p) for p in valid])
        return True


def stop_watcher() -> None:
    global _observer, _observer_paths
    if _observer is not None:
        try:
            _observer.stop()
            _observer.join(timeout=2.0)
        except Exception:
            pass
        _observer = None
        _observer_paths = []


def observer_status() -> Dict[str, Any]:
    return {
        "running": _observer is not None and _observer.is_alive() if _observer else False,
        "paths": [str(p) for p in _observer_paths],
        "polling_interval_sec": _DEBOUNCE_SEC,
    }


__all__ = [
    "WATCH_FOLDERS_KEY",
    "configure_ingest_callbacks",
    "extract_doi_from_name",
    "extract_doi_from_pdf",
    "load_watch_folders",
    "observer_status",
    "process_pdf_file",
    "save_watch_folders",
    "scan_folder_once",
    "start_watcher",
    "stop_watcher",
]
