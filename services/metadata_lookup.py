"""Online metadata lookup layer — Semantic Scholar / OpenAlex / CrossRef.

Single public entrypoint: ``lookup_metadata_by_doi(doi)``. PR-24 ships
the SERIAL S2 → OpenAlex → CrossRef chain. PR-25 will swap the serial
loop for a ThreadPoolExecutor fan-out.
"""

from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests

from services.fetch_service import (
    HTTP_TIMEOUT,
    REQUEST_HEADERS,
    clean_text,
    normalize_doi,
    openalex_abstract_from_inverted_index,
)

__all__ = [
    "LookupResult",
    "lookup_metadata_by_doi",
    "lookup_metadata_by_title_authors",
    "lookup_metadata_by_identifier",
    "lookup_by_arxiv",
    "detect_identifier_kind",
    "CROSSREF_AUTHORITATIVE_FIELDS",
]

# ---------------------------------------------------------------------------
# Provider endpoints
# ---------------------------------------------------------------------------
_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_OPENALEX_BASE = "https://api.openalex.org"
_CROSSREF_BASE = "https://api.crossref.org/works"
_UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
ARXIV_API = "http://export.arxiv.org/api/query"

# Unpaywall requires a contact email; falls back to a polite-pool fallback.
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "noreply@example.invalid")
TITLE_SIM_THRESHOLD = 0.82

_S2_FIELDS = (
    "title,authors,year,venue,journal,abstract,tldr,openAccessPdf,"
    "externalIds"
)

_BACKOFFS_S: Tuple[int, ...] = (1, 2, 4, 8, 16)
_REQUIRED_FIELDS: Tuple[str, ...] = ("title", "authors", "year", "journal")

# PERFORMANCE.md follow-up (Fix B): hard wall-clock cap for the
# fan-out's per-future ``.result()`` wait. The underlying ``requests``
# call still uses ``HTTP_TIMEOUT`` (25 s) so the wire-level timeout
# is unchanged — this constant only bounds how long the merger waits
# on one slow provider before treating its result as missing. A 30 s
# cap is roomy enough to absorb one 429 backoff (16 s + 1 retry) but
# stops a fully-stuck provider from pinning the response.
PROVIDER_FUTURE_TIMEOUT_S: float = 30.0

# PR-26 — Q10 hybrid: CrossRef wins on conflict for these authoritative keys.
CROSSREF_AUTHORITATIVE_FIELDS: frozenset = frozenset({
    "title", "journal", "container-title", "year", "authors", "issn",
})


@dataclass(frozen=True)
class LookupResult:
    """Outcome of one lookup_metadata_by_doi call."""

    status: Literal["ok", "partial", "failed"]
    source: Optional[str]
    fields: Dict[str, Any] = field(default_factory=dict)
    missing_fields: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    # PR-25 — per-field provenance for audit. Empty dict on failed lookups.
    meta_source: Dict[str, str] = field(default_factory=dict)
    # PR-26 — audit log of CrossRef-wins-on-conflict overrides.
    meta_conflict_log: Tuple[Dict[str, Any], ...] = ()


class _HttpClient:
    """requests.Session + ETag cache + 429-aware exponential backoff."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._etag: Dict[str, Tuple[str, Dict[str, Any], float]] = {}
        self._lock = threading.Lock()

    def get_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        merged_headers = {**REQUEST_HEADERS, **(headers or {})}
        with self._lock:
            cached = self._etag.get(url)
        if cached is not None:
            merged_headers["If-None-Match"] = cached[0]

        last_exc: Optional[BaseException] = None
        for attempt in range(len(_BACKOFFS_S) + 1):
            if attempt > 0:
                time.sleep(_BACKOFFS_S[attempt - 1])
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers=merged_headers,
                    timeout=HTTP_TIMEOUT,
                )
                if response.status_code == 304 and cached is not None:
                    return cached[1]
                if response.status_code == 404:
                    return None
                if response.status_code == 429:
                    retry_after = 0
                    try:
                        retry_after = int(response.headers.get("Retry-After", "0") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0
                    next_backoff_idx = min(attempt, len(_BACKOFFS_S) - 1)
                    extra = max(0, retry_after - _BACKOFFS_S[next_backoff_idx])
                    if extra:
                        time.sleep(extra)
                    continue
                response.raise_for_status()
                body = response.json()
                etag = response.headers.get("ETag")
                if etag:
                    with self._lock:
                        self._etag[url] = (etag, body, time.time())
                return body
            except requests.RequestException as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        return None


_http = _HttpClient()


def _fetch_s2_by_doi(doi: str) -> Tuple[Dict[str, Any], str]:
    url = f"{_S2_BASE}/paper/DOI:{doi}"
    try:
        body = _http.get_json(url, params={"fields": _S2_FIELDS})
    except requests.RequestException as exc:
        return {}, f"s2: {exc}"
    if not body:
        return {}, ""
    title = (body.get("title") or "").strip()
    if not title:
        return {}, ""
    authors = [
        (entry.get("name") or "").strip()
        for entry in (body.get("authors") or [])
        if isinstance(entry, dict)
    ]
    year = body.get("year")
    venue = body.get("venue") or (body.get("journal") or {}).get("name") or ""
    fields: Dict[str, Any] = {
        "title": title,
        "authors": [a for a in authors if a],
        "journal": venue,
        "year": str(year) if year else "",
        "published_date": f"{year:04d}" if isinstance(year, int) else "",
        "abstract": clean_text(body.get("abstract") or ""),
        "link": f"https://doi.org/{doi}",
        "doi": doi,
    }
    return fields, ""


def _fetch_openalex_by_doi(doi: str) -> Tuple[Dict[str, Any], str]:
    url = f"{_OPENALEX_BASE}/works/https://doi.org/{doi}"
    try:
        body = _http.get_json(url)
    except requests.RequestException as exc:
        return {}, f"openalex: {exc}"
    if not body:
        return {}, ""
    title = (body.get("title") or "").strip()
    if not title:
        return {}, ""
    authorships = body.get("authorships") or []
    authors = [
        ((entry.get("author") or {}).get("display_name") or "").strip()
        for entry in authorships
        if isinstance(entry, dict)
    ]
    primary_location = body.get("primary_location") or {}
    source_obj = primary_location.get("source") or body.get("host_venue") or {}
    year = body.get("publication_year")
    fields: Dict[str, Any] = {
        "title": title,
        "authors": [a for a in authors if a],
        "journal": (source_obj.get("display_name") or "").strip(),
        "year": str(year) if year else "",
        "published_date": body.get("publication_date") or "",
        "abstract": clean_text(
            openalex_abstract_from_inverted_index(body.get("abstract_inverted_index"))
        ),
        "link": body.get("doi") or f"https://doi.org/{doi}",
        "doi": doi,
    }
    return fields, ""


def _fetch_crossref_by_doi(doi: str) -> Tuple[Dict[str, Any], str]:
    url = f"{_CROSSREF_BASE}/{doi}"
    try:
        body = _http.get_json(url)
    except requests.RequestException as exc:
        return {}, f"crossref: {exc}"
    if not body:
        return {}, ""
    work = body.get("message") or {}
    title_list = work.get("title") or []
    title = (title_list[0] if title_list else "").strip()
    if not title:
        return {}, ""
    authors_raw = work.get("author") or []
    authors = [
        ((entry.get("given", "") + " " + entry.get("family", "")).strip()
         or entry.get("name", ""))
        for entry in authors_raw
        if isinstance(entry, dict)
    ]
    date_parts = (work.get("issued") or {}).get("date-parts") or [[]]
    parts = (date_parts[0] if date_parts else []) + [None, None, None]
    year, month, day = parts[0], parts[1], parts[2]
    published_date = ""
    if year:
        published_date = "-".join([
            f"{year:04d}",
            f"{month:02d}" if month else "",
            f"{day:02d}" if day else "",
        ]).rstrip("-")
    raw_abstract = work.get("abstract") or ""
    abstract = clean_text(re.sub(r"</?jats:[^>]+>", "", raw_abstract))
    issn_list = work.get("ISSN") or []
    fields: Dict[str, Any] = {
        "title": title,
        "authors": [a for a in authors if a],
        "journal": (work.get("container-title") or [""])[0],
        "container-title": (work.get("container-title") or [""])[0],
        "year": str(year) if year else "",
        "published_date": published_date,
        "abstract": abstract,
        "link": work.get("URL") or f"https://doi.org/{doi}",
        "doi": doi,
    }
    if issn_list:
        fields["issn"] = issn_list[0]
    return fields, ""


def _merge_fill_empty(target: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    """Fill *target* with entries from *incoming* WHEN target has an empty value."""
    for key, value in incoming.items():
        if not target.get(key) and value:
            target[key] = value


def _status_for(fields: Dict[str, Any]) -> Literal["ok", "partial", "failed"]:
    if not fields.get("title"):
        return "failed"
    if all(fields.get(name) for name in _REQUIRED_FIELDS):
        return "ok"
    return "partial"


def _missing(fields: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(name for name in _REQUIRED_FIELDS if not fields.get(name))


def _safe_fetch(fetcher, doi: str) -> Tuple[Dict[str, Any], str]:
    """Wrap a fetcher so an exception becomes ({}, error_str)."""
    try:
        return fetcher(doi)
    except Exception as exc:  # noqa: BLE001
        return {}, f"{getattr(fetcher, '__name__', 'fetcher')}: {exc.__class__.__name__}: {exc}"


def _probe_unpaywall_pdf(doi: str) -> Optional[str]:
    """Return best-effort OA PDF URL or None.

    Independent of metadata chain; submitted as a 4th parallel task in
    ``lookup_metadata_by_doi``.  Tries Unpaywall first, falls back to
    OpenAlex's ``open_access.oa_url``.
    """
    doi = normalize_doi(doi)
    if not doi:
        return None
    try:
        body = _http.get_json(
            f"{_UNPAYWALL_BASE}/{doi}", params={"email": UNPAYWALL_EMAIL},
        )
        if isinstance(body, dict):
            loc = body.get("best_oa_location") or {}
            url = (loc.get("url_for_pdf") or "").strip()
            if url:
                return url
    except requests.RequestException:
        pass
    try:
        body = _http.get_json(
            f"{_OPENALEX_BASE}/works/https://doi.org/{doi}",
        )
        if isinstance(body, dict):
            oa = body.get("open_access") or {}
            url = (oa.get("oa_url") or "").strip()
            if url:
                return url
    except requests.RequestException:
        pass
    return None


@lru_cache(maxsize=1024)
def _lookup_by_normalised_doi(doi: str) -> LookupResult:
    """PR-25 parallel fan-out S2/OpenAlex/CrossRef.

    Replaces PR-24's serial chain with a ThreadPoolExecutor(max_workers=3)
    so latency is bounded by the slowest provider, not the sum.
    """
    errors: List[str] = []
    provider_results: Dict[str, Dict[str, Any]] = {}

    # PR-27 — bump max_workers to 4; layer the Unpaywall OA-PDF probe
    # alongside the three metadata providers. Costs zero extra wall time
    # because the slowest provider already dominates.
    #
    # PERFORMANCE.md follow-up (Fix B): wrap each ``.result()`` in
    # PROVIDER_FUTURE_TIMEOUT_S so a fully-stuck provider can't pin the
    # merger past ~30 s. The wire-level ``requests`` timeout
    # (``HTTP_TIMEOUT`` from fetch_service, currently 25 s) is unchanged.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="mdlookup") as pool:
        futures = {
            "s2":       pool.submit(_safe_fetch, _fetch_s2_by_doi,       doi),
            "openalex": pool.submit(_safe_fetch, _fetch_openalex_by_doi, doi),
            "crossref": pool.submit(_safe_fetch, _fetch_crossref_by_doi, doi),
            "pdf":      pool.submit(_probe_unpaywall_pdf, doi),
        }
        for prov in ("s2", "openalex", "crossref"):
            try:
                fields, err = futures[prov].result(timeout=PROVIDER_FUTURE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                # TimeoutError or any other unhandled provider failure.
                # Treat as a missing provider; never let one slow service
                # pin the whole metadata lookup.
                fields, err = {}, f"{prov}: future_timeout: {exc.__class__.__name__}"
            if err:
                errors.append(err)
            provider_results[prov] = fields
        try:
            pdf_url = futures["pdf"].result(timeout=PROVIDER_FUTURE_TIMEOUT_S)
        except Exception:
            pdf_url = None

    # Field-by-field merge: priority order PLUS the PR-26 Q10 hybrid rule
    # (CrossRef wins on conflict for the authoritative-fields set).
    merged: Dict[str, Any] = {"doi": doi, "link": f"https://doi.org/{doi}"}
    meta_source: Dict[str, str] = {}
    conflict_log: List[Dict[str, Any]] = []
    order = ("s2", "openalex", "crossref")
    cr_payload = provider_results.get("crossref") or {}
    all_keys = set()
    for f in provider_results.values():
        if f:
            all_keys.update(f.keys())
    for k in all_keys:
        # Step 1 — priority-chain pick.
        chain_value = None
        chain_source: Optional[str] = None
        for src in order:
            v = (provider_results.get(src) or {}).get(k)
            if v:
                chain_value = v
                chain_source = src
                break

        # Step 2 — PR-26 Q10 override on the authoritative set.
        cr_value = cr_payload.get(k)
        if k in CROSSREF_AUTHORITATIVE_FIELDS and cr_value:
            if not merged.get(k):
                merged[k] = cr_value
                meta_source[k] = "crossref"
            if chain_source and chain_source != "crossref" and chain_value != cr_value:
                alt = {
                    src: (provider_results.get(src) or {}).get(k)
                    for src in order
                    if src != "crossref" and (provider_results.get(src) or {}).get(k)
                }
                conflict_log.append({
                    "field": k,
                    "chosen_source": "crossref",
                    "alternative_values": alt,
                })
            continue

        # Step 3 — keep the chain pick.
        if chain_value is not None and not merged.get(k):
            merged[k] = chain_value
            if chain_source:
                meta_source[k] = chain_source

    # Determine primary source (first provider that supplied a title).
    primary: Optional[str] = None
    for prov in order:
        if provider_results.get(prov) and provider_results[prov].get("title"):
            primary = prov
            break

    # PR-27 — layer Unpaywall PDF URL on top of merged record. Skipped
    # when the metadata merge is empty (no point asking for a PDF for a
    # DOI that doesn't resolve).
    if merged.get("title") and pdf_url:
        merged["pdf_url"] = pdf_url
        meta_source["pdf_url"] = "unpaywall"

    if not merged.get("title"):
        return LookupResult(
            status="failed",
            source=None,
            fields={},
            missing_fields=_REQUIRED_FIELDS,
            errors=tuple(errors),
            meta_source={},
            meta_conflict_log=(),
        )

    return LookupResult(
        status=_status_for(merged),
        source=primary,
        fields=merged,
        missing_fields=_missing(merged),
        errors=tuple(errors),
        meta_source=meta_source,
        meta_conflict_log=tuple(conflict_log),
    )


def lookup_metadata_by_doi(doi: str) -> Optional[LookupResult]:
    """Fetch metadata for *doi* via S2 / OpenAlex / CrossRef chain."""
    if not doi:
        return None
    normalised = normalize_doi(doi)
    if not normalised or "/" not in normalised:
        return None
    return _lookup_by_normalised_doi(normalised)


# ---------------------------------------------------------------------------
# PR-27 — title-only fallback for PDF-derived ingest (no DOI).
# ---------------------------------------------------------------------------
def _normalized_title_local(s: str) -> str:
    """Lowercase + collapse non-word to spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip()


def _title_similarity_local(a: str, b: str) -> float:
    """Token-set containment similarity (max-based)."""
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    intersection = ta & tb
    return len(intersection) / max(len(ta), len(tb))


def lookup_metadata_by_title_authors(
    title: str,
    authors_hint: Optional[List[str]] = None,
    *,
    min_similarity: float = TITLE_SIM_THRESHOLD,
) -> Optional[LookupResult]:
    """Title-only fallback. Queries S2 search; accepts top hit only above threshold."""
    title = (title or "").strip()
    if len(title) < 8:
        return None
    try:
        body = _http.get_json(
            f"{_S2_BASE}/paper/search",
            params={
                "query": title,
                "limit": 10,
                "fields": "title,authors,year,venue,journal,abstract,externalIds,openAccessPdf",
            },
        )
    except requests.RequestException:
        return None
    hits = (body or {}).get("data") or []
    if not hits:
        return None

    norm_input = _normalized_title_local(title)
    hint_surnames = {
        (n.split()[-1] if n else "").lower()
        for n in (authors_hint or []) if n
    } - {""}

    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for cand in hits:
        cand_title = (cand.get("title") or "").strip()
        sim = _title_similarity_local(norm_input, _normalized_title_local(cand_title))
        accept_strict = sim >= min_similarity
        accept_author = False
        if hint_surnames and sim >= 0.75:
            cand_surnames = {
                ((a.get("name") or "").split()[-1] or "").lower()
                for a in (cand.get("authors") or [])
            } - {""}
            accept_author = bool(hint_surnames & cand_surnames)
        if not (accept_strict or accept_author):
            continue
        if best is None or sim > best[0]:
            best = (sim, cand)

    if best is None:
        return None

    sim, cand = best
    fields: Dict[str, Any] = {
        "title":   (cand.get("title") or "").strip(),
        "authors": [a.get("name", "") for a in (cand.get("authors") or []) if a.get("name")],
        "year":    cand.get("year"),
        "journal": (cand.get("venue") or (cand.get("journal") or {}).get("name") or "").strip(),
        "abstract": clean_text(cand.get("abstract") or ""),
        "doi":     (cand.get("externalIds") or {}).get("DOI", "").strip(),
        "_match_similarity": round(float(sim), 3),
    }
    pdf = ((cand.get("openAccessPdf") or {}).get("url") or "").strip()
    if pdf:
        fields["pdf_url"] = pdf
    missing = tuple(k for k in _REQUIRED_FIELDS if not fields.get(k))
    status = "ok" if not missing else "partial"
    return LookupResult(
        status=status,
        source="s2_search",
        missing_fields=missing,
        fields=fields,
        errors=(),
        meta_source={k: "s2_search" for k in fields.keys()},
        meta_conflict_log=(),
    )


# ---------------------------------------------------------------------------
# PR-28 — Identifier sniffer + arXiv/EarthArXiv/ChemRxiv dispatcher.
# ---------------------------------------------------------------------------
# Order matters: preprint DOI prefixes must be tested BEFORE the generic
# DOI fall-through so the dispatcher can tag preprint origin without a
# second regex pass.

_ARXIV_NEW_RE   = re.compile(r"^(?:arxiv:\s*)?(\d{4}\.\d{4,5})(v\d+)?$", re.I)
_ARXIV_OLD_RE   = re.compile(r"^arxiv:\s*([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$", re.I)
_EARTHARXIV_RE  = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/)?(10\.31223/[A-Z0-9._\-/]+)$", re.I)
_CHEMRXIV_RE    = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/)?(10\.26434/chemrxiv[A-Z0-9._\-/]*)$", re.I)
_DOI_FULL_RE    = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)$", re.I)
_PMCID_RE       = re.compile(r"^(?:pmc(?:id)?:?\s*)?PMC?(\d+)$", re.I)
_PMID_RE        = re.compile(r"^pmid:\s*(\d{1,9})$", re.I)


def detect_identifier_kind(input: str) -> str:
    """Sniff the canonical identifier kind of a user-pasted string.

    Returns one of: ``doi``, ``arxiv``, ``eartharxiv``, ``chemrxiv``,
    ``pmid``, ``pmcid``, ``url``, ``freetext``.
    """
    if not input:
        return "freetext"
    s = input.strip()
    if not s:
        return "freetext"
    if _ARXIV_NEW_RE.match(s) or _ARXIV_OLD_RE.match(s):
        return "arxiv"
    if _EARTHARXIV_RE.match(s):
        return "eartharxiv"
    if _CHEMRXIV_RE.match(s):
        return "chemrxiv"
    if _PMID_RE.match(s):
        return "pmid"
    if _PMCID_RE.match(s):
        return "pmcid"
    if _DOI_FULL_RE.match(s):
        return "doi"
    if s.lower().startswith(("http://", "https://")):
        return "url"
    return "freetext"


# arXiv Atom fetcher with mandatory 3-second throttle per ToU.
_arxiv_throttle = threading.Semaphore(1)
_last_arxiv_call_ts: List[float] = [0.0]
_ARXIV_MIN_INTERVAL_S = 3.0

_ATOM_NS = {
    "atom":  "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _arxiv_throttle_wait() -> None:
    """Honour arXiv ToU 3-second min-interval per process."""
    with _arxiv_throttle:
        now = time.time()
        delta = now - _last_arxiv_call_ts[0]
        if delta < _ARXIV_MIN_INTERVAL_S:
            time.sleep(_ARXIV_MIN_INTERVAL_S - delta)
        _last_arxiv_call_ts[0] = time.time()


def _lookup_arxiv(arxiv_id: str) -> Optional[LookupResult]:
    """Fetch one record from export.arxiv.org/api/query."""
    if not arxiv_id:
        return None
    cleaned = re.sub(r"v\d+$", "", arxiv_id.strip())
    cleaned = re.sub(r"^arxiv:\s*", "", cleaned, flags=re.I)
    _arxiv_throttle_wait()
    try:
        r = requests.get(
            ARXIV_API,
            params={"id_list": cleaned},
            headers=REQUEST_HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as exc:
        return LookupResult(
            status="failed", source="arxiv",
            missing_fields=_REQUIRED_FIELDS, fields={},
            errors=(f"arxiv: {exc}",),
        )
    entries = root.findall("atom:entry", _ATOM_NS)
    if not entries:
        return None
    e = entries[0]
    title = clean_text(e.findtext("atom:title", default="", namespaces=_ATOM_NS) or "")
    summary = clean_text(e.findtext("atom:summary", default="", namespaces=_ATOM_NS) or "")
    published = e.findtext("atom:published", default="", namespaces=_ATOM_NS) or ""
    authors = [
        (a.findtext("atom:name", default="", namespaces=_ATOM_NS) or "").strip()
        for a in e.findall("atom:author", _ATOM_NS)
    ]
    authors = [a for a in authors if a]
    doi_el = e.find("arxiv:doi", _ATOM_NS)
    primary_cat_el = e.find("arxiv:primary_category", _ATOM_NS)
    pdf_url = ""
    for link in e.findall("atom:link", _ATOM_NS):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            pdf_url = link.attrib.get("href", "")
            break
    fields = {
        "title":     title,
        "authors":   authors,
        "year":      int(published[:4]) if published[:4].isdigit() else None,
        "journal":   "arXiv",
        "abstract":  summary,
        "doi":       (doi_el.text or "").strip() if doi_el is not None else "",
        "arxiv_id":  cleaned,
        "arxiv_category": (primary_cat_el.attrib.get("term", "") if primary_cat_el is not None else ""),
        "pdf_url":   pdf_url,
    }
    missing = tuple(k for k in _REQUIRED_FIELDS if not fields.get(k))
    status = "ok" if not missing else "partial"
    return LookupResult(
        status=status, source="arxiv",
        missing_fields=missing, fields=fields, errors=(),
    )


def lookup_by_arxiv(arxiv_id: str) -> Optional[LookupResult]:
    """Public alias for _lookup_arxiv."""
    return _lookup_arxiv(arxiv_id)


def lookup_metadata_by_identifier(
    kind: str, value: str,
) -> Optional[LookupResult]:
    """Route a pre-sniffed identifier to the right fetcher.

    See ``detect_identifier_kind`` for accepted ``kind`` values.
    """
    kind_norm = (kind or "").strip().lower()
    if kind_norm in ("doi", "eartharxiv", "chemrxiv"):
        return lookup_metadata_by_doi(normalize_doi(value) or value)
    if kind_norm == "arxiv":
        return _lookup_arxiv(value)
    if kind_norm in ("pmid", "pmcid", "url", "freetext"):
        return None
    return None
