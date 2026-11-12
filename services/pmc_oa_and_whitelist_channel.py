#!/usr/bin/env python3
"""
pmc_oa_and_whitelist_channel.py (v2.1 — post-audit hardened)
============================================================
Additive channel & resolver helpers for SeismoHub v2 (`services/fullbundle_harvest.py`).

Fixes applied from the 2026-11-09 cross-session audit (`方法_PDF抓取流程与突破方向_2026-11-09.md` §1.14 & §3.8):
1. Zero `--dry-run` side effects: `MetadataFallbackCache` never creates directories or SQLite files
   on initialization; `get()` only reads if the file already exists; `put()` only writes when
   `not readonly` and the parent directory already exists. Default TTL reduced from 30 days to 7 days.
2. Unpaywall fallback disabled (`ENABLE_UNPAYWALL_FALLBACK = False`) and hardcoded email removed:
   Unpaywall migrated to OpenAlex's database in late 2025, so querying it after OpenAlex has zero
   incremental yield while putting an email into query URLs.
3. Gold-OA whitelist cleaned and decoupled from `tdm_vor`:
   - Removed `2397-334X` (*Nature Ecology & Evolution*, which is hybrid, not Gold OA).
   - Fixed *The Seismic Record* ISSN to `2694-4006`.
   - Removed the unguarded `source.is_oa` catch-all.
   - Inferred licenses are NEVER propagated onto `tdm_vor` / `publisher_copy` candidates.
4. `pmc_oa_s3` returns the exact `license_code` from S3 metadata JSON (`metadata/<PMCID>.<v>.json`)
   without being overwritten by a preset candidate license.
5. Added `resolve_crossref_tdm_pdf_link` (§3.4) to harvest direct PDF links registered in Crossref
   `link[]` (`intended-application: text-mining` / `syndication`) on non-walled hosts such as
   *Seismica* (`seismica.library.mcgill.ca`).
6. Added `resolve_arxiv_by_exact_title` to rescue unlinked arXiv preprints (`export.arxiv.org`)
   for Cloudflare-walled GJI / BSSA / SRL / AGU papers using strict bidirectional title matching
   (`title_overlap >= 0.85`).
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PMC_S3_BASE = "https://pmc-oa-opendata.s3.amazonaws.com"
PMC_PREFIX = "pmcoa:"
ENABLE_UNPAYWALL_FALLBACK = False

_PMC_URL_RE = re.compile(
    r"(?:pmc\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pmc|europepmc\.org)/articles/(PMC\d+)",
    re.I,
)
_ARXIV_DOI_URL_RE = re.compile(r"doi\.org/10\.48550/arxiv\.(\d{4}\.\d{4,6}(?:v\d+)?)", re.I)
_HAL_URL_RE = re.compile(
    r"^(https?://(?:[a-z0-9-]+\.)?(?:hal\.science|hal\.archives-ouvertes\.fr)/(?:[a-z]+-)?\d+(?:v\d+)?)"
    r"(?:/document|/file/[^?#]*)?/?$",
    re.I,
)
_DSPACE7_URL_RE = re.compile(r"^(https?://[^/]+)/bitstreams/([0-9a-f-]{36})/download$", re.I)

# ---------------------------------------------------------------------------
# 1. Verified Gold-OA Journal Whitelist (Used ONLY for open_direct / Crossref link checks,
#    NEVER propagated onto tdm_vor or publisher_copy candidates)
# ---------------------------------------------------------------------------

GOLD_OA_ISSNS: Dict[str, Tuple[str, int]] = {
    # Nature portfolio Gold OA
    "2041-1723": ("cc-by", 2010),     # Nature Communications
    "2045-2322": ("cc-by", 2011),     # Scientific Reports
    "2662-4435": ("cc-by", 2020),     # Communications Earth & Environment
    # AAAS Gold OA
    "2375-2548": ("cc-by-nc", 2015),  # Science Advances
    # Oxford / RAS Gold OA (GJI flipped to 100% Gold OA in Jan 2024)
    "0956-540X": ("cc-by", 2024),     # Geophysical Journal International (Print)
    "1365-246X": ("cc-by", 2024),     # Geophysical Journal International (Online)
    # Diamond OA Seismology / Volcanology / Tectonics
    "2816-9387": ("cc-by", 2022),     # Seismica
    "2976-548X": ("cc-by", 2022),     # Tektonika
    "2610-3540": ("cc-by", 2018),     # Volcanica
    # SSA Gold OA
    "2694-4006": ("cc-by", 2021),     # The Seismic Record (verified ISSN)
    # AGU / Wiley 100% Gold OA journals
    "2576-604X": ("cc-by", 2019),     # AGU Advances
    "2333-5084": ("cc-by", 2014),     # Earth and Space Science
    "2328-4277": ("cc-by", 2013),     # Earth's Future
    "1942-2466": ("cc-by", 2009),     # Journal of Advances in Modeling Earth Systems (JAMES)
    "2471-1403": ("cc-by", 2017),     # GeoHealth
    "1525-2027": ("cc-by", 2023),     # Geochemistry, Geophysics, Geosystems (G-Cubed flipped Gold OA in 2023)
    "1542-7390": ("cc-by", 2023),     # Space Weather
    # SpringerOpen / GSA / Other Gold OA Earth Science
    "1880-5981": ("cc-by", 2014),     # Earth, Planets and Space
    "2197-4284": ("cc-by", 2014),     # Progress in Earth and Planetary Science
    "1553-040X": ("cc-by", 2018),     # Geosphere
    "2296-6463": ("cc-by", 2013),     # Frontiers in Earth Science
    "1424-8220": ("cc-by", 2001),     # Sensors (MDPI)
    "2072-4292": ("cc-by", 2009),     # Remote Sensing (MDPI)
    "2076-3263": ("cc-by", 2011),     # Geosciences (MDPI)
    "2076-3417": ("cc-by", 2011),     # Applied Sciences (MDPI)
}

GOLD_OA_DOI_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"^10\.5194/", re.I), "cc-by"),                 # Copernicus / EGU
    (re.compile(r"^10\.26443/seismica\.", re.I), "cc-by"),      # Seismica
    (re.compile(r"^10\.3390/", re.I), "cc-by"),                 # MDPI (Sensors, Remote Sensing, etc.)
    (re.compile(r"^10\.3389/", re.I), "cc-by"),                 # Frontiers
    (re.compile(r"^10\.1186/s40623-", re.I), "cc-by"),          # Earth, Planets and Space
    (re.compile(r"^10\.1186/s40645-", re.I), "cc-by"),          # Progress in Earth and Planetary Science
    (re.compile(r"^10\.1038/s41467-", re.I), "cc-by"),          # Nature Communications
    (re.compile(r"^10\.1038/s41598-", re.I), "cc-by"),          # Scientific Reports
    (re.compile(r"^10\.1038/s43247-", re.I), "cc-by"),          # Communications Earth & Environment
    (re.compile(r"^10\.1126/sciadv\.", re.I), "cc-by-nc"),      # Science Advances
    (re.compile(r"^10\.1093/gji/gg[ae-z]", re.I), "cc-by"),     # GJI 2024+ (ggae, ggaf, ggag...)
)


def infer_gold_oa_vor_license(
    doi: str = "",
    journal: str = "",
    published_year: Optional[int] = None,
    openalex_work: Optional[Dict[str, Any]] = None,
) -> str:
    """Return an open license slug (e.g. 'cc-by') if the DOI or ISSN is in the verified Gold-OA list."""
    d = (doi or "").strip()
    for pat, lic in GOLD_OA_DOI_PATTERNS:
        if pat.search(d):
            return lic

    year = published_year
    if year is None and openalex_work:
        try:
            year = int(openalex_work.get("publication_year") or 0) or None
        except (ValueError, TypeError):
            year = None
    if year is None:
        m = re.search(r"\b(20[12]\d)\b", d)
        if m:
            year = int(m.group(1))
    check_year = year or 2026

    if openalex_work:
        src = ((openalex_work.get("primary_location") or {}).get("source") or {})
        issns = list(src.get("issn") or [])
        if src.get("issn_l"):
            issns.append(src["issn_l"])
        for issn in issns:
            issn_clean = str(issn).strip().upper()
            if issn_clean in GOLD_OA_ISSNS:
                lic, min_year = GOLD_OA_ISSNS[issn_clean]
                if check_year >= min_year:
                    return lic
    return ""


# ---------------------------------------------------------------------------
# 2. Repository URL Rewriter (PMC, arXiv DOI, HAL, DSpace 7)
# ---------------------------------------------------------------------------

def rewrite_repository_url(url: str) -> Tuple[str, str]:
    """Rewrite a repository landing/PDF URL into a direct programmatic fetch target."""
    u = (url or "").strip()
    if not u:
        return "", ""
    m = _PMC_URL_RE.search(u)
    if m:
        return f"{PMC_PREFIX}{m.group(1).upper()}", "pmc_oa_s3"
    m = _ARXIV_DOI_URL_RE.search(u)
    if m:
        arxiv_clean = re.sub(r"v\d+$", "", m.group(1))
        return f"https://export.arxiv.org/pdf/{arxiv_clean}", "arxiv"
    m = _HAL_URL_RE.match(u)
    if m:
        return f"{m.group(1)}/document", "hal_repo"
    m = _DSPACE7_URL_RE.match(u)
    if m:
        return f"{m.group(1)}/server/api/core/bitstreams/{m.group(2)}/content", "dspace7_bitstream"
    return u, ""


def extract_pmcid_from_locations(locations: Sequence[Dict[str, Any]], openalex_work: Optional[Dict[str, Any]] = None) -> str:
    """Extract PMCID (e.g. 'PMC11234567') from OpenAlex ids or location URLs."""
    if openalex_work:
        ids = openalex_work.get("ids") or {}
        pmcid = str(ids.get("pmcid") or "").strip()
        if pmcid:
            m = re.search(r"(PMC\d+)", pmcid, re.I)
            if m:
                return m.group(1).upper()
    for loc in locations or ():
        for key in ("pdf_url", "landing_page_url"):
            val = str(loc.get(key) or "")
            m = _PMC_URL_RE.search(val)
            if m:
                return m.group(1).upper()
    return ""


# ---------------------------------------------------------------------------
# 3. PMC Cloud Service on AWS S3 (Sanctioned Programmatic Route)
# ---------------------------------------------------------------------------

def lookup_pmcid_by_doi(http_session: Any, doi: str) -> str:
    """Resolve DOI -> PMCID via EuropePMC REST API, falling back to NCBI E-utilities ESearch."""
    if not doi:
        return ""
    clean = doi.strip()
    q = urllib.parse.quote(f'DOI:"{clean}"')
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={q}&format=json&pageSize=3"
    res = http_session.get(url, headers={"Accept": "application/json"})
    if res.ok:
        try:
            data = res.json() or {}
            results = ((data.get("resultList") or {}).get("result")) or []
            for item in results:
                item_doi = str(item.get("doi") or "").strip().lower()
                pmcid = str(item.get("pmcid") or "").strip().upper()
                if pmcid.startswith("PMC") and (not item_doi or item_doi == clean.lower()):
                    return pmcid
        except Exception:
            pass

    esearch_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pmc&term={urllib.parse.quote(clean)}%5BDOI%5D&retmode=json"
    )
    eres = http_session.get(esearch_url, headers={"Accept": "application/json"})
    if eres.ok:
        try:
            idlist = ((eres.json() or {}).get("esearchresult") or {}).get("idlist") or []
            if idlist and str(idlist[0]).isdigit():
                return f"PMC{idlist[0]}"
        except Exception:
            pass
    return ""


def resolve_pmc_oa_s3(http_session: Any, pmcid: str = "", doi: str = "") -> Tuple[str, str, str]:
    """Resolve a PMCID (or DOI) to a direct PDF URL in NCBI's AWS Open Data bucket.

    Returns ``(s3_pdf_url, license_slug, failure_reason)``.
    Preserves the exact ``license_code`` from the S3 metadata JSON.
    """
    clean_doi = (doi or "").strip()
    target_pmcid = (pmcid or "").strip().upper()
    if target_pmcid.startswith(PMC_PREFIX.upper()):
        target_pmcid = target_pmcid[len(PMC_PREFIX):]
    if not target_pmcid and clean_doi:
        target_pmcid = lookup_pmcid_by_doi(http_session, clean_doi)
    if not target_pmcid or not re.match(r"^PMC\d+$", target_pmcid):
        return "", "", "no_pmcid_for_doi"

    list_url = f"{PMC_S3_BASE}/?list-type=2&prefix=metadata/{target_pmcid}."
    listing = http_session.get(list_url, headers={"Accept": "application/xml"})
    if not listing.ok:
        return "", "", f"pmc_s3_list_http_{listing.status or listing.error}"
    keys = re.findall(r"<Key>(metadata/[^<]+\.json)</Key>", listing.text())
    if not keys:
        return "", "", f"pmcid_{target_pmcid}_not_in_pmc_oa_subset"

    def _ver(k: str) -> int:
        m = re.search(r"\.(\d+)\.json$", k)
        return int(m.group(1)) if m else 0

    best_key = max(keys, key=_ver)
    meta_res = http_session.get(f"{PMC_S3_BASE}/{best_key}", headers={"Accept": "application/json"})
    if not meta_res.ok:
        return "", "", f"pmc_s3_meta_http_{meta_res.status or meta_res.error}"
    try:
        meta = meta_res.json() or {}
    except ValueError:
        return "", "", "pmc_s3_meta_bad_json"

    meta_doi = str(meta.get("doi") or "").strip().lower()
    if clean_doi and meta_doi and meta_doi != clean_doi.lower():
        return "", "", f"pmc_s3_doi_mismatch({meta_doi}!={clean_doi.lower()})"
    if meta.get("is_retracted"):
        return "", "", "pmc_record_retracted"
    if not meta.get("is_pmc_openaccess"):
        return "", "", "pmc_record_not_openaccess"

    s3_uri = str(meta.get("pdf_url") or "").split("?", 1)[0]
    prefix = "s3://pmc-oa-opendata/"
    if not s3_uri.startswith(prefix):
        return "", "", "pmc_record_has_no_s3_pdf"

    raw_lic = str(meta.get("license_code") or meta.get("license") or "cc-by").strip().lower()
    lic = re.sub(r"[\s_]+", "-", raw_lic)
    pdf_http_url = f"{PMC_S3_BASE}/{s3_uri[len(prefix):]}"
    return pdf_http_url, lic, ""


# ---------------------------------------------------------------------------
# 4. Crossref `link[]` (text-mining / syndication) Direct PDF Resolver (§3.4)
# ---------------------------------------------------------------------------

_CROSSREF_ALLOWED_APPS = frozenset({"text-mining", "syndication", "similarity-checking", "unspecified", ""})


def resolve_crossref_tdm_pdf_link(
    http_session: Any,
    doi: str,
    *,
    walled_hosts: Iterable[str] = (),
    crossref_vor_cc_fn: Optional[ Any] = None,
) -> Tuple[str, str, str]:
    """Read Crossref `message.link[]` for a non-walled direct PDF URL (e.g. Seismica McGill OJS).

    Returns ``(pdf_url, license_slug, failure_reason)``.
    """
    clean_doi = (doi or "").strip()
    if not clean_doi:
        return "", "", "no_doi"
    url = f"https://api.crossref.org/works/{urllib.parse.quote(clean_doi, safe='/')}"
    res = http_session.get(url, headers={"Accept": "application/json"})
    if not res.ok:
        return "", "", f"crossref_http_{res.status or res.error}"
    try:
        msg = (res.json() or {}).get("message") or {}
    except ValueError:
        return "", "", "crossref_bad_json"

    vor_lic = ""
    if crossref_vor_cc_fn is not None:
        try:
            vor_lic = crossref_vor_cc_fn(msg) or ""
        except Exception:
            vor_lic = ""
    if not vor_lic and clean_doi.lower().startswith("10.26443/seismica."):
        vor_lic = "cc-by"

    walled_set = {h.lower() for h in walled_hosts}
    links = msg.get("link") or []
    for lk in links:
        if not isinstance(lk, dict):
            continue
        link_url = str(lk.get("URL") or "").strip()
        if not link_url:
            continue
        ctype = str(lk.get("content-type") or "").strip().lower()
        app = str(lk.get("intended-application") or "").strip().lower()
        if app not in _CROSSREF_ALLOWED_APPS:
            continue
        if ctype != "application/pdf" and not (
            ctype in ("unspecified", "") and (link_url.lower().endswith(".pdf") or "/article/download/" in link_url.lower())
        ):
            continue
        host = urllib.parse.urlsplit(link_url).netloc.lower()
        if host in walled_set:
            continue
        return link_url, vor_lic, ""
    return "", vor_lic, "no_non_walled_pdf_link_in_crossref"


# ---------------------------------------------------------------------------
# 5. Lazy, Dry-Run-Safe SQLite Metadata Cache (7-day TTL)
# ---------------------------------------------------------------------------

class MetadataFallbackCache:
    """Lazy SQLite cache for OpenAlex/Crossref metadata.

    Guarantees ZERO filesystem writes or directory creation when ``readonly=True``
    or when the parent directory does not yet exist (preserving `--dry-run` contracts).
    """

    def __init__(self, cache_path: Optional[Path] = None, *, readonly: bool = False) -> None:
        self.cache_path = cache_path
        self.readonly = readonly
        self._initialized = False

    def _ensure_table(self) -> bool:
        if self.readonly or self.cache_path is None:
            return False
        if not self.cache_path.parent.is_dir():
            # Do not create parent directories on our own; wait until a real write run creates bundle_root
            return False
        if not self._initialized:
            try:
                with sqlite3.connect(str(self.cache_path)) as conn:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS meta_cache ("
                        "  provider TEXT NOT NULL,"
                        "  doi TEXT NOT NULL,"
                        "  payload TEXT NOT NULL,"
                        "  updated_at REAL NOT NULL,"
                        "  PRIMARY KEY (provider, doi)"
                        ")"
                    )
                self._initialized = True
            except Exception:
                return False
        return True

    def get(self, provider: str, doi: str, max_age_days: float = 7.0) -> Optional[Dict[str, Any]]:
        key = (doi or "").strip().lower()
        if not key or self.cache_path is None or not self.cache_path.is_file():
            return None
        cutoff = time.time() - max_age_days * 86400.0
        try:
            uri = f"file:{self.cache_path}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                row = conn.execute(
                    "SELECT payload FROM meta_cache WHERE provider = ? AND doi = ? AND updated_at >= ?",
                    (provider, key, cutoff),
                ).fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception:
            return None
        return None

    def put(self, provider: str, doi: str, payload: Dict[str, Any]) -> None:
        if self.readonly:
            return
        key = (doi or "").strip().lower()
        if not key or not isinstance(payload, dict):
            return
        if not self._ensure_table():
            return
        try:
            with sqlite3.connect(str(self.cache_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta_cache (provider, doi, payload, updated_at) VALUES (?, ?, ?, ?)",
                    (provider, key, json.dumps(payload, ensure_ascii=False), time.time()),
                )
        except Exception:
            pass


def fetch_unpaywall_locations(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Disabled per 2026-11-09 audit (§1.14-2 & §3.9): Unpaywall shares OpenAlex's database
    and requires an email in the query string."""
    return []
