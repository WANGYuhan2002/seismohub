#!/usr/bin/env python3
"""
seismohub_upgrade_runner.py (v2.3 — Carbon Deep-Dive Edition)
=============================================================
Non-destructive in-memory upgrade runner for SeismoHub v2 (`services/fullbundle_harvest.py`).

Upgrades in v2.3 (building on v2.2):
1. Wave 1 Blind-Spot Fixes:
   - Infers `10.1038/*` DOIs from `nature.com/articles/*` links and resolves ETH RC `20.500.11850/*`
     handles + HAL `hal-*` links when `papers.doi` is empty, recording inferred DOIs for safe DB backfill.
   - Adds `nature_aap` (`{suffix}_reference.pdf`) fallback for Gold OA *Commun. Earth Environ.* and
     *Scientific Reports* articles in press whose `.pdf` endpoint serves HTML.
   - Adds `springer_link` (`link.springer.com/content/pdf/{doi}.pdf`) for `10.1007/*` (routed through
     licence tiering + `'All rights reserved'` TDM seal so subscribed copies stay strictly in `paper_bundles/`).
   - Adds `researchsquare` (`10.21203/rs.*`) direct CC-BY asset PDF resolver.
   - Upgrades `_resolve_zenodo` to prioritize main manuscript/preprint PDFs over larger supplement/poster PDFs.
   - Raises default stream cap from 80 MB to 160 MB so high-res geoscience mapping papers (>80 MB) succeed.
   - Enhances Gate 4 (`identity_check`) with >=3 co-author surname verification when a preprint's title was
     refined for journal publication (`0.35 <= title_overlap < 0.70`).
   - Moves the `'All rights reserved'` publisher-PDF re-check inside the candidate loop (§3.8-i) so a
     subscribed VoR is held in `out.held_hit` while Group 2 open preprints/repositories are still searched.
2. Wave 2 Cross-Version VoR & Multi-Source Green-OA / Public-Domain Discovery:
   - `essoar_vor`: maps `10.22541/essoar.*` preprints via Crossref `is-preprint-of` and strict bidirectional
     Title+Author verification to published AGU/Wiley, Copernicus, Elsevier, or OSTI VoRs (skipping DOIs
     already retrieved in `seismo_hub.db` to prevent cross-DOI duplicate downloads).
   - `osti_gov`: US DOE OSTI.gov open manuscripts & `10.2172/*` technical reports (`/servlets/purl/{id}`).
   - `usgs_pubs`: USGS Publications Warehouse public-domain document PDFs.
   - `gfz_repo`: GFZ Potsdam PubMan PUBLIC repository PDFs (`gfzpublic.gfz.de`).
   - `hal_api`: French HAL Open Archive by `halId`, `doi`, or strict Title+Author (`api.archives-ouvertes.fr`).
   - `datacite_repo`: DataCite Meta-Resolver across global preprint & university repositories (Zenodo,
     EarthArXiv, arXiv, ETH RC, KITopen, BORIS Unibe, etc.) with strict Title (>=0.80) + Author verification,
     and a generic DSpace 7 / `citation_pdf_url` landing-page extractor.
   - Semantic Scholar is not a channel: the `s2_oa` name was dropped on 2026-11-13. No candidate called its
     resolver (`resolve_s2_open_copy`, kept unwired), and its 27 attempts in the 2026-11-10 runs found no copy.
3. Zero Key Leakage & Quota Protection:
   - `OPENALEX_API_KEY` is sent ONLY to `content.openalex.org` and free singleton `/works/doi:` calls
     (never to paid list/search endpoints), and stripped from all `HttpResult` URLs, logs, DB rows, and manifests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

UPGRADE_DIR = Path(__file__).resolve().parent
for _cand_dir in (UPGRADE_DIR, UPGRADE_DIR.parent / "services", UPGRADE_DIR.parent):
    if _cand_dir.is_dir() and str(_cand_dir) not in sys.path:
        sys.path.insert(0, str(_cand_dir))

from pmc_oa_and_whitelist_channel import (  # noqa: E402
    MetadataFallbackCache,
    PMC_PREFIX,
    extract_pmcid_from_locations,
    infer_gold_oa_vor_license,
    resolve_crossref_tdm_pdf_link,
    resolve_pmc_oa_s3,
    rewrite_repository_url,
)
from deep_oa_and_vor_resolvers import (  # noqa: E402
    discover_essoar_published_doi,
    enhanced_identity_check,
    infer_identifiers_from_link,
    nature_aap_pdf_url,
    resolve_ethrc_handle_or_doi,
    resolve_hal_open_archive,
    resolve_osti_gov,
    resolve_researchsquare_pdf,
    resolve_unified_green_oa,
    resolve_zenodo_smart,
    springer_pdf_url,
)
from tdm_guard_and_xml_renderer import (  # noqa: E402
    batch_compile_all_elsevier_xml_bundles,
)

_TLS_CTX = threading.local()

_RIGHTS_RESERVED_RE = re.compile(r"all\s+rights\s+reserved", re.I)
_OPEN_STATEMENT_RE = re.compile(
    r"creativecommons|creative\s+commons|open\s+access\s+article|this\s+is\s+an\s+open\s+access|"
    r"open\s+access\s+this\s+article|©\s*\d{4}\.?\s*the\s+authors?\b|\bthe\s+author\(s\)\s*\d{4}",
    re.I,
)

WALLED_JOURNAL_PREFIXES = (
    "10.1029/",   # AGU / JGR / GRL (seek CaltechAUTHORS / OSTI / green OA copy alongside TDM VoR!)
    "10.1002/",   # Wiley geoscience
    "10.1111/",   # Pre-2013 GJI (Wiley)
    "10.1016/",   # EPSL / Tectonophysics / PEPI (seek green OA PDF alongside TDM XML!)
    "10.1785/",   # BSSA / SRL / TSR (GeoScienceWorld)
    "10.1093/",   # GJI 2013+ (Oxford Univ. Press)
    "10.1190/",   # Geophysics / TLE (SEG / GSW)
    "10.1126/",   # Science / Sci. Adv.
    "10.1073/",   # PNAS
    "10.1130/",   # GSA Geology / Geosphere
    "10.1121/",   # JASA
    "10.3390/",   # MDPI
    "10.1007/",   # Springer / PAGEOPH / J. Seismol.
)

DEFAULT_STREAM_MAX_BYTES = 160_000_000  # 160 MB for high-resolution geoscience mapping papers
PRIVACY_SAFE_USER_AGENT = "SeismoHub/2.4 (academic-research)"


def _best_oa_cc_license(work: Optional[dict], is_open_license_fn: Any) -> str:
    """Return an open CC license from work.best_oa_location or primary_location if present."""
    if not isinstance(work, dict):
        return ""
    for key in ("best_oa_location", "primary_location"):
        loc = work.get(key)
        if isinstance(loc, dict):
            lic = str(loc.get("license") or "").strip().lower()
            if lic and is_open_license_fn(lic):
                return lic
    for loc in work.get("locations") or []:
        if isinstance(loc, dict):
            lic = str(loc.get("license") or "").strip().lower()
            if lic and is_open_license_fn(lic):
                return lic
    return ""


def _pdf_says_rights_reserved_bytes(data: bytes, kind: str) -> bool:
    """In-loop publisher rights check (§3.8-i): True if a non-repository PDF states 'All rights reserved'
    on its first two pages with no open-access / Creative Commons statement."""
    if kind == "open_copy":
        return False
    try:
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            text = " ".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
    except Exception:
        return True
    return bool(_RIGHTS_RESERVED_RE.search(text)) and not _OPEN_STATEMENT_RE.search(text)


def _load_retrieved_dois_set(db_path: Optional[Path]) -> Set[str]:
    """Read-only snapshot of DOIs that already have `pdf_path` or `held_tdm_only` in `seismo_hub.db`."""
    if db_path is None or not db_path.is_file():
        return set()
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute(
                "SELECT lower(trim(doi)) FROM papers "
                "WHERE doi IS NOT NULL AND trim(doi) != '' "
                "AND ((pdf_path IS NOT NULL AND pdf_path != '') OR retrieval_status = 'held_tdm_only')"
            ).fetchall()
            return {r[0] for r in rows if r and r[0]}
    except Exception:
        return set()


def install_harvester_upgrades(
    fb_module: Any,
    cache_path: Optional[Path] = None,
    *,
    readonly: Optional[bool] = None,
    keys_files: Iterable[Any] = (),
) -> MetadataFallbackCache:
    """Apply v2.3 Wave 1 + Wave 2 channel, resolver, and compliance upgrades to `services.fullbundle_harvest`.

    `keys_files` are extra key files (e.g. `harvest_pdfs.py --keys-file`), read in `load_harvest_keys` order."""
    if readonly is None:
        readonly = "--dry-run" in sys.argv
    meta_cache = MetadataFallbackCache(cache_path, readonly=readonly)

    # 0. Allow load_harvest_keys to load OPENALEX_API_KEY: from $SEISMO_HARVEST_KEYS_FILE, then `keys_files`, then
    #    ~/.config/seismo_harvester/keys.env (without `keys_files` an OPENALEX_API_KEY given only there was never read)
    if hasattr(fb_module, "KEY_NAMES") and "OPENALEX_API_KEY" not in fb_module.KEY_NAMES:
        fb_module.KEY_NAMES = tuple(list(fb_module.KEY_NAMES) + ["OPENALEX_API_KEY"])
    try:
        fb_module.load_harvest_keys(list(keys_files))
    except Exception:
        pass

    # 1. Politeness rate limits across all APIs & institutional repositories
    if hasattr(fb_module, "HOST_MIN_INTERVAL") and isinstance(fb_module.HOST_MIN_INTERVAL, dict):
        fb_module.HOST_MIN_INTERVAL.update({
            "api.wiley.com": 10.0,
            "www.research-collection.ethz.ch": 10.0,
            "content.openalex.org": 0.5,
            "api.semanticscholar.org": 1.2,
            "www.osti.gov": 0.6,
            "pubs.usgs.gov": 0.5,
            "api.archives-ouvertes.fr": 0.4,
            "gfzpublic.gfz.de": 0.6,
            "link.springer.com": 1.0,
            "www.researchsquare.com": 1.0,
        })

    # 2. Failure severity ordering (§3.8-h): TRANSIENT_ERROR beats skipped BOT_WALLED_HOSTS
    if hasattr(fb_module, "FAILURE_SEVERITY") and hasattr(fb_module, "TRANSIENT_ERROR"):
        sev = list(fb_module.FAILURE_SEVERITY)
        if fb_module.TRANSIENT_ERROR in sev:
            sev.remove(fb_module.TRANSIENT_ERROR)
            sev.insert(0, fb_module.TRANSIENT_ERROR)
            fb_module.FAILURE_SEVERITY = tuple(sev)

    # 3. Register all Wave 1 & Wave 2 channels in ALL_CHANNELS
    extra_channels = (
        "pmc_oa_s3",
        "crossref_link",
        "openalex_content",
        "hal_repo",
        "dspace7_bitstream",
        "nature_aap",
        "springer_link",
        "researchsquare",
        "essoar_vor",
        "osti_gov",
        "usgs_pubs",
        "gfz_repo",
        "hal_api",
        "datacite_repo",
        "crossref_preprint",
        "green_oa_cascade",
    )
    for ch in extra_channels:
        if ch not in fb_module.ALL_CHANNELS:
            fb_module.ALL_CHANNELS = tuple(list(fb_module.ALL_CHANNELS) + [ch])

    # Capture genuine `services.fullbundle_harvest` originals (never capture a pytest monkeypatch mock!)
    if not hasattr(fb_module, "_v2_3_orig_polite_init") and fb_module.PoliteSession.__init__.__module__ == fb_module.__name__:
        fb_module._v2_3_orig_polite_init = fb_module.PoliteSession.__init__
        fb_module._v2_3_orig_http_get = fb_module.PoliteSession.get
        fb_module._v2_3_orig_identity_check = fb_module.identity_check
        fb_module._v2_3_orig_init = fb_module.FullBundleHarvester.__init__
        fb_module._v2_3_orig_openalex_work = fb_module.FullBundleHarvester._openalex_work
        fb_module._v2_3_orig_build_candidates = fb_module.FullBundleHarvester.build_candidates
    if not hasattr(fb_module, "_v2_3_orig_harvest") and fb_module.FullBundleHarvester.harvest.__module__ == fb_module.__name__:
        fb_module._v2_3_orig_harvest = fb_module.FullBundleHarvester.harvest

    # If a test explicitly monkeypatched `FullBundleHarvester.harvest` with a fake, respect the mock!
    cur_harvest = fb_module.FullBundleHarvester.harvest
    harvest_is_mocked = (
        cur_harvest.__module__ != fb_module.__name__
        and cur_harvest is not getattr(fb_module.FullBundleHarvester, "_v2_3_harvest_fn", None)
    )
    if harvest_is_mocked:
        fb_module.FullBundleHarvester._v2_3_meta_cache = meta_cache
        return meta_cache

    if (
        getattr(fb_module.FullBundleHarvester, "_v2_3_upgraded", False)
        and fb_module.FullBundleHarvester.harvest is getattr(fb_module.FullBundleHarvester, "_v2_3_harvest_fn", None)
        and fb_module.FullBundleHarvester.build_candidates is getattr(fb_module.FullBundleHarvester, "_v2_3_build_fn", None)
    ):
        fb_module.FullBundleHarvester._v2_3_meta_cache = meta_cache
        return meta_cache

    # 4. Transport layer: privacy-safe UA (no personal email), 160 MB stream cap, `post_json`, & OpenAlex key isolation
    if hasattr(fb_module, "DEFAULT_USER_AGENT"):
        fb_module.DEFAULT_USER_AGENT = PRIVACY_SAFE_USER_AGENT
    orig_polite_init = getattr(fb_module, "_v2_3_orig_polite_init", fb_module.PoliteSession.__init__)
    orig_http_get = getattr(fb_module, "_v2_3_orig_http_get", fb_module.PoliteSession.get)
    fb_module._oa_content_quota_exhausted = ""

    def upgraded_polite_init(self: Any, user_agent: str = "", *, max_bytes: int = 0, **kwargs: Any) -> None:
        effective_max = max_bytes or int(os.environ.get("SEISMO_HARVEST_MAX_BYTES") or DEFAULT_STREAM_MAX_BYTES)
        effective_ua = user_agent or os.environ.get("SEISMO_HARVEST_UA") or PRIVACY_SAFE_USER_AGENT
        orig_polite_init(self, user_agent=effective_ua, max_bytes=effective_max, **kwargs)

    def polite_post_json(
        self: Any,
        url: str,
        *,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        max_bytes: int = 2_000_000,
    ) -> Any:
        import time as _t
        import requests as _requests
        host = urllib.parse.urlsplit(url).netloc.lower()
        self._throttle(host)
        started = _t.monotonic()
        res = fb_module.HttpResult(url=url, final_url=url)
        hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        try:
            resp = self.session.post(url, json=payload, headers=hdrs, timeout=self.timeout, stream=True)
            try:
                res.status = resp.status_code
                res.final_url = resp.url or url
                res.content_type = resp.headers.get("Content-Type", "")
                buf = bytearray()
                for chunk in resp.iter_content(chunk_size=65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        res.error = f"too_large:>{max_bytes}"
                        break
                if not res.error:
                    res.data = bytes(buf)
            finally:
                resp.close()
        except _requests.RequestException as exc:
            res.error = f"{type(exc).__name__}: {exc}"[:240]
        finally:
            self._last[host] = _t.monotonic()
            res.elapsed_ms = int((_t.monotonic() - started) * 1000)
        return res

    def upgraded_http_get(
        self: Any,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        sensitive_headers: Iterable[str] = (),
        max_bytes: int = 0,
        retries: int = 1,
    ) -> Any:
        clean_url = url
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.lower()
        if host == "content.openalex.org" and getattr(fb_module, "_oa_content_quota_exhausted", ""):
            res = fb_module.HttpResult(url=clean_url, final_url=clean_url, status=429)
            res.error = fb_module._oa_content_quota_exhausted
            return res

        oa_key = (os.environ.get("OPENALEX_API_KEY") or "").strip()
        req_url = url
        hdrs = dict(headers or {})
        sens = list(sensitive_headers)
        effective_retries = 0 if host == "content.openalex.org" else retries

        # Inject OPENALEX_API_KEY ONLY on content.openalex.org and $0.00 singleton /works/doi: calls
        is_free_oa_singleton = (host == "api.openalex.org" and parts.path.startswith("/works/doi:"))
        if oa_key and (host == "content.openalex.org" or is_free_oa_singleton):
            hdrs.setdefault("api_key", oa_key)
            if "api_key" not in sens:
                sens.append("api_key")
            if host == "content.openalex.org" and "api_key=" not in req_url:
                sep = "&" if "?" in req_url else "?"
                req_url = f"{req_url}{sep}api_key={urllib.parse.quote(oa_key)}"

        res = orig_http_get(
            self,
            req_url,
            headers=hdrs,
            sensitive_headers=tuple(sens),
            max_bytes=max_bytes,
            retries=effective_retries,
        )
        if host == "content.openalex.org" and res.status in (401, 402, 403, 429):
            fb_module._oa_content_quota_exhausted = (
                f"openalex_content_daily_quota_reached(http_{res.status}): "
                "free $1.00/day (100 PDFs) allowance used; resets at 00:00 UTC"
            )
        # Never expose api_key or temporary Cloudflare R2 signed URLs in manifests, DB, or reports
        res.url = clean_url
        if host == "content.openalex.org" or "api_key=" in (res.final_url or ""):
            res.final_url = clean_url
        return res

    fb_module.PoliteSession.__init__ = upgraded_polite_init
    fb_module.PoliteSession.get = upgraded_http_get
    fb_module.PoliteSession.post_json = polite_post_json

    # 5. Enhanced Gate-4 identity check (conference-abstract guard + multi-author verification)
    orig_identity_check = fb_module._v2_3_orig_identity_check

    def upgraded_identity_check(head_text: str, *, doi: str, title: str) -> Tuple[bool, str]:
        authors_raw = getattr(_TLS_CTX, "authors", None)
        return enhanced_identity_check(
            head_text,
            doi=doi,
            title=title,
            authors_raw=authors_raw,
            orig_identity_fn=orig_identity_check,
            title_overlap_fn=fb_module.title_overlap,
        )

    fb_module.identity_check = upgraded_identity_check

    # 6. Harvester method upgrades
    orig_init = fb_module._v2_3_orig_init
    orig_openalex_work = fb_module._v2_3_orig_openalex_work
    orig_build_candidates = fb_module._v2_3_orig_build_candidates
    orig_harvest = fb_module._v2_3_orig_harvest
    orig_elsevier_xml = getattr(fb_module, "_v2_3_orig_elsevier_xml", fb_module.FullBundleHarvester._elsevier_xml_fallback)
    fb_module._v2_3_orig_elsevier_xml = orig_elsevier_xml

    def upgraded_elsevier_xml_fallback(self: Any, cand: Any, doi: str, out: Any) -> None:
        """Support BOTH modern `<ce:sections>` XML AND pre-1995 scanned `<xocs:rawtext>` OCR full-text XML (e.g. PREM 1981)."""
        if out.held_xml is not None or not doi:
            return
        if self.elsevier_xml_disabled_reason:
            orig_elsevier_xml(self, cand, doi, out)
            return
        started = fb_module._now_iso()
        url = (
            f"https://api.elsevier.com/content/article/doi/{urllib.parse.quote(doi, safe='/')}"
            "?httpAccept=text/xml&view=FULL"
        )
        headers = {k: v for k, v in cand.headers.items() if k.lower() != "accept"}
        headers["Accept"] = "text/xml"
        res = self.http.get(url, headers=headers, sensitive_headers=cand.sensitive_headers)
        att = fb_module.Attempt(
            "elsevier_tdm_xml", res.final_url or url, fb_module.SUCCESS,
            http_status=res.status or None, bytes=len(res.data),
            started_at=started, duration_ms=res.elapsed_ms,
        )
        text = res.text() if res.ok else ""
        has_body = ("<ce:sections" in text or "<body" in text or "<xocs:rawtext" in text or "<ja:raw-text" in text)
        chars = len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))) if has_body else 0
        xml_dois = {fb_module.normalize_doi(d).lower() for d in fb_module.ELSEVIER_XML_DOI_RE.findall(text)}
        unentitled = True
        if not res.ok:
            att.outcome, att.reason = fb_module.classify_http_failure(res)
        elif not has_body or chars < self.min_chars:
            att.outcome = fb_module.AUTH_REQUIRED
            att.reason = "elsevier_xml_without_full_text: not entitled from this network (use an institutional network / VPN)"
        elif fb_module.normalize_doi(doi).lower() not in xml_dois:
            att.outcome, att.reason = fb_module.UNSUPPORTED_CONTENT_TYPE, f"identity_mismatch: xml doi {sorted(xml_dois)[:1]}"
            unentitled = False
        if att.outcome != fb_module.SUCCESS:
            if unentitled:
                self._elsevier_xml_failures += 1
                if self._elsevier_xml_failures >= self.elsevier_preview_breaker and not self.elsevier_xml_disabled_reason:
                    self.elsevier_xml_disabled_reason = (
                        f"skipped: {self._elsevier_xml_failures} consecutive Elsevier XML requests without full "
                        "text - not entitled from this network (use an institutional network / VPN)"
                    )
            out.attempts.append(att)
            return
        self._elsevier_xml_failures = 0
        att.reason = f"verified full-text XML {chars} chars doi=match tier={fb_module.TIER_TDM}"
        out.attempts.append(att)
        out.held_xml = fb_module.XmlHit(
            channel="elsevier_tdm_xml", url=res.final_url or url, data=res.data, chars=chars,
            license=cand.license or "tdm-licence", tier=fb_module.TIER_TDM, identity="doi=match",
        )

    def upgraded_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        for attr_name in ("min_interval", "host_intervals"):
            tbl = getattr(getattr(self, "http", None), attr_name, None)
            if isinstance(tbl, dict):
                tbl["api.wiley.com"] = 10.0
                tbl["www.research-collection.ethz.ch"] = 10.0
                tbl["content.openalex.org"] = 0.5
        # Smart Zenodo & handle-aware ETH RC resolvers
        self._resolve_zenodo = lambda d: resolve_zenodo_smart(self.http, d)

    def upgraded_openalex_work(self: Any, doi: str) -> Tuple[Optional[dict], Any]:
        active_cache: MetadataFallbackCache = getattr(
            fb_module.FullBundleHarvester, "_v2_3_meta_cache", meta_cache
        )
        cached = active_cache.get("openalex", doi)
        if cached is not None and "has_content" in cached:
            att = fb_module.Attempt(
                "openalex_meta",
                f"cache://openalex/{doi}",
                fb_module.SUCCESS,
                reason="sqlite_cache_hit",
                http_status=200,
                started_at=fb_module._now_iso(),
            )
            return cached, att
        work, att = orig_openalex_work(self, doi)
        if work is not None:
            active_cache.put("openalex", doi, work)
        return work, att

    def upgraded_build_candidates(
        self: Any,
        ref: Any,
        doi: str,
        title: str,
        work: Optional[dict],
        vor_license: str,
    ) -> List[Any]:
        locs = [loc for loc in ((work or {}).get("locations") or []) if isinstance(loc, dict)]
        pmcid = extract_pmcid_from_locations(locs, openalex_work=work)

        rewritten_locs: List[Dict[str, Any]] = []
        for loc in locs:
            loc_copy = dict(loc)
            for url_key in ("pdf_url", "landing_page_url"):
                raw_u = str(loc_copy.get(url_key) or "")
                if not raw_u:
                    continue
                rw_u, rw_tag = rewrite_repository_url(raw_u)
                if rw_tag == "pmc_oa_s3" and rw_u.startswith(PMC_PREFIX):
                    if not pmcid:
                        pmcid = rw_u[len(PMC_PREFIX):]
                elif rw_tag in ("arxiv", "hal_repo", "dspace7_bitstream"):
                    if rw_tag == "arxiv" and "arxiv" not in self.channels:
                        continue
                    loc_copy["pdf_url"] = rw_u
            rewritten_locs.append(loc_copy)
        if work is not None:
            work = dict(work, locations=rewritten_locs)

        base_cands = orig_build_candidates(self, ref, doi, title, work, vor_license)

        # Upgrade any ethrc candidate to use ONLY robots.txt-allowed HTML handle / DataCite / bitstreams paths
        backfill_box: Optional[Dict[str, str]] = getattr(_TLS_CTX, "backfill_box", None)
        for idx, c in enumerate(base_cands):
            if c.channel == "ethrc":
                base_cands[idx] = fb_module.Candidate(
                    channel="ethrc",
                    resolver=lambda d=doi, lk=getattr(ref, "link", ""), t=title, bx=backfill_box, lc=locs: resolve_ethrc_handle_or_doi(
                        self.http,
                        doi=d,
                        link=lk,
                        title=t,
                        title_overlap_fn=fb_module.title_overlap,
                        normalize_lic_fn=fb_module.normalize_license_label,
                        out_backfill=bx,
                        openalex_locations=lc,
                    ),
                    kind="open_copy",
                    note="research-collection.ethz.ch handle/bitstream",
                )
        inst_str = str(getattr(ref, "institutions", "") or "")

        # Find end of Group 0 and end of Group 1
        group0_end = 0
        group1_end = 0
        for idx, c in enumerate(base_cands):
            if c.kind == "open_vor" or c.channel in ("copernicus", "ethrc"):
                group0_end = idx + 1
                group1_end = max(group1_end, group0_end)
            elif c.kind in ("tdm_vor", "publisher_copy"):
                group1_end = idx + 1

        extra_group0: List[Any] = []
        extra_group1: List[Any] = []
        extra_group2: List[Any] = []

        low_doi = (doi or "").lower()
        link_str = getattr(ref, "link", "") or ""
        authors_raw = getattr(ref, "authors", None)
        link_info = infer_identifiers_from_link(doi, link_str)

        gold_lic = vor_license or _best_oa_cc_license(work, fb_module.is_open_license) or infer_gold_oa_vor_license(
            doi=doi,
            journal=getattr(ref, "journal", "") or "",
            openalex_work=work,
        )
        has_non_walled_group0 = any(
            c.kind == "open_vor"
            and c.url
            and urllib.parse.urlsplit(c.url).netloc.lower() not in fb_module.BOT_WALLED_HOSTS
            and not (low_doi.startswith("10.1038/") and c.channel in ("nature", "openalex"))
            for c in base_cands[:group0_end]
        ) if group0_end else False

        # --- Wave 1A: Nature Accelerated Article Preview (`_reference.pdf`) fallback ---
        if "nature_aap" in self.channels and low_doi.startswith("10.1038/"):
            aap_url = nature_aap_pdf_url(doi)
            if aap_url:
                if gold_lic:
                    extra_group0.append(
                        fb_module.Candidate(
                            channel="nature_aap",
                            url=aap_url,
                            kind="open_vor",
                            version="publishedVersion",
                            license=gold_lic,
                            note=f"Nature Accelerated Article Preview ({doi})",
                        )
                    )
                else:
                    extra_group1.append(
                        fb_module.Candidate(
                            channel="nature_aap",
                            url=aap_url,
                            kind="publisher_copy",
                            version="publishedVersion",
                            note=f"Nature Accelerated Article Preview ({doi})",
                        )
                    )

        # --- Wave 1B: Springer (`10.1007/*`) direct content PDF (strictly tiered OPEN vs TDM) ---
        if "springer_link" in self.channels and low_doi.startswith("10.1007/"):
            sp_url = springer_pdf_url(doi)
            if sp_url:
                if vor_license:
                    extra_group0.append(
                        fb_module.Candidate(
                            channel="springer_link",
                            url=sp_url,
                            kind="open_vor",
                            version="publishedVersion",
                            license=vor_license,
                            note=f"Springer OA PDF ({doi})",
                        )
                    )
                else:
                    extra_group1.append(
                        fb_module.Candidate(
                            channel="springer_link",
                            url=sp_url,
                            kind="publisher_copy",
                            version="publishedVersion",
                            note=f"Springer licensed PDF ({doi})",
                        )
                    )

        # --- Wave 1C: Research Square (`10.21203/rs.*`) CC-BY preprint ---
        if "researchsquare" in self.channels and (
            low_doi.startswith("10.21203/") or "researchsquare.com/" in link_str.lower()
        ):
            extra_group2.append(
                fb_module.Candidate(
                    channel="researchsquare",
                    resolver=lambda d=doi, lk=link_str: resolve_researchsquare_pdf(self.http, doi=d, landing_url=lk if "researchsquare.com" in lk.lower() else ""),
                    kind="open_copy",
                    version="submittedVersion",
                    license="cc-by",
                    note=f"Research Square preprint ({doi or link_str})",
                )
            )

        # --- Existing Group 0 Upgrades: Crossref `link[]`, PMC Cloud S3, OpenAlex Content ---
        if "crossref_link" in self.channels:
            if doi and (low_doi.startswith("10.26443/seismica.") or (gold_lic and not has_non_walled_group0 and not low_doi.startswith("10.1038/"))):
                extra_group0.append(
                    fb_module.Candidate(
                        channel="crossref_link",
                        resolver=lambda d=doi: resolve_crossref_tdm_pdf_link(
                            self.http,
                            d,
                            walled_hosts=fb_module.BOT_WALLED_HOSTS,
                            crossref_vor_cc_fn=getattr(fb_module, "crossref_vor_cc_license", None),
                        ),
                        kind="open_vor",
                        version="publishedVersion",
                        license="",
                        note=f"Crossref text-mining link ({doi})",
                    )
                )

        if "pmc_oa_s3" in self.channels:
            pmc_prefixes = ("10.1126/sciadv.", "10.1073/pnas.", "10.3390/", "10.1371/", "10.3389/")
            if bool(pmcid) or (doi and low_doi.startswith(pmc_prefixes)):
                extra_group0.append(
                    fb_module.Candidate(
                        channel="pmc_oa_s3",
                        resolver=lambda p=pmcid, d=doi: resolve_pmc_oa_s3(self.http, pmcid=p, doi=d),
                        kind="open_vor",
                        version="publishedVersion",
                        license="",
                        note=f"PMC Cloud S3 ({pmcid or doi})",
                    )
                )

        oa_key = (os.environ.get("OPENALEX_API_KEY") or "").strip()
        has_cached_pdf = bool(((work or {}).get("has_content") or {}).get("pdf"))
        work_id = str((work or {}).get("id") or "").rsplit("/", 1)[-1]
        is_essoar_preprint = low_doi.startswith("10.22541/essoar.")
        if (
            oa_key
            and has_cached_pdf
            and work_id.startswith("W")
            and not has_non_walled_group0
            and "openalex_content" in self.channels
        ):
            content_url = f"https://content.openalex.org/works/{work_id}.pdf"
            if gold_lic:
                extra_group0.append(
                    fb_module.Candidate(
                        channel="openalex_content",
                        url=content_url,
                        kind="open_vor",
                        version="publishedVersion",
                        license=gold_lic,
                        note=f"OpenAlex cached OA PDF ({work_id})",
                    )
                )
            elif is_essoar_preprint:
                extra_group2.append(
                    fb_module.Candidate(
                        channel="openalex_content",
                        url=content_url,
                        kind="open_copy",
                        version="submittedVersion",
                        license="preprint-essoar",
                        note=f"OpenAlex cached ESSOAr preprint ({work_id})",
                    )
                )

        # --- Wave 2A: OSTI direct for `10.2172/*` and HAL direct for no-DOI `hal.science` links ---
        if "osti_gov" in self.channels and low_doi.startswith("10.2172/"):
            extra_group0.append(
                fb_module.Candidate(
                    channel="osti_gov",
                    resolver=lambda d=doi: resolve_osti_gov(self.http, d),
                    kind="open_copy",
                    version="publishedVersion",
                    license="public-domain",
                    note=f"OSTI.gov technical report ({doi})",
                )
            )

        if "hal_api" in self.channels and not doi and link_info["hal_id"]:
            extra_group0.append(
                fb_module.Candidate(
                    channel="hal_api",
                    resolver=lambda lk=link_str, t=title, au=authors_raw: resolve_hal_open_archive(
                        self.http,
                        doi="",
                        link=lk,
                        title=t,
                        authors_raw=au,
                        title_overlap_fn=fb_module.title_overlap,
                    ),
                    kind="open_copy",
                    version="submittedVersion",
                    license="hal-open-archive",
                    note=f"HAL direct record ({link_info['hal_id']})",
                )
            )

        # --- Wave 2B: Single affiliation-aware Green-OA & US Federal Public-Domain cascade ---
        needs_deep_green_oa = low_doi.startswith(WALLED_JOURNAL_PREFIXES) or is_essoar_preprint
        if needs_deep_green_oa and (
            "green_oa_cascade" in self.channels
            or "osti_gov" in self.channels
            or "datacite_repo" in self.channels
        ):
            extra_group2.append(
                fb_module.Candidate(
                    channel="green_oa_cascade",
                    resolver=lambda d=doi, lk=link_str, t=title, au=authors_raw, ins=inst_str: resolve_unified_green_oa(
                        self.http,
                        doi=d,
                        link=lk,
                        title=t,
                        authors_raw=au,
                        institutions_raw=ins,
                        title_overlap_fn=fb_module.title_overlap,
                        normalize_lic_fn=fb_module.normalize_license_label,
                        copernicus_url_fn=fb_module.copernicus_pdf_url,
                        resolve_eartharxiv_fn=self._resolve_eartharxiv,
                        walled_hosts=fb_module.BOT_WALLED_HOSTS,
                    ),
                    kind="open_copy",
                    version="acceptedVersion",
                    license="",
                    note=f"Unified affiliation-aware Green-OA cascade ({doi})",
                )
            )

        # Splice Group 0, Group 1, and Group 2 additions cleanly in priority order
        if extra_group1:
            base_cands[group1_end:group1_end] = extra_group1
        if extra_group0:
            base_cands[group0_end:group0_end] = extra_group0
        if extra_group2:
            base_cands.extend(extra_group2)

        return base_cands

    def upgraded_harvest(self: Any, ref: Any) -> Any:
        # Infer DOI from `papers.link` when `ref.doi` is blank (e.g. `nature.com/articles/s41467-...`)
        orig_doi = fb_module.normalize_doi(getattr(ref, "doi", "") or "")
        link_info = infer_identifiers_from_link(orig_doi, getattr(ref, "link", "") or "")
        backfill_box: Dict[str, str] = {}
        if not orig_doi and link_info["inferred_doi"]:
            backfill_box["doi"] = link_info["inferred_doi"]
            ref_for_run = fb_module.PaperRef(
                paper_id=ref.paper_id,
                doi=link_info["inferred_doi"],
                title=ref.title,
                journal=ref.journal,
                link=ref.link,
                source=ref.source,
                arxiv_id=getattr(ref, "arxiv_id", ""),
            )
            setattr(ref_for_run, "authors", getattr(ref, "authors", None))
        else:
            ref_for_run = ref

        _TLS_CTX.authors = getattr(ref, "authors", None)
        _TLS_CTX.backfill_box = backfill_box
        try:
            out = orig_harvest(self, ref_for_run)

            # In-loop publisher 'All rights reserved' re-check (§3.8-i): if `orig_harvest` stopped on a
            # publisher copy whose text says 'All rights reserved' with no open statement, demote it to
            # `held_hit` and run any remaining Group 2 `open_copy` candidates so we don't miss preprints!
            if (
                out.app_hit is not None
                and not self.attach_tdm
                and out.app_hit.tier == fb_module.TIER_OPEN
                and _pdf_says_rights_reserved_bytes(out.app_hit.data, out.app_hit.kind)
            ):
                demoted = out.app_hit
                demoted.tier = fb_module.TIER_TDM
                demoted.license = (
                    f"publisher-licensed (pdf: all rights reserved; metadata said {demoted.license or 'open'})"
                )
                if out.held_hit is None:
                    out.held_hit = demoted
                out.app_hit = None

                # Search remaining open_copy candidates for an attachable preprint/repository copy
                eff_doi = out.doi
                work, _ = self._openalex_work(eff_doi) if (eff_doi and not eff_doi.lower().startswith(fb_module.ARXIV_DOI_PREFIX)) else (None, None)
                vor_lic = fb_module.vor_open_license(work)
                tried_urls = {a.url for a in out.pdf_attempts if a.url}
                for cand in self.build_candidates(ref_for_run, eff_doi, out.title, work, vor_lic):
                    if cand.kind != "open_copy":
                        continue
                    if cand.url and cand.url in tried_urls:
                        continue
                    hit = self._try(cand, eff_doi, out.title, vor_lic, out)
                    if hit is not None and hit.tier == fb_module.TIER_OPEN:
                        out.app_hit = hit
                        break

                if out.app_hit is not None:
                    out.status = fb_module.SUCCESS
                    out.reason = f"{out.app_hit.channel}:{out.app_hit.tier}"
                else:
                    out.status = fb_module.HELD_TDM_ONLY
                    out.reason = (
                        f"{out.held_hit.channel} copy is {out.held_hit.tier} ({out.held_hit.license}); "
                        "kept in bundle only. No open copy found."
                    )

            # Wave 2C: If an ESSOAr preprint (`10.22541/essoar.*`) still has neither an `app_hit` nor a
            # `held_hit`/`held_xml`, check whether it has been formally published as a journal VoR / report!
            if (
                out.app_hit is None
                and out.held_hit is None
                and out.held_xml is None
                and "essoar_vor" in self.channels
                and (out.doi or "").lower().startswith("10.22541/essoar.")
            ):
                pub_doi, via_why = discover_essoar_published_doi(
                    self.http,
                    out.doi,
                    out.title,
                    getattr(ref, "authors", None),
                    fb_module.title_overlap,
                )
                if pub_doi:
                    db_env = os.environ.get("SEISMO_DB_PATH") or str(UPGRADE_DIR.parent / "seismo_hub.db")
                    retrieved_dois = _load_retrieved_dois_set(Path(db_env))
                    if pub_doi.lower() in retrieved_dois:
                        out.attempts.append(
                            fb_module.Attempt(
                                "essoar_vor",
                                f"https://doi.org/{pub_doi}",
                                fb_module.UNSUPPORTED,
                                reason=f"published_vor_already_in_db:{pub_doi}({via_why})",
                                started_at=fb_module._now_iso(),
                            )
                        )
                        out.status = fb_module.UNSUPPORTED
                        out.reason = f"published_vor_already_in_db:{pub_doi}({via_why})"
                    else:
                        # Delegate to a sub-harvest for the published DOI (strictly enforcing its VoR licence!)
                        pub_ref = fb_module.PaperRef(
                            paper_id=ref.paper_id,
                            doi=pub_doi,
                            title=out.title,
                            journal=ref.journal,
                            link=f"https://doi.org/{pub_doi}",
                            source=ref.source,
                        )
                        setattr(pub_ref, "authors", getattr(ref, "authors", None))
                        pub_out = orig_harvest(self, pub_ref)
                        if (
                            pub_out.app_hit is not None
                            and not self.attach_tdm
                            and pub_out.app_hit.tier == fb_module.TIER_OPEN
                            and _pdf_says_rights_reserved_bytes(pub_out.app_hit.data, pub_out.app_hit.kind)
                        ):
                            pub_out.app_hit.tier = fb_module.TIER_TDM
                            pub_out.app_hit.license = "publisher-licensed (pdf: all rights reserved)"
                            pub_out.held_hit = pub_out.app_hit
                            pub_out.app_hit = None
                            pub_out.status = fb_module.HELD_TDM_ONLY

                        for patt in pub_out.attempts:
                            patt.channel = f"essoar_vor->{patt.channel}" if not patt.channel.endswith("_meta") else patt.channel
                            out.attempts.append(patt)

                        if pub_out.app_hit is not None:
                            pub_out.app_hit.channel = f"essoar_vor({pub_out.app_hit.channel})"
                            out.app_hit = pub_out.app_hit
                            out.status = fb_module.SUCCESS
                            out.reason = f"{out.app_hit.channel}:{out.app_hit.tier} via {pub_doi}"
                        elif pub_out.held_hit is not None:
                            pub_out.held_hit.channel = f"essoar_vor({pub_out.held_hit.channel})"
                            out.held_hit = pub_out.held_hit
                            out.status = fb_module.HELD_TDM_ONLY
                            out.reason = (
                                f"published VoR {pub_doi} ({out.held_hit.channel}) is TDM-licensed; "
                                "kept in bundle only"
                            )
                        elif pub_out.held_xml is not None:
                            out.held_xml = pub_out.held_xml
                            out.status = fb_module.HELD_TDM_ONLY
                            out.reason = f"published VoR {pub_doi} XML kept in bundle only (TDM)"
        finally:
            _TLS_CTX.authors = None
            _TLS_CTX.backfill_box = None

        if backfill_box.get("doi") and not orig_doi:
            setattr(out, "inferred_doi", backfill_box["doi"])
            if not out.doi:
                out.doi = backfill_box["doi"]
        return out

    fb_module.FullBundleHarvester.__init__ = upgraded_init
    fb_module.FullBundleHarvester._openalex_work = upgraded_openalex_work
    fb_module.FullBundleHarvester.build_candidates = upgraded_build_candidates
    fb_module.FullBundleHarvester.harvest = upgraded_harvest
    fb_module.FullBundleHarvester._elsevier_xml_fallback = upgraded_elsevier_xml_fallback
    fb_module.FullBundleHarvester._v2_3_harvest_fn = upgraded_harvest
    fb_module.FullBundleHarvester._v2_3_build_fn = upgraded_build_candidates
    fb_module.FullBundleHarvester._v2_3_upgraded = True
    fb_module.FullBundleHarvester._v2_3_meta_cache = meta_cache
    return meta_cache


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SeismoHub v2 Non-Destructive Coverage Upgrade Runner (v2.3 Carbon Deep-Dive Edition)"
    )
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parents[1]),
        help="Path to the SeismoHub checkout (default: this repository)",
    )
    parser.add_argument(
        "--compile-elsevier-xml",
        action="store_true",
        help="Compile XML-only Elsevier bundles in paper_bundles/ into multi-page PDFs (optional, disabled by default)",
    )
    parser.add_argument(
        "--overwrite-rendered-pdf",
        action="store_true",
        help="Re-render main_manuscript.pdf even if it already exists in an XML bundle",
    )
    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    if not repo_path.is_dir():
        raise SystemExit(f"Repository path not found: {repo_path}")
    if str(repo_path) not in sys.path:
        sys.path.insert(0, str(repo_path))

    if args.compile_elsevier_xml:
        bundle_root = repo_path / "paper_bundles"
        print(f"[*] Scanning {bundle_root} for XML-only Elsevier bundles...")
        summary = batch_compile_all_elsevier_xml_bundles(bundle_root, overwrite=args.overwrite_rendered_pdf)
        print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
