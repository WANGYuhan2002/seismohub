"""Full-Bundle PDF harvester for SeismoHub (2026-11-08).

An ADDITIVE module: the app does not import it at startup. The CLI is ``scripts/harvest_pdfs.py``;
usage notes live in ``docs/PDF_HARVESTING.md`` and the agent skill
``.claude/skills/seismohub-pdf-harvest/SKILL.md``.

For one paper (DOI and/or arXiv id) the harvester:

1. Builds an ordered candidate list from OpenAlex ``locations`` plus channel rules that were verified live
   from this Mac on 2026-11-08:

   - open version of record: CC-licensed published copies (Copernicus/EGU, Seismica, Nature OA, ...),
     ETH Research Collection (DSpace REST API, works for publisher DOIs of ETH-authored papers)
   - licensed version of record: Wiley TDM API (10.1002 / 10.1111 / 10.1029), Elsevier TDM (10.1016; the
     API serves open-access PDFs anywhere, but for subscribed articles only a 1-page PDF preview - verified
     even from the institutional VPN on 2026-11-08 - while their full-text XML IS served from an institutional network;
     that XML, plus high-resolution figures and supplements, is kept TDM-only in the bundle), Nature for
     non-OA articles
   - other open copies: OpenAlex repository PDFs, arXiv (export mirror), EarthArXiv, Zenodo PDF records

2. Downloads politely: a per-host minimum interval, a hard size cap, an honest academic User-Agent (no
   browser spoofing), and hosts that answer scripts with a bot challenge are skipped instead of retried.

3. Accepts a PDF only after four gates: ``%PDF-`` magic, size, >=3 pages with >=5000 characters of text
   (PyMuPDF is required, nothing is accepted unverified), and an identity check (the title, or the DOI
   plus a looser title match, must appear on the first two pages). That keeps previews, login walls and
   wrong papers out.

4. Assigns the licence tier from the copy actually downloaded, NOT from the work's ``oa_status`` (the
   original script marked a TDM-fetched publisher PDF of a "green" paper as open):

   - ``OPEN_CC_BY_OR_PREPRINT``: CC / public-domain licence, or an openly hosted repository/preprint copy
   - ``LICENSED_TDM_ONLY``: publisher version obtained through institutional licences (Wiley / Elsevier TDM, ...)

   TDM-only copies must not go to hosted AI. The app's analyze pipeline uploads PDF bytes to Gemini
   (``services/analyze_service.py`` ``_run_analysis``), so by default a TDM-only copy is kept in the
   bundle and NOT attached to the paper row; the harvester keeps looking for an open copy to attach.

5. Optional bundle extras: EGU full-text XML, supplement and open-review files, Elsevier XML + ``mmc``
   supplements, Nature ESM supplements, and code/data links (DataCite + Data-Availability scan).

The network/verification core imports only ``requests`` + PyMuPDF, so it is unit-testable offline and can
be copied to an HPC cluster or server. The DB helpers at the bottom import the ORM lazily.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import html as _html
import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

try:  # PyMuPDF is a project dependency; without it verification refuses every PDF.
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

HARVEST_VERSION = "2026-11-08.2"  # .2: Elsevier subscribed full-text XML bundles; breaker spares OA papers
STAGE = "fullbundle"  # retrieval_attempts.stage / papers.retrieval_last_stage are String(16)

# Set SEISMO_HARVEST_UA (e.g. "SeismoHub/2.0 (mailto:you@example.org)") to join the OpenAlex / Crossref
# polite pools with your own contact address.
DEFAULT_USER_AGENT = "SeismoHub/2.0 (academic literature harvester)"

TIER_OPEN = "OPEN_CC_BY_OR_PREPRINT"
TIER_TDM = "LICENSED_TDM_ONLY"

# Outcome strings mirror services.pdf_acquisition.common.RetrievalStatus (the DB contract), plus
# HELD_TDM_ONLY: a verified licensed copy kept in the bundle but deliberately not attached to the app.
SUCCESS = "success"
NO_OA_COPY = "no_oa_copy"
AUTH_REQUIRED = "auth_required"
CAPTCHA_OR_BOT_BLOCK = "captcha_or_bot_block"
NO_PDF_BUT_HTML = "no_pdf_but_html"
UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
TRANSIENT_ERROR = "transient_error"
UNSUPPORTED = "unsupported"
HELD_TDM_ONLY = "held_tdm_only"

# Highest first (same order as RetrievalStatus.SEVERITY) - used to summarise a failed paper.
FAILURE_SEVERITY = (
    CAPTCHA_OR_BOT_BLOCK, AUTH_REQUIRED, NO_PDF_BUT_HTML, UNSUPPORTED_CONTENT_TYPE,
    NO_OA_COPY, TRANSIENT_ERROR, UNSUPPORTED,
)

WILEY_PREFIXES = ("10.1002/", "10.1111/", "10.1029/")
ELSEVIER_PREFIXES = ("10.1016/",)
NATURE_PREFIX = "10.1038/"
COPERNICUS_PREFIX = "10.5194/"
EARTHARXIV_PREFIX = "10.31223/"
ETHRC_PREFIX = "10.3929/"
ZENODO_PREFIX = "10.5281/zenodo."
ARXIV_DOI_PREFIX = "10.48550/arxiv."

ALL_CHANNELS = ("openalex", "copernicus", "nature", "ethrc", "wiley", "elsevier", "arxiv", "eartharxiv", "zenodo")

# Hosts that answered a plain scripted GET with a Cloudflare challenge (HTTP 403 + cf-ray) when
# probed from this Mac on 2026-11-08. Skipped (and recorded) rather than hammered; papers that only
# live there go to the pending list for a browser/Zotero download + the app's "upload missing PDFs".
BOT_WALLED_HOSTS = frozenset({
    "academic.oup.com",                 # GJI
    "pubs.geoscienceworld.org",         # BSSA / SRL / TSR
    "www.mdpi.com",
    "essopenarchive.org",
    "agupubs.onlinelibrary.wiley.com",  # AGU web (use the Wiley TDM API instead)
    "onlinelibrary.wiley.com",
    "www.sciencedirect.com",            # (use the Elsevier API instead)
    "www.science.org",
    "www.pnas.org",
})

HOST_MIN_INTERVAL = {
    "api.openalex.org": 0.15,
    "api.crossref.org": 0.25,
    "api.datacite.org": 0.3,
    "api.wiley.com": 1.0,
    "api.elsevier.com": 0.5,
    "export.arxiv.org": 3.0,  # arXiv asks automated clients for <= 1 request / 3 s
    "arxiv.org": 3.0,
    "zenodo.org": 0.6,
    "api.github.com": 1.0,
    "www.research-collection.ethz.ch": 0.5,
    "www.nature.com": 1.0,
}
DEFAULT_MIN_INTERVAL = 1.0

KEY_NAMES = ("WILEY_TDM_TOKEN", "ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN")
DEFAULT_KEYS_FILE = Path.home() / ".config" / "seismo_harvester" / "keys.env"

_FITZ_LOCK = threading.Lock()  # PyMuPDF is not thread-safe


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def load_harvest_keys(extra_files: Sequence[os.PathLike] = ()) -> Dict[str, bool]:
    """Load TDM keys from env-style files (``export KEY="value"``) without overriding the environment.

    Search order: ``$SEISMO_HARVEST_KEYS_FILE``, ``extra_files``, ``~/.config/seismo_harvester/keys.env``.
    Keys live OUTSIDE the project folder on purpose: the folder (with its ``.env``) is shared with the group.
    Returns which keys are available - never the values.
    """
    paths: List[Path] = []
    env_file = (os.environ.get("SEISMO_HARVEST_KEYS_FILE") or "").strip()
    if env_file:
        paths.append(Path(env_file).expanduser())
    paths.extend(Path(p).expanduser() for p in extra_files)
    paths.append(DEFAULT_KEYS_FILE)
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key in KEY_NAMES and value:
                os.environ.setdefault(key, value)
    return {k: bool(os.environ.get(k)) for k in ("WILEY_TDM_TOKEN", "ELSEVIER_API_KEY")}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@dataclass
class HttpResult:
    url: str
    status: int = 0
    final_url: str = ""
    content_type: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    data: bytes = b""
    error: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    def text(self) -> str:
        return self.data.decode("utf-8", "ignore")

    def json(self) -> Any:
        return json.loads(self.data.decode("utf-8", "ignore") or "null")


def _registered_domain(host: str) -> str:
    parts = (host or "").lower().split(":")[0].split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


class PoliteSession:
    """requests.Session with a per-host minimum interval, a body size cap and manual redirects.

    Redirects are followed by hand so that ``sensitive_headers`` (API keys) are dropped as soon as a
    redirect leaves the original registered domain - requests only does that for ``Authorization``.
    """

    def __init__(
        self,
        user_agent: str = "",
        *,
        max_bytes: int = 0,
        timeout: Tuple[float, float] = (15.0, 120.0),
        host_intervals: Optional[Dict[str, float]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            user_agent or os.environ.get("SEISMO_HARVEST_UA") or DEFAULT_USER_AGENT
        )
        self.max_bytes = max_bytes or int(os.environ.get("SEISMO_HARVEST_MAX_BYTES") or 80_000_000)
        self.timeout = timeout
        self.host_intervals = dict(HOST_MIN_INTERVAL)
        if host_intervals:
            self.host_intervals.update(host_intervals)
        self._last: Dict[str, float] = {}
        self._sleep = sleep

    def _throttle(self, host: str) -> None:
        gap = self.host_intervals.get(host, DEFAULT_MIN_INTERVAL)
        last = self._last.get(host)
        if last is not None:
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)

    def get(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        sensitive_headers: Iterable[str] = (),
        max_bytes: int = 0,
        retries: int = 1,
    ) -> HttpResult:
        limit = max_bytes or self.max_bytes
        origin_host = urllib.parse.urlsplit(url).netloc.lower()
        origin_domain = _registered_domain(origin_host)
        sensitive = {h.lower() for h in sensitive_headers}
        attempt = 0
        while True:
            self._throttle(origin_host)
            started = time.monotonic()
            res = HttpResult(url=url)
            hdrs = dict(headers or {})
            current = url
            retry_after = ""
            try:
                for _hop in range(10):
                    resp = self.session.get(current, headers=hdrs, timeout=self.timeout,
                                            stream=True, allow_redirects=False)
                    try:
                        location = resp.headers.get("Location")
                        if resp.status_code in (301, 302, 303, 307, 308) and location:
                            current = urllib.parse.urljoin(current, location)
                            if sensitive and _registered_domain(urllib.parse.urlsplit(current).netloc) != origin_domain:
                                hdrs = {k: v for k, v in hdrs.items() if k.lower() not in sensitive}
                            continue
                        res.status = resp.status_code
                        res.final_url = current
                        res.content_type = resp.headers.get("Content-Type", "")
                        res.headers = {k.lower(): v for k, v in list(resp.headers.items())[:60]}
                        retry_after = resp.headers.get("Retry-After", "")
                        declared = resp.headers.get("Content-Length", "")
                        if declared.isdigit() and int(declared) > limit:
                            res.error = f"too_large:{declared}"
                            break
                        buf = bytearray()
                        for chunk in resp.iter_content(chunk_size=65536):
                            buf.extend(chunk)
                            if len(buf) > limit:
                                res.error = f"too_large:>{limit}"
                                break
                        if not res.error:
                            res.data = bytes(buf)
                        break
                    finally:
                        resp.close()
                else:
                    res.error = "too_many_redirects"
            except requests.RequestException as exc:
                res.error = f"{type(exc).__name__}: {exc}"[:240]
            finally:
                self._last[origin_host] = time.monotonic()
                res.elapsed_ms = int((time.monotonic() - started) * 1000)
            if res.status in (429, 502, 503, 504) and attempt < retries:
                attempt += 1
                delay = float(retry_after) if retry_after.isdigit() else 5.0 * attempt
                self._sleep(min(30.0, delay))
                continue
            return res


def classify_http_failure(res: HttpResult) -> Tuple[str, str]:
    """Map a non-2xx / failed response to (RetrievalStatus outcome, short reason)."""
    if res.error.startswith("too_large"):
        return UNSUPPORTED_CONTENT_TYPE, res.error
    if res.error:
        return TRANSIENT_ERROR, res.error
    status = res.status
    if status == 403:
        body = res.data[:4096].lower()
        cloudflare = (
            "cf-ray" in res.headers or "cloudflare" in res.headers.get("server", "").lower()
            or b"just a moment" in body or b"cf-chl" in body
        )
        return (CAPTCHA_OR_BOT_BLOCK, "http_403_bot_challenge") if cloudflare else (AUTH_REQUIRED, "http_403")
    if status in (401, 402):
        return AUTH_REQUIRED, f"http_{status}"
    if status in (404, 410):
        return NO_OA_COPY, f"http_{status}"
    if status == 429 or status >= 500:
        return TRANSIENT_ERROR, f"http_{status}"
    return UNSUPPORTED_CONTENT_TYPE, f"http_{status}"


# ---------------------------------------------------------------------------
# PDF verification gates
# ---------------------------------------------------------------------------

@dataclass
class PdfCheck:
    ok: bool
    reason: str
    outcome: str
    pages: int = 0
    chars: int = 0
    head_text: str = ""


def inspect_pdf(data: bytes, *, min_bytes: int = 40_000, min_pages: int = 3, min_chars: int = 5000) -> PdfCheck:
    """Gates 1-3: magic header, size floor, page count + text layer (PyMuPDF required)."""
    if not data or b"%PDF-" not in data[:1024]:
        low = (data or b"")[:4096].lower()
        if b"<html" in low or b"<!doctype" in low:
            return PdfCheck(False, "html_instead_of_pdf", NO_PDF_BUT_HTML)
        return PdfCheck(False, "missing_pdf_magic", UNSUPPORTED_CONTENT_TYPE)
    if len(data) < min_bytes:
        return PdfCheck(False, f"too_small_{len(data) // 1024}KB", UNSUPPORTED_CONTENT_TYPE)
    if fitz is None:
        return PdfCheck(False, "pymupdf_missing_cannot_verify", UNSUPPORTED)
    try:
        with _FITZ_LOCK:
            with fitz.open(stream=data, filetype="pdf") as doc:
                if doc.needs_pass:
                    return PdfCheck(False, "encrypted_pdf", UNSUPPORTED_CONTENT_TYPE)
                pages = doc.page_count
                if pages < min_pages:
                    # A 1-2 page PDF from a full-text endpoint is a preview / errata / login stub.
                    return PdfCheck(False, f"too_few_pages_{pages}", AUTH_REQUIRED, pages=pages)
                head: List[str] = []
                chars = 0
                for index in range(min(pages, 60)):
                    text = doc[index].get_text("text") or ""
                    if index < 2:
                        head.append(text)
                    chars += len(text.strip())
                    if chars >= min_chars and index >= 1:
                        break
    except Exception as exc:
        return PdfCheck(False, f"corrupt_pdf:{type(exc).__name__}", UNSUPPORTED_CONTENT_TYPE)
    if chars < min_chars:
        return PdfCheck(False, f"too_little_text_{chars}", UNSUPPORTED_CONTENT_TYPE, pages=pages, chars=chars)
    return PdfCheck(True, "verified_full_pdf", SUCCESS, pages=pages, chars=chars, head_text="\n".join(head))


_STOPWORDS = frozenset(
    "a an and are as at be by for from in into is it its of on or that the their this to via with "
    "using based new study".split()
)


def _tokens(text: str) -> List[str]:
    text = unicodedata.normalize("NFKC", _html.unescape(text or "")).lower()
    text = re.sub(r"<[^>]+>", " ", text)  # Crossref titles can carry <i>/<sub> markup
    text = text.replace("­", "")
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"-\s*\n\s*", "", text)  # re-join words hyphenated across a line break
    return re.findall(r"[^\W_]+", text)


def title_overlap(title: str, text: str) -> float:
    """Share of the title's content words that occur anywhere in ``text`` (0..1)."""
    words = [t for t in _tokens(title) if len(t) >= 3 and t not in _STOPWORDS]
    if not words:
        words = _tokens(title)
    if not words:
        return 0.0
    vocab = set(_tokens(text))
    return sum(1 for w in words if w in vocab) / len(words)


def doi_in_text(doi: str, text: str) -> bool:
    if not doi:
        return False
    squashed = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()
    return doi.lower() in squashed


def identity_check(head_text: str, *, doi: str, title: str) -> Tuple[bool, str]:
    """Gate 4: does this PDF belong to the paper we asked for?

    Title agreement is primary. A DOI on the page is supporting evidence only (papers cite other DOIs on
    page 1-2 - the lesson recorded in services/pdf_match_core.py), so it needs a looser title match too.
    """
    overlap = title_overlap(title, head_text) if title else 0.0
    has_doi = doi_in_text(doi, head_text)
    if title and overlap >= 0.7:
        return True, f"title({overlap:.2f})" + ("+doi" if has_doi else "")
    if has_doi and (not title or overlap >= 0.35):
        return True, f"doi+title({overlap:.2f})"
    if not title and not doi:
        return True, "unchecked_no_reference"
    return False, f"identity_mismatch(title={overlap:.2f},doi={'yes' if has_doi else 'no'})"


# ---------------------------------------------------------------------------
# Identifiers, licences, URL rules
# ---------------------------------------------------------------------------

def normalize_doi(value: str) -> str:
    doi = (value or "").strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
    return doi.strip().rstrip(".")


def safe_bundle_name(doi: str = "", arxiv_id: str = "", paper_id: str = "") -> str:
    if doi:
        return "PaperBundle_" + re.sub(r"[^A-Za-z0-9_.-]", "_", doi)
    if arxiv_id:
        return "PaperBundle_arXiv_" + re.sub(r"[^A-Za-z0-9_.-]", "_", arxiv_id)
    return "PaperBundle_" + re.sub(r"[^A-Za-z0-9_.-]", "_", paper_id or "unknown")


def is_open_license(lic: str) -> bool:
    lic = (lic or "").strip().lower()
    return lic.startswith("cc") or lic in ("public-domain", "pd")


def normalize_license_label(raw: str) -> str:
    """'Creative Commons Attribution 4.0 International' -> 'cc-by'; other strings pass through."""
    text = (raw or "").strip()
    low = text.lower()
    if "creativecommons.org/publicdomain" in low or "cc0" in low or "public domain" in low:
        return "cc0"
    if "creative commons" in low or "creativecommons.org/licenses" in low:
        parts = ["cc-by"]
        if "noncommercial" in low or "non-commercial" in low or "-nc" in low:
            parts.append("nc")
        if "noderivatives" in low or "no derivatives" in low or "-nd" in low:
            parts.append("nd")
        if "sharealike" in low or "share alike" in low or "-sa" in low:
            parts.append("sa")
        return "-".join(parts)
    return text


def vor_open_license(work: Optional[dict]) -> str:
    """CC licence of the published version according to OpenAlex, else ''."""
    for loc in (work or {}).get("locations") or []:
        lic = (loc.get("license") or "").lower()
        if loc.get("version") == "publishedVersion" and is_open_license(lic):
            return lic
    return ""


def cc_label_from_url(url: str) -> str:
    """'https://creativecommons.org/licenses/by-nc-nd/4.0/' -> 'cc-by-nc-nd'; non-CC URLs -> ''."""
    low = (url or "").lower()
    if "creativecommons.org" not in low:
        return ""
    if "/publicdomain/" in low:
        return "cc0"
    m = re.search(r"/licenses/([a-z\-]+)/", low)
    return f"cc-{m.group(1)}" if m else "cc"


def crossref_vor_cc_license(message: dict, today: Optional[_dt.date] = None) -> str:
    """CC licence the publisher registered for the version of record, if it is already in force.

    Crossref ``license[]`` entries are deposited by the publisher; ``content-version`` 'vor' (or
    'unspecified') covers the published PDF, and ``start`` handles embargoes (a CC licence starting in
    the future does not count yet). Used when OpenAlex has no licence - e.g. papers a few days old,
    whose OpenAlex record can still be missing (404) or change between calls (seen 2026-11-08).
    """
    today = today or _dt.date.today()
    for entry in (message or {}).get("license") or []:
        label = cc_label_from_url(entry.get("URL") or "")
        if not label or (entry.get("content-version") or "unspecified").lower() not in ("vor", "unspecified"):
            continue
        parts = ((entry.get("start") or {}).get("date-parts") or [[None]])[0] or [None]
        if parts and parts[0]:
            try:
                start = _dt.date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1,
                                 int(parts[2]) if len(parts) > 2 else 1)
            except (TypeError, ValueError):
                start = None
            if start and start > today:
                continue
        return label
    return ""


_COPERNICUS_FINAL = re.compile(r"^10\.5194/([a-z]+)-(\d+)-(\d+)-(\d{4})$", re.I)
_COPERNICUS_PREPRINT = re.compile(r"^10\.5194/([a-z]+)-(\d{4})-(\d+)$", re.I)


def copernicus_pdf_url(doi: str) -> Tuple[str, str]:
    """Deterministic EGU/Copernicus PDF URL -> (url, version). GA abstracts (egusphere-egu24-…) -> ('', '')."""
    m = _COPERNICUS_FINAL.match(doi or "")
    if m:
        journal, vol, page, year = m.groups()
        journal = journal.lower()
        return (f"https://{journal}.copernicus.org/articles/{vol}/{page}/{year}/"
                f"{journal}-{vol}-{page}-{year}.pdf", "publishedVersion")
    m = _COPERNICUS_PREPRINT.match(doi or "")
    if m:
        journal, year, num = m.groups()
        journal = journal.lower()
        if journal == "egusphere":
            return (f"https://egusphere.copernicus.org/preprints/{year}/egusphere-{year}-{num}/"
                    f"egusphere-{year}-{num}.pdf", "submittedVersion")
        return (f"https://{journal}.copernicus.org/preprints/{journal}-{year}-{num}/"
                f"{journal}-{year}-{num}.pdf", "submittedVersion")
    return "", ""


def nature_pdf_url(doi: str) -> str:
    return f"https://www.nature.com/articles/{doi.split('/', 1)[1]}.pdf" if "/" in doi else ""


_ARXIV_URL_NEW = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_ARXIV_URL_OLD = re.compile(r"arxiv\.org/(?:abs|pdf)/([a-z\-]+(?:\.[a-z]{2})?/\d{7})", re.I)


def arxiv_id_from(doi: str, link: str = "", locations: Sequence[dict] = ()) -> str:
    """arXiv id without version (export mirror serves the latest version)."""
    if (doi or "").lower().startswith(ARXIV_DOI_PREFIX):
        return re.sub(r"v\d+$", "", doi[len(ARXIV_DOI_PREFIX):])
    sources = [link or ""]
    for loc in locations or ():
        sources.append(loc.get("landing_page_url") or "")
        sources.append(loc.get("pdf_url") or "")
    for text in sources:
        m = _ARXIV_URL_NEW.search(text) or _ARXIV_URL_OLD.search(text)
        if m:
            return m.group(1)
    return ""


# EGU open review files: RC = referee comment, AC = author comment, CC/SC = community/short comment,
# EC/ED = editor, AR = author response, ATC = author track changes; "-print" = printer-friendly
# rendering of an inline comment. Verified upper-case on se/tc discussion pages 2026-11-08 - the
# original lower-case regex matched 0 of them.
EGU_REVIEW_PDF_RE = re.compile(
    r"https?://[^\s\"'<>]+?-(?:RC|AC|CC|SC|EC|ED|AR|ATC)\d+(?:-print|-supplement)?\.pdf", re.I)
EGU_MANUSCRIPT_VERSION_RE = re.compile(r"https?://[^\s\"'<>]+?-manuscript-version\d+\.pdf", re.I)

# Elsevier full-text XML lists attachments as <xocs:attachment-eid> (namespaced) - the original regex
# '<attachment-eid>' never matched.
ELSEVIER_ATTACHMENT_EID_RE = re.compile(r"<(?:[\w-]+:)?attachment-eid>\s*([^<\s]+)\s*</(?:[\w-]+:)?attachment-eid>")
# FULL-view XML lists figures as e.g. <object ref="gr1" category="high" type="IMAGE-HIGH-RES" mimetype="image/jpeg"
# ...>https://api.elsevier.com/content/object/eid/1-s2.0-S...-gr1_lrg.jpg?httpAccept=%2A%2F%2A</object>
ELSEVIER_HIGHRES_OBJECT_RE = re.compile(
    r'<object\b[^>]*\bcategory="high"[^>]*>\s*(https://api\.elsevier\.com/content/object/eid/[^<\s]+)\s*</object>',
    re.I)
ELSEVIER_XML_DOI_RE = re.compile(r"<prism:doi>\s*([^<\s]+)\s*</prism:doi>", re.I)
ELSEVIER_MAX_FIGURES = 40

NATURE_ESM_RE = re.compile(r"https://static-content\.springer\.com/esm/art%3A[^\"'\s<>]+")

# Supplements: skip videos and cap each file (pilot 2026-11-08: main PDFs ~10 MB/paper, extras ~+40%).
SUPPLEMENT_MAX_BYTES = 50_000_000
SKIP_SUPPLEMENT_EXT = (".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg")


def _wanted_supplement(name: str) -> bool:
    return not urllib.parse.unquote(name).lower().endswith(SKIP_SUPPLEMENT_EXT)

CODE_DATA_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"|gitlab\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"|bitbucket\.org/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"|zenodo\.org/(?:records?|doi)/[0-9.]+"
    r"|doi\.org/10\.5281/zenodo\.[0-9]+"
    r"|doi\.org/10\.7914/[^\s\"<>)\]]+"
    r"|figshare\.com/[^\s\"<>)\]]+"
    r"|osf\.io/[A-Za-z0-9]+"
    r"|ds\.iris\.edu/[^\s\"<>)\]]+"
    r"|(?:www\.)?earthscope\.org/[^\s\"<>)\]]+"
    r")",
    re.I,
)
BARE_ZENODO_DOI_RE = re.compile(r"\b10\.5281/zenodo\.\d+", re.I)
DATA_SECTION_RE = re.compile(
    r"(?:data\s+and\s+code\s+availability|data\s+availability(?:\s+statement)?|code\s+availability|"
    r"open\s+research|data\s+and\s+resources|availability\s+of\s+data(?:\s+and\s+materials)?)",
    re.I,
)


def scan_pdf_for_code_data_urls(data: bytes) -> List[Dict[str, str]]:
    """Code/data links from the Data-Availability section, the last 4 pages and link annotations."""
    if fitz is None or not data:
        return []
    found: Dict[str, str] = {}

    def add(url: str, where: str) -> None:
        url = url.rstrip(".,;:)]}'\"")
        if url and url not in found:
            found[url] = where

    try:
        with _FITZ_LOCK:
            with fitz.open(stream=data, filetype="pdf") as doc:
                texts = [doc[i].get_text("text") or "" for i in range(doc.page_count)]
                links = [lnk.get("uri") for page in doc for lnk in page.get_links() if lnk.get("uri")]
    except Exception:
        return []
    full = "\n".join(texts)
    for match in DATA_SECTION_RE.finditer(full):
        window = re.sub(r"-\s*\n\s*", "", full[match.start(): match.start() + 4000]).replace("\n", " ")
        for url in CODE_DATA_URL_RE.findall(window):
            add(url, "data_availability_section")
        for bare in BARE_ZENODO_DOI_RE.findall(window):
            add("https://doi.org/" + bare, "data_availability_section")
    tail = re.sub(r"-\s*\n\s*", "", "\n".join(texts[-4:])).replace("\n", " ")
    for url in CODE_DATA_URL_RE.findall(tail):
        add(url, "tail_pages")
    for uri in links:
        if CODE_DATA_URL_RE.match(uri or ""):
            add(uri, "link_annotation")
    return [{"url": u, "found_in": w} for u, w in sorted(found.items())]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PaperRef:
    paper_id: str
    doi: str = ""
    title: str = ""
    journal: str = ""
    link: str = ""
    source: str = ""
    arxiv_id: str = ""


@dataclass
class Attempt:
    channel: str
    url: str
    outcome: str
    reason: str = ""
    http_status: Optional[int] = None
    bytes: int = 0
    started_at: str = ""
    duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Candidate:
    channel: str
    url: str = ""
    resolver: Optional[Callable[[], Tuple[str, str, str]]] = None  # -> (url, licence, failure reason)
    headers: Dict[str, str] = field(default_factory=dict)
    sensitive_headers: Tuple[str, ...] = ()
    kind: str = "open_copy"  # open_vor | tdm_vor | publisher_copy | open_copy
    version: str = "unknown"
    license: str = ""
    note: str = ""


@dataclass
class PdfHit:
    channel: str
    url: str
    data: bytes
    pages: int
    chars: int
    kind: str
    version: str
    license: str
    tier: str
    identity: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def md5(self) -> str:
        return hashlib.md5(self.data).hexdigest()

    def describe(self) -> Dict[str, Any]:
        return {
            "channel": self.channel, "source_url": self.url, "kind": self.kind, "version": self.version,
            "license": self.license, "license_tier": self.tier,
            "hosted_ai_upload_allowed": self.tier == TIER_OPEN,
            "pages": self.pages, "text_chars_checked": self.chars, "identity": self.identity,
            "bytes": len(self.data), "sha256": self.sha256, "md5": self.md5,
        }


@dataclass
class XmlHit:
    """Full-text XML of a version of record. Elsevier's API serves subscribed full text as XML only (the PDF
    endpoint gives a 1-page preview), so this is kept in the bundle, TDM-only, and never attached."""
    channel: str
    url: str
    data: bytes
    chars: int
    license: str
    tier: str
    identity: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def md5(self) -> str:
        return hashlib.md5(self.data).hexdigest()

    def describe(self) -> Dict[str, Any]:
        return {
            "channel": self.channel, "source_url": self.url, "kind": "tdm_vor", "version": "publishedVersion",
            "format": "xml", "license": self.license, "license_tier": self.tier,
            "hosted_ai_upload_allowed": self.tier == TIER_OPEN, "text_chars": self.chars,
            "identity": self.identity, "bytes": len(self.data), "sha256": self.sha256, "md5": self.md5,
        }


@dataclass
class HarvestOutcome:
    ref: PaperRef
    doi: str = ""
    title: str = ""
    arxiv_id: str = ""
    app_hit: Optional[PdfHit] = None   # copy to attach to the paper row (OPEN, or TDM with --attach-tdm)
    held_hit: Optional[PdfHit] = None  # TDM-only version of record kept in the bundle only
    held_xml: Optional[XmlHit] = None  # TDM-only full-text XML when no full PDF is served (bundle only)
    attempts: List[Attempt] = field(default_factory=list)
    openalex: Dict[str, Any] = field(default_factory=dict)
    status: str = ""
    reason: str = ""

    @property
    def pdf_attempts(self) -> List[Attempt]:
        """Attempts at an actual PDF (metadata lookups such as openalex_meta / crossref_meta excluded)."""
        return [a for a in self.attempts if not a.channel.endswith("_meta")]


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tier_for(kind: str, license_: str, vor_license: str) -> Tuple[str, str]:
    """Licence tier of the copy we actually downloaded."""
    lic = (license_ or "").strip().lower()
    if kind == "open_copy":
        return TIER_OPEN, lic or "repository-or-preprint"
    if is_open_license(lic):
        return TIER_OPEN, lic
    if vor_license:  # licensed channel, but the version of record itself is CC (gold / hybrid OA)
        return TIER_OPEN, vor_license
    return TIER_TDM, lic or ("tdm-licence" if kind == "tdm_vor" else "publisher-licensed")


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------

class FullBundleHarvester:
    def __init__(
        self,
        http: Optional[PoliteSession] = None,
        *,
        channels: Optional[Iterable[str]] = None,
        attach_tdm: bool = False,
        download_code: bool = False,
        min_pages: int = 3,
        min_chars: int = 5000,
        min_bytes: int = 40_000,
        elsevier_preview_breaker: int = 3,
    ) -> None:
        self.http = http or PoliteSession()
        self.channels = {c.strip().lower() for c in channels} if channels else set(ALL_CHANNELS)
        unknown = self.channels - set(ALL_CHANNELS)
        if unknown:
            raise ValueError(f"unknown channel(s): {sorted(unknown)}; valid: {ALL_CHANNELS}")
        self.attach_tdm = attach_tdm
        self.download_code = download_code
        self.min_pages, self.min_chars, self.min_bytes = min_pages, min_chars, min_bytes
        self.elsevier_preview_breaker = elsevier_preview_breaker
        self._elsevier_previews = 0
        # Set after N consecutive PDF previews of SUBSCRIBED papers: their PDF request is skipped from then on
        # (open-access Elsevier PDFs and the full-text XML fallback are still tried).
        self.elsevier_disabled_reason = ""
        self._elsevier_xml_failures = 0
        self.elsevier_xml_disabled_reason = ""  # set after N consecutive XML requests without full text
        self._crossref_cache: Dict[str, str] = {}

    def _crossref_vor_license(self, doi: str, out: "HarvestOutcome") -> str:
        """Second opinion on the VoR licence from the publisher's Crossref deposit (cached per DOI)."""
        if doi in self._crossref_cache:
            return self._crossref_cache[doi]
        url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}"
        res = self.http.get(url, headers={"Accept": "application/json"})
        att = Attempt("crossref_meta", url, SUCCESS, http_status=res.status or None, bytes=len(res.data),
                      started_at=_now_iso(), duration_ms=res.elapsed_ms)
        licence = ""
        if res.ok:
            try:
                licence = crossref_vor_cc_license((res.json() or {}).get("message") or {})
            except ValueError:
                att.outcome, att.reason = UNSUPPORTED_CONTENT_TYPE, "crossref_bad_json"
        else:
            att.outcome, att.reason = classify_http_failure(res)
        att.reason = att.reason or (f"vor licence {licence}" if licence else "no CC licence registered for the VoR")
        out.attempts.append(att)
        self._crossref_cache[doi] = licence
        return licence

    # -- metadata ------------------------------------------------------------------------------

    def _openalex_work(self, doi: str) -> Tuple[Optional[dict], Attempt]:
        url = f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='/')}"
        res = self.http.get(url, headers={"Accept": "application/json"})
        att = Attempt("openalex_meta", url, SUCCESS, http_status=res.status or None,
                      bytes=len(res.data), started_at=_now_iso(), duration_ms=res.elapsed_ms)
        if not res.ok:
            att.outcome, att.reason = classify_http_failure(res)
            return None, att
        try:
            return res.json(), att
        except ValueError:
            att.outcome, att.reason = UNSUPPORTED_CONTENT_TYPE, "openalex_bad_json"
            return None, att

    # -- candidates ----------------------------------------------------------------------------

    def build_candidates(self, ref: PaperRef, doi: str, title: str, work: Optional[dict],
                         vor_license: str) -> List[Candidate]:
        out: List[Candidate] = []
        seen: set = set()
        on = self.channels.__contains__
        locs = [loc for loc in ((work or {}).get("locations") or []) if isinstance(loc, dict)]

        def add(cand: Candidate) -> None:
            key = cand.url or f"{cand.channel}:{cand.note}"
            if key not in seen:
                seen.add(key)
                out.append(cand)

        def src(loc: dict) -> Tuple[str, str]:
            source = loc.get("source") or {}
            return (source.get("type") or ""), (source.get("display_name") or "")

        # Group 0 - open version of record (best copy: final text AND free to analyse).
        if on("openalex"):
            for loc in locs:
                if (loc.get("pdf_url") and loc.get("version") == "publishedVersion"
                        and is_open_license(loc.get("license") or "")):
                    add(Candidate("openalex", url=loc["pdf_url"], kind="open_vor", version="publishedVersion",
                                  license=loc.get("license") or "", note=src(loc)[1]))
        if on("copernicus") and doi.lower().startswith(COPERNICUS_PREFIX):
            url, version = copernicus_pdf_url(doi)
            if url:
                add(Candidate("copernicus", url=url, kind="open_vor" if version == "publishedVersion" else "open_copy",
                              version=version, license="cc-by"))
        if on("nature") and doi.lower().startswith(NATURE_PREFIX) and vor_license:
            add(Candidate("nature", url=nature_pdf_url(doi), kind="open_vor", version="publishedVersion",
                          license=vor_license))
        if on("ethrc") and self._ethrc_relevant(ref, doi, locs):
            add(Candidate("ethrc", resolver=lambda d=doi, t=title: self._resolve_ethrc(d, t),
                          kind="open_copy", note="research-collection.ethz.ch DOI search"))

        # Group 1 - licensed version of record (tier decided by the VoR licence).
        if on("wiley") and doi.lower().startswith(WILEY_PREFIXES) and os.environ.get("WILEY_TDM_TOKEN"):
            add(Candidate(
                "wiley_tdm",
                url=f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{urllib.parse.quote(doi, safe='')}",
                headers={"Wiley-TDM-Client-Token": os.environ["WILEY_TDM_TOKEN"], "Accept": "application/pdf"},
                sensitive_headers=("Wiley-TDM-Client-Token",), kind="tdm_vor", version="publishedVersion"))
        if on("elsevier") and doi.lower().startswith(ELSEVIER_PREFIXES) and os.environ.get("ELSEVIER_API_KEY"):
            headers = {"X-ELS-APIKey": os.environ["ELSEVIER_API_KEY"], "Accept": "application/pdf"}
            if os.environ.get("ELSEVIER_INSTTOKEN"):
                headers["X-ELS-Insttoken"] = os.environ["ELSEVIER_INSTTOKEN"]
            add(Candidate(
                "elsevier_tdm",
                url=(f"https://api.elsevier.com/content/article/doi/{urllib.parse.quote(doi, safe='/')}"
                     "?httpAccept=application/pdf"),
                headers=headers, sensitive_headers=("X-ELS-APIKey", "X-ELS-Insttoken"),
                kind="tdm_vor", version="publishedVersion"))
        if on("nature") and doi.lower().startswith(NATURE_PREFIX) and not vor_license:
            add(Candidate("nature", url=nature_pdf_url(doi), kind="publisher_copy", version="publishedVersion"))

        # Group 2 - other open copies (accepted / submitted versions, preprints, datasets with a PDF).
        if on("openalex"):
            for loc in locs:
                if loc.get("pdf_url") and src(loc)[0] == "repository":
                    add(Candidate("openalex_repo", url=loc["pdf_url"], kind="open_copy",
                                  version=loc.get("version") or "unknown", license=loc.get("license") or "",
                                  note=src(loc)[1]))
        arxiv_id = ref.arxiv_id or arxiv_id_from(doi, ref.link, locs)
        if on("arxiv") and arxiv_id:
            add(Candidate("arxiv", url=f"https://export.arxiv.org/pdf/{arxiv_id}", kind="open_copy",
                          version="submittedVersion"))
        if on("eartharxiv"):
            if doi.lower().startswith(EARTHARXIV_PREFIX):
                add(Candidate("eartharxiv", resolver=lambda d=doi: self._resolve_eartharxiv(f"https://doi.org/{d}"),
                              kind="open_copy", version="submittedVersion", note=doi))
            for loc in locs:
                landing = loc.get("landing_page_url") or ""
                if "eartharxiv.org/repository/view/" in landing and not loc.get("pdf_url"):
                    add(Candidate("eartharxiv", resolver=lambda u=landing: self._resolve_eartharxiv(u),
                                  kind="open_copy", version=loc.get("version") or "submittedVersion", note=landing))
        if on("zenodo") and doi.lower().startswith(ZENODO_PREFIX):
            add(Candidate("zenodo", resolver=lambda d=doi: self._resolve_zenodo(d), kind="open_copy", note=doi))
        if on("openalex"):  # remaining publisher pdf_urls without an open licence (bronze etc.)
            for loc in locs:
                if loc.get("pdf_url"):
                    stype = src(loc)[0]
                    add(Candidate("openalex", url=loc["pdf_url"],
                                  kind="open_copy" if stype == "repository" else "publisher_copy",
                                  version=loc.get("version") or "unknown", license=loc.get("license") or "",
                                  note=src(loc)[1]))
        return out

    @staticmethod
    def _ethrc_relevant(ref: PaperRef, doi: str, locs: Sequence[dict]) -> bool:
        if doi.lower().startswith(ETHRC_PREFIX) or (ref.source or "") == "eth_rc":
            return True
        if "research-collection.ethz.ch" in (ref.link or "") or "20.500.11850" in (ref.link or ""):
            return True
        for loc in locs:
            name = ((loc.get("source") or {}).get("display_name") or "").lower()
            landing = (loc.get("landing_page_url") or "").lower()
            if "research collection" in name or "research-collection.ethz.ch" in landing or "20.500.11850" in landing:
                return True
        return False

    # -- resolvers (return url, licence, failure reason) ----------------------------------------

    def _resolve_ethrc(self, doi: str, title: str) -> Tuple[str, str, str]:
        api = "https://www.research-collection.ethz.ch/server/api"
        query = urllib.parse.quote(f'"{doi}"')
        res = self.http.get(f"{api}/discover/search/objects?query={query}&size=5",
                            headers={"Accept": "application/json"})
        if not res.ok:
            return "", "", f"ethrc_search_{res.status or res.error}"
        try:
            objects = res.json()["_embedded"]["searchResult"]["_embedded"]["objects"]
        except (KeyError, TypeError, ValueError):
            return "", "", "ethrc_search_unparseable"
        for obj in objects or []:
            item = (obj.get("_embedded") or {}).get("indexableObject") or {}
            uuid = item.get("uuid")
            meta = item.get("metadata") or {}
            rc_title = ((meta.get("dc.title") or [{}])[0] or {}).get("value", "")
            if title and rc_title and max(title_overlap(title, rc_title), title_overlap(rc_title, title)) < 0.6:
                continue  # the search hit some other record that merely mentions this DOI
            licence = normalize_license_label(((meta.get("dc.rights.license") or [{}])[0] or {}).get("value", ""))
            if not uuid:
                continue
            bundles = self.http.get(f"{api}/core/items/{uuid}/bundles", headers={"Accept": "application/json"})
            if not bundles.ok:
                continue
            try:
                bundle_list = bundles.json()["_embedded"]["bundles"]
            except (KeyError, TypeError, ValueError):
                continue
            for bundle in bundle_list:
                if bundle.get("name") != "ORIGINAL":
                    continue
                href = ((bundle.get("_links") or {}).get("bitstreams") or {}).get("href")
                if not href:
                    continue
                streams = self.http.get(href, headers={"Accept": "application/json"})
                if not streams.ok:
                    continue
                try:
                    bitstreams = streams.json()["_embedded"]["bitstreams"]
                except (KeyError, TypeError, ValueError):
                    continue
                pdfs = [b for b in bitstreams if str(b.get("name", "")).lower().endswith(".pdf")]
                pdfs.sort(key=lambda b: bool(re.search(r"supp|supplement|_si\b|-si\b", str(b.get("name", "")), re.I)))
                for bitstream in pdfs:
                    content = ((bitstream.get("_links") or {}).get("content") or {}).get("href")
                    if content:
                        return content, licence, ""
            return "", licence, "ethrc_item_has_no_pdf_bitstream"
        return "", "", "ethrc_no_matching_item"

    def _resolve_eartharxiv(self, landing_url: str) -> Tuple[str, str, str]:
        res = self.http.get(landing_url)
        if not res.ok:
            return "", "", f"eartharxiv_landing_{res.status or res.error}"
        links = re.findall(r'href="([^"]*/repository/object/\d+/download/\d+/?)"', res.text())
        if not links:
            return "", "", "eartharxiv_no_download_link"
        return urllib.parse.urljoin(res.final_url or landing_url, links[0]), "", ""

    def _resolve_zenodo(self, doi: str) -> Tuple[str, str, str]:
        record_id = doi.lower().split("zenodo.", 1)[1]
        res = self.http.get(f"https://zenodo.org/api/records/{record_id}", headers={"Accept": "application/json"})
        if not res.ok:
            return "", "", f"zenodo_record_{res.status or res.error}"
        try:
            record = res.json() or {}
        except ValueError:
            return "", "", "zenodo_bad_json"
        meta = record.get("metadata") or {}
        files = [f for f in (record.get("files") or []) if str(f.get("key", "")).lower().endswith(".pdf")]
        if not files:
            rtype = (meta.get("resource_type") or {}).get("type") or "?"
            return "", "", f"zenodo_record_has_no_pdf(resource_type={rtype})"
        best = max(files, key=lambda f: f.get("size") or 0)
        url = (best.get("links") or {}).get("self") or (
            f"https://zenodo.org/records/{record_id}/files/{urllib.parse.quote(best['key'])}?download=1")
        licence = ((meta.get("license") or {}).get("id") or "").lower()
        return url, licence, ""

    # -- main loop --------------------------------------------------------------------------------

    def harvest(self, ref: PaperRef) -> HarvestOutcome:
        doi = normalize_doi(ref.doi)
        out = HarvestOutcome(ref=ref, doi=doi, title=(ref.title or "").strip())
        work: Optional[dict] = None
        # Metadata lookup always runs (even with --channels lacking "openalex"): the licence tier of every
        # copy depends on the version-of-record licence it reports.
        if doi and not doi.lower().startswith(ARXIV_DOI_PREFIX):
            work, meta_attempt = self._openalex_work(doi)
            out.attempts.append(meta_attempt)
        if work:
            out.openalex = {
                "id": work.get("id"),
                "oa_status": (work.get("open_access") or {}).get("oa_status"),
                "vor_open_license": vor_open_license(work),
                "n_locations": len(work.get("locations") or []),
            }
            if not out.title:
                out.title = work.get("title") or work.get("display_name") or ""
        vor_license = vor_open_license(work)
        locs = (work or {}).get("locations") or []
        out.arxiv_id = ref.arxiv_id or arxiv_id_from(doi, ref.link, locs)

        candidates = self.build_candidates(ref, doi, out.title, work, vor_license)
        if not candidates:
            out.status, out.reason = NO_OA_COPY, "no candidate source for this identifier/publisher"
            return out

        for cand in candidates:
            if out.app_hit:
                break
            if (out.held_hit or out.held_xml) and cand.kind in ("tdm_vor", "publisher_copy"):
                continue  # already hold a licensed VoR; only an attachable open copy is still useful
            hit = self._try(cand, doi, out.title, vor_license, out)
            if hit is None:
                continue
            if hit.tier == TIER_OPEN or self.attach_tdm:
                out.app_hit = hit
            elif out.held_hit is None:
                out.held_hit = hit

        if out.app_hit:
            out.status = SUCCESS
            out.reason = f"{out.app_hit.channel}:{out.app_hit.tier}"
        elif out.held_hit:
            out.status = HELD_TDM_ONLY
            out.reason = (f"{out.held_hit.channel} version of record is TDM-licensed; kept in bundle only "
                          "(the app's analyze step uploads PDFs to Gemini). No open copy found.")
        elif out.held_xml:
            out.status = HELD_TDM_ONLY
            out.reason = (f"{out.held_xml.channel}: full-text XML of the TDM-licensed version of record kept in the "
                          "bundle only (the Elsevier API serves no full PDF for this subscription). No open copy found.")
        else:
            failures = [a.outcome for a in out.pdf_attempts if a.outcome != SUCCESS]
            out.status = next((s for s in FAILURE_SEVERITY if s in failures), NO_OA_COPY)
            out.reason = "; ".join(f"{a.channel}:{a.reason or a.outcome}" for a in out.pdf_attempts)[:900]
        return out

    def _try(self, cand: Candidate, doi: str, title: str, vor_license: str,
             out: HarvestOutcome) -> Optional[PdfHit]:
        started = _now_iso()
        if cand.channel == "elsevier_tdm" and self.elsevier_disabled_reason and not vor_license:
            # Subscribed paper and this requestor only ever gets page-1 previews: skip the PDF request. The
            # full-text XML may still be served (it is from an institutional network), and OA papers are never skipped.
            out.attempts.append(Attempt(cand.channel, cand.url, AUTH_REQUIRED, self.elsevier_disabled_reason,
                                        started_at=started))
            self._elsevier_xml_fallback(cand, doi, out)
            return None
        url, licence = cand.url, cand.license
        if cand.resolver is not None:
            try:
                url, resolved_licence, why = cand.resolver()
            except Exception as exc:  # a resolver must never kill the run
                url, resolved_licence, why = "", "", f"resolver_error:{type(exc).__name__}"
            licence = licence or resolved_licence
            if not url:
                out.attempts.append(Attempt(cand.channel, cand.note, NO_OA_COPY, why, started_at=started))
                return None
        host = urllib.parse.urlsplit(url).netloc.lower()
        if host in BOT_WALLED_HOSTS:
            out.attempts.append(Attempt(cand.channel, url, CAPTCHA_OR_BOT_BLOCK,
                                        "skipped: host serves a bot challenge to scripts", started_at=started))
            return None

        res = self.http.get(url, headers=cand.headers, sensitive_headers=cand.sensitive_headers)
        att = Attempt(cand.channel, res.final_url or url, SUCCESS, http_status=res.status or None,
                      bytes=len(res.data), started_at=started, duration_ms=res.elapsed_ms)
        if not res.ok:
            att.outcome, att.reason = classify_http_failure(res)
            out.attempts.append(att)
            return None
        check = inspect_pdf(res.data, min_bytes=self.min_bytes, min_pages=self.min_pages, min_chars=self.min_chars)
        if not check.ok:
            att.outcome, att.reason = check.outcome, check.reason
            preview = cand.channel == "elsevier_tdm" and check.reason.startswith("too_few_pages")
            if preview:
                att.reason = f"elsevier_preview_only({check.reason}): the API serves only page 1 of this PDF here"
                if not vor_license:  # only subscribed papers count towards the breaker
                    self._elsevier_previews += 1
                    if self._elsevier_previews >= self.elsevier_preview_breaker and not self.elsevier_disabled_reason:
                        self.elsevier_disabled_reason = (
                            f"skipped: {self._elsevier_previews} consecutive page-1 previews - the Elsevier API "
                            "serves subscribed PDFs to this requestor only as previews (seen even on the institutional VPN); "
                            "open-access PDFs and the full-text XML are still tried")
            out.attempts.append(att)
            if preview:
                self._elsevier_xml_fallback(cand, doi, out)
            return None
        ok, identity = identity_check(check.head_text, doi=doi, title=title)
        if not ok:
            att.outcome, att.reason = UNSUPPORTED_CONTENT_TYPE, identity
            out.attempts.append(att)
            return None
        if cand.channel == "elsevier_tdm" and not vor_license:
            self._elsevier_previews = 0  # a full subscribed PDF: this requestor is entitled after all
        tier, tier_licence = tier_for(cand.kind, licence, vor_license)
        if tier == TIER_TDM and doi:
            crossref_licence = self._crossref_vor_license(doi, out)
            if crossref_licence:
                tier, tier_licence = TIER_OPEN, crossref_licence
        att.reason = f"verified {check.pages}p {identity} tier={tier}"
        out.attempts.append(att)
        return PdfHit(channel=cand.channel, url=res.final_url or url, data=res.data, pages=check.pages,
                      chars=check.chars, kind=cand.kind, version=cand.version, license=tier_licence,
                      tier=tier, identity=identity)

    def _elsevier_xml_fallback(self, cand: Candidate, doi: str, out: HarvestOutcome) -> None:
        """Subscribed Elsevier article whose PDF the API only previews: keep its full-text XML instead.

        Verified 2026-11-08 from the institutional VPN: ``view=FULL`` XML carries the complete article (sections,
        references, figure and supplement object links) while the PDF endpoint answers "requestor not
        entitled". The XML is TDM-licensed: bundle only, never attached, never sent to hosted AI.
        """
        if out.held_xml is not None or not doi:
            return
        started = _now_iso()
        url = (f"https://api.elsevier.com/content/article/doi/{urllib.parse.quote(doi, safe='/')}"
               "?httpAccept=text/xml&view=FULL")
        if self.elsevier_xml_disabled_reason:
            out.attempts.append(Attempt("elsevier_tdm_xml", url, AUTH_REQUIRED, self.elsevier_xml_disabled_reason,
                                        started_at=started))
            return
        headers = {k: v for k, v in cand.headers.items() if k.lower() != "accept"}
        headers["Accept"] = "text/xml"
        res = self.http.get(url, headers=headers, sensitive_headers=cand.sensitive_headers)
        att = Attempt("elsevier_tdm_xml", res.final_url or url, SUCCESS, http_status=res.status or None,
                      bytes=len(res.data), started_at=started, duration_ms=res.elapsed_ms)
        text = res.text() if res.ok else ""
        has_body = "<ce:sections" in text or "<body" in text
        chars = len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))) if has_body else 0
        xml_dois = {normalize_doi(d).lower() for d in ELSEVIER_XML_DOI_RE.findall(text)}
        unentitled = True
        if not res.ok:
            att.outcome, att.reason = classify_http_failure(res)
        elif not has_body or chars < self.min_chars:
            att.outcome = AUTH_REQUIRED
            att.reason = "elsevier_xml_without_full_text: not entitled from this network (use an institutional network / VPN)"
        elif normalize_doi(doi).lower() not in xml_dois:
            att.outcome, att.reason = UNSUPPORTED_CONTENT_TYPE, f"identity_mismatch: xml doi {sorted(xml_dois)[:1]}"
            unentitled = False
        if att.outcome != SUCCESS:
            if unentitled:
                self._elsevier_xml_failures += 1
                if (self._elsevier_xml_failures >= self.elsevier_preview_breaker
                        and not self.elsevier_xml_disabled_reason):
                    self.elsevier_xml_disabled_reason = (
                        f"skipped: {self._elsevier_xml_failures} consecutive Elsevier XML requests without full "
                        "text - not entitled from this network (use an institutional network / VPN, then rerun with "
                        "--doi-prefix 10.1016 --retry-failed)")
            out.attempts.append(att)
            return
        self._elsevier_xml_failures = 0
        att.reason = f"verified full-text XML {chars} chars doi=match tier={TIER_TDM}"
        out.attempts.append(att)
        out.held_xml = XmlHit(channel="elsevier_tdm_xml", url=res.final_url or url, data=res.data, chars=chars,
                              license=cand.license or "tdm-licence", tier=TIER_TDM, identity="doi=match")

    # -- bundle extras ----------------------------------------------------------------------------

    def collect_extras(self, out: HarvestOutcome, bundle_dir: Path) -> Tuple[List[str], Dict[str, Any]]:
        """Supplements / XML / open review / code-data links for a harvested paper. Never raises."""
        saved: List[str] = []
        hits = [h for h in (out.held_hit, out.app_hit) if h]
        try:
            for hit in hits:
                host = urllib.parse.urlsplit(hit.url).netloc.lower()
                if host.endswith(".copernicus.org") and "/articles/" in hit.url and hit.url.endswith(".pdf"):
                    saved += self._copernicus_extras(hit.url, bundle_dir)
                    break
            if out.held_xml is not None:  # XML-only bundle: no PDF, so keep the figures themselves too
                saved += self._elsevier_extras(out.doi, bundle_dir, xml_data=out.held_xml.data, figures=True)
            elif any(h.channel == "elsevier_tdm" for h in hits):
                saved += self._elsevier_extras(out.doi, bundle_dir)
            if out.doi.lower().startswith(NATURE_PREFIX) and hits:
                saved += self._nature_extras(out.doi, bundle_dir)
        except Exception as exc:  # extras are best effort
            saved.append(f"(extras error: {type(exc).__name__})")
        code_report = self._code_and_data(out, bundle_dir)
        return sorted(set(saved)), code_report

    def _save(self, path: Path, data: bytes) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return str(path)

    def _copernicus_extras(self, pdf_url: str, bundle: Path) -> List[str]:
        saved: List[str] = []
        base = pdf_url[:-4]
        xml = self.http.get(base + ".xml")
        if xml.ok and b"<article" in xml.data[:4000]:
            self._save(bundle / "fulltext_structured.xml", xml.data)
            saved.append("fulltext_structured.xml")
        for ext in ("-supplement.pdf", "-supplement.zip"):
            supp = self.http.get(base + ext, max_bytes=SUPPLEMENT_MAX_BYTES)
            if supp.ok and len(supp.data) > 2048 and b"<html" not in supp.data[:2048].lower():
                name = Path(urllib.parse.urlsplit(base + ext).path).name
                self._save(bundle / "supplements" / name, supp.data)
                saved.append(f"supplements/{name}")
        disc = self.http.get(base + "-discussion.html")
        if disc.ok and disc.data:
            self._save(bundle / "peer_review_rc_ac" / "discussion.html", disc.data)
            saved.append("peer_review_rc_ac/discussion.html")
            page = disc.text()
            links = sorted(set(EGU_REVIEW_PDF_RE.findall(page)) | set(EGU_MANUSCRIPT_VERSION_RE.findall(page)))
            for link in links[:40]:
                rev = self.http.get(link, max_bytes=40_000_000)
                if rev.ok and b"%PDF-" in rev.data[:1024]:
                    name = Path(urllib.parse.urlsplit(link).path).name
                    self._save(bundle / "peer_review_rc_ac" / name, rev.data)
                    saved.append(f"peer_review_rc_ac/{name}")
        return saved

    def _elsevier_extras(self, doi: str, bundle: Path, *, xml_data: Optional[bytes] = None,
                         figures: bool = False) -> List[str]:
        """FULL-view XML (unless already fetched), mmc supplements and, with ``figures``, high-res figures."""
        key = os.environ.get("ELSEVIER_API_KEY", "")
        if not key:
            return []
        headers = {"X-ELS-APIKey": key, "Accept": "text/xml"}
        if os.environ.get("ELSEVIER_INSTTOKEN"):
            headers["X-ELS-Insttoken"] = os.environ["ELSEVIER_INSTTOKEN"]
        sensitive = ("X-ELS-APIKey", "X-ELS-Insttoken")
        object_headers = {k: v for k, v in headers.items() if k != "Accept"}
        saved: List[str] = []
        if xml_data is None:
            xml = self.http.get(
                f"https://api.elsevier.com/content/article/doi/{urllib.parse.quote(doi, safe='/')}?httpAccept=text/xml&view=FULL",
                headers=headers, sensitive_headers=sensitive)
            if not xml.ok:
                return saved
            text = xml.text()
            if "<ce:sections" in text or "<body" in text:
                self._save(bundle / "fulltext_structured.xml", xml.data)
                saved.append("fulltext_structured.xml")
        else:  # already fetched; write_bundle stores it as main_manuscript.xml
            text = xml_data.decode("utf-8", "ignore")
        eids = [e for e in ELSEVIER_ATTACHMENT_EID_RE.findall(text)
                if re.search(r"-mmc\d+", e, re.I) and _wanted_supplement(e)]
        for eid in list(dict.fromkeys(eids))[:10]:
            obj = self.http.get(f"https://api.elsevier.com/content/object/eid/{urllib.parse.quote(eid)}?httpAccept=*/*",
                                headers=object_headers, sensitive_headers=sensitive, max_bytes=SUPPLEMENT_MAX_BYTES)
            if obj.ok and obj.data:
                self._save(bundle / "supplements" / eid, obj.data)
                saved.append(f"supplements/{eid}")
        if figures:
            for url in list(dict.fromkeys(ELSEVIER_HIGHRES_OBJECT_RE.findall(text)))[:ELSEVIER_MAX_FIGURES]:
                url = _html.unescape(url)
                name = Path(urllib.parse.urlsplit(url).path).name
                if not name or not _wanted_supplement(name):
                    continue
                obj = self.http.get(url, headers=object_headers, sensitive_headers=sensitive,
                                    max_bytes=SUPPLEMENT_MAX_BYTES)
                if obj.ok and obj.data:
                    self._save(bundle / "figures" / name, obj.data)
                    saved.append(f"figures/{name}")
        return saved

    def _nature_extras(self, doi: str, bundle: Path) -> List[str]:
        page = self.http.get(f"https://www.nature.com/articles/{doi.split('/', 1)[1]}")
        if not page.ok:
            return []
        saved: List[str] = []
        links = [l for l in dict.fromkeys(NATURE_ESM_RE.findall(page.text())) if _wanted_supplement(l)]
        for link in links[:12]:
            esm = self.http.get(_html.unescape(link), max_bytes=SUPPLEMENT_MAX_BYTES)
            if esm.ok and esm.data:
                name = urllib.parse.unquote(Path(urllib.parse.urlsplit(link).path).name)
                self._save(bundle / "supplements" / name, esm.data)
                saved.append(f"supplements/{name}")
        return saved

    def _code_and_data(self, out: HarvestOutcome, bundle: Path) -> Dict[str, Any]:
        report: Dict[str, Any] = {"datacite_relations": [], "pdf_extracted_urls": [], "downloaded": []}
        if out.doi:
            query = urllib.parse.quote(f'relatedIdentifiers.relatedIdentifier:"{out.doi}"')
            res = self.http.get(f"https://api.datacite.org/dois?query={query}&page[size]=25",
                                headers={"Accept": "application/vnd.api+json"})
            if res.ok:
                try:
                    for item in (res.json() or {}).get("data", []):
                        attr = item.get("attributes") or {}
                        relations = sorted({
                            ri.get("relationType", "") for ri in attr.get("relatedIdentifiers") or []
                            if out.doi.lower() in str(ri.get("relatedIdentifier", "")).lower()
                        })
                        report["datacite_relations"].append({
                            "doi": attr.get("doi"),
                            "type": (attr.get("types") or {}).get("resourceTypeGeneral"),
                            "title": ((attr.get("titles") or [{}])[0] or {}).get("title"),
                            "url": attr.get("url"),
                            "relation": relations,
                        })
                except ValueError:
                    pass
        best = out.held_hit or out.app_hit
        if best is not None:
            report["pdf_extracted_urls"] = scan_pdf_for_code_data_urls(best.data)
        if self.download_code:
            report["downloaded"] = self._download_github(report["pdf_extracted_urls"], bundle)
        if report["datacite_relations"] or report["pdf_extracted_urls"] or report["downloaded"]:
            path = bundle / "companion_code_data" / "code_and_data_links.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    def _download_github(self, urls: Sequence[Dict[str, str]], bundle: Path, max_repo_kb: int = 51_200) -> List[str]:
        saved: List[str] = []
        repos = []
        for entry in urls:
            m = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", entry.get("url", ""))
            if m:
                owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2)).rstrip(".")
                if (owner, repo) not in repos:
                    repos.append((owner, repo))
        for owner, repo in repos[:3]:
            meta = self.http.get(f"https://api.github.com/repos/{owner}/{repo}",
                                 headers={"Accept": "application/vnd.github+json"})
            if not meta.ok:
                continue
            try:
                size_kb = int((meta.json() or {}).get("size") or 0)
            except (ValueError, TypeError):
                continue
            if size_kb > max_repo_kb:
                saved.append(f"(skipped github {owner}/{repo}: {size_kb // 1024} MB > cap)")
                continue
            tar = self.http.get(f"https://api.github.com/repos/{owner}/{repo}/tarball", max_bytes=max_repo_kb * 1024 * 2)
            if tar.ok and len(tar.data) > 500:
                name = f"github_{owner}_{repo}.tar.gz"
                self._save(bundle / "companion_code_data" / name, tar.data)
                saved.append(f"companion_code_data/{name}")
        return saved


# ---------------------------------------------------------------------------
# Bundle writing (filesystem only)
# ---------------------------------------------------------------------------

def _link_or_copy(src: Path, dst: Path) -> str:
    """Hard-link (zero extra disk; survives the app deleting/renaming its copy), else copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        dst.write_bytes(src.read_bytes())
        return "copy"


def write_bundle(out: HarvestOutcome, bundle_root: Path, *, attached_pdf_path: str = "",
                 attach_reason: str = "", extras: Sequence[str] = (),
                 code_report: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Write PaperBundle_<SAFE_DOI>/ with main_manuscript.pdf (or .xml) + bundle_manifest.json.

    main_manuscript.pdf is the best version we hold (the version of record when we have it). When the VoR
    is TDM-only and an open copy was attached to the app, that copy is kept as app_attached_copy.pdf. When
    the VoR is only available as full-text XML (Elsevier subscriptions), it is main_manuscript.xml.
    """
    main = out.held_hit or out.app_hit
    if main is None and out.held_xml is None:
        return None
    bundle = bundle_root / safe_bundle_name(out.doi, out.arxiv_id, out.ref.paper_id)
    bundle.mkdir(parents=True, exist_ok=True)
    files: Dict[str, str] = {}

    def place(hit: PdfHit, name: str) -> None:
        target = bundle / name
        if attached_pdf_path and out.app_hit is hit and Path(attached_pdf_path).is_file():
            files[name] = _link_or_copy(Path(attached_pdf_path), target)
        else:
            tmp = target.with_name(f".{name}.part")
            tmp.write_bytes(hit.data)
            os.replace(tmp, target)
            files[name] = "written"

    held_desc: Optional[Dict[str, str]]
    if out.held_hit is None and out.held_xml is not None:
        # Elsevier subscription: the version of record is held as full-text XML (no full PDF is served).
        tmp = bundle / ".main_manuscript.xml.part"
        tmp.write_bytes(out.held_xml.data)
        os.replace(tmp, bundle / "main_manuscript.xml")
        files["main_manuscript.xml"] = "written"
        main_desc = dict(out.held_xml.describe(), file="main_manuscript.xml")
        held_desc = {"file": "main_manuscript.xml",
                     "why": ("TDM-licensed full-text XML (the Elsevier API serves no full PDF for this "
                             "subscription); local use only - never upload it to a hosted AI service")}
        if out.app_hit is not None:
            place(out.app_hit, "app_attached_copy.pdf")
    else:
        place(main, "main_manuscript.pdf")
        if out.held_hit is not None and out.app_hit is not None:
            place(out.app_hit, "app_attached_copy.pdf")
        main_desc = dict(main.describe(), file="main_manuscript.pdf")
        held_desc = ({"file": "main_manuscript.pdf",
                      "why": "TDM-licensed; not attached because the app sends PDFs to Gemini"}
                     if out.held_hit else None)

    manifest = {
        "harvester": f"seismohub.fullbundle_harvest/{HARVEST_VERSION}",
        "harvested_at": _now_iso(),
        "paper_id": out.ref.paper_id,
        "doi": out.doi or None,
        "arxiv_id": out.arxiv_id or None,
        "title": out.title,
        "journal": out.ref.journal,
        "openalex": out.openalex,
        "main_manuscript": main_desc,
        "app_attached": {
            "attached": bool(attached_pdf_path),
            "pdf_path": attached_pdf_path or None,
            "copy": (out.app_hit.describe() if out.app_hit else None),
            "reason": attach_reason,
        },
        "held_back_tdm_copy": held_desc,
        "files": files,
        "extras_downloaded": list(extras),
        "companion_code_and_data": code_report or {},
        "attempts": [a.as_dict() for a in out.attempts],
    }
    (bundle / "bundle_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return bundle


# ---------------------------------------------------------------------------
# DB helpers (lazy ORM imports so the core stays importable without the app)
# ---------------------------------------------------------------------------

@dataclass
class AttachResult:
    attached: bool
    reason: str
    pdf_path: str = ""


def attach_hit_to_paper(session, paper_id: str, hit: PdfHit, *, downloads_dir: Optional[Path] = None) -> AttachResult:
    """Write the PDF into downloads/ with the app's naming scheme and set pdf_path/pdf_sha256.

    Never overwrites an existing PDF and never attaches bytes that another paper already owns (the same
    rules as services/pdf_match_core.py). Does NOT commit and does NOT trigger AI analysis.
    """
    from models import Paper  # type: ignore
    from services import pdf_naming  # type: ignore

    target_dir = downloads_dir or pdf_naming.DOWNLOADS_DIR
    paper = session.get(Paper, paper_id)
    if paper is None:
        return AttachResult(False, "paper_missing")
    if paper.pdf_path:
        return AttachResult(False, "already_has_pdf", paper.pdf_path)
    sha = hit.sha256
    owner = session.query(Paper.id).filter(Paper.pdf_sha256 == sha, Paper.id != paper_id).first()
    if owner is not None:
        return AttachResult(False, f"same_pdf_already_attached_to:{owner[0]}")
    target = pdf_naming.pdf_target_path_for_paper(paper, target_dir)
    pdf_naming.write_pdf_atomic(target, hit.data)
    paper.pdf_path = str(target.resolve())
    paper.pdf_sha256 = sha
    paper.updated_at = _dt.datetime.utcnow()
    return AttachResult(True, "attached", paper.pdf_path)


def _retry_after(status: str) -> Optional[_dt.timedelta]:
    if status == TRANSIENT_ERROR:
        return _dt.timedelta(days=1)
    if status in (SUCCESS, HELD_TDM_ONLY):
        return None
    return _dt.timedelta(days=30)


def record_harvest(session, out: HarvestOutcome, attach: AttachResult) -> None:
    """Persist RetrievalAttempt rows + the paper's retrieval_* summary (caller commits)."""
    from models import Paper, RetrievalAttempt  # type: ignore

    now = _dt.datetime.utcnow()
    paper = session.get(Paper, out.ref.paper_id)
    if paper is None:
        return
    for att in out.pdf_attempts:
        try:
            started = _dt.datetime.strptime(att.started_at, "%Y-%m-%dT%H:%M:%SZ") if att.started_at else now
        except ValueError:
            started = now
        session.add(RetrievalAttempt(
            paper_id=paper.id, stage=STAGE, source=att.channel[:64], url_attempted=att.url or None,
            outcome=att.outcome[:32], http_status=att.http_status, bytes_downloaded=att.bytes or None,
            error_reason=(att.reason or None), started_at=started,
            finished_at=started + _dt.timedelta(milliseconds=att.duration_ms or 0),
            duration_ms=att.duration_ms or None,
        ))
    paper.retrieval_attempts_count = (paper.retrieval_attempts_count or 0) + len(out.pdf_attempts)
    paper.retrieval_attempted_at = now
    paper.retrieval_last_stage = STAGE
    if attach.attached and out.app_hit is not None:
        paper.retrieval_status = SUCCESS
        paper.retrieval_source = out.app_hit.channel[:64]
        paper.retrieval_source_url = out.app_hit.url
        paper.retrieval_error_reason = None
        paper.retrieval_succeeded_at = now
        paper.retrieval_next_retry_at = None
        return
    if attach.reason == "already_has_pdf":
        return  # someone attached a PDF meanwhile - leave their state alone
    status = out.status if out.status != SUCCESS else UNSUPPORTED  # app copy found but not attachable
    paper.retrieval_status = status
    held = out.held_hit or out.held_xml
    paper.retrieval_source = (held.channel[:64] if held else None)
    paper.retrieval_source_url = (held.url if held else None)
    reason = f"{attach.reason}; {out.reason}" if attach.reason else out.reason
    paper.retrieval_error_reason = reason[:2000]
    delta = _retry_after(status)
    paper.retrieval_next_retry_at = (now + delta) if delta else None
