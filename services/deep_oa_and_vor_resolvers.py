#!/usr/bin/env python3
"""
deep_oa_and_vor_resolvers.py (v2.3 — Carbon Deep-Dive Edition)
==============================================================
High-precision, compliance-first resolvers for SeismoHub v2 (`services/fullbundle_harvest.py`).

Covers both Wave 1 (engineering blind-spot fixes) and Wave 2 (cross-version VoR mapping &
green-OA / public-domain discovery):
  1. Link-based identifier inference for `doi=""` rows (`nature.com/articles/*` -> `10.1038/*`,
     ETH Research Collection handles `20.500.11850/*`, HAL `hal-*`, arXiv).
  2. Nature Accelerated Article Preview (`{suffix}_reference.pdf`) fallback for Gold OA articles
     in press whose `.pdf` URL redirects to HTML.
  3. Springer (`10.1007/*`) PDF resolver (`link.springer.com/content/pdf/{doi}.pdf`), strictly
     subject to licence tiering + the `'All rights reserved'` TDM seal.
  4. Research Square (`10.21203/rs.3.*`) CC-BY preprint asset PDF resolver.
  5. Smart Zenodo PDF selector (deprioritizes `supplement/supp/appendix/poster/slides` files so
     main preprints/papers win even when supplements are larger in byte size).
  6. Multi-author surname enhanced Gate-4 identity check for preprints whose titles were refined
     upon journal submission (`0.35 <= title_overlap < 0.70` + >=3 co-author surnames verified).
  7. ESSOAr (`10.22541/essoar.*`) -> published journal VoR / report DOI resolver via Crossref
     `relation.is-preprint-of` + strict bidirectional title (>=0.80) & author surname verification.
  8. US Federal Public-Domain / Open Manuscript resolvers:
     - OSTI.gov (`www.osti.gov/api/v1/records` & `10.2172/*` -> `/servlets/purl/{osti_id}`)
     - USGS Publications Warehouse (`pubs.usgs.gov/pubs-services/publication`)
  9. Institutional & Preprint Green-OA resolvers (strict bidirectional title >=0.80 + author surname):
     - HAL Open Archive (`api.archives-ouvertes.fr/search/`)
     - GFZ Potsdam PubMan (`gfzpublic.gfz.de/rest/items/search` PUBLIC PDFs)
     - DataCite Meta-Resolver (`api.datacite.org/dois` covering Zenodo preprints, EarthArXiv,
       arXiv, ETH RC, KITopen, BORIS Unibe, and university DSpace repositories), with a generic
       DSpace 7 / `citation_pdf_url` landing-page extractor.
     - Semantic Scholar `openAccessPdf` (`resolve_s2_open_copy`): kept, but not wired into the candidate list.
"""
from __future__ import annotations

import html
import json
import re
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

_NATURE_ARTICLE_LINK_RE = re.compile(
    r"nature\.com/articles/((?:s|d|n)[0-9a-z]+-[0-9a-z-]+?)(?:\.pdf|_reference\.pdf|[/?#]|$)",
    re.I,
)
_ETHRC_HANDLE_RE = re.compile(r"20\.500\.11850/(\d+)(?:\.\d+)?", re.I)
_GENERIC_HANDLE_RE = re.compile(r"^(https?://[^/]+)/(?:handle|items)/([^/?#]+(?:/[^/?#]+)?)", re.I)
_HAL_ID_RE = re.compile(r"(?:hal\.science|archives-ouvertes\.fr)/((?:[a-z]+-)?\d{6,})(?:v\d+)?", re.I)
_RS_URL_RE = re.compile(r"https?://assets(?:-[a-z0-9]+)?\.researchsquare\.com/files/[^\s\"'<>]+\.pdf", re.I)
_SUPPLEMENT_NAME_RE = re.compile(
    r"(?:supp|supplement|supplementary|appendix|esm|moesm|_si\b|-si\b|poster|slides|presentation"
    r"|readme|code_doc|manual|user[-_]?guide)",
    re.I,
)
_DOC_README_ONLY_RE = re.compile(r"^(?:readme|license|changelog|install|instructions?)(?:[-_.\s].*)?\.pdf$", re.I)
_CONFERENCE_ABSTRACT_BANNER_RE = re.compile(
    r"(?:\bEGU\d{2}-\d+\b|EGU\s+General\s+Assembly\s+20\d{2}|10\.5194/egusphere-egu\d{2}-\d+)",
    re.I,
)
_META_CITATION_DOI_RE = re.compile(
    r'<meta\b[^>]*name=["\']citation_doi["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta\b[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']citation_doi["\']',
    re.I,
)
_META_CITATION_TITLE_RE = re.compile(
    r'<meta\b[^>]*name=["\']citation_title["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta\b[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']citation_title["\']',
    re.I,
)
_META_CITATION_PDF_RE = re.compile(
    r'<meta\b[^>]*name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta\b[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']citation_pdf_url["\']',
    re.I,
)
_DSPACE7_BITSTREAM_PATH_RE = re.compile(r"/bitstreams/([0-9a-f]{8}-[0-9a-f-]{27})/download", re.I)

NON_PAPER_DATACITE_TYPES = frozenset({
    "dataset", "software", "audiovisual", "image", "workflow",
    "collection", "model", "computationalnotebook", "physicalobject",
    "interactiveResource", "interactiveresource", "service", "sound", "event",
})


# ---------------------------------------------------------------------------
# Author & Title Matching Utilities
# ---------------------------------------------------------------------------

def parse_author_surnames(authors_raw: Any) -> List[str]:
    """Extract normalized ASCII lowercase surnames in author order from JSON list or string."""
    items: List[str] = []
    if isinstance(authors_raw, list):
        items = [str(x) for x in authors_raw if x]
    elif isinstance(authors_raw, str):
        s = authors_raw.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    items = [str(x) for x in parsed if x]
            except Exception:
                items = [x.strip() for x in s.split(",") if x.strip()]
        elif s:
            items = [x.strip() for x in s.split(";") if x.strip()]
    out: List[str] = []
    for raw in items:
        clean = raw.strip()
        if not clean:
            continue
        if "," in clean:
            sur = clean.split(",", 1)[0].strip()
        else:
            parts = clean.split()
            sur = parts[-1].strip() if parts else ""
        sur = unicodedata.normalize("NFKD", sur).encode("ascii", "ignore").decode("ascii")
        sur = re.sub(r"[^a-zA-Z-]", "", sur).lower().strip("-")
        if len(sur) >= 2 and sur not in out:
            out.append(sur)
    return out


def bidirectional_title_overlap(title_a: str, title_b: str, overlap_fn: Callable[[str, str], float]) -> float:
    if not title_a or not title_b:
        return 0.0
    return min(overlap_fn(title_a, title_b), overlap_fn(title_b, title_a))


def enhanced_identity_check(
    head_text: str,
    *,
    doi: str,
    title: str,
    authors_raw: Any,
    orig_identity_fn: Callable[..., Tuple[bool, str]],
    title_overlap_fn: Callable[[str, str], float],
) -> Tuple[bool, str]:
    """Gate 4 with conference-abstract guard + multi-author verification for retitled preprints."""
    clean_doi = (doi or "").strip().lower()
    # Reject 1-4p EGU General Assembly / conference abstracts masquerading under the same title as a journal paper!
    if head_text and _CONFERENCE_ABSTRACT_BANNER_RE.search(head_text) and "egusphere-egu" not in clean_doi:
        return False, "conference_abstract_not_full_paper(EGU_GA)"

    ok, reason = orig_identity_fn(head_text, doi=doi, title=title)
    if ok:
        return True, reason

    if not title or not head_text:
        return ok, reason

    overlap = title_overlap_fn(title, head_text)
    if overlap < 0.35:
        return ok, reason

    surnames = parse_author_surnames(authors_raw)
    # Require at least 3 distinct co-authors to avoid any single/pair-author collision across different papers
    if len(surnames) < 3:
        return ok, reason

    norm_head = unicodedata.normalize("NFKD", head_text).encode("ascii", "ignore").decode("ascii").lower()
    head_words = set(re.findall(r"[a-z]+", norm_head))

    def _surname_present(sur: str) -> bool:
        if "-" in sur:
            return sur in norm_head or all(p in head_words for p in sur.split("-") if len(p) >= 2)
        return sur in head_words

    matched = [s for s in surnames if _surname_present(s)]
    ratio = len(matched) / len(surnames)
    first_ok = _surname_present(surnames[0])
    last_ok = _surname_present(surnames[-1])

    if first_ok and last_ok and (len(matched) == len(surnames) or (len(surnames) >= 4 and ratio >= 0.85)):
        return True, f"title({overlap:.2f})+authors({len(matched)}/{len(surnames)})"

    return ok, reason


# ---------------------------------------------------------------------------
# Wave 1: Link-based Identifier Inference & Direct Resolvers
# ---------------------------------------------------------------------------

def infer_identifiers_from_link(doi: str, link: str) -> Dict[str, str]:
    """Extract canonical DOI / handle / HAL id from `papers.link` when `papers.doi` is blank."""
    clean_doi = (doi or "").strip()
    clean_link = (link or "").strip()
    inferred_doi = ""
    ethrc_handle_num = ""
    hal_id = ""

    m_eth = _ETHRC_HANDLE_RE.search(clean_link)
    if m_eth:
        ethrc_handle_num = m_eth.group(1)

    m_hal = _HAL_ID_RE.search(clean_link)
    if m_hal:
        hal_id = m_hal.group(1).lower()

    if not clean_doi and clean_link:
        m_nat = _NATURE_ARTICLE_LINK_RE.search(clean_link)
        if m_nat:
            inferred_doi = f"10.1038/{m_nat.group(1)}"

    return {
        "effective_doi": clean_doi or inferred_doi,
        "inferred_doi": inferred_doi,
        "ethrc_handle_num": ethrc_handle_num,
        "hal_id": hal_id,
    }


def nature_aap_pdf_url(doi: str) -> str:
    """Nature Portfolio Accelerated Article Preview ('Article in Press') direct PDF URL."""
    if "/" not in (doi or ""):
        return ""
    suffix = doi.split("/", 1)[1].strip()
    return f"https://www.nature.com/articles/{suffix}_reference.pdf" if suffix else ""


def springer_pdf_url(doi: str) -> str:
    """Springer (`10.1007/*`) direct content PDF URL."""
    clean = (doi or "").strip()
    return f"https://link.springer.com/content/pdf/{clean}.pdf" if clean.lower().startswith("10.1007/") else ""


def resolve_researchsquare_pdf(http_session: Any, doi: str = "", landing_url: str = "") -> Tuple[str, str, str]:
    """Resolve Research Square (`10.21203/rs.3.*`) landing page to its direct `assets*.researchsquare.com` PDF."""
    target = landing_url or (f"https://doi.org/{doi.strip()}" if doi else "")
    if not target:
        return "", "", "no_researchsquare_target"
    res = http_session.get(target)
    if not res.ok:
        return "", "", f"researchsquare_http_{res.status or res.error}"
    text = res.text()
    assets = _RS_URL_RE.findall(text)
    if assets:
        return html.unescape(assets[0]), "cc-by", ""
    for m in _META_CITATION_PDF_RE.finditer(text):
        u = html.unescape(m.group(1) or m.group(2) or "").strip()
        if u:
            return u, "cc-by", ""
    return "", "", "researchsquare_no_pdf_link"


def resolve_zenodo_smart(http_session: Any, doi: str) -> Tuple[str, str, str]:
    """Resolve Zenodo record PDF, prioritizing main manuscripts/preprints over larger supplement/poster PDFs."""
    clean = (doi or "").strip().lower()
    if "zenodo." not in clean:
        return "", "", "not_zenodo_doi"
    record_id = clean.split("zenodo.", 1)[1].strip()
    res = http_session.get(f"https://zenodo.org/api/records/{record_id}", headers={"Accept": "application/json"})
    if not res.ok:
        return "", "", f"zenodo_record_{res.status or res.error}"
    try:
        record = res.json() or {}
    except ValueError:
        return "", "", "zenodo_bad_json"
    meta = record.get("metadata") or {}
    rtype = str(((meta.get("resource_type") or {}).get("type")) or "?").lower()
    raw_pdfs = [f for f in (record.get("files") or []) if str(f.get("key", "")).lower().endswith(".pdf")]
    files = [f for f in raw_pdfs if not _DOC_README_ONLY_RE.match(Path(str(f.get("key") or "")).name)]
    if not files:
        if raw_pdfs:
            return "", "", "zenodo_only_readme_or_doc_pdf"
        return "", "", f"zenodo_record_has_no_pdf(resource_type={rtype})"

    # Sort non-supplement/non-poster PDFs first; within the same bucket, pick the largest PDF
    files.sort(
        key=lambda f: (
            bool(_SUPPLEMENT_NAME_RE.search(str(f.get("key") or ""))),
            -(int(f.get("size") or 0)),
        )
    )
    best = files[0]
    url = (best.get("links") or {}).get("self") or (
        f"https://zenodo.org/records/{record_id}/files/{urllib.parse.quote(str(best['key']))}?download=1"
    )
    licence = str(((meta.get("license") or {}).get("id")) or "").lower()
    return url, licence, ""


def resolve_ethrc_handle_or_doi(
    http_session: Any,
    *,
    doi: str,
    link: str,
    title: str,
    title_overlap_fn: Callable[[str, str], float],
    normalize_lic_fn: Callable[[str], str],
    out_backfill: Optional[Dict[str, str]] = None,
    openalex_locations: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[str, str, str]:
    """100% `robots.txt`-compliant ETH Research Collection resolver.

    Never touches `/server/api/discover/*` or `/server/api/core/items/*` (disallowed by ETH RC `robots.txt`).
    Uses ONLY allowed paths:
      - `https://www.research-collection.ethz.ch/handle/20.500.11850/<num>` (SSR HTML with `citation_pdf_url`)
      - `https://api.datacite.org/dois/10.3929/...` (external DataCite metadata -> handle URL)
      - `/server/api/core/bitstreams/<uuid>/content` (explicitly `Allow:`ed in ETH RC `robots.txt` + `Crawl-delay: 10`).
    """
    info = infer_identifiers_from_link(doi, link)
    handle_num = info["ethrc_handle_num"]
    clean_doi = (doi or "").strip()

    if not handle_num and openalex_locations:
        for loc in openalex_locations:
            for k in ("landing_page_url", "pdf_url"):
                u_cand = str((loc or {}).get(k) or "")
                m_h = _ETHRC_HANDLE_RE.search(u_cand)
                if m_h:
                    handle_num = m_h.group(1)
                    break
            if handle_num:
                break

    landing_url = ""
    if handle_num:
        landing_url = f"https://www.research-collection.ethz.ch/handle/20.500.11850/{handle_num}"
    elif clean_doi.lower().startswith("10.3929/"):
        dc_res = http_session.get(
            f"https://api.datacite.org/dois/{urllib.parse.quote(clean_doi, safe='/')}",
            headers={"Accept": "application/vnd.api+json"},
        )
        if dc_res.ok:
            try:
                attrs = ((dc_res.json() or {}).get("data") or {}).get("attributes") or {}
                landing_url = str(attrs.get("url") or "").strip()
            except Exception:
                landing_url = ""

    if not landing_url:
        return "", "", "ethrc_no_robots_allowed_handle_url"

    res = http_session.get(landing_url)
    if not res.ok:
        return "", "", f"ethrc_handle_http_{res.status or res.error}"
    if b"%PDF-" in res.data[:1024]:
        return res.final_url or landing_url, "ethrc-open", ""

    page_html = res.text()
    m_title = _META_CITATION_TITLE_RE.search(page_html)
    if m_title and title:
        rc_title = html.unescape(m_title.group(1) or m_title.group(2) or "").strip()
        if rc_title and not handle_num and bidirectional_title_overlap(title, rc_title, title_overlap_fn) < 0.65:
            return "", "", "ethrc_title_mismatch"

    if out_backfill is not None and not clean_doi:
        for m_d in _META_CITATION_DOI_RE.finditer(page_html):
            raw_d = html.unescape(m_d.group(1) or m_d.group(2) or "").strip()
            raw_d = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw_d, flags=re.I).strip()
            if raw_d.startswith("10."):
                out_backfill["doi"] = raw_d
                break

    bitstream_uuids = _DSPACE7_BITSTREAM_PATH_RE.findall(page_html)
    if bitstream_uuids:
        uuid = bitstream_uuids[0]
        return f"https://www.research-collection.ethz.ch/server/api/core/bitstreams/{uuid}/content", "ethrc-open", ""

    for m_pdf in _META_CITATION_PDF_RE.finditer(page_html):
        u_pdf = html.unescape(m_pdf.group(1) or m_pdf.group(2) or "").strip()
        if u_pdf:
            m_u = _DSPACE7_BITSTREAM_PATH_RE.search(u_pdf)
            if m_u:
                return f"https://www.research-collection.ethz.ch/server/api/core/bitstreams/{m_u.group(1)}/content", "ethrc-open", ""
            return urllib.parse.urljoin(res.final_url or landing_url, u_pdf), "ethrc-open", ""

    return "", "", "ethrc_item_has_no_pdf_bitstream"


# ---------------------------------------------------------------------------
# Generic Institutional Repository Landing Page -> Direct PDF Extractor
# ---------------------------------------------------------------------------

def extract_pdf_from_repo_landing(
    http_session: Any,
    landing_url: str,
    *,
    title: str = "",
    title_overlap_fn: Optional[Callable[[str, str], float]] = None,
    walled_hosts: Iterable[str] = (),
) -> Tuple[str, str]:
    """Resolve an institutional repository landing URL (DSpace 7, EPrints, KITopen, etc.) to a direct PDF URL."""
    u = (landing_url or "").strip()
    if not u:
        return "", "empty_repo_url"
    walled_set = {h.lower() for h in walled_hosts}
    parsed = urllib.parse.urlsplit(u)
    host = parsed.netloc.lower()
    if not host or host in walled_set or host in ("doi.org", "dx.doi.org"):
        return "", "walled_or_bare_doi_host"

    if u.lower().endswith(".pdf") or "/server/api/core/bitstreams/" in u.lower():
        return u, ""

    res = http_session.get(u)
    if not res.ok:
        return "", f"repo_landing_http_{res.status or res.error}"

    # If the landing URL already redirected directly to a PDF stream, return final_url
    if b"%PDF-" in res.data[:1024]:
        return res.final_url or u, ""

    text = res.text()
    base = res.final_url or u
    base_parts = urllib.parse.urlsplit(base)
    origin = f"{base_parts.scheme}://{base_parts.netloc}"

    # 1. Explicit Highwire Press `citation_pdf_url` meta tag (KITopen, EPrints, OJS, etc.)
    for m in _META_CITATION_PDF_RE.finditer(text):
        raw_pdf = html.unescape(m.group(1) or m.group(2) or "").strip()
        if raw_pdf:
            full_pdf = urllib.parse.urljoin(base, raw_pdf)
            if urllib.parse.urlsplit(full_pdf).netloc.lower() not in walled_set:
                return full_pdf, ""

    # 2. DSpace 7 `/bitstreams/<uuid>/download` links in SSR HTML (Cambridge Apollo, etc.)
    m_ds = _DSPACE7_BITSTREAM_PATH_RE.search(text)
    if m_ds:
        return f"{origin}/server/api/core/bitstreams/{m_ds.group(1)}/content", ""

    # 3. DSpace 7 Angular SPA fallback (`/handle/<prefix>/<num>` -> `/server/api/discover/search/objects`)
    m_hdl = _GENERIC_HANDLE_RE.match(u)
    if m_hdl:
        hdl_tail = m_hdl.group(2).split("/")[-1]
        if hdl_tail.isdigit():
            api_search = f"{origin}/server/api/discover/search/objects?query={hdl_tail}&size=3"
            s_res = http_session.get(api_search, headers={"Accept": "application/json"})
            if s_res.ok:
                try:
                    objs = s_res.json()["_embedded"]["searchResult"]["_embedded"]["objects"]
                except Exception:
                    objs = []
                for obj in objs or []:
                    item = (obj.get("_embedded") or {}).get("indexableObject") or {}
                    uuid = item.get("uuid")
                    rc_title = (((item.get("metadata") or {}).get("dc.title") or [{}])[0] or {}).get("value", "")
                    if title and rc_title and title_overlap_fn:
                        if bidirectional_title_overlap(title, rc_title, title_overlap_fn) < 0.65:
                            continue
                    if not uuid:
                        continue
                    b_res = http_session.get(f"{origin}/server/api/core/items/{uuid}/bundles", headers={"Accept": "application/json"})
                    if not b_res.ok:
                        continue
                    try:
                        bundles = b_res.json()["_embedded"]["bundles"]
                    except Exception:
                        bundles = []
                    for b in bundles:
                        if b.get("name") != "ORIGINAL":
                            continue
                        href = ((b.get("_links") or {}).get("bitstreams") or {}).get("href")
                        if not href:
                            continue
                        st_res = http_session.get(href, headers={"Accept": "application/json"})
                        if not st_res.ok:
                            continue
                        try:
                            bitstreams = st_res.json()["_embedded"]["bitstreams"]
                        except Exception:
                            bitstreams = []
                        pdfs = [bs for bs in bitstreams if str(bs.get("name", "")).lower().endswith(".pdf")]
                        pdfs.sort(key=lambda bs: bool(_SUPPLEMENT_NAME_RE.search(str(bs.get("name", "")))))
                        for bs in pdfs:
                            c_href = ((bs.get("_links") or {}).get("content") or {}).get("href")
                            if c_href:
                                return c_href, ""
    return "", "repo_landing_no_open_pdf_bitstream"


# ---------------------------------------------------------------------------
# Wave 2: ESSOAr -> Published Journal VoR / Report DOI Resolver
# ---------------------------------------------------------------------------

_ACTIONABLE_VOR_PREFIXES = (
    "10.1029/", "10.1002/", "10.1111/",  # Wiley / AGU TDM
    "10.5194/",                          # Copernicus / EGU Gold OA
    "10.1016/",                          # Elsevier OA / TDM XML
    "10.1038/", "10.1007/",              # Nature / Springer
    "10.26443/",                         # Seismica Gold OA
    "10.2172/",                          # OSTI US DOE Technical Report
    "10.48550/", "10.31223/",            # arXiv / EarthArXiv
    "10.3929/",                          # ETH Research Collection
)


def discover_essoar_published_doi(
    http_session: Any,
    essoar_doi: str,
    title: str,
    authors_raw: Any,
    title_overlap_fn: Callable[[str, str], float],
) -> Tuple[str, str]:
    """Find a published journal VoR or open report DOI for an ESSOAr preprint (`10.22541/essoar.*`).

    Returns `(published_doi, discovery_source)` or `("", reason)`.
    """
    clean_doi = (essoar_doi or "").strip()
    if not clean_doi.lower().startswith("10.22541/"):
        return "", "not_essoar"

    # Step 1: Official Crossref `relation.is-preprint-of` deposit
    cr_url = f"https://api.crossref.org/works/{urllib.parse.quote(clean_doi, safe='/')}"
    res = http_session.get(cr_url, headers={"Accept": "application/json"})
    if res.ok:
        try:
            msg = (res.json() or {}).get("message") or {}
            rel = msg.get("relation") or {}
            for item in (rel.get("is-preprint-of") or []):
                if str(item.get("id-type") or "").lower() == "doi":
                    target_doi = str(item.get("id") or "").strip()
                    if target_doi and not target_doi.lower().startswith("10.22541/"):
                        return target_doi, "crossref_is_preprint_of"
        except Exception:
            pass

    # Step 2: Crossref bibliographic Title + First-Author search with strict bidirectional verification
    clean_title = (title or "").strip()
    if len(clean_title) < 20:
        return "", "title_too_short"
    surnames = parse_author_surnames(authors_raw)
    surnames_set = set(surnames)
    q_params: Dict[str, str] = {
        "query.title": clean_title,
        "rows": "5",
    }
    if surnames:
        q_params["query.author"] = surnames[0]
    search_url = "https://api.crossref.org/works?" + urllib.parse.urlencode(q_params)
    s_res = http_session.get(search_url, headers={"Accept": "application/json"})
    if not s_res.ok:
        return "", f"crossref_search_{s_res.status or s_res.error}"
    try:
        items = ((s_res.json() or {}).get("message") or {}).get("items") or []
    except Exception:
        return "", "crossref_search_bad_json"

    for it in items:
        cand_doi = str(it.get("DOI") or "").strip()
        low_doi = cand_doi.lower()
        if not cand_doi or low_doi.startswith("10.22541/"):
            continue
        # Skip EGU conference abstracts (`egusphere-egu26-...`) which have no full-paper PDF
        if "egusphere-egu" in low_doi or re.search(r"\.(?:v\d+\.\d+)$", low_doi):
            continue
        if not low_doi.startswith(_ACTIONABLE_VOR_PREFIXES):
            continue
        cand_title = ((it.get("title") or [""])[0] or "").strip()
        if not cand_title or bidirectional_title_overlap(clean_title, cand_title, title_overlap_fn) < 0.80:
            continue
        cand_surnames: Set[str] = set()
        for a in (it.get("author") or []):
            fam = unicodedata.normalize("NFKD", str(a.get("family") or "")).encode("ascii", "ignore").decode("ascii")
            fam = re.sub(r"[^a-zA-Z-]", "", fam).lower().strip("-")
            if fam:
                cand_surnames.add(fam)
        if surnames_set and cand_surnames and not (surnames_set & cand_surnames):
            continue
        return cand_doi, "crossref_title_author_vor"

    return "", "no_published_vor_found"


# ---------------------------------------------------------------------------
# Wave 2: US Public-Domain & Multi-Repository Green OA Resolvers
# ---------------------------------------------------------------------------

def resolve_osti_gov(
    http_session: Any,
    doi: str,
    title: str = "",
    authors_raw: Any = None,
    title_overlap_fn: Optional[Callable[[str, str], float]] = None,
) -> Tuple[str, str, str]:
    """Resolve US DOE OSTI.gov open full-text PDF (`https://www.osti.gov/servlets/purl/{osti_id}`)."""
    clean_doi = (doi or "").strip()
    if clean_doi.lower().startswith("10.2172/"):
        osti_id = clean_doi.split("/", 1)[1].strip()
        if osti_id.isdigit():
            return f"https://www.osti.gov/servlets/purl/{osti_id}", "public-domain", ""

    if clean_doi:
        api_url = f"https://www.osti.gov/api/v1/records?doi={urllib.parse.quote(clean_doi)}"
        res = http_session.get(api_url, headers={"Accept": "application/json"})
        if res.ok and res.data.strip():
            try:
                records = res.json() or []
            except Exception:
                records = []
            for rec in records:
                rec_doi = str(rec.get("doi") or "").strip().lower()
                if rec_doi and rec_doi != clean_doi.lower():
                    continue
                for lk in (rec.get("links") or []):
                    if lk.get("rel") == "fulltext" and lk.get("href"):
                        return str(lk["href"]), "public-domain", ""

    # Fallback: strict Title + First Author check on OSTI for national-lab preprints/reports
    clean_title = (title or "").strip()
    surnames = parse_author_surnames(authors_raw)
    if len(clean_title) >= 25 and surnames and title_overlap_fn is not None:
        params = {"title": clean_title, "author": surnames[0], "rows": "3"}
        t_url = "https://www.osti.gov/api/v1/records?" + urllib.parse.urlencode(params)
        t_res = http_session.get(t_url, headers={"Accept": "application/json"})
        if t_res.ok and t_res.data.strip():
            try:
                records = t_res.json() or []
            except Exception:
                records = []
            for rec in records:
                rt = str(rec.get("title") or "").strip()
                if not rt or bidirectional_title_overlap(clean_title, rt, title_overlap_fn) < 0.82:
                    continue
                for lk in (rec.get("links") or []):
                    if lk.get("rel") == "fulltext" and lk.get("href"):
                        return str(lk["href"]), "public-domain", ""

    return "", "", "osti_no_fulltext_record"


def resolve_usgs_pubs(http_session: Any, doi: str, walled_hosts: Iterable[str] = ()) -> Tuple[str, str, str]:
    """Resolve US Geological Survey Publications Warehouse public-domain full-text PDF links."""
    clean_doi = (doi or "").strip()
    if not clean_doi:
        return "", "", "no_doi"
    walled_set = {h.lower() for h in walled_hosts} | {"doi.org", "dx.doi.org"}
    url = f"https://pubs.usgs.gov/pubs-services/publication?doi={urllib.parse.quote(clean_doi)}&mimeType=json"
    res = http_session.get(url, headers={"Accept": "application/json"})
    if not res.ok:
        return "", "", f"usgs_http_{res.status or res.error}"
    try:
        records = (res.json() or {}).get("records") or []
    except Exception:
        return "", "", "usgs_bad_json"

    for rec in records:
        rec_doi = str(rec.get("doi") or "").strip().lower()
        if rec_doi and rec_doi != clean_doi.lower():
            continue
        for lk in (rec.get("links") or []):
            ltype = str(((lk.get("type") or {}).get("text")) or "").lower()
            lurl = str(lk.get("url") or "").strip()
            if not lurl or "thumbnail" in ltype or lurl.lower().endswith((".jpg", ".png", ".gif")):
                continue
            host = urllib.parse.urlsplit(lurl).netloc.lower()
            if not host or host in walled_set:
                continue
            if lurl.lower().endswith(".pdf") or "document" in ltype or "manuscript" in ltype or "pdf" in ltype:
                return lurl, "public-domain", ""
    return "", "", "usgs_no_unwalled_document_pdf"


def resolve_hal_open_archive(
    http_session: Any,
    *,
    doi: str,
    link: str,
    title: str,
    authors_raw: Any,
    title_overlap_fn: Callable[[str, str], float],
) -> Tuple[str, str, str]:
    """Resolve open full-text PDF from French HAL (`api.archives-ouvertes.fr`) by halId, DOI, or Title+Author."""
    info = infer_identifiers_from_link(doi, link)
    hal_id = info["hal_id"]
    clean_doi = (doi or "").strip()

    # 1. Direct HAL ID from `papers.link` (for no-DOI HAL rows)
    if hal_id:
        u = (
            f"https://api.archives-ouvertes.fr/search/?q=halId_s:{urllib.parse.quote(hal_id)}"
            "&fl=halId_s,doiId_s,fileMain_s,openAccess_bool,docType_s&rows=2&wt=json"
        )
        r = http_session.get(u, headers={"Accept": "application/json"})
        if r.ok:
            try:
                docs = ((r.json() or {}).get("response") or {}).get("docs") or []
            except Exception:
                docs = []
            for d in docs:
                dtype = str(d.get("docType_s") or "").upper()
                if dtype in ("SOFTWARE", "POSTER", "PRES", "VIDEO", "IMG", "SON", "MAP"):
                    return "", "", f"hal_record_non_paper(docType={dtype})"
                if d.get("fileMain_s"):
                    return str(d["fileMain_s"]), "hal-open-archive", ""
            return "", "", "hal_record_has_no_open_file"

    # 2. Exact DOI lookup in HAL
    if clean_doi:
        q_doi = urllib.parse.quote(f'doiId_s:"{clean_doi}"')
        u = f"https://api.archives-ouvertes.fr/search/?q={q_doi}&fq=submitType_s:file&fl=halId_s,docType_s,fileMain_s&rows=2&wt=json"
        r = http_session.get(u, headers={"Accept": "application/json"})
        if r.ok:
            try:
                docs = ((r.json() or {}).get("response") or {}).get("docs") or []
            except Exception:
                docs = []
            for d in docs:
                if str(d.get("docType_s") or "").upper() in ("SOFTWARE", "POSTER", "PRES"):
                    continue
                if d.get("fileMain_s"):
                    return str(d["fileMain_s"]), "hal-open-archive", ""

    # 3. Strict Title + First-Author Surname lookup in HAL (restricted to full articles/preprints/theses,
    #    excluding conference abstracts `COMM`/`POSTER` when resolving a journal DOI!)
    clean_title = (title or "").strip()
    surnames = parse_author_surnames(authors_raw)
    if len(clean_title) >= 25 and surnames:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", clean_title) if len(w) >= 4][:5]
        if len(words) >= 3:
            q_expr = (
                " AND ".join([f"title_t:{w}" for w in words] + [f"authLastName_t:{surnames[0]}"])
                + " AND docType_s:(ART OR UNDEFINED OR REPORT OR THESE OR HDR OR COUV)"
            )
            u = (
                "https://api.archives-ouvertes.fr/search/?"
                + urllib.parse.urlencode({
                    "q": q_expr,
                    "fq": "submitType_s:file",
                    "fl": "halId_s,title_s,authLastName_s,fileMain_s",
                    "rows": "3",
                    "wt": "json",
                })
            )
            r = http_session.get(u, headers={"Accept": "application/json"})
            if r.ok:
                try:
                    docs = ((r.json() or {}).get("response") or {}).get("docs") or []
                except Exception:
                    docs = []
                for d in docs:
                    ht = ((d.get("title_s") or [""])[0] or "").strip()
                    if ht and bidirectional_title_overlap(clean_title, ht, title_overlap_fn) >= 0.80 and d.get("fileMain_s"):
                        return str(d["fileMain_s"]), "hal-open-archive", ""

    return "", "", "hal_no_open_file"


def resolve_gfz_pubman(
    http_session: Any,
    doi: str,
    title: str,
    authors_raw: Any,
    title_overlap_fn: Callable[[str, str], float],
) -> Tuple[str, str, str]:
    """Resolve PUBLIC full-text PDFs hosted on GFZ Potsdam PubMan (`gfzpublic.gfz.de`) via `PoliteSession.post_json`."""
    clean_doi = (doi or "").strip()
    if not clean_doi and len((title or "").strip()) < 25:
        return "", "", "gfz_insufficient_metadata"

    endpoint = "https://gfzpublic.gfz.de/rest/items/search?format=json"
    must_clauses: List[Dict[str, Any]] = []
    if clean_doi:
        must_clauses.append({"match_phrase": {"metadata.identifiers.id": clean_doi}})
    else:
        must_clauses.append({"match_phrase": {"metadata.title": title.strip()}})

    records: List[Dict[str, Any]] = []
    post_fn = getattr(http_session, "post_json", None)
    if callable(post_fn):
        res = post_fn(endpoint, payload={"query": {"bool": {"must": must_clauses}}, "size": 3})
        if res.ok:
            try:
                records = (res.json() or {}).get("records") or []
            except Exception:
                records = []
    else:
        return "", "", "gfz_http_session_has_no_post_json"

    for rec in records:
        d = rec.get("data") or {}
        rec_title = str(((d.get("metadata") or {}).get("title")) or "").strip()
        if title and rec_title and bidirectional_title_overlap(title, rec_title, title_overlap_fn) < 0.70:
            continue
        files = [
            f for f in (d.get("files") or [])
            if f.get("visibility") == "PUBLIC"
            and f.get("storage") == "INTERNAL_MANAGED"
            and (str(f.get("mimeType") or "").endswith("pdf") or str(f.get("name") or "").lower().endswith(".pdf"))
            and f.get("content")
        ]
        files.sort(key=lambda f: bool(_SUPPLEMENT_NAME_RE.search(str(f.get("name") or ""))))
        if files:
            content_path = str(files[0]["content"])
            pdf_url = content_path if content_path.startswith("http") else f"https://gfzpublic.gfz.de{content_path}"
            return pdf_url, "gfz-open-repo", ""

    return "", "", "gfz_no_public_pdf"


def resolve_s2_open_copy(
    http_session: Any,
    doi: str,
    title: str,
    title_overlap_fn: Callable[[str, str], float],
    walled_hosts: Iterable[str] = (),
) -> Tuple[str, str, str]:
    """Check Semantic Scholar Graph API for an unlinked ArXiv ID or institutional green-OA repository PDF.

    Not wired into the candidate list: the `s2_oa` channel name was dropped on 2026-11-13 (27 attempts in the
    2026-11-10 runs, no copy found)."""
    clean_doi = (doi or "").strip()
    if not clean_doi:
        return "", "", "no_doi"
    walled_set = {h.lower() for h in walled_hosts} | {"doi.org", "dx.doi.org"}
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(clean_doi, safe='/')}"
        "?fields=externalIds,openAccessPdf"
    )
    res = http_session.get(url, headers={"Accept": "application/json"})
    if not res.ok:
        return "", "", f"s2_http_{res.status or res.error}"
    try:
        data = res.json() or {}
    except Exception:
        return "", "", "s2_bad_json"

    ext = data.get("externalIds") or {}
    arxiv_id = str(ext.get("ArXiv") or "").strip()
    if arxiv_id:
        arxiv_clean = re.sub(r"v\d+$", "", arxiv_id)
        return f"https://export.arxiv.org/pdf/{arxiv_clean}", "arxiv-preprint", ""

    oap = (data.get("openAccessPdf") or {}).get("url") or ""
    oap = str(oap).strip()
    if not oap:
        return "", "", "s2_no_open_access_pdf"
    host = urllib.parse.urlsplit(oap).netloc.lower()
    if not host or host in walled_set:
        return "", "", f"s2_pdf_walled({host})"

    direct_pdf, why = extract_pdf_from_repo_landing(
        http_session,
        oap,
        title=title,
        title_overlap_fn=title_overlap_fn,
        walled_hosts=walled_set,
    )
    if direct_pdf:
        return direct_pdf, "green-oa-repo", ""
    return "", "", why or "s2_landing_no_pdf"


def resolve_datacite_open_copy(
    http_session: Any,
    *,
    doi: str,
    title: str,
    authors_raw: Any,
    title_overlap_fn: Callable[[str, str], float],
    normalize_lic_fn: Callable[[str], str],
    resolve_eartharxiv_fn: Optional[Callable[[str], Tuple[str, str, str]]] = None,
    walled_hosts: Iterable[str] = (),
) -> Tuple[str, str, str]:
    """Search DataCite across global preprint & university repositories by strict Title + First Author."""
    clean_title = (title or "").strip()
    surnames = parse_author_surnames(authors_raw)
    if len(clean_title) < 25 or not surnames:
        return "", "", "datacite_insufficient_metadata"
    words = [w for w in re.findall(r"[A-Za-z0-9]+", clean_title) if len(w) >= 4][:5]
    if len(words) < 3:
        return "", "", "datacite_few_title_words"

    q_expr = " AND ".join([f"titles.title:{w}" for w in words] + [f"creators.name:{surnames[0]}"])
    url = "https://api.datacite.org/dois?" + urllib.parse.urlencode({"query": q_expr, "page[size]": "5"})
    res = http_session.get(url, headers={"Accept": "application/vnd.api+json"})
    if not res.ok:
        return "", "", f"datacite_http_{res.status or res.error}"
    try:
        items = (res.json() or {}).get("data") or []
    except Exception:
        return "", "", "datacite_bad_json"

    my_doi = (doi or "").strip().lower()
    surnames_set = set(surnames)
    for item in items:
        attr = item.get("attributes") or {}
        cand_doi = str(attr.get("doi") or "").strip()
        low_cand = cand_doi.lower()
        if not cand_doi or low_cand == my_doi:
            continue
        rtype = str(((attr.get("types") or {}).get("resourceTypeGeneral")) or "").strip().lower()
        if rtype in NON_PAPER_DATACITE_TYPES:
            continue
        cand_title = str(((attr.get("titles") or [{}])[0] or {}).get("title") or "").strip()
        if not cand_title or bidirectional_title_overlap(clean_title, cand_title, title_overlap_fn) < 0.80:
            continue
        # Verify co-author surname overlap
        cand_surs: Set[str] = set()
        for cr in (attr.get("creators") or []):
            fam = str(cr.get("familyName") or cr.get("name") or "").strip()
            if "," in fam:
                fam = fam.split(",", 1)[0].strip()
            elif " " in fam:
                fam = fam.split()[-1].strip()
            fam = unicodedata.normalize("NFKD", fam).encode("ascii", "ignore").decode("ascii")
            fam = re.sub(r"[^a-zA-Z-]", "", fam).lower().strip("-")
            if fam:
                cand_surs.add(fam)
        if surnames_set and cand_surs and not (surnames_set & cand_surs):
            continue

        # Dispatch to the clean direct PDF resolver for this repository DOI
        if low_cand.startswith("10.48550/arxiv."):
            aid = re.sub(r"v\d+$", "", low_cand[len("10.48550/arxiv."):])
            return f"https://export.arxiv.org/pdf/{aid}", "arxiv-preprint", ""
        if low_cand.startswith("10.5281/zenodo."):
            u, lic, _ = resolve_zenodo_smart(http_session, cand_doi)
            if u:
                return u, lic or "zenodo-open", ""
            continue
        if low_cand.startswith("10.31223/") and resolve_eartharxiv_fn is not None:
            u, lic, _ = resolve_eartharxiv_fn(f"https://doi.org/{cand_doi}")
            if u:
                return u, lic or "eartharxiv-preprint", ""
            continue
        if low_cand.startswith("10.3929/"):
            u, lic, _ = resolve_ethrc_handle_or_doi(
                http_session,
                doi=cand_doi,
                link=str(attr.get("url") or ""),
                title=clean_title,
                title_overlap_fn=title_overlap_fn,
                normalize_lic_fn=normalize_lic_fn,
            )
            if u:
                return u, lic or "ethrc-open", ""
            continue

        landing = str(attr.get("url") or "").strip()
        if landing:
            u, _ = extract_pdf_from_repo_landing(
                http_session,
                landing,
                title=clean_title,
                title_overlap_fn=title_overlap_fn,
                walled_hosts=walled_hosts,
            )
            if u:
                rights = attr.get("rightsList") or []
                lic_raw = str((rights[0].get("rightsIdentifier") or rights[0].get("rights") or "") if rights else "")
                return u, normalize_lic_fn(lic_raw) or "datacite-open-repo", ""

    return "", "", "datacite_no_verified_open_pdf"


def resolve_crossref_posted_preprint(
    http_session: Any,
    *,
    doi: str,
    title: str,
    authors_raw: Any,
    title_overlap_fn: Callable[[str, str], float],
    copernicus_url_fn: Callable[[str], Tuple[str, str]],
    resolve_eartharxiv_fn: Optional[Callable[[str], Tuple[str, str, str]]] = None,
) -> Tuple[str, str, str]:
    """Reverse-lookup non-walled preprints (EarthArXiv `10.31223/*`, EGUsphere `10.5194/*`,
    Research Square `10.21203/*`) registered in Crossref as `type:posted-content`."""
    clean_doi = (doi or "").strip()
    # 1. Check Crossref `relation.has-preprint` on the journal DOI itself
    if clean_doi:
        r_rel = http_session.get(
            f"https://api.crossref.org/works/{urllib.parse.quote(clean_doi, safe='/')}",
            headers={"Accept": "application/json"},
        )
        if r_rel.ok:
            try:
                rel = ((r_rel.json() or {}).get("message") or {}).get("relation") or {}
                for item in (rel.get("has-preprint") or []):
                    if str(item.get("id-type") or "").lower() == "doi":
                        pdoi = str(item.get("id") or "").strip()
                        low_p = pdoi.lower()
                        if low_p.startswith("10.31223/") and resolve_eartharxiv_fn is not None:
                            u, lic, _ = resolve_eartharxiv_fn(f"https://doi.org/{pdoi}")
                            if u:
                                return u, lic or "eartharxiv-preprint", ""
                        if low_p.startswith("10.5194/"):
                            u, _ = copernicus_url_fn(pdoi)
                            if u:
                                return u, "cc-by", ""
                        if low_p.startswith("10.21203/"):
                            u, lic, _ = resolve_researchsquare_pdf(http_session, doi=pdoi)
                            if u:
                                return u, lic or "cc-by", ""
            except Exception:
                pass

    # 2. Reverse search Crossref `type:posted-content` by strict Title (>=0.80) + Author surname
    clean_title = (title or "").strip()
    surnames = parse_author_surnames(authors_raw)
    if len(clean_title) < 25 or not surnames:
        return "", "", "crossref_preprint_insufficient_metadata"

    surnames_set = set(surnames)
    q_url = "https://api.crossref.org/works?" + urllib.parse.urlencode({
        "filter": "type:posted-content",
        "query.title": clean_title,
        "query.author": surnames[0],
        "rows": "4",
    })
    res = http_session.get(q_url, headers={"Accept": "application/json"})
    if not res.ok:
        return "", "", f"crossref_preprint_http_{res.status or res.error}"
    try:
        items = ((res.json() or {}).get("message") or {}).get("items") or []
    except Exception:
        return "", "", "crossref_preprint_bad_json"

    for it in items:
        pdoi = str(it.get("DOI") or "").strip()
        low_p = pdoi.lower()
        if not pdoi or low_p == clean_doi.lower() or low_p.startswith("10.22541/") or "egusphere-egu" in low_p:
            continue
        pt = ((it.get("title") or [""])[0] or "").strip()
        if not pt or bidirectional_title_overlap(clean_title, pt, title_overlap_fn) < 0.80:
            continue
        cand_surs: Set[str] = set()
        for a in (it.get("author") or []):
            fam = unicodedata.normalize("NFKD", str(a.get("family") or "")).encode("ascii", "ignore").decode("ascii")
            fam = re.sub(r"[^a-zA-Z-]", "", fam).lower().strip("-")
            if fam:
                cand_surs.add(fam)
        if surnames_set and cand_surs and not (surnames_set & cand_surs):
            continue

        if low_p.startswith("10.31223/") and resolve_eartharxiv_fn is not None:
            u, lic, _ = resolve_eartharxiv_fn(f"https://doi.org/{pdoi}")
            if u:
                return u, lic or "eartharxiv-preprint", ""
        elif low_p.startswith("10.5194/"):
            u, _ = copernicus_url_fn(pdoi)
            if u:
                return u, "cc-by", ""
        elif low_p.startswith("10.21203/"):
            u, lic, _ = resolve_researchsquare_pdf(http_session, doi=pdoi)
            if u:
                return u, lic or "cc-by", ""

    return "", "", "crossref_no_unwalled_preprint"


def resolve_caltech_authors(
    http_session: Any,
    *,
    doi: str,
    title: str,
    authors_raw: Any,
    title_overlap_fn: Callable[[str, str], float],
) -> Tuple[str, str, str]:
    """Resolve open full-text PDFs from CaltechAUTHORS (`authors.library.caltech.edu` InvenioRDM API).

    Crucial for multi-decade foundational seismology papers from the Caltech Seismological Laboratory
    (Gutenberg, Richter, Benioff, Kanamori, Anderson, Helmberger, Simons, Hauksson, Clayton, Zhan, Ross).
    """
    clean_doi = (doi or "").strip()
    clean_title = (title or "").strip()
    queries: List[str] = []
    if clean_doi:
        queries.append(f'"{clean_doi}"')
    if len(clean_title) >= 18:
        queries.append(f'"{clean_title[:140]}"')
    if not queries:
        return "", "", "caltech_insufficient_metadata"

    surnames_set = set(parse_author_surnames(authors_raw))
    for q_raw in queries:
        url = "https://authors.library.caltech.edu/api/records?" + urllib.parse.urlencode({"q": q_raw, "size": "3"})
        res = http_session.get(url, headers={"Accept": "application/json"})
        if not res.ok:
            continue
        try:
            hits = (((res.json() or {}).get("hits") or {}).get("hits")) or []
        except Exception:
            hits = []
        for h in hits:
            meta = h.get("metadata") or {}
            ht = str(meta.get("title") or "").strip()
            if clean_title and ht and bidirectional_title_overlap(clean_title, ht, title_overlap_fn) < 0.80:
                continue
            if surnames_set:
                c_surs: Set[str] = set()
                for cr in (meta.get("creators") or []):
                    po = (cr.get("person_or_org") or {})
                    fam = str(po.get("family_name") or po.get("name") or "").strip()
                    if "," in fam:
                        fam = fam.split(",", 1)[0].strip()
                    fam = unicodedata.normalize("NFKD", fam).encode("ascii", "ignore").decode("ascii")
                    fam = re.sub(r"[^a-zA-Z-]", "", fam).lower().strip("-")
                    if fam:
                        c_surs.add(fam)
                if c_surs and not (surnames_set & c_surs):
                    continue
            files_obj = h.get("files") or {}
            if not files_obj.get("enabled"):
                continue
            entries = files_obj.get("entries") or {}
            pdf_keys = [
                k for k in entries.keys()
                if str(k).lower().endswith(".pdf") and not _DOC_README_ONLY_RE.match(Path(str(k)).name)
            ]
            pdf_keys.sort(key=lambda k: bool(_SUPPLEMENT_NAME_RE.search(str(k))))
            rec_id = str(h.get("id") or "").strip()
            if pdf_keys and rec_id:
                chosen = urllib.parse.quote(str(pdf_keys[0]))
                return (
                    f"https://authors.library.caltech.edu/api/records/{rec_id}/files/{chosen}/content",
                    "caltech-open-repo",
                    "",
                )
    return "", "", "caltech_no_open_pdf"


def resolve_unified_green_oa(
    http_session: Any,
    *,
    doi: str,
    link: str,
    title: str,
    authors_raw: Any,
    institutions_raw: str,
    title_overlap_fn: Callable[[str, str], float],
    normalize_lic_fn: Callable[[str], str],
    copernicus_url_fn: Callable[[str], Tuple[str, str]],
    resolve_eartharxiv_fn: Optional[Callable[[str], Tuple[str, str, str]]] = None,
    walled_hosts: Iterable[str] = (),
) -> Tuple[str, str, str]:
    """Single fast, affiliation-aware Green-OA & Public-Domain cascade (replaces 7 blind sequential calls).

    - Queries OSTI/USGS only when affiliation hints US federal/national lab (or empty affiliation).
    - Queries GFZ only when affiliation hints GFZ/Potsdam/Germany (or empty affiliation).
    - Queries HAL/DataCite/Crossref posted-content with fast short-circuit on first verified hit.
    Returns `(pdf_url, license_slug, failure_reason)`.
    """
    inst_low = (institutions_raw or "").lower()
    low_doi = (doi or "").strip().lower()
    is_essoar = low_doi.startswith("10.22541/essoar.")
    reasons: List[str] = []

    # 1. US DOE OSTI.gov (fast; always run if affiliation is empty or mentions national lab / USA / DOE)
    us_lab_hint = (not inst_low) or any(
        k in inst_low for k in ("national laboratory", "los alamos", "lawrence", "sandia", "oak ridge", "pacific northwest", "united states", "usa")
    )
    if us_lab_hint:
        u, lic, why = resolve_osti_gov(http_session, doi, title=title, authors_raw=authors_raw, title_overlap_fn=title_overlap_fn)
        if u:
            return u, lic, ""
        reasons.append(f"osti:{why}")

    # 2. USGS Publications Warehouse (only when affiliation is empty or mentions USGS / Geological Survey)
    usgs_hint = (not is_essoar) and ((not inst_low) or ("geological survey" in inst_low or "usgs" in inst_low))
    if usgs_hint:
        u, lic, why = resolve_usgs_pubs(http_session, doi, walled_hosts=walled_hosts)
        if u:
            return u, lic, ""
        reasons.append(f"usgs:{why}")

    # 3. GFZ Potsdam PubMan (exact DOI check is 1 fast JSON POST; run if affiliation is empty or mentions GFZ/Potsdam/Germany)
    gfz_hint = (not inst_low) or any(k in inst_low for k in ("gfz", "potsdam", "helmholtz", "germany"))
    if gfz_hint and doi:
        u, lic, why = resolve_gfz_pubman(http_session, doi, title=title, authors_raw=authors_raw, title_overlap_fn=title_overlap_fn)
        if u:
            return u, lic, ""
        reasons.append(f"gfz:{why}")

    # 4. CaltechAUTHORS (Caltech Seismological Laboratory foundational & modern papers)
    caltech_hint = (not inst_low) or any(k in inst_low for k in ("california institute of technology", "caltech", "seismological laboratory", "pasadena"))
    if caltech_hint:
        u, lic, why = resolve_caltech_authors(
            http_session, doi=doi, title=title, authors_raw=authors_raw, title_overlap_fn=title_overlap_fn
        )
        if u:
            return u, lic, ""
        reasons.append(f"caltech:{why}")

    # 5. French HAL Open Archive (fast Solr query; excludes conference abstracts/posters)
    u, lic, why = resolve_hal_open_archive(
        http_session, doi=doi, link=link, title=title, authors_raw=authors_raw, title_overlap_fn=title_overlap_fn
    )
    if u:
        return u, lic, ""
    reasons.append(f"hal:{why}")

    # 5. DataCite global preprint & institutional repository meta-search
    u, lic, why = resolve_datacite_open_copy(
        http_session,
        doi=doi,
        title=title,
        authors_raw=authors_raw,
        title_overlap_fn=title_overlap_fn,
        normalize_lic_fn=normalize_lic_fn,
        resolve_eartharxiv_fn=resolve_eartharxiv_fn,
        walled_hosts=walled_hosts,
    )
    if u:
        return u, lic, ""
    reasons.append(f"datacite:{why}")

    # 6. Crossref `posted-content` reverse preprint check (EarthArXiv / EGUsphere / Research Square)
    if not is_essoar:
        u, lic, why = resolve_crossref_posted_preprint(
            http_session,
            doi=doi,
            title=title,
            authors_raw=authors_raw,
            title_overlap_fn=title_overlap_fn,
            copernicus_url_fn=copernicus_url_fn,
            resolve_eartharxiv_fn=resolve_eartharxiv_fn,
        )
        if u:
            return u, lic, ""
        reasons.append(f"cr_pre:{why}")

    return "", "", "; ".join(reasons[:4])

