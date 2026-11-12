"""Shared utilities for the PDF acquisition pipeline.

Everything here is imported by both the stage modules and the orchestrator in
``pipeline.py``. Keep it dependency-free beyond ``requests`` so the stage
stubs do not pull in Playwright or any publisher-specific library.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from services.pdf_naming import pdf_target_path_for_paper, write_pdf_atomic


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parents[2]
DOWNLOADS_DIR = BASE_DIR / "downloads"

# Placeholder emails that load_acquisition_config() rejects, so the pipeline
# refuses to run with an obviously unconfigured contact address.
_PLACEHOLDER_EMAILS = frozenset(
    {
        "",
        "you@example.com",
        "you@university.edu",
        "user@example.com",
        "change-me@example.com",
    }
)

# Hosts that strongly indicate we hit an auth wall instead of a real PDF.
LOGIN_HOST_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern) for pattern in [
        r"^login\.",
        r"^shibboleth\.",
        r"^idp\.",
        r"^ezproxy\.",
        r"\.ezproxy\.",
        r"^wayf\.",
        r"\.shibboleth\.",
        r"^sso\.",
        r"^auth\.",
    ]
)

_HTML_SIGNIN_MARKERS = (
    b"<form",
    b"sign in",
    b"log in",
    b"login required",
    b"please log in",
    b"subscribe",
    b"access options",
    b"shibboleth",
    b"institutional login",
)


# ---------------------------------------------------------------------------
# RetrievalStatus — string constants rather than enum.Enum so SQLite stores
# them as plain VARCHAR without needing a custom type adapter.
# ---------------------------------------------------------------------------


class RetrievalStatus:
    NOT_ATTEMPTED = "not_attempted"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"

    # Failure categories explicitly enumerated by the design doc.
    NO_OA_COPY = "no_oa_copy"
    RESOLVER_NO_SERVICE = "resolver_no_service"
    AUTH_REQUIRED = "auth_required"
    PAYWALLED_AFTER_LOGIN = "paywalled_after_login"
    CAPTCHA_OR_BOT_BLOCK = "captcha_or_bot_block"
    NO_PDF_BUT_HTML = "no_pdf_but_html"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    TRANSIENT_ERROR = "transient_error"

    # Pipeline-only statuses used to describe stub stages cleanly.
    SKIPPED_PENDING_VERIFICATION = "skipped_pending_verification"
    UNSUPPORTED = "unsupported"

    # Severity ordering, highest-first. Used by pipeline.finalize() to pick the
    # "worst" non-success outcome across all stages when aggregating errors.
    SEVERITY = (
        PAYWALLED_AFTER_LOGIN,
        CAPTCHA_OR_BOT_BLOCK,
        AUTH_REQUIRED,
        NO_PDF_BUT_HTML,
        UNSUPPORTED_CONTENT_TYPE,
        RESOLVER_NO_SERVICE,
        NO_OA_COPY,
        TRANSIENT_ERROR,
        UNSUPPORTED,
        SKIPPED_PENDING_VERIFICATION,
    )

    AUTO_RETRY_POLICY: Dict[str, Dict[str, int]] = {
        TRANSIENT_ERROR: {"max_attempts": 5, "base_hours": 1, "cap_hours": 24},
        NO_OA_COPY: {"max_attempts": 3, "base_hours": 14 * 24, "cap_hours": 14 * 24},
        RESOLVER_NO_SERVICE: {"max_attempts": 3, "base_hours": 14 * 24, "cap_hours": 14 * 24},
    }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PdfAcquisitionError(Exception):
    """Base class for errors surfaced by the acquisition pipeline."""


class PdfValidationError(PdfAcquisitionError):
    """A candidate URL failed PDF validation.

    Carries a ``category`` drawn from :class:`RetrievalStatus` so the caller
    can distinguish transient failures from permanent ones without having to
    inspect free-form error strings.
    """

    def __init__(self, category: str, detail: str, http_status: Optional[int] = None):
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail
        self.http_status = http_status


class PaperNotFoundError(PdfAcquisitionError):
    """Raised when acquire_pdf is called with an unknown paper_id."""


class ConfigError(PdfAcquisitionError):
    """Raised at startup when the acquisition config is incomplete."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    """One row destined for the ``retrieval_attempts`` SQL table."""

    stage: str
    source: Optional[str]
    url_attempted: Optional[str]
    outcome: str
    http_status: Optional[int] = None
    bytes_downloaded: Optional[int] = None
    error_reason: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None

    def finalize(self) -> None:
        if self.finished_at is None:
            self.finished_at = datetime.utcnow()
        if self.duration_ms is None:
            delta = self.finished_at - self.started_at
            self.duration_ms = int(delta.total_seconds() * 1000)


@dataclass
class StageResult:
    """Return value of each ``stageN_try_*`` function."""

    success: bool
    status: str
    source: Optional[str] = None
    source_url: Optional[str] = None
    pdf_bytes: Optional[bytes] = None
    error_reason: Optional[str] = None
    attempts: List[AttemptRecord] = field(default_factory=list)


@dataclass
class AcquisitionResult:
    """Pipeline-level return value surfaced to the HTTP layer."""

    paper_id: str
    status: str
    source: Optional[str] = None
    source_url: Optional[str] = None
    last_stage: Optional[str] = None
    pdf_path: Optional[str] = None
    error_reason: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    stage_attempts: Dict[str, str] = field(default_factory=dict)
    attempts_recorded: int = 0
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "status": self.status,
            "source": self.source,
            "source_url": self.source_url,
            "last_stage": self.last_stage,
            "pdf_path": self.pdf_path,
            "error_reason": self.error_reason,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "stage_attempts": dict(self.stage_attempts),
            "attempts_recorded": self.attempts_recorded,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ValidatedPdf:
    data: bytes
    source_url: str
    http_status: int
    content_type: str


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AcquisitionConfig:
    contact_email: str
    enabled_stages: frozenset = field(
        default_factory=lambda: frozenset({"stage1", "stage2", "stage3", "stage4", "stage5"})
    )
    enable_crossref_tdm_links: bool = False
    publisher_min_delay_sec: float = 10.0
    global_max_per_hour: int = 50
    global_max_per_day: int = 300
    per_host_max_per_hour: int = 10
    min_pdf_bytes: int = 10_000
    max_pdf_bytes: int = 50_000_000
    unpaywall_daily_cap: int = 50_000
    unpaywall_email: Optional[str] = None
    http_timeout_sec: int = 30
    user_agent: str = "SeismoPaperBot/4.0"
    # Stage 5: Playwright + persisted browser profile. We use
    # launch_persistent_context(user_data_dir=...) instead of storage_state
    # because publishers' bot detection (Cloudflare) fingerprints more than
    # cookies — it looks at the Chrome binary build ("Chrome for Testing" vs
    # real stable Chrome), localStorage, IndexedDB, service workers, etc.
    # A real Chrome user profile directory carries all of that.
    playwright_user_data_dir: Optional[str] = None
    # Playwright browser channel — "chrome" uses system-installed Google Chrome
    # (stable), which is much harder to bot-detect than playwright's bundled
    # Chrome for Testing. Empty string = fall back to bundled chromium.
    playwright_browser_channel: str = "chrome"
    playwright_per_paper_delay_sec: float = 5.0
    playwright_navigation_timeout_ms: int = 45_000
    playwright_networkidle_timeout_ms: int = 20_000
    # headless=False is required to bypass Elsevier/Cloudflare bot detection.
    # Personal-research workflow runs locally so a brief visible browser is OK.
    playwright_headless: bool = False

    def effective_unpaywall_email(self) -> str:
        return self.unpaywall_email or self.contact_email


def _parse_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: Optional[str], default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _parse_float(value: Optional[str], default: float) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def load_acquisition_config() -> AcquisitionConfig:
    """Read configuration from process environment.

    Raises ConfigError if ``PIPELINE_CONTACT_EMAIL`` is missing or set to a
    placeholder. The pipeline absolutely refuses to hit public APIs without
    a real contact address because that is what the OpenAlex / Unpaywall /
    Crossref polite pools require.
    """

    contact_email = (os.getenv("PIPELINE_CONTACT_EMAIL") or "").strip()
    if contact_email.lower() in _PLACEHOLDER_EMAILS:
        raise ConfigError(
            "PIPELINE_CONTACT_EMAIL is missing or set to a placeholder. "
            "Edit .env and set it to a real email address before using the "
            "PDF acquisition pipeline."
        )

    enabled_raw = (os.getenv("PDF_ACQUIRE_ENABLED_STAGES") or "").strip()
    if enabled_raw:
        enabled_stages = frozenset(
            part.strip() for part in enabled_raw.split(",") if part.strip()
        )
    else:
        enabled = set()
        if _parse_bool(os.getenv("ENABLE_STAGE1_ETH_RC"), False):
            enabled.add("stage1")
        if _parse_bool(os.getenv("ENABLE_STAGE2_OA"), True):
            enabled.add("stage2")
        if _parse_bool(os.getenv("ENABLE_STAGE3_DISCOVERY"), False):
            enabled.add("stage3")
        if _parse_bool(os.getenv("ENABLE_STAGE4_RESOLVER"), False):
            enabled.add("stage4")
        if _parse_bool(os.getenv("ENABLE_STAGE5_BROWSER"), False):
            enabled.add("stage5")
        enabled_stages = frozenset(enabled)

    raw_user_data_dir = (os.getenv("PLAYWRIGHT_USER_DATA_DIR") or "").strip()
    if not raw_user_data_dir:
        raw_user_data_dir = "~/.config/swp-paper-hub/chrome_profile"
    playwright_user_data_dir = os.path.expanduser(raw_user_data_dir)

    playwright_browser_channel = (
        os.getenv("PLAYWRIGHT_BROWSER_CHANNEL", "chrome") or ""
    ).strip()

    cfg = AcquisitionConfig(
        contact_email=contact_email,
        enabled_stages=enabled_stages,
        enable_crossref_tdm_links=_parse_bool(os.getenv("ENABLE_CROSSREF_TDM_LINKS"), False),
        publisher_min_delay_sec=_parse_float(os.getenv("PUBLISHER_MIN_DELAY_SEC"), 10.0),
        global_max_per_hour=_parse_int(os.getenv("PIPELINE_MAX_PER_HOUR"), 50),
        global_max_per_day=_parse_int(os.getenv("PIPELINE_MAX_PER_DAY"), 300),
        per_host_max_per_hour=_parse_int(os.getenv("PIPELINE_PER_HOST_MAX_PER_HOUR"), 10),
        min_pdf_bytes=_parse_int(os.getenv("MIN_PDF_BYTES"), 10_000),
        max_pdf_bytes=_parse_int(os.getenv("MAX_PDF_BYTES"), 50_000_000),
        unpaywall_daily_cap=_parse_int(os.getenv("UNPAYWALL_DAILY_CAP"), 50_000),
        unpaywall_email=(os.getenv("UNPAYWALL_EMAIL") or "").strip() or None,
        http_timeout_sec=_parse_int(os.getenv("PDF_HTTP_TIMEOUT_SEC"), 30),
        playwright_user_data_dir=playwright_user_data_dir,
        playwright_browser_channel=playwright_browser_channel,
        playwright_per_paper_delay_sec=_parse_float(
            os.getenv("PLAYWRIGHT_PER_PAPER_DELAY_SEC"), 5.0
        ),
        playwright_navigation_timeout_ms=_parse_int(
            os.getenv("PLAYWRIGHT_NAVIGATION_TIMEOUT_MS"), 45_000
        ),
        playwright_networkidle_timeout_ms=_parse_int(
            os.getenv("PLAYWRIGHT_NETWORKIDLE_TIMEOUT_MS"), 20_000
        ),
        playwright_headless=_parse_bool(os.getenv("PLAYWRIGHT_HEADLESS"), False),
    )
    return cfg


def make_headers(cfg: AcquisitionConfig, accept: str = "application/pdf, */*;q=0.8") -> Dict[str, str]:
    ua = (
        f"{cfg.user_agent} (+mailto:{cfg.contact_email}; "
        "personal-research)"
    )
    return {
        "User-Agent": ua,
        "From": cfg.contact_email,
        "Accept": accept,
    }


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class DomainRateLimiter:
    """Per-host min-interval gate.

    Tracks the last request monotonic timestamp per hostname and sleeps the
    current thread until the next allowed slot. Thread-safe. Does not persist
    across process restarts — this is the "polite to API providers" layer,
    not the "honor service-level quotas" layer (that's GlobalQuota).
    """

    # Built-in polite-pool intervals. Anything not matched here falls back to
    # ``default_interval`` (which the pipeline sets to publisher_min_delay_sec
    # for everything that isn't an API provider).
    BUILT_IN_INTERVALS = {
        "api.openalex.org": 0.1,
        "api.crossref.org": 0.05,
        "api.unpaywall.org": 1.0,
        "api.library.ethz.ch": 0.5,
        "eth.swisscovery.slsp.ch": 2.0,
        "swisscovery.slsp.ch": 2.0,
    }

    def __init__(self, default_interval: float) -> None:
        self._default = max(0.0, default_interval)
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def interval_for(self, host: str) -> float:
        host = (host or "").lower()
        if host in self.BUILT_IN_INTERVALS:
            return self.BUILT_IN_INTERVALS[host]
        for built_in, interval in self.BUILT_IN_INTERVALS.items():
            if host.endswith("." + built_in):
                return interval
        return self._default

    def wait(self, host: str) -> float:
        """Block until the next request to ``host`` is allowed. Returns the
        number of seconds actually slept, for logging."""
        host = (host or "").lower()
        interval = self.interval_for(host)
        with self._lock:
            last = self._last.get(host, 0.0)
            now = time.monotonic()
            target = last + interval
            if target <= now:
                self._last[host] = now
                return 0.0
            sleep_for = target - now
            self._last[host] = target
        time.sleep(sleep_for)
        return sleep_for


# ---------------------------------------------------------------------------
# Global quota — persisted into AppState so the cap survives restarts.
# ---------------------------------------------------------------------------


class GlobalQuota:
    """Hourly / daily / per-host hard caps for the whole pipeline.

    Counters are stored in AppState with time-bucket keys so a process
    restart or accidental DB rollback does not lose the current window.
    """

    def __init__(self, cfg: AcquisitionConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.utcnow()

    @staticmethod
    def _hour_key(ts: datetime) -> str:
        return "pdf_acquire_hourly_count_" + ts.strftime("%Y%m%d%H")

    @staticmethod
    def _day_key(ts: datetime) -> str:
        return "pdf_acquire_daily_count_" + ts.strftime("%Y%m%d")

    @staticmethod
    def _host_key(host: str, ts: datetime) -> str:
        return f"pdf_acquire_host_{host.lower()}_{ts.strftime('%Y%m%d%H')}"

    @staticmethod
    def _unpaywall_key(ts: datetime) -> str:
        return "unpaywall_daily_count_" + ts.strftime("%Y%m%d")

    def _read_int(self, db_session, key: str) -> int:
        from models import AppState  # local import to avoid circular
        row = db_session.get(AppState, key)
        if row is None or not row.value:
            return 0
        try:
            return int(row.value)
        except ValueError:
            return 0

    def _write_int(self, db_session, key: str, value: int) -> None:
        from models import AppState
        row = db_session.get(AppState, key)
        if row is None:
            row = AppState(key=key, value=str(value))
            db_session.add(row)
        else:
            row.value = str(value)

    def check_and_reserve(self, db_session, host: Optional[str]) -> Optional[Tuple[str, datetime]]:
        """Reserve one slot. Returns None on success, or a (reason, next_retry)
        tuple describing the quota that was hit."""
        with self._lock:
            now = self._now()
            hour_key = self._hour_key(now)
            day_key = self._day_key(now)
            host_key = self._host_key(host or "_none_", now)

            hourly = self._read_int(db_session, hour_key)
            daily = self._read_int(db_session, day_key)
            per_host = self._read_int(db_session, host_key) if host else 0

            if daily >= self._cfg.global_max_per_day:
                return (
                    f"daily cap {self._cfg.global_max_per_day} reached",
                    (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                )
            if hourly >= self._cfg.global_max_per_hour:
                return (
                    f"hourly cap {self._cfg.global_max_per_hour} reached",
                    (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0),
                )
            if host and per_host >= self._cfg.per_host_max_per_hour:
                return (
                    f"per-host hourly cap {self._cfg.per_host_max_per_hour} for {host}",
                    (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0),
                )

            self._write_int(db_session, hour_key, hourly + 1)
            self._write_int(db_session, day_key, daily + 1)
            if host:
                self._write_int(db_session, host_key, per_host + 1)
            db_session.commit()
            return None

    def check_and_reserve_unpaywall(self, db_session) -> Optional[Tuple[str, datetime]]:
        with self._lock:
            now = self._now()
            key = self._unpaywall_key(now)
            current = self._read_int(db_session, key)
            if current >= self._cfg.unpaywall_daily_cap:
                return (
                    f"unpaywall daily cap {self._cfg.unpaywall_daily_cap} reached",
                    (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                )
            self._write_int(db_session, key, current + 1)
            db_session.commit()
            return None


# ---------------------------------------------------------------------------
# Acquisition context — handed to each stage
# ---------------------------------------------------------------------------


@dataclass
class AcquisitionContext:
    cfg: AcquisitionConfig
    db_session: Any
    rate_limiter: DomainRateLimiter
    quota: GlobalQuota
    progress_callback: Optional[Callable[..., None]] = None
    enrichment: Dict[str, Any] = field(default_factory=dict)

    def log(self, message: str, *, stage: Optional[str] = None, **stats: Any) -> None:
        if self.progress_callback:
            try:
                self.progress_callback(message, stage=stage, **stats)
            except Exception:
                pass
        else:
            print(message, flush=True)

    def headers(self, accept: str = "application/pdf, */*;q=0.8") -> Dict[str, str]:
        return make_headers(self.cfg, accept=accept)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def is_login_host(url: str) -> bool:
    host = host_of(url)
    return any(pattern.search(host) for pattern in LOGIN_HOST_PATTERNS)


def dedupe_urls(urls: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for url in urls:
        if not url:
            continue
        parts = urlsplit(url)
        key = (parts.scheme.lower(), (parts.hostname or "").lower(), parts.path, parts.query)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


# ---------------------------------------------------------------------------
# PDF fetch + validation
# ---------------------------------------------------------------------------


def validate_pdf_bytes(
    data: bytes,
    source_url: str,
    *,
    min_bytes: int,
    max_bytes: int,
    content_type: str = "",
    http_status: int = 200,
) -> ValidatedPdf:
    """Run the same magic-bytes / size / HTML / login-host checks that
    :func:`fetch_and_validate_pdf` performs, but on bytes that were already
    downloaded by some other transport (e.g. Playwright's APIRequestContext).

    Raises :class:`PdfValidationError` with a precise category on failure.
    Returns a :class:`ValidatedPdf` on success.
    """

    total_bytes = len(data)
    if total_bytes == 0:
        raise PdfValidationError(
            RetrievalStatus.TRANSIENT_ERROR,
            "empty response body",
            http_status=http_status,
        )
    if total_bytes > max_bytes:
        raise PdfValidationError(
            RetrievalStatus.UNSUPPORTED_CONTENT_TYPE,
            f"response exceeds max_pdf_bytes={max_bytes}",
            http_status=http_status,
        )
    if is_login_host(source_url):
        raise PdfValidationError(
            RetrievalStatus.AUTH_REQUIRED,
            f"served from login host {host_of(source_url)}",
            http_status=http_status,
        )

    first_chunk = data[:16]
    is_pdf_magic = first_chunk.startswith(b"%PDF-")
    ct_lower = (content_type or "").lower()
    if not is_pdf_magic:
        looks_html = ct_lower.startswith("text/html") or first_chunk.lstrip().lower().startswith(
            (b"<!doctype", b"<html", b"<head")
        )
        if looks_html:
            head_lower = data[:8192].lower()
            if b"cloudflare" in head_lower or b"cf-challenge" in head_lower or b"captcha" in head_lower:
                raise PdfValidationError(
                    RetrievalStatus.CAPTCHA_OR_BOT_BLOCK,
                    f"cloudflare or captcha page from {host_of(source_url)}",
                    http_status=http_status,
                )
            if any(marker in head_lower for marker in _HTML_SIGNIN_MARKERS):
                raise PdfValidationError(
                    RetrievalStatus.NO_PDF_BUT_HTML,
                    f"landing page html from {host_of(source_url)}",
                    http_status=http_status,
                )
            raise PdfValidationError(
                RetrievalStatus.NO_PDF_BUT_HTML,
                f"html body from {host_of(source_url)}",
                http_status=http_status,
            )
        raise PdfValidationError(
            RetrievalStatus.UNSUPPORTED_CONTENT_TYPE,
            f"non-PDF content-type {ct_lower!r} from {host_of(source_url)}",
            http_status=http_status,
        )

    if total_bytes < min_bytes:
        raise PdfValidationError(
            RetrievalStatus.NO_PDF_BUT_HTML,
            f"pdf too small ({total_bytes} < {min_bytes})",
            http_status=http_status,
        )

    return ValidatedPdf(
        data=data,
        source_url=source_url,
        http_status=http_status,
        content_type=ct_lower or "application/pdf",
    )


def fetch_and_validate_pdf(
    url: str,
    ctx: AcquisitionContext,
    *,
    referer: Optional[str] = None,
) -> ValidatedPdf:
    """Download a candidate URL and verify that the response is a real PDF.

    Raises :class:`PdfValidationError` with a precise
    :class:`RetrievalStatus` category when validation fails. Callers should
    catch that exception and append an :class:`AttemptRecord` to the current
    :class:`StageResult` before trying the next candidate.
    """

    ctx.rate_limiter.wait(host_of(url))
    headers = ctx.headers()
    if referer:
        headers["Referer"] = referer

    try:
        response = requests.get(
            url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=ctx.cfg.http_timeout_sec,
        )
    except requests.exceptions.Timeout as exc:
        raise PdfValidationError(RetrievalStatus.TRANSIENT_ERROR, f"timeout: {exc}") from exc
    except requests.exceptions.SSLError as exc:
        raise PdfValidationError(RetrievalStatus.TRANSIENT_ERROR, f"ssl error: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise PdfValidationError(RetrievalStatus.TRANSIENT_ERROR, f"connection error: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        raise PdfValidationError(RetrievalStatus.TRANSIENT_ERROR, f"request error: {exc}") from exc

    status_code = response.status_code
    final_url = response.url or url
    content_type = (response.headers.get("Content-Type") or "").lower()

    if status_code != 200:
        response.close()
        if status_code in (401, 403):
            raise PdfValidationError(
                RetrievalStatus.AUTH_REQUIRED,
                f"http {status_code}",
                http_status=status_code,
            )
        if status_code == 404:
            raise PdfValidationError(
                RetrievalStatus.NO_OA_COPY,
                "http 404",
                http_status=status_code,
            )
        if status_code == 429 or 500 <= status_code < 600:
            raise PdfValidationError(
                RetrievalStatus.TRANSIENT_ERROR,
                f"http {status_code}",
                http_status=status_code,
            )
        raise PdfValidationError(
            RetrievalStatus.TRANSIENT_ERROR,
            f"http {status_code}",
            http_status=status_code,
        )

    if is_login_host(final_url):
        response.close()
        raise PdfValidationError(
            RetrievalStatus.AUTH_REQUIRED,
            f"redirected to login host {host_of(final_url)}",
            http_status=status_code,
        )

    # Inspect the first chunk: magic bytes beat Content-Type lies.
    first_chunk = b""
    total_bytes = 0
    body_parts: List[bytes] = []
    max_bytes = ctx.cfg.max_pdf_bytes
    try:
        iterator = response.iter_content(chunk_size=16_384)
        for chunk in iterator:
            if not chunk:
                continue
            if not first_chunk:
                first_chunk = chunk[:16]
            body_parts.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise PdfValidationError(
                    RetrievalStatus.UNSUPPORTED_CONTENT_TYPE,
                    f"response exceeds max_pdf_bytes={max_bytes}",
                    http_status=status_code,
                )
    finally:
        response.close()

    if total_bytes == 0:
        raise PdfValidationError(
            RetrievalStatus.TRANSIENT_ERROR,
            "empty response body",
            http_status=status_code,
        )

    is_pdf_magic = first_chunk.startswith(b"%PDF-")
    if not is_pdf_magic:
        # If the server claims PDF but the body is not, trust the body.
        looks_html = content_type.startswith("text/html") or first_chunk.lstrip().lower().startswith(
            (b"<!doctype", b"<html", b"<head")
        )
        if looks_html:
            # Sniff a few kilobytes for signin markers to distinguish
            # "no_pdf_but_html" from "captcha".
            head_lower = b"".join(body_parts)[:8192].lower()
            if b"cloudflare" in head_lower or b"cf-challenge" in head_lower or b"captcha" in head_lower:
                raise PdfValidationError(
                    RetrievalStatus.CAPTCHA_OR_BOT_BLOCK,
                    f"cloudflare or captcha page from {host_of(final_url)}",
                    http_status=status_code,
                )
            if any(marker in head_lower for marker in _HTML_SIGNIN_MARKERS):
                raise PdfValidationError(
                    RetrievalStatus.NO_PDF_BUT_HTML,
                    f"landing page html from {host_of(final_url)}",
                    http_status=status_code,
                )
            raise PdfValidationError(
                RetrievalStatus.NO_PDF_BUT_HTML,
                f"html body from {host_of(final_url)}",
                http_status=status_code,
            )
        raise PdfValidationError(
            RetrievalStatus.UNSUPPORTED_CONTENT_TYPE,
            f"non-PDF content-type {content_type!r} from {host_of(final_url)}",
            http_status=status_code,
        )

    if total_bytes < ctx.cfg.min_pdf_bytes:
        raise PdfValidationError(
            RetrievalStatus.NO_PDF_BUT_HTML,
            f"pdf too small ({total_bytes} < {ctx.cfg.min_pdf_bytes})",
            http_status=status_code,
        )

    return ValidatedPdf(
        data=b"".join(body_parts),
        source_url=final_url,
        http_status=status_code,
        content_type=content_type or "application/pdf",
    )


def save_pdf_atomic(paper, data: bytes) -> str:
    """Write ``data`` to a normalized downloads/*.pdf path atomically.

    Returns the absolute path of the saved file.
    """

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    target = pdf_target_path_for_paper(paper, DOWNLOADS_DIR)
    return write_pdf_atomic(target, data)


# ---------------------------------------------------------------------------
# DOI helpers — duplicated narrowly rather than importing from fetch_service
# to keep this package free of side-effect-heavy imports.
# ---------------------------------------------------------------------------


def normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    if doi.lower().startswith("doi:"):
        doi = doi[4:]
    return doi.strip()
