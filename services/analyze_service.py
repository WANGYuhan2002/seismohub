import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from sqlalchemy import or_, text as _sa_text
from tenacity import retry, stop_after_attempt, wait_exponential

from models import AIAnalysis, Paper, Project, ProjectPaper
from services.llm_client import display_name_for_model
# NOTE: ``display_name_for_model`` is the same helper PR-06 added to
# ``services/llm_client.py``.  We import it here (not redefine it) so the
# QA-chat pill and the AI-analysis-card pill stay in lockstep when the
# display-name dict gains future entries (Claude / GPT).
from services.cos_topics import (
    COS_TOPICS,
    COS_TOPIC_RENAMES,
    METHODS,
    DATA_TYPES,
    TARGETS,
    PAPER_ROLES,
    MAX_PRIMARY_TOPICS,
)
from services.pdf_page_cache import (
    FITZ_LOCK,
    get_or_create_cache as _cache_pages,
    get_paragraphs_by_page,
    find_paragraph_by_opening_sentence,
)

# PERFORMANCE.md follow-up (Fix F): COS_TOPICS is a small list (≤10) that
# every paper ingest tests membership against in ``coerce_topics_to_preset``
# and ``_normalize_topic_matches``. ``list.__contains__`` is O(n); a frozenset
# is O(1). Build once at import time, never mutate.
_COS_TOPICS_SET: frozenset = frozenset(COS_TOPICS)


load_dotenv()

try:
    import fitz  # type: ignore
except Exception:
    fitz = None


# Core analysis logic ported directly from 2_analyze_papers_api.py.
MIN_ABSTRACT_CHARS = 100
SUMMARY_MAX_WORDS_EN = 150
# 2026-06-13: paper-analysis model routing. Gemini **Pro** is used ONLY when a
# PDF (bytes or extracted full text) is actually analyzed with the flagship
# **Pro**; the quick / no-PDF (title + abstract + web-context) path uses the
# newest **Flash**. 2026-07-10: re-probed this Vertex — gemini-3.1-pro-preview
# and gemini-3.5-flash now BOTH resolve (the old "3.1-pro-preview 404s here" note
# is stale), so we promote to them. gemini-3.1-pro is a reasoning model: its
# thinking tokens draw from max_output_tokens, so keep that budget generous (see
# _make_generation_config) or the JSON truncates. Override via env if needed.
# 2026-10-16: Gemini 2.5 retires on Vertex 2026-10-20, so the quick fallback moves
# from gemini-2.5-flash-lite to gemini-3.5-flash-lite (GA, retires >= 2027-07-21).
# Probed through _get_vertex_client() + _make_generation_config(): valid JSON, ~0.8 s.
# 2026-11-09: owner switched the PDF-analysis model to gemini-4-argon (was
# gemini-3.1-pro-preview; fallback unchanged). Argon is NOT served on the express
# route this API key uses (europe-west1 -> 404), so _run_analysis sends it on the
# project-scoped global path via services/vertex_model_routing.wire_model_name().
PDF_ANALYSIS_MODEL = os.environ.get("SEISMO_PDF_ANALYSIS_MODEL") or "gemini-4-argon"
PDF_ANALYSIS_FALLBACK = "gemini-3.5-flash"
QUICK_ANALYSIS_MODEL = os.environ.get("SEISMO_METADATA_ANALYSIS_MODEL") or "gemini-3.5-flash"
QUICK_ANALYSIS_FALLBACK = "gemini-3.5-flash-lite"
# Generic defaults — used only if call_gemini() is invoked without an explicit
# model, and as the honest-record fallback when _last_model_used is unset.
PRIMARY_MODEL = PDF_ANALYSIS_MODEL
FALLBACK_MODEL = PDF_ANALYSIS_FALLBACK
PREFER_PDF = True
SEND_PDF_BYTES_TO_GEMINI = True
MAX_PDF_MB_TO_SEND = 30
PDF_TEXT_MAX_PAGES = 30
PDF_TEXT_MAX_CHARS = 120_000
FIGURE_SCAN_MAX_PAGES = 40
FIGURE_ANALYSES_MAX_ITEMS = 8
FIGURE_CAPTION_MAX_CHARS = 420
HTTP_TIMEOUT = 25
USE_GOOGLE_CSE = True
GOOGLE_NUM_RESULTS = 5

# Sentinel used when a paper's free-form topics can't be coerced into any
# preset. Surfaced in the UI as the topic badge for those papers.
UNABLE_TO_CATEGORIZE = "Unable to categorize"

# Free-form topic strings → canonical preset topic. Used to coerce drifted
# labels (e.g. "Fibre-optic seismology / DAS" — old display alias) and common
# BibTeX keywords (e.g. "DAS", "distributed acoustic sensing") back to the
# preset COS_TOPICS list. Keys are LOWERCASED for case-insensitive lookup.
# Anything that doesn't map and isn't a literal preset → UNABLE_TO_CATEGORIZE.
# NOTE (Stage B): the values below are the LEGACY 7-topic labels. The preset
# guard in coerce_topics_to_preset() drops anything not in the current
# COS_TOPICS, so these entries are INERT until remapped to the v3 vocab
# (tracked follow-up). The AI classifier emits v3 primary topics directly.
TOPIC_ALIASES: Dict[str, str] = {
    # ── former display labels with extra suffixes ────────────────────────
    "fibre-optic seismology / das":                                   "Fibre-optic seismology",
    "full-waveform inversion (fwi) & adjoint imaging":                "Full-waveform inversion",
    "ambient noise seismology & interferometry":                      "Ambient noise seismology",
    "inverse theory & uncertainty quantification":                    "Inverse theory",
    "cryosphere / cryoseismology":                                    "Cryosphere Research",
    "numerical wave propagation (seismic) & large-scale simulation":  "Numerical wave propagation and HPC",
    # ── common BibTeX-keyword variants of Fibre-optic seismology ─────────
    "das":                            "Fibre-optic seismology",
    "distributed acoustic sensing":   "Fibre-optic seismology",
    "distributed vibration sensing":  "Fibre-optic seismology",
    "fibre optic":                    "Fibre-optic seismology",
    "fibre optics":                   "Fibre-optic seismology",
    "fibre optic cables":             "Fibre-optic seismology",
    "fibre optics and optical communications": "Fibre-optic seismology",
    "fiber-optic":                    "Fibre-optic seismology",
    "fiber-optic seismology":         "Fibre-optic seismology",
    "fiber optic":                    "Fibre-optic seismology",
    "fiber optics":                   "Fibre-optic seismology",
    "directional sensitivity":        "Fibre-optic seismology",  # always DAS context here
    "multicomponent sensing":         "Fibre-optic seismology",  # ditto
    "forward transmission":           "Fibre-optic seismology",  # ditto
    # ── Full-waveform inversion synonyms ─────────────────────────────────
    "fwi":                            "Full-waveform inversion",
    "full waveform inversion":        "Full-waveform inversion",
    "adjoint imaging":                "Full-waveform inversion",
    # ── Inverse theory ───────────────────────────────────────────────────
    "inversion":                      "Inverse theory",
    "inverse problem":                "Inverse theory",
    "uncertainty quantification":     "Inverse theory",
    "bayesian inversion":             "Inverse theory",
    # ── Ambient noise ────────────────────────────────────────────────────
    "ambient noise":                  "Ambient noise seismology",
    "interferometry":                 "Ambient noise seismology",
    "noise cross-correlation":        "Ambient noise seismology",
    # ── Cryosphere ───────────────────────────────────────────────────────
    "cryoseismology":                 "Cryosphere Research",
    "cryosphere":                     "Cryosphere Research",
    "glacial earthquake":             "Cryosphere Research",
    "icequake":                       "Cryosphere Research",
    # ── Medical ultrasound ───────────────────────────────────────────────
    "ultrasound":                     "Medical ultrasound imaging",
    "ultrasound tomography":          "Medical ultrasound imaging",
    "medical imaging":                "Medical ultrasound imaging",
    # ── Numerical wave propagation / HPC ─────────────────────────────────
    "numerical wave propagation":     "Numerical wave propagation and HPC",
    "wave propagation":               "Numerical wave propagation and HPC",
    "spectral element":               "Numerical wave propagation and HPC",
    "salvus":                         "Numerical wave propagation and HPC",
    "high performance computing":     "Numerical wave propagation and HPC",
    "hpc":                            "Numerical wave propagation and HPC",
}


# Title-keyword sniffing — last-resort classifier when neither the AI's
# cos_topic_match nor any TOPIC_ALIASES entry matches. Key = substring (case
# insensitive, matched against `paper.title + " " + paper.abstract[:200]`),
# Value = preset COS_TOPIC. First match wins; order matters.
# NOTE (Stage B): legacy 7-topic values — inert under the preset guard until
# remapped to the v3 vocab (tracked follow-up).
TITLE_KEYWORD_HINTS: List[Tuple[str, str]] = [
    # ── Fibre-optic seismology / DAS ──────────────────────────────────────
    ("distributed acoustic sensing", "Fibre-optic seismology"),
    ("distributed vibration sensing", "Fibre-optic seismology"),
    # "das" as a whole word — also catch "das:", "das,", "das." etc by
    # checking common punctuation endings explicitly.
    (" das ",                         "Fibre-optic seismology"),
    (" das:",                         "Fibre-optic seismology"),
    (" das,",                         "Fibre-optic seismology"),
    (" das.",                         "Fibre-optic seismology"),
    (" das-",                         "Fibre-optic seismology"),
    ("das acquisition",               "Fibre-optic seismology"),
    ("das array",                     "Fibre-optic seismology"),
    ("das data",                      "Fibre-optic seismology"),
    ("das sensitivity",               "Fibre-optic seismology"),
    # Submarine / telecom fibre — fibre-optic seismology subdomain
    ("submarine cable",               "Fibre-optic seismology"),
    ("submarine fiber",               "Fibre-optic seismology"),
    ("submarine fibre",               "Fibre-optic seismology"),
    ("telecom cable",                 "Fibre-optic seismology"),
    ("seafloor cable",                "Fibre-optic seismology"),
    ("laser interferometry",          "Fibre-optic seismology"),  # cable-strain sensing
    ("strain field reconstruction",   "Fibre-optic seismology"),
    ("seismic tensor strain",         "Fibre-optic seismology"),
    ("dark fiber",                    "Fibre-optic seismology"),
    ("dark fibre",                    "Fibre-optic seismology"),
    ("dark-fiber",                    "Fibre-optic seismology"),
    ("dark-fibre",                    "Fibre-optic seismology"),
    ("fiber-optic",                   "Fibre-optic seismology"),
    ("fibre-optic",                   "Fibre-optic seismology"),
    ("fiber optic",                   "Fibre-optic seismology"),
    ("fibre optic",                   "Fibre-optic seismology"),
    ("helically wound",               "Fibre-optic seismology"),
    ("helical-winding",               "Fibre-optic seismology"),
    ("helical winding",               "Fibre-optic seismology"),
    ("hwc-das",                       "Fibre-optic seismology"),
    ("optical fiber",                 "Fibre-optic seismology"),
    ("optical fibre",                 "Fibre-optic seismology"),
    # ── Cryosphere ────────────────────────────────────────────────────────
    ("cryosphere",         "Cryosphere Research"),
    ("cryoseismology",     "Cryosphere Research"),
    ("ice sheet",          "Cryosphere Research"),
    ("ice shelf",          "Cryosphere Research"),
    ("glacial earthquake", "Cryosphere Research"),
    ("icequake",           "Cryosphere Research"),
    ("antarctic",          "Cryosphere Research"),
    # ── Full-waveform inversion ───────────────────────────────────────────
    ("full-waveform inversion", "Full-waveform inversion"),
    ("full waveform inversion", "Full-waveform inversion"),
    ("adjoint imaging",         "Full-waveform inversion"),
    # ── Ambient noise ─────────────────────────────────────────────────────
    ("ambient noise",        "Ambient noise seismology"),
    ("noise interferometry", "Ambient noise seismology"),
    ("noise tomography",     "Ambient noise seismology"),
    # ── Medical ultrasound ────────────────────────────────────────────────
    ("medical ultrasound", "Medical ultrasound imaging"),
    ("ultrasound tomography", "Medical ultrasound imaging"),
    # ── Numerical wave propagation / HPC ─────────────────────────────────
    ("spectral-element", "Numerical wave propagation and HPC"),
    ("spectral element", "Numerical wave propagation and HPC"),
    ("salvus",           "Numerical wave propagation and HPC"),
    ("wavefield simulation", "Numerical wave propagation and HPC"),
    # ── Inverse theory ────────────────────────────────────────────────────
    ("bayesian inversion", "Inverse theory"),
    ("monte carlo inversion", "Inverse theory"),
    ("hamiltonian monte carlo", "Inverse theory"),
]


def coerce_topics_to_preset(
    raw_topics: Optional[List[str]],
    cos_topic_match: Optional[List[Any]] = None,
    title_hint: Optional[str] = None,
) -> List[str]:
    """Coerce a paper's free-form ``topics`` list into ONLY preset COS_TOPICS.

    Priority:
      1. Honour any topic name already in ``COS_TOPICS`` exactly.
      2. Pull canonical names out of ``cos_topic_match`` (which the AI
         classifier already produces against COS_TOPICS).
      3. Map via :data:`TOPIC_ALIASES` for known display/synonym variants.
      4. If still empty AND ``title_hint`` provided, do a substring sniff
         against :data:`TITLE_KEYWORD_HINTS` (case-insensitive). Useful for
         papers without AI analysis whose original BibTeX keywords are too
         generic to alias (e.g. "Acquisition", "Time lapse" — but title
         clearly says "Distributed Acoustic Sensing").
      5. Fallback: ``["Unable to categorize"]`` (UNABLE_TO_CATEGORIZE).

    Returns a deduped list preserving insertion order. Never empty.
    """
    # PERFORMANCE.md follow-up (Fix F): reuse the module-level frozenset
    # instead of rebuilding ``set(COS_TOPICS)`` on every call.
    preset_set = _COS_TOPICS_SET
    result: List[str] = []

    def _add(name: str) -> None:
        if name and name not in result:
            result.append(name)

    # 1. cos_topic_match goes first (highest signal: AI scored against preset)
    if cos_topic_match:
        for item in cos_topic_match:
            if isinstance(item, dict):
                t = item.get("topic") or item.get("name") or item.get("label") or ""
            else:
                t = str(item or "")
            t = t.strip()
            if t in preset_set:
                _add(t)

    # 2. Pass through any existing topic that's already canonical, else alias
    for raw in (raw_topics or []):
        s = (raw or "").strip()
        if not s:
            continue
        if s in preset_set:
            _add(s)
            continue
        alias = TOPIC_ALIASES.get(s.lower())
        if alias and alias in preset_set:  # guard: drop stale/off-vocab aliases
            _add(alias)

    # 3. Title-keyword sniff fallback (only if still empty — don't override
    #    AI/alias decisions with weaker title heuristics)
    if not result and title_hint:
        hint_lower = " " + title_hint.lower() + " "  # pad so " das " matches end-of-word
        for needle, canonical in TITLE_KEYWORD_HINTS:
            if needle in hint_lower and canonical in preset_set:  # guard: preset only
                _add(canonical)
                break  # one hint is enough

    # 4. Fallback
    if not result:
        return [UNABLE_TO_CATEGORIZE]
    return result

DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
FIGURE_CAPTION_RE = re.compile(
    r"(?ims)^\s*(Figure|Fig\.?|Table)\s*(\d+)\s*[:.\-]?\s*(.+?)(?=\n\s*\n|^\s*(?:Figure|Fig\.?|Table)\s*\d+\s*[:.\-]?|\Z)"
)
_CLIENT = None
# Guards the lazy _CLIENT init (and its os.environ writes) — without it two
# analysis workers racing the first call each build a genai.Client.
_CLIENT_LOCK = threading.Lock()

# Cap concurrent web-context lookups (2026-07-06). Crossref / OpenAlex /
# Semantic Scholar are hit anonymously with no 429 retry — with the analysis
# pool running several Gemini calls in parallel, this keeps the metadata burst
# at 3 concurrent requests while the (much longer) Gemini calls overlap freely.
_WEB_CTX_SEMAPHORE = threading.BoundedSemaphore(3)

# Per-process poison-paper guard (2026-07-06). A paper whose analysis RAISED
# repeatedly (e.g. pdf_path pointing at a deleted file → FileNotFoundError
# every pass) is skipped by the parallel eager selection after
# _ANALYSIS_FAILURE_LIMIT attempts, so it cannot pin a worker slot and burn
# up to 6 Gemini calls per pass forever. In-memory only — a restart retries.
# JSON-parse failures never land here (they store a stub AIAnalysis row and
# leave the pending queue entirely).
_ANALYSIS_FAILURES: Dict[str, int] = {}
_ANALYSIS_FAILURES_LOCK = threading.Lock()
_ANALYSIS_FAILURE_LIMIT = 3


def _record_analysis_failure(paper_id: str) -> None:
    with _ANALYSIS_FAILURES_LOCK:
        _ANALYSIS_FAILURES[paper_id] = _ANALYSIS_FAILURES.get(paper_id, 0) + 1


def _is_analysis_blacklisted(paper_id: str) -> bool:
    with _ANALYSIS_FAILURES_LOCK:
        return _ANALYSIS_FAILURES.get(paper_id, 0) >= _ANALYSIS_FAILURE_LIMIT


def _analysis_blacklist_size() -> int:
    with _ANALYSIS_FAILURES_LOCK:
        return sum(1 for count in _ANALYSIS_FAILURES.values() if count >= _ANALYSIS_FAILURE_LIMIT)


def _emit(progress_callback: Optional[Callable[..., None]], message: str, **kwargs: Any) -> None:
    if progress_callback:
        progress_callback(message, **kwargs)
    else:
        print(message, flush=True)


logger = logging.getLogger(__name__)


# ===========================================================================
# BRIEF v2 §3 / §6 — CHEAP ingest embedding (NO PDF, NO full Gemini Pro call)
# ===========================================================================
#
# At intake we compute ONE small title+abstract vector per paper via
# services/embedding_service.py (Gemini embeddings) and persist it on
# ``Paper.embedding`` / ``Paper.embedding_model`` / ``Paper.embedded_at``.
# This is the *only* model call ingest makes; the expensive full Gemini Pro
# analysis is deferred (see the lazy-analysis queue below). The embedding lets
# the semantic tracker predicate score papers by cosine without ever opening a
# PDF (BRIEF v2 §3) and lets tracker views sort by ProjectPaper.match_score.
#
# Graceful degradation: embedding_service swallows missing-key / quota /
# network failures to ``None``, so a paper ALWAYS persists — it simply stays
# embedding-less and never matches a semantic predicate until a re-embed pass
# recovers it. These helpers therefore NEVER raise into ingest.


def _store_embedding_on_paper(paper: Paper, vector: Optional[List[float]]) -> bool:
    """Write a (possibly ``None``) embedding vector onto ``paper`` in place.

    Returns ``True`` only when a non-empty vector was stored. ``None`` /empty
    leaves the three embedding columns untouched (a previously-stored vector is
    NOT wiped — a transient failure must not erase a good embedding). Does not
    commit; the caller owns the transaction boundary.
    """
    from services.embedding_service import EMBED_MODEL, encode_vector

    if not vector:
        return False
    encoded = encode_vector(vector)
    if not encoded:
        return False
    paper.embedding = encoded
    paper.embedding_model = EMBED_MODEL
    paper.embedded_at = datetime.utcnow()
    return True


def compute_and_store_embedding(
    paper: Paper,
    db_session,
    *,
    commit: bool = True,
    force: bool = False,
) -> bool:
    """Compute + persist the cheap title+abstract embedding for one paper.

    BRIEF v2 §3 ingest hook. Uses ``services.embedding_service`` only
    (``text_for_embedding`` -> ``embed_text`` -> ``encode_vector``); NEVER opens
    a PDF and NEVER triggers the full Gemini Pro analysis. Returns ``True`` when
    a vector was stored, ``False`` when skipped (blank text, no API key, quota /
    network failure, or already-embedded with the same model and ``force`` not
    set). NEVER raises — ingest must keep inserting papers even when embedding is
    unavailable.

    ``commit=False`` lets a batch caller (ingest / migration) defer the commit.
    ``force=True`` re-embeds even when a current-model vector already exists.
    """
    from services.embedding_service import EMBED_MODEL, embed_text, text_for_embedding

    try:
        # Skip if already embedded under the active model (idempotent re-runs)
        # unless the caller forces a refresh.
        if (
            not force
            and getattr(paper, "embedding", None)
            and getattr(paper, "embedding_model", None) == EMBED_MODEL
        ):
            return False

        text = text_for_embedding(paper)
        if not text:
            return False

        vector = embed_text(text, is_query=False)
        stored = _store_embedding_on_paper(paper, vector)
        if stored and commit:
            db_session.commit()
        return stored
    except Exception:  # noqa: BLE001 — embedding must never sink ingest.
        logger.warning(
            "compute_and_store_embedding failed for paper %s; persisting without "
            "embedding",
            getattr(paper, "id", "?"),
            exc_info=True,
        )
        try:
            db_session.rollback()
        except Exception:
            pass
        return False


def embed_papers_batch(
    papers: List[Paper],
    db_session,
    *,
    commit: bool = True,
    force: bool = False,
    progress_callback: Optional[Callable[..., None]] = None,
) -> int:
    """Compute embeddings for many papers in one batched embedding call.

    Used by ingest (``fetch_and_store_new_papers``) and the migration seeder so
    the corpus vector is precomputed at intake without N separate round-trips.
    Returns the count of papers that received a vector. Honours the same
    graceful-degradation contract as :func:`compute_and_store_embedding` — a
    paper with blank text or a failed/missing embedding is simply left
    vector-less; this function NEVER raises into the caller.

    ``force=False`` skips papers already embedded under the active model so a
    re-run is cheap and idempotent.
    """
    from services.embedding_service import (
        EMBED_MODEL,
        embed_texts,
        text_for_embedding,
    )

    if not papers:
        return 0

    try:
        targets: List[Paper] = []
        texts: List[str] = []
        for paper in papers:
            if (
                not force
                and getattr(paper, "embedding", None)
                and getattr(paper, "embedding_model", None) == EMBED_MODEL
            ):
                continue
            text = text_for_embedding(paper)
            if not text:
                continue
            targets.append(paper)
            texts.append(text)

        if not targets:
            return 0

        vectors = embed_texts(texts, is_query=False)
        stored = 0
        for paper, vector in zip(targets, vectors):
            if _store_embedding_on_paper(paper, vector):
                stored += 1

        if commit:
            db_session.commit()
        _emit(
            progress_callback,
            f"Embedded {stored}/{len(targets)} papers (title+abstract, no PDF)",
            stage="embed_batch",
            embedded=stored,
            attempted=len(targets),
        )
        return stored
    except Exception:  # noqa: BLE001 — batch embedding is best-effort.
        logger.warning("embed_papers_batch failed; some papers stay unembedded", exc_info=True)
        try:
            db_session.rollback()
        except Exception:
            pass
        return 0


def backfill_unembedded_papers(
    db_session,
    *,
    limit: int = 200,
    progress_callback: Optional[Callable[..., None]] = None,
) -> int:
    """Re-embed papers an earlier intake left vector-less (H1 fix, 2026-06-30).

    ``embed_papers_batch`` only ever ran on the freshly-touched fetch set, so a
    transient embedding failure (quota / network / missing key) at intake left a
    paper at ``embedding IS NULL`` *permanently* — invisible to every semantic
    predicate (``SemanticPredicate`` returns ``(False, 0.0)`` on a blank vector,
    which is indistinguishable from a genuine non-match) and never retried unless
    that same paper happened to be re-fetched. This scans for unembedded (or
    stale-model) papers and re-embeds a bounded batch so they recover on the next
    refresh. ``limit`` keeps any single pass cheap; subsequent passes pick up the
    rest. Best-effort: never raises into the caller.
    """
    from services.embedding_service import EMBED_MODEL

    try:
        candidates = (
            db_session.query(Paper)
            .filter(
                or_(
                    Paper.embedding.is_(None),
                    Paper.embedding == "",
                    Paper.embedding_model.is_(None),
                    Paper.embedding_model != EMBED_MODEL,
                )
            )
            .order_by(Paper.datetime_utc.desc())
            .limit(limit)
            .all()
        )
    except Exception:  # noqa: BLE001 — query is best-effort.
        logger.warning("backfill_unembedded_papers: candidate query failed", exc_info=True)
        return 0

    if not candidates:
        return 0

    # embed_papers_batch is idempotent (force=False skips anything already
    # embedded under the active model) and itself swallows API failures, so a
    # still-broken key simply leaves the rows for the next pass — no raise.
    stored = embed_papers_batch(
        candidates, db_session, commit=True, progress_callback=progress_callback
    )
    if stored:
        _emit(
            progress_callback,
            f"Embedding backfill: re-embedded {stored} previously vector-less paper(s)",
            stage="embed_backfill",
            embedded=stored,
            candidates=len(candidates),
        )
    return stored


# ===========================================================================
# BRIEF v2 §6 — LAZY deep analysis (full Gemini Pro runs only on demand)
# ===========================================================================
#
# Ingest computes only the cheap embedding above. The full Gemini Pro analysis
# (analyze_paper_from_pdf / analyze_paper_from_metadata) runs ONLY when:
#   (a) a paper matches a tracker  -> enqueue_analysis(paper_id)   [deferred]
#   (b) a paper is opened          -> analyze_on_open(paper_id, ...) [immediate]
#
# (a) uses a simple in-process queue (a thread-safe set of paper ids). A worker
# (drain_analysis_queue) — driven by the tracker scheduler or a route — pops ids
# and runs the real analysis. The set is process-local and best-effort: it is a
# work hint, NOT durable state. The source of truth for "needs analysis" remains
# "no AIAnalysis row" (analyze_pending_papers already queries exactly that), so a
# process restart simply loses the hint, not the work — a later scheduler pass or
# an open re-discovers it.


_ANALYSIS_QUEUE: Set[str] = set()
_ANALYSIS_QUEUE_LOCK = threading.Lock()

# In-flight guard (2026-07-05): prevents the SAME paper being analysed twice
# concurrently — e.g. the eager background analyser and the open-detail (H3)
# trigger firing together. _run_analysis commits its AIAnalysis row only at the
# END, so two racers would each pass _has_analysis, both call Gemini (wasted
# spend) and race the UNIQUE(paper_id) write. Claim-before-run makes the second
# racer a no-op. (Also the H3-R1 fix.)
_INFLIGHT_ANALYSIS: Set[str] = set()
_INFLIGHT_LOCK = threading.Lock()


def _claim_analysis(paper_id: str) -> bool:
    """Reserve ``paper_id`` for analysis; return False if already in flight."""
    if not paper_id:
        return False
    with _INFLIGHT_LOCK:
        if paper_id in _INFLIGHT_ANALYSIS:
            return False
        _INFLIGHT_ANALYSIS.add(paper_id)
        return True


def _release_analysis(paper_id: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT_ANALYSIS.discard(paper_id)


def enqueue_analysis(paper_id: str) -> bool:
    """Mark ``paper_id`` as wanting the full Gemini Pro analysis (lazy hint).

    Called when a paper newly matches a tracker (from the predicate matcher and
    the tracker scheduler). Cheap, thread-safe, and idempotent — it only records
    intent in an in-process set; it does NOT call Gemini. A worker
    (:func:`drain_analysis_queue`) or an open-route later does the real work.
    Returns ``True`` when the id was newly added, ``False`` if already queued or
    blank. NEVER raises (matching/ingest callers wrap broadly anyway).
    """
    if not paper_id:
        return False
    try:
        with _ANALYSIS_QUEUE_LOCK:
            if paper_id in _ANALYSIS_QUEUE:
                return False
            _ANALYSIS_QUEUE.add(paper_id)
            return True
    except Exception:  # pragma: no cover — defensive; set ops don't raise.
        return False


def analysis_queue_snapshot() -> List[str]:
    """Return a copy of the currently-queued paper ids (test / introspection)."""
    with _ANALYSIS_QUEUE_LOCK:
        return sorted(_ANALYSIS_QUEUE)


def _pop_analysis_batch(limit: Optional[int] = None) -> List[str]:
    """Atomically remove and return up to ``limit`` queued ids (all if None)."""
    with _ANALYSIS_QUEUE_LOCK:
        if not _ANALYSIS_QUEUE:
            return []
        if limit is None or limit >= len(_ANALYSIS_QUEUE):
            popped = list(_ANALYSIS_QUEUE)
            _ANALYSIS_QUEUE.clear()
            return popped
        popped = []
        for _ in range(limit):
            popped.append(_ANALYSIS_QUEUE.pop())
        return popped


def _has_analysis(db_session, paper_id: str) -> bool:
    """True when an AIAnalysis row already exists for ``paper_id``."""
    try:
        row = db_session.execute(
            _sa_text("SELECT 1 FROM ai_analyses WHERE paper_id = :pid LIMIT 1"),
            {"pid": paper_id},
        ).fetchone()
        return row is not None
    except Exception:
        return False


def analyze_on_open(
    paper_id: str,
    db_session,
    *,
    force: bool = False,
    progress_callback: Optional[Callable[..., None]] = None,
) -> Optional[dict]:
    """Run the full Gemini Pro analysis on demand when a paper is OPENED.

    BRIEF v2 §6 (b). Callable from the open route. Idempotent: if an analysis
    already exists and ``force`` is False, returns ``None`` (nothing to do) so
    opening an already-analysed paper is free. Otherwise runs the real analysis
    — using the local PDF when present, else abstract+web metadata — and returns
    the stored payload. Also drops the id from the lazy queue since it is now
    handled.

    NEVER raises for the "already analysed" / "missing paper" cases; a genuine
    model error from ``_run_analysis`` propagates (the caller route decides how
    to surface it).
    """
    paper = db_session.get(Paper, paper_id)
    if paper is None:
        return None

    if not force and _has_analysis(db_session, paper_id):
        _discard_from_queue(paper_id)
        return None

    if not _claim_analysis(paper_id):
        return None  # another worker (eager pass / concurrent open) has it

    pdf_path = paper.pdf_path or None
    try:
        return _run_analysis(
            paper,
            db_session,
            pdf_path=pdf_path,
            force_metadata_only=(pdf_path is None),
            progress_callback=progress_callback,
            position=1,
            total=1,
        )
    finally:
        _release_analysis(paper_id)
        _discard_from_queue(paper_id)


def _discard_from_queue(paper_id: str) -> None:
    with _ANALYSIS_QUEUE_LOCK:
        _ANALYSIS_QUEUE.discard(paper_id)


def drain_analysis_queue(
    db_session,
    *,
    limit: Optional[int] = None,
    progress_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, int]:
    """Run the deferred full analysis for papers enqueued by tracker matches.

    BRIEF v2 §6 (a). Pops up to ``limit`` ids from the in-process queue (all when
    ``None``) and runs the real Gemini Pro analysis for each that still lacks an
    AIAnalysis row. One paper's failure is logged and skipped — it never stops
    the drain. Returns ``{"analyzed", "skipped", "failed"}``.

    Designed to be called by a route or a background worker; the tracker
    scheduler enqueues but does NOT auto-drain (keeping a scheduler pass cheap
    and network-free for tests). The queue is a hint only — the authoritative
    backlog is still "papers with no AIAnalysis row" (see
    :func:`analyze_pending_papers`).
    """
    ids = _pop_analysis_batch(limit)
    stats = {"analyzed": 0, "skipped": 0, "failed": 0}
    if not ids:
        return stats

    _emit(
        progress_callback,
        f"Lazy analysis queue: draining {len(ids)} paper(s)",
        stage="analysis_queue_drain",
        total=len(ids),
    )
    for index, paper_id in enumerate(ids, start=1):
        paper = db_session.get(Paper, paper_id)
        if paper is None:
            stats["skipped"] += 1
            continue
        if _has_analysis(db_session, paper_id):
            stats["skipped"] += 1
            continue
        # 2026-07-06: claim like analyze_pending_papers/analyze_on_open do —
        # without it the drain races the eager analysis pool on the same paper
        # (double Gemini spend + UNIQUE(ai_analyses.paper_id) IntegrityError).
        if not _claim_analysis(paper_id):
            stats["skipped"] += 1
            continue
        try:
            _run_analysis(
                paper,
                db_session,
                pdf_path=paper.pdf_path or None,
                force_metadata_only=(paper.pdf_path is None),
                progress_callback=progress_callback,
                position=index,
                total=len(ids),
            )
            stats["analyzed"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad paper must not stop the drain.
            stats["failed"] += 1
            logger.warning(
                "drain_analysis_queue: analysis failed for paper %s: %s",
                paper_id,
                exc,
            )
            try:
                db_session.rollback()
            except Exception:
                pass
        finally:
            _release_analysis(paper_id)
    _emit(
        progress_callback,
        f"Lazy analysis drain complete: {stats}",
        stage="analysis_queue_drain_done",
        **stats,
    )
    return stats


def _topic_payload_for_paper(paper: Paper) -> List[Dict[str, Any]]:
    """Synthesise a ``cos_topic_match``-shaped payload from a paper's stored topics.

    At ingest time there is NO AIAnalysis yet (the full Gemini Pro analysis is
    lazy — BRIEF v2 §6), so the ``cos_topic`` predicate cannot consult AI labels.
    We feed it the paper's already-stored ``topics`` column instead (the same
    shape the tracker scheduler builds in ``_run_one_tracker``) so a legacy
    ``{topics: [...]}`` tracker still matches on intake without re-running Gemini.
    """
    payload: List[Dict[str, Any]] = []
    try:
        for topic in (paper.topics or []):
            if topic:
                payload.append({"topic": str(topic), "score": 1.0})
    except Exception:
        return []
    return payload


def match_new_papers_to_trackers(
    papers: List[Paper],
    db_session,
    *,
    progress_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, int]:
    """Run the predicate matcher over freshly-ingested papers (no analysis yet).

    BRIEF v2 §1/§6: this is the *cheap* matching pass the steady-state fetch
    pipeline runs straight after the title+abstract embedding. For each paper it
    calls :func:`_auto_assign_to_topic_monitors`, which delegates to
    ``services.predicates.match_tracker`` — so semantic (cosine over the just-
    computed embedding), keyword, journal, author, cos_topic, institution and
    date_window predicates all evaluate, ``ProjectPaper.match_score`` is written
    (best score, esp. the semantic cosine), and a matched paper is enqueued for
    the lazy full analysis via ``enqueue_analysis``. NO Gemini Pro call happens
    here — analysis stays deferred to :func:`drain_analysis_queue` / the open
    route. NEVER raises; one bad paper is logged and skipped.

    Orphan candidate pool (owner decision 2026-06-03): papers with zero tracker
    matches remain in the orphan candidate pool — not added to any project, no
    analysis enqueued — until a future tracker that matches them is created
    (back-fill).

    Returns ``{"papers_evaluated", "papers_matched", "links_created"}`` where
    ``papers_matched`` counts papers that gained at least one new tracker link in
    this pass.
    """
    stats = {"papers_evaluated": 0, "papers_matched": 0, "links_created": 0}
    if not papers:
        return stats

    _emit(
        progress_callback,
        f"Matching {len(papers)} new paper(s) against trackers (semantic + predicates)",
        stage="tracker_match",
        total=len(papers),
    )

    for paper in papers:
        stats["papers_evaluated"] += 1
        before = 0
        try:
            before = (
                db_session.query(ProjectPaper)
                .filter(ProjectPaper.paper_id == paper.id)
                .count()
            )
        except Exception:
            before = 0
        try:
            _auto_assign_to_topic_monitors(
                db_session, paper, _topic_payload_for_paper(paper)
            )
        except Exception as exc:  # noqa: BLE001 — one bad paper must not stop the pass.
            logger.warning(
                "match_new_papers_to_trackers: matcher failed for paper %s: %s",
                getattr(paper, "id", "?"),
                exc,
            )
            try:
                db_session.rollback()
            except Exception:
                pass
            continue
        try:
            after = (
                db_session.query(ProjectPaper)
                .filter(ProjectPaper.paper_id == paper.id)
                .count()
            )
        except Exception:
            after = before
        gained = max(0, after - before)
        if gained:
            stats["papers_matched"] += 1
            stats["links_created"] += gained

    _emit(
        progress_callback,
        f"Tracker match complete: {stats['papers_matched']}/{len(papers)} "
        f"paper(s) matched, {stats['links_created']} new link(s)",
        stage="tracker_match_done",
        **stats,
    )
    return stats


def process_ingested_papers(
    papers: List[Paper],
    db_session,
    *,
    drain: bool = True,
    drain_limit: Optional[int] = None,
    progress_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, Any]:
    """End-to-end LAZY ingest pass for a batch of freshly-fetched papers.

    BRIEF v2 §3/§6 — the steady-state replacement for the old eager
    ``analyze_pending_papers`` on the fetch path. In order:

    1. Compute + persist the CHEAP title+abstract embedding for each paper
       (:func:`embed_papers_batch`; no PDF, no Gemini Pro).
    2. Match every paper against all trackers
       (:func:`match_new_papers_to_trackers` -> ``match_tracker``), writing
       ``ProjectPaper.match_score`` and enqueuing matched papers for the full
       analysis.
    3. Optionally drain the lazy analysis queue (:func:`drain_analysis_queue`)
       so the expensive Gemini Pro analysis runs ONLY for papers that matched a
       tracker — never for the whole intake.

    Set ``drain=False`` to defer the heavy analysis to a separate worker/route
    (the tracker scheduler enqueues but never auto-drains; a caller that wants a
    network-free pass passes ``drain=False``). NEVER raises into the caller.

    Returns ``{"embedded", "match": {...}, "analysis": {...}}``.
    """
    result: Dict[str, Any] = {
        "embedded": 0,
        "match": {"papers_evaluated": 0, "papers_matched": 0, "links_created": 0},
        "analysis": {"analyzed": 0, "skipped": 0, "failed": 0},
    }
    if not papers:
        return result

    try:
        result["embedded"] = embed_papers_batch(
            papers, db_session, commit=True, progress_callback=progress_callback
        )
    except Exception:  # noqa: BLE001 — embedding is best-effort; ingest must survive.
        logger.warning("process_ingested_papers: embedding pass failed", exc_info=True)

    try:
        result["match"] = match_new_papers_to_trackers(
            papers, db_session, progress_callback=progress_callback
        )
    except Exception:  # noqa: BLE001 — matching must not sink ingest.
        logger.warning("process_ingested_papers: tracker match pass failed", exc_info=True)

    if drain:
        try:
            result["analysis"] = drain_analysis_queue(
                db_session, limit=drain_limit, progress_callback=progress_callback
            )
        except Exception:  # noqa: BLE001 — a drain failure must not sink ingest.
            logger.warning("process_ingested_papers: analysis drain failed", exc_info=True)

    return result


@dataclass
class AnalysisPaper:
    paper_id: str
    title: str
    authors: str
    year: str
    journal: str
    doi: str
    abstract: str
    url: str
    pdf_paths: List[str]
    has_pdf: bool
    abstract_flag: str


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&nbsp;", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_doi(doi: str) -> str:
    doi = (doi or "").strip()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("doi:", "").strip()
    # 2026-07-10: a DOI scraped from PDF body text often carries a trailing period
    # or a wrapping bracket/quote ("10.1234/x.", "(10.1234/x)") — these break the
    # exact func.lower(doi)==doi compare in match_pdfs. Strip them, but WITHOUT
    # corrupting DOIs whose own terminal char is a bracket (e.g. GSA book DOIs
    # 10.1130/2011.2483(00)). Always-safe trailing chars first: sentence
    # punctuation + wrapping quotes.
    doi = doi.strip("\"'").rstrip(".,;:").strip("\"'")
    # unwrap a fully-surrounding bracket pair: "(10.1234/x)" -> "10.1234/x"
    for lo, hi in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">")):
        if len(doi) > 1 and doi.startswith(lo) and doi.endswith(hi):
            doi = doi[1:-1].strip()
            break
    # strip only an UNBALANCED trailing close-bracket (a stray wrapper), keeping a
    # balanced terminal ")" that is part of the DOI.
    while len(doi) > 1 and doi[-1] in ")]}>":
        opener = {")": "(", "]": "[", "}": "{", ">": "<"}[doi[-1]]
        if doi.count(opener) < doi.count(doi[-1]):
            doi = doi[:-1].rstrip(".,;:")
        else:
            break
    return doi


def extract_doi_from_any(*texts: str) -> str:
    for text in texts:
        if not text:
            continue
        match = DOI_RE.search(text)
        if match:
            return normalize_doi(match.group(1))
    return ""


def abstract_flag(abstract: str) -> str:
    abstract = (abstract or "").strip()
    if not abstract:
        return "missing"
    return "ok" if len(abstract) >= MIN_ABSTRACT_CHARS else "short"


def file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def read_pdf_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def extract_pdf_text(path: str, max_pages: int = PDF_TEXT_MAX_PAGES, max_chars: int = PDF_TEXT_MAX_CHARS) -> str:
    if fitz is None:
        return ""
    # FITZ_LOCK: MuPDF's global context is not thread-safe (see pdf_page_cache).
    with FITZ_LOCK:
        doc = None
        try:
            doc = fitz.open(path)
            texts = []
            total_chars = 0
            for page_index in range(min(len(doc), max_pages)):
                page_text = doc[page_index].get_text("text")
                texts.append(page_text)
                total_chars += len(page_text)
                if total_chars >= max_chars:
                    break
            return clean_text("\n".join(texts))[:max_chars]
        except Exception:
            return ""
        finally:
            try:
                if doc is not None:
                    doc.close()
            except Exception:
                pass


def _clip_text(text: str, limit: int) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _caption_to_title(caption: str) -> str:
    cleaned = clean_text(caption)
    if not cleaned:
        return ""
    title = re.split(r"[:.;]", cleaned, maxsplit=1)[0].strip()
    words = title.split()
    if len(words) > 14:
        title = " ".join(words[:14]).rstrip() + "..."
    return title


def _infer_figure_role(caption: str) -> str:
    lowered = caption.lower()
    if any(keyword in lowered for keyword in ["workflow", "pipeline", "framework", "schematic", "architecture", "overview"]):
        return "Likely role: this figure sketches the workflow, architecture, or study design."
    if any(keyword in lowered for keyword in ["map", "location", "study area", "survey", "layout", "geometry", "parametrization"]):
        return "Likely role: this figure locates the study area or lays out the acquisition and model geometry."
    if any(keyword in lowered for keyword in ["waveform", "trace", "gather", "seismogram", "signal", "spectrum", "frequency"]):
        return "Likely role: this figure compares seismic signals, spectra, or data examples used in the paper."
    if any(keyword in lowered for keyword in ["velocity model", "tomography", "inversion", "cross-section", "profile", "image"]):
        return "Likely role: this figure visualizes the recovered model or subsurface structure."
    if any(keyword in lowered for keyword in ["error", "accuracy", "misfit", "benchmark", "performance", "comparison"]):
        return "Likely role: this figure compares methods or quantifies the reported performance differences."
    if any(keyword in lowered for keyword in ["dataset", "experiment", "training", "validation", "sample"]):
        return "Likely role: this figure introduces the dataset or experiment setup behind the evaluation."
    return "Likely role: this figure supports one of the paper's setup, evidence, or comparison points."


def extract_figure_insights_from_pdf(path: str, max_pages: int = FIGURE_SCAN_MAX_PAGES) -> List[Dict[str, str]]:
    if fitz is None:
        return []

    figure_analyses: List[Dict[str, str]] = []
    seen_labels = set()

    # FITZ_LOCK: MuPDF's global context is not thread-safe (see pdf_page_cache).
    # The regex work below happens on already-extracted page text, but the page
    # iteration itself drives MuPDF, so the whole document walk stays locked.
    with FITZ_LOCK:
        try:
            doc = fitz.open(path)
        except Exception:
            return []
        try:
            return _scan_figure_captions(doc, max_pages, figure_analyses, seen_labels)
        finally:
            try:
                doc.close()
            except Exception:
                pass


def _scan_figure_captions(doc, max_pages: int, figure_analyses: List[Dict[str, str]], seen_labels: set) -> List[Dict[str, str]]:
    try:
        for page_index in range(min(len(doc), max_pages)):
            page_text = doc[page_index].get_text("text")
            if not page_text:
                continue

            for match in FIGURE_CAPTION_RE.finditer(page_text):
                prefix = match.group(1)
                number = match.group(2)
                label = f"{prefix.rstrip('.')} {number}"
                if label in seen_labels:
                    continue

                caption = _clip_text(match.group(3), FIGURE_CAPTION_MAX_CHARS)
                if len(caption) < 24:
                    continue

                title = _caption_to_title(caption) or f"{label} overview"
                description = f"Caption clue: {caption}"
                interpretation = _infer_figure_role(caption)
                relevance = "N/A"
                if "(a)" in caption.lower() or "(b)" in caption.lower():
                    interpretation = f"{interpretation} Read it panel by panel because the caption suggests a multi-part comparison."

                figure_analyses.append(
                    {
                        "figure_id": label,
                        "title": title,
                        "description": description,
                        "interpretation": interpretation,
                        "relevance_to_cos": relevance,
                        "caption_excerpt": caption,
                    }
                )
                seen_labels.add(label)

                if len(figure_analyses) >= FIGURE_ANALYSES_MAX_ITEMS:
                    return figure_analyses
    except Exception:
        return figure_analyses

    return figure_analyses


def http_get_json(url: str, params: Optional[dict] = None) -> dict:
    response = requests.get(
        url,
        params=params,
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": "SeismoHub-PaperBot/1.0"},
    )
    response.raise_for_status()
    return response.json()


def openalex_lookup_by_doi(doi: str) -> Optional[dict]:
    if not doi:
        return None
    try:
        return http_get_json(f"https://api.openalex.org/works/https://doi.org/{doi}")
    except Exception:
        return None


def crossref_lookup_by_doi(doi: str) -> Optional[dict]:
    if not doi:
        return None
    try:
        return http_get_json(f"https://api.crossref.org/works/{doi}").get("message")
    except Exception:
        return None


def semantic_scholar_lookup(doi: str = "", title: str = "") -> Optional[dict]:
    try:
        if doi:
            url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
            params = {
                "fields": "title,abstract,authors,year,venue,url,externalIds,openAccessPdf,citationCount,referenceCount"
            }
            return http_get_json(url, params=params)
        if title:
            url = "https://api.semanticscholar.org/graph/v1/paper/search"
            params = {
                "query": title,
                "limit": 1,
                "fields": "title,abstract,authors,year,venue,url,externalIds,openAccessPdf",
            }
            data = http_get_json(url, params=params)
            arr = data.get("data") or []
            return arr[0] if arr else None
    except Exception:
        return None
    return None


def google_cse_search(query: str, api_key: str, cx: str, num: int = 5) -> List[dict]:
    if not api_key or not cx:
        return []
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": api_key, "cx": cx, "q": query, "num": num}
    response = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    items = []
    for item in data.get("items", [])[:num]:
        items.append(
            {
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
        )
    return items


def gather_web_context(item: AnalysisPaper) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "openalex": None,
        "crossref": None,
        "semantic_scholar": None,
        "google": [],
        "used_sources": [],
    }

    google_api_key = (os.getenv("GOOGLE_CUSTOM_SEARCH_API_KEY") or "").strip()
    google_cx = (os.getenv("GOOGLE_CSE_CX") or "").strip()

    if item.doi:
        openalex_payload = openalex_lookup_by_doi(item.doi)
        if openalex_payload:
            ctx["openalex"] = openalex_payload
            ctx["used_sources"].append("openalex")

        crossref_payload = crossref_lookup_by_doi(item.doi)
        if crossref_payload:
            ctx["crossref"] = crossref_payload
            ctx["used_sources"].append("crossref")

        semantic_payload = semantic_scholar_lookup(doi=item.doi)
        if semantic_payload:
            ctx["semantic_scholar"] = semantic_payload
            ctx["used_sources"].append("semantic_scholar")

        if USE_GOOGLE_CSE and google_api_key and google_cx:
            try:
                google_hits = google_cse_search(f"doi:{item.doi}", google_api_key, google_cx, GOOGLE_NUM_RESULTS)
                if google_hits:
                    ctx["google"] = google_hits
                    ctx["used_sources"].append("google_cse")
            except Exception:
                pass
    else:
        semantic_payload = semantic_scholar_lookup(title=item.title)
        if semantic_payload:
            ctx["semantic_scholar"] = semantic_payload
            ctx["used_sources"].append("semantic_scholar")

        if USE_GOOGLE_CSE and google_api_key and google_cx:
            query = f"\"{item.title}\" {item.year} {item.journal}".strip()
            try:
                google_hits = google_cse_search(query, google_api_key, google_cx, GOOGLE_NUM_RESULTS)
                if google_hits:
                    ctx["google"] = google_hits
                    ctx["used_sources"].append("google_cse")
            except Exception:
                pass

    return ctx


def extract_text(response) -> str:
    if hasattr(response, "text") and response.text:
        return response.text.strip()

    if hasattr(response, "candidates") and response.candidates:
        candidate = response.candidates[0]
        if hasattr(candidate, "content") and candidate.content:
            parts = getattr(candidate.content, "parts", None)
            if parts and isinstance(parts, list):
                texts = []
                for part in parts:
                    if hasattr(part, "text") and part.text:
                        texts.append(part.text)
                if texts:
                    return "\n".join(texts).strip()

    return "[WARNING] Gemini returned no parseable text output."


def build_prompt(item: AnalysisPaper, mode: str, web_ctx: Optional[dict] = None) -> str:
    base = f"""
You are a research assistant for geophysics and computational wave physics. Evaluate the paper and classify it into the preset research topics below.

Primary topics — choose the 1–3 that best fit (multi-select, ≤3):
{json.dumps(COS_TOPICS, ensure_ascii=False)}

Then tag the paper along four more facets, choosing ONLY from these controlled lists:
- method (choose 0–3):    {json.dumps(METHODS, ensure_ascii=False)}
- data_type (choose 0–3): {json.dumps(DATA_TYPES, ensure_ascii=False)}
- target (choose 0–3):    {json.dumps(TARGETS, ensure_ascii=False)}
- paper_role (choose 1):  {json.dumps(PAPER_ROLES, ensure_ascii=False)}

Return strict JSON only. Do not return markdown. Do not return prose outside JSON. Keep the field names exactly as listed:
{{
  "citekey": "...",
  "title": "...",
  "year": "...",
  "venue": "...",
  "doi": "...",
  "url": "...",

  "has_pdf": true,
  "pdf_used": true,
  "abstract_flag": "ok|short|missing",
  "used_sources": ["pdf"|"abstract"|"openalex"|"crossref"|"semantic_scholar"|"google_cse"],

  "cos_topic_match": [
    {{"topic": "...", "confidence": 0.0, "why": "one sentence grounded in evidence"}}
  ],
  "facets": {{
    "method": ["0–3 values copied verbatim from the method list"],
    "data_type": ["0–3 values copied verbatim from the data_type list"],
    "target": ["0–3 values copied verbatim from the target list"],
    "paper_role": "exactly one value copied verbatim from the paper_role list"
  }},

  "one_sentence_takeaway_en": "One sentence in English, at most 30 words",
  "one_sentence_takeaway_zh": "一句话中文概括，不超过30个字",
  "summary_150w_en": "English summary plus novelty, at most 150 words",
  "summary_150w_zh": "中文摘要，包含研究方法、数据、主要结论与创新点，不超过150个字",

  "key_contributions": ["English bullet"],
  "key_contributions_zh": ["对应中文条目"],
  "methods": ["English bullet"],
  "methods_zh": ["对应中文条目"],
  "data_or_experiments": ["English bullet"],
  "data_or_experiments_zh": ["对应中文条目"],
  "limitations": ["English bullet"],
  "limitations_zh": ["对应中文条目"],
  "followups_for_our_group": ["English bullet"],
  "followups_for_our_group_zh": ["对应中文条目"],

  "structured_analysis": {{
    "method":      {{
      "text": "≤40-word description of the core methodology or algorithm",
      "source": "§3.1 Methods — first paragraph (p. 3)",
      "source_anchor": {{
        "page": 3,
        "paragraph_index": 1,
        "opening_sentence": "<copy the text shown after 'opens:' on the page block VERBATIM>",
        "sentence_index": 1,
        "quote": "<verbatim ≤32-word span from that paragraph that supports the text claim>",
        "section_title": "§3.1 Methods"
      }}
    }},
    "data":        {{
      "text": "≤40-word description of the dataset or experiment",
      "source": "§2 Data — Table 1 caption (p. 4)",
      "source_anchor": {{ "page": 4, "paragraph_index": 2, "opening_sentence": "...", "sentence_index": 1, "quote": "...", "section_title": "§2 Data" }}
    }},
    "results":     {{
      "text": "≤40-word description of the key quantitative result",
      "source": "§4 Results — Fig. 3 and surrounding text (p. 5-6)",
      "source_anchor": {{ "page": 5, "paragraph_index": 3, "opening_sentence": "...", "sentence_index": 2, "quote": "...", "section_title": "§4 Results" }}
    }},
    "limitations": {{
      "text": "≤30-word description of the main limitation or caveat",
      "source": "§5 Discussion — last two paragraphs (p. 10)",
      "source_anchor": {{ "page": 10, "paragraph_index": 8, "opening_sentence": "...", "sentence_index": 1, "quote": "...", "section_title": "§5 Discussion" }}
    }},
    "novelty":     {{
      "text": "≤30-word description of what is genuinely new vs prior work",
      "source": "§1 Introduction — final paragraph; §6 Conclusions",
      "source_anchor": {{ "page": 1, "paragraph_index": 4, "opening_sentence": "...", "sentence_index": 1, "quote": "...", "section_title": "§1 Introduction" }}
    }}
  }},
  "structured_analysis_zh": {{
    "method":      {{"text": "≤40字，核心方法或算法描述"}},
    "data":        {{"text": "≤40字，数据集或实验描述"}},
    "results":     {{"text": "≤40字，主要量化结果描述"}},
    "limitations": {{"text": "≤30字，主要局限或不足"}},
    "novelty":     {{"text": "≤30字，相较于前人工作的创新点"}}
  }},

  "relevance_score": 0,
  "keywords": ["keyword1", "keyword2", "keyword3", "..."],

  "figure_analyses": [
    {{"figure_id": "Fig. 1", "title": "short title describing the figure",
      "caption_summary": "a 1–2 sentence summary of the figure caption (also accepted as 'caption_excerpt')",
      "description": "what this figure/table shows",
      "description_zh": "图表内容的中文描述",
      "key_finding": "the key takeaway / main finding of the figure (also accepted as 'description')",
      "interpretation": "what it means for the paper conclusions",
      "interpretation_zh": "对论文结论意义的中文解读",
      "relevance_to_cos": "relevance to the preset research topics, or N/A",
      "source": "Figure caption on p. 4 — referenced in §3 Results, second paragraph"}}
  ],

  "evidence": {{
    "based_on": "pdf|abstract+web",
    "notes": "Say what evidence you actually used"
  }}
}}

Constraints:
- summary_150w_en must be present and stay within 150 English words.
- summary_150w_zh must be present. Write in Simplified Chinese (简体中文). Stay within 150 Chinese characters. Cover method, data, key result, and novelty.
- one_sentence_takeaway_zh must be present. Simplified Chinese, ≤30 characters.
- key_contributions_zh, methods_zh, data_or_experiments_zh, limitations_zh, followups_for_our_group_zh: each must mirror the corresponding English list, translated to Simplified Chinese. Must have the same number of items as the English list.
- structured_analysis_zh must be present. Each of the five keys must have a "text" field in Simplified Chinese (≤40 Chinese characters). Do NOT include "source" in structured_analysis_zh.
- figure_analyses: each entry must include description_zh and interpretation_zh in Simplified Chinese.
- Do not invent numerical results without evidence.
- keywords: extract 3 or more specific technical keywords or key phrases from the paper. Use normal case (not all caps). Do not repeat topic names.
- cos_topic_match: 1–3 PRIMARY topics, each copied verbatim from the primary-topics list. facets.method / data_type / target: 0–3 each, verbatim from their lists; facets.paper_role: exactly one. Use the EXACT strings — never invent or rephrase a label.
- OFF-TOPIC RULE: if the paper is NOT about seismology / geophysics / wave physics at all (e.g. unrelated biology, medicine, ecology, food science, pure climate/ocean chemistry, materials), set cos_topic_match to EXACTLY ONE entry — "Off-topic / not seismology" — and leave facets.method / data_type / target EMPTY ([]). Do not force a seismology label onto an unrelated paper.
- figure_analyses is REQUIRED. Scan the paper for all figures and tables — they typically have captions starting with "Figure 1", "Fig. 1", "Fig. 2", "Table 1", etc. For each figure/table you find, create an entry with figure_id (e.g. "Fig. 1"), a short title, what it shows, what it means, and its relevance to the preset topics. If the paper has figures, you MUST list them. Only return an empty array [] if you genuinely cannot identify any figures (e.g. abstract-only analysis with no PDF).
- structured_analysis is REQUIRED. Each of the five keys (method, data, results, limitations, novelty) must have a "text" string (≤40 words), a "source" string (legacy, free-form), AND a "source_anchor" object.
- "source" (legacy field, kept for backward compatibility) tells the reader WHERE in the paper to verify. Format: section number + name, a contextual hint (paragraph / figure / equation / table), and optionally page. Examples: "§3.1 Methods — first paragraph (p. 4)", "§4 Results — Fig. 5 caption". When no PDF was available, set "source" to "".
- "source_anchor" (REQUIRED whenever a PDF was provided) is a typed object with six fields:
    page              — the integer page number you saw in the page blocks above ("[Page N]").
    paragraph_index   — the ¶i counter from that same page block (1-indexed within the page, NOT within the section).
    opening_sentence  — copy VERBATIM the text shown after 'opens: "..."' on the chosen paragraph. Do not rewrite, do not paraphrase, do not translate, do not summarise. Character-for-character including punctuation.
    sentence_index    — 1-indexed sentence within the paragraph that contains the quote.
    quote             — a verbatim ≤32-word span from that paragraph that supports the "text" claim. Copy character-for-character.
    section_title     — the §-numbered heading if visible on the page ("§3.2 Receiver functions"), or "" if not.
  If no PDF was provided, omit source_anchor entirely (do NOT emit an empty object).
- If you cannot find a single paragraph that supports the claim, choose the *closest* paragraph and add a sibling field "anchor_confidence": "weak". Do NOT fabricate a quote that is not in the paper text shown above. Never invent paragraph numbers.
- For figure_analyses, "source" describes where the figure caption lives plus where it's referenced in the body, e.g. "Figure 3 caption on p. 6; referenced in §4 Results".
- Return ALL fields listed in the schema above. Do not add fields beyond those listed.
"""

    metadata_block = f"""
Paper metadata:
- title: {item.title}
- authors: {item.authors}
- year: {item.year}
- venue/journal: {item.journal}
- doi: {item.doi}
- url: {item.url}
- abstract_flag: {item.abstract_flag}
- abstract:
{item.abstract[:2500]}
"""

    if mode == "pdf":
        # PR-20: also inject a paragraph-numbered digest of the cached page
        # text so the model has a fixed addressing scheme.
        paragraph_digest = _build_paragraph_digest_block(item)
        suffix = "\nYou will also receive the PDF content. Use the PDF as the primary evidence.\n"
        if paragraph_digest:
            suffix += "\n" + paragraph_digest
        return base + "\n" + metadata_block + suffix

    web_block = ""
    if web_ctx:
        semantic_scholar = web_ctx.get("semantic_scholar") or {}
        openalex_payload = web_ctx.get("openalex") or {}
        crossref_payload = web_ctx.get("crossref") or {}
        google_hits = web_ctx.get("google") or []
        crossref_title = ""
        if crossref_payload:
            titles = crossref_payload.get("title") or []
            crossref_title = titles[0] if titles else ""

        web_block = f"""
Public web context:
- semantic_scholar_url: {semantic_scholar.get("url", "")}
- semantic_scholar_open_pdf: {((semantic_scholar.get("openAccessPdf") or {}) or {}).get("url", "")}
- semantic_scholar_abstract: {clean_text(semantic_scholar.get("abstract", "") or "")[:1500]}
- openalex_has_abstract: {bool(openalex_payload.get("abstract_inverted_index"))}
- openalex_landing_page: {((openalex_payload.get("primary_location") or {}) or {}).get("landing_page_url", "")}
- crossref_title: {clean_text(crossref_title)}
- google_top_hits:
{json.dumps(google_hits[:GOOGLE_NUM_RESULTS], ensure_ascii=False)}
- sources_used: {web_ctx.get("used_sources", [])}
"""

    return base + "\n" + metadata_block + "\nNo PDF is available. Base the analysis on the abstract and public web metadata only.\n" + web_block


def _dump_raw_json_debug(text: str) -> None:
    try:
        with open("/tmp/swp_gemini_raw_debug.txt", "w", encoding="utf-8") as f:
            f.write(text or "(empty)")
    except Exception:
        pass


def _extract_balanced_json(text: str) -> Optional[str]:
    """Return the first balanced ``{...}`` object in *text*.

    2026-07-10: gemini-3.1-pro-preview sometimes emits a valid JSON object
    followed by a STRAY trailing ``}`` (or prose). The old greedy ``{.*}`` span
    reached the LAST brace and swallowed that garbage, so json.loads rejected an
    otherwise-perfect analysis and it fell back to an empty result. Scanning for
    the first BALANCED object (ignoring braces inside string literals) stops at
    the correct close and drops any trailing junk. Returns None only when no
    complete object exists (genuinely truncated output)."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def safe_json_load(text: str) -> dict:
    text = (text or "").strip()
    # Strip a ```json … ``` fence if the model wrapped the object in one.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    candidate = _extract_balanced_json(text)
    if candidate is None:
        # Last resort: the greedy span (handles a couple of odd shapes the
        # balanced scan can't, e.g. an unclosed-but-recoverable tail).
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        candidate = match.group(0) if match else None
    if candidate is None:
        _dump_raw_json_debug(text or "(empty)")
        raise ValueError("Model output is not valid JSON.")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        _dump_raw_json_debug(candidate)
        raise


def normalize_result_fields(result: dict) -> dict:
    if not (result.get("summary_150w_en") or "").strip():
        for key in ["summary_250w_en", "summary_200w_en", "summary_en", "summary_150_en", "summary"]:
            if (result.get(key) or "").strip():
                result["summary_150w_en"] = result[key]
                break

    summary = (result.get("summary_150w_en") or "").strip()
    if summary:
        words = summary.split()
        if len(words) > SUMMARY_MAX_WORDS_EN:
            result["summary_150w_en"] = " ".join(words[:SUMMARY_MAX_WORDS_EN]).rstrip() + "..."

    # Chinese summary: allow up to 150 Chinese characters (≈ roughly 300 bytes in UTF-8).
    # We trim by character count rather than whitespace-separated words.
    SUMMARY_MAX_CHARS_ZH = 150
    zh = (result.get("summary_150w_zh") or "").strip()
    if len(zh) > SUMMARY_MAX_CHARS_ZH:
        result["summary_150w_zh"] = zh[:SUMMARY_MAX_CHARS_ZH].rstrip() + "……"
    elif not zh:
        result["summary_150w_zh"] = ""

    return result


def _as_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_topic_matches(value: Any) -> List[Dict[str, Any]]:
    matches = []
    if not isinstance(value, list):
        return matches
    for item in value:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic", "")).strip()
        why = str(item.get("why", "")).strip()
        try:
            confidence = float(item.get("confidence", 0))
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if topic:
            matches.append({"topic": topic, "confidence": confidence, "why": why})
    return matches


def _normalize_facets(value: Any, primary_topics: List[str]) -> Dict[str, Any]:
    """Coerce the model's raw ``facets`` object to the controlled vocab.

    ``primary_topics`` is the already-validated primary_topic list (the in-preset
    subset of cos_topic_match). The other four facets are filtered against
    METHODS / DATA_TYPES / TARGETS / PAPER_ROLES; off-vocab values are dropped.
    Lists are capped at 3; paper_role is a single preset string or "".
    """
    out: Dict[str, Any] = {
        "primary_topic": list(primary_topics),
        "method": [],
        "data_type": [],
        "target": [],
        "paper_role": "",
    }

    def _pick(raw: Any, vocab: List[str], cap: int = 3) -> List[str]:
        allowed = set(vocab)
        picked: List[str] = []
        if isinstance(raw, list):
            for item in raw:
                s = item.get("value") if isinstance(item, dict) else item
                s = str(s or "").strip()
                if s in allowed and s not in picked:
                    picked.append(s)
                if len(picked) >= cap:
                    break
        return picked

    if isinstance(value, dict):
        out["method"] = _pick(value.get("method"), METHODS)
        out["data_type"] = _pick(value.get("data_type"), DATA_TYPES)
        out["target"] = _pick(value.get("target"), TARGETS)
        role = str(value.get("paper_role") or "").strip()
        if role in set(PAPER_ROLES):
            out["paper_role"] = role
    return out


def _normalize_figure_analyses(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        # `source` (new schema, 2026-05-16) — a plain-text annotation of WHERE
        # the figure caption + its body references live, e.g.
        # "Figure caption on p. 4 — referenced in §3 Results, second paragraph".
        # Trim to 220 chars to keep the UI single-line. Empty string is fine.
        source = str(item.get("source") or "").strip()
        if len(source) > 220:
            source = source[:220].rstrip() + "…"
        # Schema reconciliation (FN-B4-A01-1, 2026-05-18):
        # The QA brief / docs reference canonical names `caption_summary` and
        # `key_finding`, while the runtime historically emitted
        # `caption_excerpt` and `description`. Emit BOTH names (aliased) so
        # downstream consumers can rely on either spelling and existing
        # payloads keep working when re-normalized.
        caption_excerpt_raw = str(item.get("caption_excerpt") or "").strip()
        caption_summary_raw = str(item.get("caption_summary") or "").strip()
        caption_value = caption_excerpt_raw or caption_summary_raw
        caption_clipped = _clip_text(caption_value, FIGURE_CAPTION_MAX_CHARS) if caption_value else ""

        description_raw = str(item.get("description") or "").strip()
        key_finding_raw = str(item.get("key_finding") or "").strip()
        interpretation_raw = str(item.get("interpretation") or "").strip()
        # `description` and `key_finding` describe the same concept ("what
        # this figure shows + what it means"). Prefer explicit fields, then
        # fall back across aliases.
        description_value = description_raw or key_finding_raw
        key_finding_value = key_finding_raw or description_raw or interpretation_raw

        out.append({
            "figure_id": str(item.get("figure_id") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "description": description_value,
            "description_zh": str(item.get("description_zh") or "").strip(),
            "interpretation": interpretation_raw,
            "interpretation_zh": str(item.get("interpretation_zh") or "").strip(),
            "relevance_to_cos": str(item.get("relevance_to_cos") or "N/A").strip(),
            "source": source,
            "caption_excerpt": caption_clipped,
            # Canonical aliases (per QA brief / docs):
            "caption_summary": caption_clipped,
            "key_finding": key_finding_value,
        })
    return out


_STRUCTURED_KEYS = ("method", "data", "results", "limitations", "novelty")

# Bounds for the typed source_anchor object (matches 04-design/15 §1 table).
_ANCHOR_OPENING_MAX = 240   # opening_sentence — ≤240 chars
_ANCHOR_QUOTE_MAX   = 320   # quote — ≤320 chars
_ANCHOR_SECTION_MAX = 120   # section_title — ≤120 chars


def _build_paragraph_digest_block(item) -> str:
    """Render the per-page paragraph digest the model uses to ground
    source_anchor.{page, paragraph_index, opening_sentence}.

    Returns "" when the cache is unavailable so callers can cheaply concat
    into the prompt regardless.
    """
    paper_id = getattr(item, "paper_id", None)
    db_session = getattr(item, "db_session", None)
    if not paper_id or not db_session:
        return ""
    try:
        paragraphs_by_page = get_paragraphs_by_page(paper_id, db_session)
    except Exception:
        return ""
    if not paragraphs_by_page:
        return ""

    lines: List[str] = [
        "Paragraph anchor reference (use these page numbers and ¶ indices",
        "verbatim in source_anchor.page / source_anchor.paragraph_index):",
        "",
    ]
    for page_num in sorted(paragraphs_by_page.keys()):
        paras = paragraphs_by_page[page_num] or []
        if not paras:
            continue
        lines.append(f"[Page {page_num}]")
        for p in paras:
            idx = (p.get("para_index") or 0) + 1  # 1-indexed for the LLM
            opening = (p.get("opening_sentence") or "").strip().replace('"', "'")
            text = (p.get("text") or "").strip()
            opening_words = opening.split()
            opening_short = " ".join(opening_words[:20])
            lines.append(f'¶{idx} (opens: "{opening_short}"): {text}')
        lines.append("")
    return "\n".join(lines).rstrip()


def _normalize_source_anchor(value: Any) -> Optional[Dict[str, Any]]:
    """Shape-check a single ``source_anchor`` tuple from the model."""
    if not isinstance(value, dict):
        return None
    try:
        page = int(value.get("page"))
        paragraph_index = int(value.get("paragraph_index"))
    except (TypeError, ValueError):
        return None
    if page < 1 or paragraph_index < 1:
        return None
    opening = str(value.get("opening_sentence") or "").strip()
    if not opening:
        return None
    if len(opening) > _ANCHOR_OPENING_MAX:
        opening = opening[:_ANCHOR_OPENING_MAX].rstrip() + "…"
    try:
        sentence_index = int(value.get("sentence_index") or 1)
        if sentence_index < 1:
            sentence_index = 1
    except (TypeError, ValueError):
        sentence_index = 1
    quote = str(value.get("quote") or "").strip()
    if len(quote) > _ANCHOR_QUOTE_MAX:
        quote = quote[:_ANCHOR_QUOTE_MAX].rstrip() + "…"
    section_title = str(value.get("section_title") or "").strip()
    if len(section_title) > _ANCHOR_SECTION_MAX:
        section_title = section_title[:_ANCHOR_SECTION_MAX].rstrip() + "…"
    confidence = str(value.get("anchor_confidence") or "").strip().lower()
    out: Dict[str, Any] = {
        "page": page,
        "paragraph_index": paragraph_index,
        "opening_sentence": opening,
        "sentence_index": sentence_index,
        "quote": quote,
        "section_title": section_title,
    }
    if confidence in ("weak", "low"):
        out["anchor_confidence"] = "weak"
    return out


def _normalize_structured_analysis(value: Any) -> Dict[str, Any]:
    """Normalise the ``structured_analysis`` block from the model output.

    Returns a dict with all five canonical keys, each containing
    ``{text: str, source: str, page_refs: [int]}``.

    - ``source`` (preferred, new schema 2026-05-16): plain-text annotation
      of WHERE in the paper the claim is supported, e.g. "§3.2 Receiver
      functions — second paragraph; p. 4". This replaces clickable chips.
    - ``page_refs`` (legacy): list of 1-indexed page numbers. Kept for
      backward compatibility — existing analyses produced before the prompt
      change still populate this. The UI renders either ``source`` (when
      non-empty) or falls back to ``page_refs``.

    Missing or malformed fields are filled with empty defaults so downstream
    code can always destructure.
    """
    template: Dict[str, Any] = {
        k: {"text": "", "source": "", "page_refs": [], "source_anchor": None}
        for k in _STRUCTURED_KEYS
    }
    # PERFORMANCE.md follow-up (Fix G): short-circuit the common empty
    # cases (None / {} / non-dict) before running the per-key normalization
    # loop. Saves five _normalize_source_anchor calls + word-split work
    # per render for any paper that never went through Gemini.
    if not value or not isinstance(value, dict):
        return template
    for key in _STRUCTURED_KEYS:
        raw = value.get(key)
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        # Trim to 60 words max to prevent bloat
        words = text.split()
        if len(words) > 60:
            text = " ".join(words[:60]).rstrip() + "…"
        # New schema: free-text source annotation
        source = str(raw.get("source") or "").strip()
        if len(source) > 220:
            source = source[:220].rstrip() + "…"
        # Legacy schema: list of int page numbers (kept for the 100 existing
        # analyses that pre-date the source field).
        raw_refs = raw.get("page_refs") or []
        refs: List[int] = []
        if isinstance(raw_refs, list):
            for r in raw_refs:
                try:
                    refs.append(int(r))
                except (TypeError, ValueError):
                    pass
        anchor = _normalize_source_anchor(raw.get("source_anchor"))
        template[key] = {
            "text": text,
            "source": source,
            "page_refs": refs,
            "source_anchor": anchor,
        }
    return template


def _normalize_structured_analysis_zh(value: Any) -> Dict[str, Any]:
    """Normalise the Chinese ``structured_analysis_zh`` block from the model.

    Same shape as ``structured_analysis`` but without page_refs (not needed
    for the Chinese mirror).
    """
    template: Dict[str, Any] = {k: {"text": ""} for k in _STRUCTURED_KEYS}
    # PERFORMANCE.md follow-up (Fix G): same short-circuit as the EN sibling.
    if not value or not isinstance(value, dict):
        return template
    for key in _STRUCTURED_KEYS:
        raw = value.get(key)
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        chars = list(text)
        if len(chars) > 60:
            text = "".join(chars[:60]) + "…"
        template[key] = {"text": text}
    return template


def _normalize_relevance_score(value: Any) -> int:
    try:
        score = int(float(value))
    except Exception:
        score = 0
    return max(0, min(100, score))


# --------------------------------------------------------------------------
# PR-21 — paragraph-anchor validator (BRIEF item H).
# --------------------------------------------------------------------------

_ANCHOR_MIN_SCORE = 0.78
_ANCHOR_RETRIES = 3


def _walk_anchor_sites(analysis_dict: dict):
    """Yield (path, anchor_dict, setter_callback) for every anchor in the analysis."""
    sa = analysis_dict.get("structured_analysis") or {}
    if isinstance(sa, dict):
        for key in ("method", "data", "results", "limitations", "novelty"):
            entry = sa.get(key)
            if not isinstance(entry, dict):
                continue
            anchor = entry.get("source_anchor")
            if isinstance(anchor, dict) and anchor.get("page"):
                def _set(updated, _entry=entry):
                    _entry["source_anchor"] = updated
                yield (f"structured_analysis.{key}.source_anchor",
                       anchor, _set)

    figs = analysis_dict.get("figure_analyses") or []
    if isinstance(figs, list):
        for i, fig in enumerate(figs):
            if not isinstance(fig, dict):
                continue
            anchors = fig.get("source_anchors")
            if not isinstance(anchors, list):
                continue
            for j, anchor in enumerate(anchors):
                if isinstance(anchor, dict) and anchor.get("page"):
                    def _set(updated, _anchors=anchors, _j=j):
                        _anchors[_j] = updated
                    yield (f"figure_analyses[{i}].source_anchors[{j}]",
                           anchor, _set)


def _validate_paragraph_anchors(
    analysis_dict: dict,
    paper_id: str,
    db_session,
) -> List[Tuple[str, Dict[str, Any], str]]:
    """Verify every anchor in ``analysis_dict`` against the page cache."""
    try:
        paragraphs_by_page = get_paragraphs_by_page(paper_id, db_session)
    except Exception:
        paragraphs_by_page = {}

    failures: List[Tuple[str, Dict[str, Any], str]] = []
    for path, anchor, _set in _walk_anchor_sites(analysis_dict):
        if not paragraphs_by_page:
            failures.append((path, anchor, "no_cache"))
            continue
        page_hint = anchor.get("page")
        opening = (anchor.get("opening_sentence") or "").strip()
        if not opening:
            failures.append((path, anchor, "missing_opening_sentence"))
            continue
        try:
            hit = find_paragraph_by_opening_sentence(
                paper_id, page_hint, opening,
                db_session, min_score=_ANCHOR_MIN_SCORE,
            )
        except Exception:
            hit = None
        if hit is None:
            failures.append((path, anchor, "no_match"))
    return failures


def _stamp_unverified(analysis_dict: dict,
                      failures: List[Tuple[str, Dict[str, Any], str]]) -> None:
    """Mark every failed anchor with degraded ⚠ flags."""
    failed_paths = {path for path, _a, _r in failures}
    for path, anchor, setter in _walk_anchor_sites(analysis_dict):
        if path not in failed_paths:
            continue
        reason = next((r for p, _a, r in failures if p == path), "no_match")
        updated = dict(anchor)
        updated["anchor_status"] = "unverified"
        updated["anchor_confidence"] = "weak"
        updated["anchor_reason"] = reason
        setter(updated)


def _paper_to_item(paper: Paper, pdf_path: Optional[str] = None) -> AnalysisPaper:
    published_year = ""
    if paper.published_date:
        published_year = paper.published_date[:4]

    authors = paper.authors or []
    author_line = ", ".join(authors) if isinstance(authors, list) else str(authors or "")
    resolved_pdf_path = pdf_path or paper.pdf_path

    doi = normalize_doi(paper.doi or "")
    if not doi:
        doi = extract_doi_from_any(paper.link or "", paper.abstract or "", paper.title or "")

    return AnalysisPaper(
        paper_id=paper.id,
        title=clean_text(paper.title or ""),
        authors=clean_text(author_line),
        year=published_year,
        journal=clean_text(paper.journal or "Others"),
        doi=doi,
        abstract=clean_text(paper.abstract or ""),
        url=paper.link or "",
        pdf_paths=[resolved_pdf_path] if resolved_pdf_path else [],
        has_pdf=bool(resolved_pdf_path),
        abstract_flag=paper.abstract_flag or abstract_flag(paper.abstract or ""),
    )


def _get_vertex_client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    with _CLIENT_LOCK:
        if _CLIENT is not None:  # double-check: another thread won the race
            return _CLIENT

        api_key = (os.getenv("VERTEX_AI_API_KEY") or "").strip()
        project = (os.getenv("VERTEX_AI_PROJECT") or "").strip()
        location = (os.getenv("VERTEX_AI_LOCATION") or "global").strip()

        if not api_key:
            raise RuntimeError("Missing VERTEX_AI_API_KEY in your local .env configuration.")

        if project:
            os.environ["GOOGLE_CLOUD_PROJECT"] = project
        os.environ["GOOGLE_CLOUD_LOCATION"] = location
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

        # This intentionally follows the initialization pattern used in the original script.
        _CLIENT = genai.Client(vertexai=True, api_key=api_key)
        return _CLIENT


def reset_vertex_client() -> None:
    """Drop the cached Vertex genai client so the next analysis rebuilds from the
    current env (M8 fix, 2026-06-30). Mirrors llm_client.reset_shared_clients /
    embedding_service.reset_client; called after a Settings key/provider change so
    a corrected key takes effect without a process restart."""
    global _CLIENT
    _CLIENT = None


def _make_generation_config():
    # 2026-07-10: raised from 8192. gemini-2.5-pro is a reasoning model whose
    # thinking tokens draw from the output budget, and a full PDF analysis
    # (per-figure notes, bilingual) easily exceeds 8k — the JSON was being
    # truncated mid-array, so EVERY Pro PDF analysis fell back to an empty
    # result ("Model output could not be parsed as JSON"). 2.5-pro/flash both
    # allow up to 65536 output tokens; a cap is not a target so short Flash
    # responses cost the same. Env-overridable.
    _max_tokens = int(os.environ.get("SEISMO_ANALYSIS_MAX_TOKENS") or "65535")
    kwargs = {
        "temperature": 0.1,
        "top_p": 0.9,
        "max_output_tokens": _max_tokens,
    }
    try:
        return types.GenerateContentConfig(**kwargs, response_mime_type="application/json")
    except TypeError:
        return types.GenerateContentConfig(**kwargs)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=12))
def call_gemini(client, prompt: str, parts: Optional[List[Any]] = None,
                model_name: Optional[str] = None,
                fallback_model: Optional[str] = None) -> Tuple[str, str]:
    """Returns ``(text, model_id)`` where model_id is the wire ID that ACTUALLY
    produced the response (primary, or the fallback when the primary raised).
    BRIEF C: the used model used to be stamped on the shared client object as
    ``_last_model_used`` — under concurrent analysis workers that let paper A
    record paper B's model, so it now travels in the return value instead."""
    config = _make_generation_config()
    # 2026-06-13: caller picks the model (Pro for PDF, Flash otherwise); these
    # default to the generic pair only when no explicit model is provided.
    primary = model_name or PRIMARY_MODEL
    secondary = fallback_model or FALLBACK_MODEL
    try:
        if parts:
            response = client.models.generate_content(model=primary, contents=[prompt] + parts, config=config)
        else:
            response = client.models.generate_content(model=primary, contents=prompt, config=config)
        used_model = primary
    except Exception:
        if parts:
            response = client.models.generate_content(model=secondary, contents=[prompt] + parts, config=config)
        else:
            response = client.models.generate_content(model=secondary, contents=prompt, config=config)
        used_model = secondary
    return extract_text(response), used_model


def _auto_assign_to_topic_monitors(
    db_session, paper: Paper, topic_matches: List[Dict[str, Any]]
) -> None:
    """Add *paper* to every tracker project whose predicate config matches it.

    BRIEF v2 §1 refactor: this no longer hand-rolls the topic/keyword/journal
    branches — it delegates to the predicate registry
    (``services.predicates.match_tracker``), which understands BOTH the new
    ``{match, predicates:[...]}`` config shape AND the legacy
    ``{topics, keywords, journals}`` (and ``config.query.*``) shape, so legacy
    trackers behave exactly as before (an OR across topic/keyword/journal is
    sufficient). The best predicate score (conventionally the semantic cosine)
    is written to ``ProjectPaper.match_score`` (BRIEF v2 §1/§6 — tracker views
    sort by it).

    Called automatically after analysis is stored. Idempotent — duplicate
    inserts are skipped via the UniqueConstraint; an existing membership has its
    ``match_score`` refreshed if the new best score is higher.

    Orphan candidate pool (owner decision 2026-06-03): papers with zero tracker
    matches remain in the orphan candidate pool — not added to any project, no
    analysis enqueued — until a future tracker that matches them is created
    (back-fill).

    Per-scope ``min_relevance`` gate (25-group-vs-personal.md §3): a tracker's
    explicit ``config.query.min_relevance`` (or ``config.min_relevance``) wins;
    otherwise the scope default applies (group 0.55, personal 0.25). The gate
    compares against the AI relevance score (0..1) when one exists; trackers
    without a stored relevance simply skip the gate (legacy behaviour).

    Folder label follows the same ``YYYY-MM`` convention as monthly projects.
    """
    from datetime import datetime

    from services.predicates import match_tracker

    # ── 1. Stash AI-matched canonical topic names on the paper so the
    #       cos_topic predicate scores against the AI labels (not just the
    #       paper.topics column). Mirrors the legacy ``matched_names`` set. ──
    matched_names = [
        str(m["topic"]).strip()
        for m in (topic_matches or [])
        if isinstance(m, dict) and m.get("topic")
    ]
    try:
        setattr(paper, "_matched_topic_names", matched_names)
    except Exception:
        pass

    # ── 2. Fetch all non-archived tracker projects ────────────────────────
    # Trackers are kind='tracker'; legacy rows use type='topic_monitor'. Match
    # either so this works pre- and post-taxonomy-migration. The is_default
    # "Monthly Literature Review" owns a separate auto-add path and has no
    # predicate query, so match_tracker() would no-op for it anyway.
    monitors = (
        db_session.query(Project)
        .filter(
            ((Project.kind == "tracker") | (Project.type == "topic_monitor")),
            Project.archived_at == None,  # noqa: E711
        )
        .all()
    )
    if not monitors:
        return

    # ── 3. AI relevance (0..1) for the per-scope min_relevance gate ────────
    relevance_01 = _paper_relevance_01(db_session, paper)

    # Determine the YYYY-MM folder for this paper.
    raw_date = paper.published_date or ""
    folder = (
        str(raw_date)[:7]
        if len(str(raw_date)) >= 7 and str(raw_date)[4] == "-"
        else datetime.utcnow().strftime("%Y-%m")
    )

    for proj in monitors:
        try:
            belongs, score = match_tracker(paper, proj)
        except Exception:
            # A malformed config must not sink the whole assignment pass.
            belongs, score = (False, 0.0)
        if not belongs:
            continue

        # Per-scope min_relevance gate (explicit config wins over scope default).
        if not _passes_relevance_gate(proj, relevance_01):
            continue

        # Already a member? Refresh match_score upward; never duplicate.
        existing = (
            db_session.query(ProjectPaper)
            .filter_by(project_id=proj.id, paper_id=paper.id)
            .first()
        )
        if existing:
            if score is not None and (
                existing.match_score is None or score > existing.match_score
            ):
                existing.match_score = float(score)
            continue

        db_session.add(
            ProjectPaper(
                project_id=proj.id,
                paper_id=paper.id,
                folder=folder,
                source="topic_monitor",
                match_score=float(score) if score is not None else None,
            )
        )
        # BRIEF v2 §6 (a) — a fresh tracker match is the trigger for the LAZY
        # full Gemini Pro analysis. We only enqueue a hint here (no Gemini call
        # on the matching hot path); a worker / route drains it later. Skip the
        # enqueue when the paper already has an analysis (re-match of an old
        # paper shouldn't re-run the expensive pass).
        if not _has_analysis(db_session, paper.id):
            enqueue_analysis(paper.id)

    # Commit any new memberships / score refreshes (no-op if nothing changed).
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()


# Per-scope min_relevance defaults (25-group-vs-personal.md §3 SCOPE_DEFAULTS).
# group is strict, personal is loose. Used only when a tracker does not set an
# explicit config.query.min_relevance.
_SCOPE_MIN_RELEVANCE = {"group": 0.55, "personal": 0.25}


def _paper_relevance_01(db_session, paper: Paper) -> Optional[float]:
    """Return the paper's AI relevance score normalised to 0..1, or ``None``.

    Reads ``ai_analyses.relevance_score`` (the model returns a 0..10 integer)
    for ``paper`` and normalises to 0..1. Returns ``None`` when there is no
    analysis row — callers then skip the relevance gate entirely (a tracker
    can't filter on a score that doesn't exist).

    NB (2026-07-12): the model has always emitted 0..10 (verified against both
    the live DB and the pre-wipe backup); the previous ``/100`` divisor mapped
    an 8/10 paper to 0.08, below every scope threshold, so the relevance gate
    silently rejected *every* paper on a freshly matched corpus. Divide by 10.
    """
    try:
        row = db_session.execute(
            _sa_text(
                "SELECT relevance_score FROM ai_analyses WHERE paper_id = :pid LIMIT 1"
            ),
            {"pid": paper.id},
        ).fetchone()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    try:
        return max(0.0, min(1.0, float(row[0]) / 10.0))
    except (TypeError, ValueError):
        return None


def _min_relevance_for(proj: Project) -> Optional[float]:
    """Resolve a tracker's effective min_relevance threshold (0..1) or ``None``.

    Precedence (25-group-vs-personal.md §3.1): explicit
    ``config.query.min_relevance`` / ``config.min_relevance`` wins; else the
    scope default (group 0.55, personal 0.25). Returns ``None`` when neither an
    explicit value nor a known scope is present, meaning "no gate".
    """
    cfg = proj.config or {}
    query = cfg.get("query") if isinstance(cfg.get("query"), dict) else {}
    raw = query.get("min_relevance")
    if raw is None:
        raw = cfg.get("min_relevance")
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            pass
    scope = (getattr(proj, "scope", None) or "").strip().lower()
    return _SCOPE_MIN_RELEVANCE.get(scope)


def _passes_relevance_gate(proj: Project, relevance_01: Optional[float]) -> bool:
    """True if ``relevance_01`` clears the tracker's min_relevance threshold.

    Passes (returns True) when there is no threshold for the tracker OR the
    paper has no AI relevance score — the gate only *rejects* when both a
    threshold and a score exist and the score falls short. This keeps legacy
    trackers (no scope / no explicit threshold) and un-analysed papers behaving
    exactly as before.
    """
    threshold = _min_relevance_for(proj)
    if threshold is None or relevance_01 is None:
        return True
    return relevance_01 >= threshold


def _store_analysis(
    db_session,
    paper: Paper,
    result: dict,
    analysis_model_id: str = "",
) -> dict:
    # ``analysis_model_id`` is the wire ID actually returned by call_gemini —
    # could be PRIMARY_MODEL or FALLBACK_MODEL depending on whether the
    # primary call raised (see PR-06 / 04-feature-design/11 §6).  Empty
    # string when PR-06 has not yet run or when the caller is legacy code
    # (re-analyse path called from a script that did not capture the client).
    # display_name_for_model("") returns "Unknown model" — see PR-06 §6.
    evidence = result.get("evidence", {}) if isinstance(result.get("evidence"), dict) else {}
    normalized_payload = {
        "citekey": result.get("citekey") or paper.id,
        "title": result.get("title") or paper.title,
        "year": str(result.get("year") or (paper.published_date or "")[:4]),
        "venue": result.get("venue") or paper.journal or "Others",
        "doi": normalize_doi(result.get("doi") or paper.doi or ""),
        "url": result.get("url") or paper.link or "",
        "has_pdf": bool(result.get("has_pdf")),
        "pdf_used": bool(result.get("pdf_used")),
        "abstract_flag": result.get("abstract_flag") or paper.abstract_flag or "missing",
        "used_sources": _as_string_list(result.get("used_sources")),
        "cos_topic_match": _normalize_topic_matches(result.get("cos_topic_match")),
        "one_sentence_takeaway_en": str(result.get("one_sentence_takeaway_en") or "").strip(),
        "one_sentence_takeaway_zh": str(result.get("one_sentence_takeaway_zh") or "").strip(),
        "summary_150w_en": str(result.get("summary_150w_en") or "").strip(),
        "summary_150w_zh": str(result.get("summary_150w_zh") or "").strip(),
        "key_contributions": _as_string_list(result.get("key_contributions")),
        "key_contributions_zh": _as_string_list(result.get("key_contributions_zh")),
        "methods": _as_string_list(result.get("methods")),
        "methods_zh": _as_string_list(result.get("methods_zh")),
        "data_or_experiments": _as_string_list(result.get("data_or_experiments")),
        "data_or_experiments_zh": _as_string_list(result.get("data_or_experiments_zh")),
        "limitations": _as_string_list(result.get("limitations")),
        "limitations_zh": _as_string_list(result.get("limitations_zh")),
        "followups_for_our_group": _as_string_list(result.get("followups_for_our_group")),
        "followups_for_our_group_zh": _as_string_list(result.get("followups_for_our_group_zh")),
        "relevance_score": _normalize_relevance_score(result.get("relevance_score")),
        "keywords": _as_string_list(result.get("keywords")),
        "figure_analyses": _normalize_figure_analyses(result.get("figure_analyses")),
        "evidence": {
            "based_on": str(evidence.get("based_on") or "").strip(),
            "notes": str(evidence.get("notes") or "").strip(),
        },
        # G4: structured 5-field analysis card (may be empty on legacy records)
        "structured_analysis": _normalize_structured_analysis(result.get("structured_analysis")),
        "structured_analysis_zh": _normalize_structured_analysis_zh(result.get("structured_analysis_zh")),
        # BRIEF C (analysis-card half) — frozen at the moment of generation
        # so a paper analysed under ``gemini-3.1-pro-preview`` still reads
        # "Gemini 3.1 Pro" months later even if that wire ID is retired
        # from the display dict (same guarantee Claude.ai gives — see
        # 04-feature-design/11-qa-model-badge.md §4).
        "analysis_model":       analysis_model_id,
        "analysis_model_label": display_name_for_model(analysis_model_id),
    }

    # v3 faceted labels — primary_topic is the in-preset subset of
    # cos_topic_match; the other four facets come from the model's `facets`
    # object, each coerced to its controlled vocab (off-vocab dropped).
    _primary = [
        m["topic"] for m in normalized_payload["cos_topic_match"]
        if m.get("topic") in _COS_TOPICS_SET
    ][:MAX_PRIMARY_TOPICS]
    normalized_payload["facets"] = _normalize_facets(result.get("facets"), _primary)

    analysis = paper.ai_analysis
    if analysis is None:
        analysis = AIAnalysis(paper_id=paper.id)
        db_session.add(analysis)

    analysis.payload = normalized_payload
    analysis.summary = normalized_payload["summary_150w_en"]
    analysis.one_sentence_takeaway = normalized_payload["one_sentence_takeaway_en"]
    analysis.relevance_score = normalized_payload["relevance_score"]
    analysis.keywords = normalized_payload["keywords"]
    analysis.topic_matches = normalized_payload["cos_topic_match"]
    analysis.facets = normalized_payload["facets"]
    analysis.used_sources = normalized_payload["used_sources"]
    analysis.pdf_used = normalized_payload["pdf_used"]
    analysis.evidence_based_on = normalized_payload["evidence"]["based_on"]
    analysis.evidence_notes = normalized_payload["evidence"]["notes"]
    # Denormalised columns added by PR-14 (`db.py` ALTER + `models.py`
    # Column definitions).  Mirror the same shape as the eleven existing
    # denormalised writes above so the analysis card template can read
    # ``paper.ai_analysis.analysis_model_label`` without a JSON parse.
    # ``hasattr`` keeps the write safe if PR-14 is reverted while PR-13
    # stays merged (the attribute simply won't be defined on the SQLAlchemy
    # model).  See §5 rollback note.
    if hasattr(analysis, "analysis_model"):
        analysis.analysis_model = normalized_payload["analysis_model"]
    if hasattr(analysis, "analysis_model_label"):
        analysis.analysis_model_label = normalized_payload["analysis_model_label"]

    # 2026-05-21: Coerce paper.topics to ONLY the preset COS_TOPICS list.
    # Any free-form topic from Gemini / BibTeX import gets mapped via
    # TOPIC_ALIASES → preset. Title-keyword sniff catches AI-less papers
    # whose original BibTeX keywords were too generic. Final fallback is
    # "Unable to categorize".
    paper.topics = coerce_topics_to_preset(
        paper.topics,
        normalized_payload["cos_topic_match"],
        title_hint=(paper.title or "") + " " + (paper.abstract or "")[:200],
    )
    db_session.commit()
    db_session.refresh(analysis)

    # Auto-assign to any matching Topic Monitoring projects (non-fatal).
    try:
        _auto_assign_to_topic_monitors(
            db_session, paper, normalized_payload["cos_topic_match"]
        )
    except Exception:
        pass  # never block analysis storage on project assignment errors

    # Vault-folder write — refresh the paper's `.md` in the user's Obsidian
    # vault (Roadmap V2 item 4). No-op if vault path isn't configured.
    try:
        from services.vault_writer import write_paper_md
        write_paper_md(db_session, paper, analysis)
    except Exception:
        pass  # never block analysis on vault write errors

    return normalized_payload


def _run_analysis(
    paper: Paper,
    db_session,
    *,
    pdf_path: Optional[str],
    force_metadata_only: bool,
    progress_callback: Optional[Callable[..., None]] = None,
    position: Optional[int] = None,
    total: Optional[int] = None,
) -> dict:
    item = _paper_to_item(paper, pdf_path=pdf_path)
    client = _get_vertex_client()
    label_prefix = f"[{position}/{total}] " if position and total else ""
    _emit(progress_callback, f"{label_prefix}Starting analysis for: {item.title[:120]}", stage="analysis_prepare", current=position, total=total, paper_id=paper.id)

    if not force_metadata_only and item.has_pdf:
        from services.tdm_guard_and_xml_renderer import is_tdm_restricted_paper
        tdm_blocked, tdm_reason = is_tdm_restricted_paper(paper, pdf_path=item.pdf_paths[0])
        if tdm_blocked:
            force_metadata_only = True
            _emit(progress_callback, f"{label_prefix}TDM-licensed PDF ({tdm_reason}); skipping Gemini PDF upload and using abstract/web context", stage="analysis_prepare", current=position, total=total, paper_id=paper.id)

    mode = "pdf" if (PREFER_PDF and item.has_pdf and not force_metadata_only) else "no_pdf"
    web_ctx = None
    parts: Optional[List[Any]] = None
    pdf_used = False

    if mode == "pdf":
        selected_pdf = item.pdf_paths[0]
        size_mb = file_size_mb(selected_pdf)
        if SEND_PDF_BYTES_TO_GEMINI and size_mb <= MAX_PDF_MB_TO_SEND:
            pdf_bytes = read_pdf_bytes(selected_pdf)
            parts = [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")]
            pdf_used = True
            _emit(progress_callback, f"{label_prefix}Using PDF file directly: {Path(selected_pdf).name} ({size_mb:.1f} MB)", stage="analysis_pdf", current=position, total=total, paper_id=paper.id)
        else:
            pdf_text = extract_pdf_text(selected_pdf)
            if pdf_text:
                parts = [f"Extracted PDF text (first pages):\n{pdf_text}"]
                pdf_used = True
                _emit(progress_callback, f"{label_prefix}PDF fallback to extracted text: {len(pdf_text)} characters", stage="analysis_pdf", current=position, total=total, paper_id=paper.id)
            else:
                mode = "no_pdf"
                _emit(progress_callback, f"{label_prefix}PDF text extraction failed; switching to metadata/web mode", stage="analysis_fallback", current=position, total=total, paper_id=paper.id)

    if mode == "no_pdf":
        with _WEB_CTX_SEMAPHORE:
            web_ctx = gather_web_context(item)
        parts = None
        _emit(
            progress_callback,
            f"{label_prefix}No PDF available; using abstract and public sources: {', '.join(web_ctx.get('used_sources', [])) or 'none'}",
            stage="analysis_web",
            current=position,
            total=total,
            paper_id=paper.id,
        )

    # Model routing (2026-06-13): Pro ONLY when a PDF is actually being analyzed
    # (pdf_used == True, i.e. PDF bytes or extracted full text were attached);
    # the quick / no-PDF (metadata + web-context) path uses Flash.
    _model = PDF_ANALYSIS_MODEL if pdf_used else QUICK_ANALYSIS_MODEL
    _fallback = PDF_ANALYSIS_FALLBACK if pdf_used else QUICK_ANALYSIS_FALLBACK
    _emit(progress_callback, f"{label_prefix}Calling Gemini ({_model}) for analysis", stage="analysis_model", current=position, total=total, paper_id=paper.id)
    prompt = build_prompt(item, mode=mode, web_ctx=web_ctx)
    # 2026-11-09: models the express route does not serve (gemini-4-argon) go out on
    # their project-scoped resource path; the DB keeps the SHORT ID, never the path.
    from services.vertex_model_routing import wire_model_name
    _wire_model = wire_model_name(_model)
    raw, analysis_model_id = call_gemini(client, prompt, parts=parts, model_name=_wire_model, fallback_model=_fallback)
    if analysis_model_id == _wire_model:
        analysis_model_id = _model

    try:
        result = normalize_result_fields(safe_json_load(raw))
    except Exception as exc:
        result = {
            "citekey": paper.id,
            "title": paper.title,
            "year": (paper.published_date or "")[:4],
            "venue": paper.journal or "Others",
            "doi": normalize_doi(paper.doi or ""),
            "url": paper.link or "",
            "has_pdf": item.has_pdf,
            "pdf_used": pdf_used,
            "abstract_flag": item.abstract_flag,
            "used_sources": (web_ctx.get("used_sources", []) if web_ctx else ["pdf"]),
            "one_sentence_takeaway_en": "",
            "summary_150w_en": "",
            "key_contributions": [],
            "methods": [],
            "data_or_experiments": [],
            "limitations": [f"AI JSON parsing failed: {exc}"],
            "followups_for_our_group": [],
            "relevance_score": 0,
            "keywords": [],
            "figure_analyses": [],
            "cos_topic_match": [],
            "evidence": {
                "based_on": "pdf" if pdf_used else "abstract+web",
                "notes": "Model output could not be parsed as JSON.",
            },
        }
        _emit(progress_callback, f"{label_prefix}Model JSON parsing failed; stored fallback result", stage="analysis_parse_error", current=position, total=total, paper_id=paper.id)

    result["citekey"] = result.get("citekey") or paper.id
    result["title"] = result.get("title") or paper.title
    result["year"] = result.get("year") or (paper.published_date or "")[:4]
    result["venue"] = result.get("venue") or paper.journal or "Others"
    result["doi"] = result.get("doi") or normalize_doi(paper.doi or "")
    result["url"] = result.get("url") or paper.link or ""
    result["has_pdf"] = item.has_pdf
    result["pdf_used"] = bool(pdf_used)
    result["abstract_flag"] = result.get("abstract_flag") or item.abstract_flag
    result["used_sources"] = result.get("used_sources") or (web_ctx.get("used_sources", []) if web_ctx else ["pdf"])
    if not result.get("figure_analyses") and item.has_pdf and item.pdf_paths:
        result["figure_analyses"] = extract_figure_insights_from_pdf(item.pdf_paths[0])
    if not result.get("evidence"):
        result["evidence"] = {
            "based_on": "pdf" if pdf_used else "abstract+web",
            "notes": "Normalized by local application safeguards.",
        }

    _emit(
        progress_callback,
        f"{label_prefix}Analysis completed: {item.title[:100]} | relevance={result.get('relevance_score', 0)}",
        stage="analysis_done",
        current=position,
        total=total,
        paper_id=paper.id,
    )

    # G6: populate (or refresh) the per-page text cache while we already
    # have the PDF path in scope.  Errors are non-fatal — we just skip.
    if pdf_path:
        try:
            _cache_pages(paper.id, pdf_path, db_session)
        except Exception:
            pass

        # PR-21 — paragraph-anchor validation (BRIEF item H).
        # Validator must never raise into the analyze pipeline. Stamp ⚠
        # on misses but persist anyway. (Retry-with-LLM-feedback hook is
        # noted but NOT wired here — would require another Gemini round-trip
        # per failed anchor. Future PR.)
        try:
            failures = _validate_paragraph_anchors(result, paper.id, db_session)
            if failures:
                _stamp_unverified(result, failures)
                _emit(
                    progress_callback,
                    f"{label_prefix}Anchor validation: {len(failures)} marked unverified",
                    stage="analysis_anchor_unverified",
                    current=position, total=total, paper_id=paper.id,
                )
        except Exception:
            pass

    # ``analysis_model_id`` came back from ``call_gemini`` itself (the wire ID
    # that actually answered — primary or fallback). Fall back to
    # ``PRIMARY_MODEL`` / "" so ``display_name_for_model("")`` renders
    # "Unknown model" rather than blowing up the analyse pipeline.
    analysis_model_id = analysis_model_id or PRIMARY_MODEL or ""
    return _store_analysis(db_session, paper, result, analysis_model_id=analysis_model_id)


def analyze_paper_from_pdf(
    paper_id: str,
    pdf_path: str,
    db_session,
    progress_callback: Optional[Callable[..., None]] = None,
) -> dict:
    paper = db_session.get(Paper, paper_id)
    if paper is None:
        raise ValueError(f"Paper not found: {paper_id}")

    resolved_path = str(Path(pdf_path).resolve())

    # FN-B8-A02-2: refuse to call Gemini on near-blank / scan-only PDFs.
    # Without this guard, junk uploads (TC-811: 3 KB stub) confabulated
    # full analyses with hallucinated titles, citekeys, and a 10/10
    # relevance score. PyMuPDF (fitz) is already a project dependency.
    if fitz is not None:
        try:
            with FITZ_LOCK:
                with fitz.open(resolved_path) as doc:
                    total_text = "".join(page.get_text() for page in doc)
            stripped_len = len(total_text.strip())
            if stripped_len < 500:
                raise ValueError(
                    f"PDF has only {stripped_len} chars of extractable text; "
                    "refusing to analyze (likely placeholder, junk, or scan-only)."
                )
        except ValueError:
            raise
        except Exception:
            # fitz failed to open the PDF for some other reason — let the
            # downstream pipeline decide what to do; we don't want this
            # cheap guard to mask real PDF problems.
            pass

    paper.pdf_path = resolved_path
    db_session.commit()
    db_session.refresh(paper)
    return _run_analysis(
        paper,
        db_session,
        pdf_path=resolved_path,
        force_metadata_only=False,
        progress_callback=progress_callback,
        position=1,
        total=1,
    )


def analyze_paper_from_metadata(
    paper_id: str,
    db_session,
    progress_callback: Optional[Callable[..., None]] = None,
) -> dict:
    paper = db_session.get(Paper, paper_id)
    if paper is None:
        raise ValueError(f"Paper not found: {paper_id}")
    return _run_analysis(
        paper,
        db_session,
        pdf_path=None,
        force_metadata_only=True,
        progress_callback=progress_callback,
        position=1,
        total=1,
    )


def analyze_pending_papers(
    db_session,
    limit: Optional[int] = None,
    progress_callback: Optional[Callable[..., None]] = None,
    workers: int = 1,
    session_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, int]:
    """Analyse every paper without an AIAnalysis row (newest first, up to
    ``limit``).

    ``workers > 1`` (2026-07-06) fans the batch out to a bounded thread pool —
    see :func:`_analyze_pending_papers_parallel`. ``workers <= 1`` keeps the
    original serial behaviour byte-for-byte (also the runtime rollback path:
    ``SEISMO_EAGER_ANALYSIS_WORKERS=1``). ``session_factory`` is only used by
    the parallel path; it MUST be late-bound (e.g. ``main._db_session`` or a
    lambda over ``db.SessionLocal``) so test fixtures that monkeypatch the
    sessionmaker keep working.
    """
    if workers and workers > 1:
        return _analyze_pending_papers_parallel(
            db_session,
            limit=limit,
            progress_callback=progress_callback,
            workers=workers,
            session_factory=session_factory,
        )
    query = db_session.query(Paper).outerjoin(AIAnalysis).filter(AIAnalysis.id.is_(None)).order_by(Paper.datetime_utc.desc())
    if limit:
        papers = query.limit(limit).all()
    else:
        papers = query.all()

    total = len(papers)
    _emit(progress_callback, f"Papers waiting for analysis: {total}", stage="analysis_queue", current=0, total=total)
    analyzed = 0
    for index, paper in enumerate(papers, start=1):
        # Skip if the open-trigger or another eager pass is already analysing it.
        if not _claim_analysis(paper.id):
            continue
        try:
            _run_analysis(
                paper,
                db_session,
                pdf_path=paper.pdf_path or None,
                force_metadata_only=(paper.pdf_path is None),
                progress_callback=progress_callback,
                position=index,
                total=total,
            )
            analyzed += 1
        finally:
            _release_analysis(paper.id)
    _emit(progress_callback, f"Analysis phase completed; processed {analyzed} papers", stage="analysis_complete", current=analyzed, total=total, analyzed=analyzed)
    return {"analyzed": analyzed}


def _analyze_pending_papers_parallel(
    db_session,
    *,
    limit: Optional[int],
    progress_callback: Optional[Callable[..., None]],
    workers: int,
    session_factory: Optional[Callable[[], Any]],
) -> Dict[str, int]:
    """Bounded-thread-pool variant of :func:`analyze_pending_papers` (2026-07-06).

    Concurrency contract (each point is load-bearing — see the 2026-07-06
    audit before changing any of them):

    - **IDs only cross threads, never ORM objects.** ``_store_analysis`` walks
      the ``paper.ai_analysis`` relationship (lazy SQL through the OWNING
      session) and commits ``paper.topics`` — both must happen on the session
      that loaded the Paper, so every worker re-loads its Paper inside its own
      ``session_factory()`` session (the same pattern as ``analyze_on_open``
      and ``main._trigger_lazy_analysis_on_open``).
    - **Per-paper failure isolation.** The serial path aborts the whole batch
      on the first raiser (one poison paper starves everything behind it); a
      worker here rolls back its own session, records the failure for the
      poison blacklist, and returns 0.
    - **The daemon's no-progress backoff still works.** Stub AIAnalysis rows
      are only stored after a successful HTTP response, so a dead/missing key
      cannot fake progress: every worker fails, analyzed == 0, the pending
      count stays flat and the eager loop backs off exactly as before.
    - ``_get_vertex_client()`` is pre-warmed before the pool spins up: with a
      missing key it raises immediately (matching the serial path's first-paper
      failure) instead of once per worker.
    """
    if workers > 1 and progress_callback is not None:
        # Serialize a real callback — runtime-log helpers upstream are not
        # written for concurrent calls. (The eager daemon passes None.)
        callback_lock = threading.Lock()
        original_callback = progress_callback

        def progress_callback(message, **kwargs):  # noqa: F811 — deliberate shadow
            with callback_lock:
                original_callback(message, **kwargs)

    if session_factory is None:
        # Late-bound module attribute lookup: tests monkeypatch db.SessionLocal
        # (tests/conftest.py isolated_db) and an early `from db import ...`
        # binding would silently bypass that and write the real database.
        import db as _db_module

        def session_factory():
            return _db_module.SessionLocal()

    id_query = (
        db_session.query(Paper.id)
        .outerjoin(AIAnalysis)
        .filter(AIAnalysis.id.is_(None))
        .order_by(Paper.datetime_utc.desc())
    )
    if limit:
        # Over-fetch so blacklisted poison papers don't shrink the effective batch.
        candidate_rows = id_query.limit(limit + _analysis_blacklist_size()).all()
    else:
        candidate_rows = id_query.all()
    ids = [row[0] for row in candidate_rows if not _is_analysis_blacklisted(row[0])]
    if limit:
        ids = ids[:limit]

    total = len(ids)
    _emit(progress_callback, f"Papers waiting for analysis: {total}", stage="analysis_queue", current=0, total=total)
    if not ids:
        _emit(progress_callback, "Analysis phase completed; processed 0 papers", stage="analysis_complete", current=0, total=0, analyzed=0)
        return {"analyzed": 0}

    _get_vertex_client()  # fail fast (missing key) before any thread is spawned

    def _analyze_one(indexed_id: Tuple[int, str]) -> int:
        index, paper_id = indexed_id
        if not _claim_analysis(paper_id):
            return 0  # open-trigger / drain / another worker already has it
        session = session_factory()
        try:
            paper = session.get(Paper, paper_id)
            if paper is None or _has_analysis(session, paper_id):
                return 0
            _run_analysis(
                paper,
                session,
                pdf_path=paper.pdf_path or None,
                force_metadata_only=(paper.pdf_path is None),
                progress_callback=progress_callback,
                position=index,
                total=total,
            )
            return 1
        except Exception:  # noqa: BLE001 — one bad paper must not stop the batch.
            logger.warning("parallel analysis failed for paper %s", paper_id, exc_info=True)
            try:
                session.rollback()
            except Exception:
                pass
            _record_analysis_failure(paper_id)
            return 0
        finally:
            _release_analysis(paper_id)
            try:
                session.close()
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="seismo-analysis") as pool:
        # Exceptions never escape _analyze_one, so consuming the map is safe;
        # the with-block joins every worker before we return (the eager loop's
        # fresh-session recount depends on that).
        analyzed = sum(pool.map(_analyze_one, list(enumerate(ids, start=1))))

    _emit(progress_callback, f"Analysis phase completed; processed {analyzed} papers", stage="analysis_complete", current=analyzed, total=total, analyzed=analyzed)
    return {"analyzed": analyzed}
