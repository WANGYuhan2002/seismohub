#!/usr/bin/env python3
"""Zotero / Browser PDF Compliance Triage & Importer (`scripts/zotero_triage_import.py`).

Bridges browser/Zotero downloads for bot-walled journals (BSSA/SRL/TSR, SEG, GJI, ESSOAr, Science)
into SeismoHub v2 with automatic OPEN vs. TDM routing:

1. `--export-batch [PATH]`:
   Generates a prioritized browser-download checklist CSV of unretrieved papers (default: GSW
   `10.1785/*` + `10.1190/*` + OUP `10.1093/*` + `10.22541/*` + `10.1126/*`), annotated with cached
   OpenAlex licence hints (`expected_route`: `APP_OPEN` vs. `LOCAL_TDM_BUNDLE`) and clickable `https://doi.org/...` links.

2. `<input_path> [...]` (accepts Zotero BibTeX export folder(s), Zotero `storage/` folder, or any folder/PDFs):
   - Matches each PDF to `seismo_hub.db` using `services.pdf_match_core` (BibTeX DOI -> scraped DOI -> arXiv -> title).
   - Verifies Gates 1-3 via `services.fullbundle_harvest.inspect_pdf` (`%PDF-`, >=40 KB, >=3 pages, >=5000 chars).
   - Evaluates licence tier from BOTH the PDF's first/last pages AND cached/online metadata:
     * Explicit Creative Commons / Open Access statement on PDF, OR preprint (`10.22541/`, `10.31223/`, `arxiv`),
       OR verified Gold/Hybrid CC licence in OpenAlex/Crossref WITHOUT `'All rights reserved'` / institution download stamp
       -> `OPEN_CC_BY_OR_PREPRINT` -> attaches to `downloads/` + sets `papers.pdf_path` (`retrieval_status='success'`).
     * Subscription / `'All rights reserved'` / institution download stamp (`Downloaded from ... by <Institution> user on <date>`) / unknown closed
       publisher PDF -> `LICENSED_TDM_ONLY` -> writes ONLY to `paper_bundles/PaperBundle_<DOI>/main_manuscript.pdf`
       with `hosted_ai_upload_allowed=False` (`retrieval_status='held_tdm_only'`, `pdf_path` left `NULL`).

Safety:
- Default is **DRY-RUN** (zero DB or file writes; prints exact `OPEN -> APP` vs `TDM -> BUNDLE` triage table).
- Pass `--commit` to write (automatically takes a consistent SQLite backup first unless `--no-backup`).
- Never calls Gemini or any hosted LLM unless `--analyze-open` is explicitly passed (and even then, ONLY on `OPEN` hits).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")

from services import fullbundle_harvest as fb  # noqa: E402
from services import pmc_oa_and_whitelist_channel as pw  # noqa: E402

_RIGHTS_RESERVED_RE = re.compile(r"all\s+rights\s+reserved", re.I)
# A publisher's per-download stamp that names the subscribing institution, e.g. "Downloaded from
# https://academic.oup.com/... by <Institution> user on 09 November 2026" or "Downloaded from
# https://www.science.org at <Institution> on November 09, 2026". "by guest" (an anonymous, open download)
# does not count.
_INSTITUTION_STAMP_RE = re.compile(
    r"downloaded\s+from\s+\S+(?:\s+\S+){0,3}?\s+(?:by|at)\s+(?!guest\b)(?:the\s+)?\S.{0,120}?"
    r"\s(?:user\s+)?on\s+(?:\[|\d|[a-z]{3,9}\.?\s+\d)",
    re.I | re.S,
)
_OPEN_STATEMENT_RE = re.compile(
    r"creativecommons\.org|creative\s+commons|open\s+access\s+article|this\s+is\s+an\s+open\s+access|"
    r"open\s+access\s+this\s+article|cc[\s-]+by(?:\b|[\s-]+[0-9])|"
    r"©\s*\d{4}\.?\s*the\s+authors?\b|\bthe\s+author\(s\)\s*\d{4}",
    re.I,
)
_PREPRINT_DOI_PREFIXES = ("10.22541/essoar.", "10.31223/", "10.48550/arxiv.", "10.21203/rs.")


def inspect_pdf_license_signals(pdf_bytes: bytes) -> Dict[str, Any]:
    """Scan first 2 pages and last page of a PDF for explicit open/CC vs subscription/institution-stamp statements."""
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        n = doc.page_count
        idxs = sorted({i for i in (0, 1, n - 1) if 0 <= i < n})
        head_two = " ".join(doc[i].get_text("text") or "" for i in range(min(2, n)))
        sampled = " ".join(doc[i].get_text("text") or "" for i in idxs)

    has_open = bool(_OPEN_STATEMENT_RE.search(sampled))
    has_reserved = bool(_RIGHTS_RESERVED_RE.search(head_two))
    has_institution_stamp = bool(_INSTITUTION_STAMP_RE.search(sampled))
    cc_slug = fb.normalize_license_label(sampled) if "creative commons" in sampled.lower() or "creativecommons.org" in sampled.lower() else ""
    if cc_slug and not cc_slug.startswith("cc"):
        cc_slug = "cc-by"
    return {
        "has_open_statement": has_open,
        "has_rights_reserved": has_reserved,
        "has_institution_stamp": has_institution_stamp,
        "pdf_cc_slug": cc_slug,
    }


def classify_pdf_compliance(
    pdf_bytes: bytes,
    *,
    doi: str,
    journal: str = "",
    openalex_work: Optional[Dict[str, Any]] = None,
    crossref_vor_cc: str = "",
) -> Tuple[str, str, str, str]:
    """Decide `(tier, kind, license_slug, why)` for a browser/Zotero-saved PDF.

    Fail-closed: a publisher PDF only gets `TIER_OPEN` when positively backed by an open/CC statement,
    preprint prefix, or verified OA metadata WITHOUT `'All rights reserved'` / institutional subscription stamp.
    """
    clean_doi = fb.normalize_doi(doi or "")
    low_doi = clean_doi.lower()
    sig = inspect_pdf_license_signals(pdf_bytes)

    # 1. Preprints (ESSOAr, EarthArXiv, arXiv, Research Square) are open copies by repository policy
    if low_doi.startswith(_PREPRINT_DOI_PREFIXES):
        return fb.TIER_OPEN, "open_copy", sig["pdf_cc_slug"] or "preprint-open", "preprint_doi"

    # 2. Explicit Creative Commons / Open-Access statement printed on the PDF itself wins
    if sig["has_open_statement"]:
        lic = sig["pdf_cc_slug"] or "open-access-statement"
        return fb.TIER_OPEN, "open_vor", lic, f"pdf_carries_open_statement({lic})"

    # 3. Subscription markers without an open statement -> strictly TDM-only (matches tdm_qa_guard rules 5 & 6)
    if sig["has_rights_reserved"]:
        return fb.TIER_TDM, "publisher_copy", "publisher-all-rights-reserved", "pdf_says_all_rights_reserved"
    if sig["has_institution_stamp"]:
        return fb.TIER_TDM, "publisher_copy", "licensed-browser-download", "pdf_carries_institution_download_stamp"

    # 4. Metadata-verified Gold/Hybrid CC VoR (when PDF has neither CC text nor 'All rights reserved'/institution download stamp)
    meta_lic = (
        fb.vor_open_license(openalex_work)
        or crossref_vor_cc
        or pw.infer_gold_oa_vor_license(doi=clean_doi, journal=journal, openalex_work=openalex_work)
    )
    if meta_lic and fb.is_open_license(meta_lic):
        return fb.TIER_OPEN, "open_vor", meta_lic, f"metadata_vor_open({meta_lic})"

    # 5. Default fail-closed: treat unverified publisher PDFs as institution-licensed TDM-only
    return fb.TIER_TDM, "publisher_copy", "publisher-licensed-unverified", "no_open_license_found_fail_closed"


def discover_input_pdfs(paths: Iterable[Path]) -> List[Tuple[Path, Optional[Dict[str, Any]]]]:
    """Collect `(pdf_path, optional_bib_entry)` from Zotero export folders, Zotero `storage/`, or plain dirs/files."""
    from services.pdf_match_core import parse_bib

    found: List[Tuple[Path, Optional[Dict[str, Any]]]] = []
    seen_paths: set[Path] = set()

    for raw_p in paths:
        p = raw_p.expanduser().resolve()
        if p.is_file() and p.suffix.lower() == ".pdf":
            if p not in seen_paths:
                seen_paths.add(p)
                found.append((p, None))
            continue
        if not p.is_dir():
            continue

        # 1) If folder has a Zotero `.bib`, parse structured entries first so BibTeX DOIs are authoritative
        bibs = sorted(p.glob("*.bib"))
        for bib in bibs:
            for entry in parse_bib(bib):
                rel = entry.get("file_rel") or ""
                if not rel:
                    continue
                cand = (p / rel).resolve()
                if cand.is_file() and cand.suffix.lower() == ".pdf" and cand not in seen_paths:
                    seen_paths.add(cand)
                    found.append((cand, entry))

        # 2) Also pick up any remaining `.pdf` files recursively (e.g. Zotero `storage/<8char>/*.pdf` or flat folder)
        for pdf_file in sorted(p.rglob("*.pdf")):
            rp = pdf_file.resolve()
            if rp not in seen_paths and not rp.name.startswith("._"):
                seen_paths.add(rp)
                found.append((rp, None))

    return found


def export_browser_checklist(
    db_path: Path,
    cache_path: Path,
    out_csv: Path,
    *,
    prefixes: Sequence[str] = (),
) -> Dict[str, int]:
    """Export a clean CSV of unretrieved papers for human Zotero Connector saving, pre-labeled by OPEN vs TDM."""
    meta_cache = pw.MetadataFallbackCache(cache_path, readonly=True)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, doi, title, journal, published_date, link, retrieval_status, retrieval_error_reason "
        "FROM papers "
        "WHERE (pdf_path IS NULL OR pdf_path = '') "
        "AND COALESCE(retrieval_status, '') NOT IN ('held_tdm_only', 'unsupported') "
        "ORDER BY published_date DESC, id"
    ).fetchall()
    conn.close()

    norm_prefixes = tuple(p.strip().lower().rstrip("/") + "/" for p in prefixes if p.strip())
    records: List[Dict[str, str]] = []
    stats: Counter = Counter()

    for r in rows:
        doi = (r["doi"] or "").strip()
        low = doi.lower()
        reason = (r["retrieval_error_reason"] or "")

        # Skip non-paper dataset / review / software / issue-cover rows automatically
        if low.startswith(("10.5281/zenodo.", "10.6084/", "10.5880/", "10.7914/", "10.14470/", "10.17603/", "10.48440/", "10.48546/")):
            continue
        if re.search(r"-(?:rc|ac|cc|sc|ec)\d+$", low) or low.endswith("-supplement"):
            continue
        if low in ("10.1029/aga2.v7.7", "10.1029/grl.v53.14", "10.1029/jgrb.v131.11"):
            continue
        if r["retrieval_status"] == "auth_required" and "too_few_pages_" in reason:
            continue
        if norm_prefixes and not low.startswith(norm_prefixes):
            continue

        work = meta_cache.get("openalex", doi, max_age_days=365.0) if doi else None
        oa_lic = (
            fb.vor_open_license(work)
            or pw.infer_gold_oa_vor_license(doi=doi, journal=r["journal"] or "", openalex_work=work)
        )
        if low.startswith(_PREPRINT_DOI_PREFIXES):
            expected = "APP_OPEN (preprint)"
        elif oa_lic:
            expected = f"APP_OPEN ({oa_lic})"
        else:
            expected = "LOCAL_TDM_BUNDLE (subscribed)"

        stats[expected.split()[0]] += 1
        records.append({
            "expected_route": expected,
            "journal": r["journal"] or "",
            "published_date": r["published_date"] or "",
            "doi": doi,
            "open_url": f"https://doi.org/{doi}" if doi else (r["link"] or ""),
            "title": " ".join((r["title"] or "").split()),
            "paper_id": r["id"],
        })

    # Sort APP_OPEN first so the user gets immediate App-attachable wins first, then by date desc
    records.sort(key=lambda x: (0 if x["expected_route"].startswith("APP_OPEN") else 1, x["journal"], x["doi"]))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["expected_route", "journal", "published_date", "doi", "open_url", "title", "paper_id"],
        )
        writer.writeheader()
        writer.writerows(records)
    return {"total": len(records), "app_open": stats["APP_OPEN"], "local_tdm": stats["LOCAL_TDM_BUNDLE"]}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Compliance-safe Zotero / Browser PDF triage importer for SeismoHub v2.")
    ap.add_argument("inputs", nargs="*", help="Zotero export folder(s), Zotero storage dir, or folder/PDF paths")
    ap.add_argument("--export-batch", default="", metavar="CSV_PATH",
                    help="Export prioritized browser/Zotero checklist CSV (e.g. paper_bundles/_reports/gsw_batch.csv)")
    ap.add_argument("--doi-prefix", action="append", default=[],
                    help="Filter --export-batch by DOI prefix (repeatable, e.g. --doi-prefix 10.1785 --doi-prefix 10.1190)")
    ap.add_argument("--db", default="", help="SQLite DB path (default: $SEISMO_DB_PATH or ./seismo_hub.db)")
    ap.add_argument("--bundle-root", default="", help="Paper bundles root (default: ./paper_bundles)")
    ap.add_argument("--downloads-dir", default="", help="App OPEN PDFs dir (default: ./downloads)")
    ap.add_argument("--commit", action="store_true", help="Write OPEN PDFs to downloads/ and TDM PDFs to paper_bundles/")
    ap.add_argument("--online-license-check", action="store_true",
                    help="Query OpenAlex/Crossref APIs when local metadata cache misses")
    ap.add_argument("--analyze-open", action="store_true",
                    help="Run analyze_paper_from_pdf ONLY on newly attached OPEN-tier papers (never TDM)")
    ap.add_argument("--no-backup", action="store_true", help="Skip pre-write SQLite backup (throwaway test DBs only)")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.db:
        os.environ["SEISMO_DB_PATH"] = str(Path(args.db).expanduser().resolve())

    import db as db_mod
    from models import Paper
    from scripts.harvest_pdfs import backup_db
    from services import pdf_naming
    from services.pdf_match_core import LibraryIndex, extract_match_signals, match_signals

    db_path = Path(db_mod.DATABASE_PATH)
    bundle_root = Path(args.bundle_root).expanduser().resolve() if args.bundle_root else (REPO / "paper_bundles")
    downloads_dir = Path(args.downloads_dir).expanduser().resolve() if args.downloads_dir else pdf_naming.DOWNLOADS_DIR
    cache_path = bundle_root / "_metadata_cache.sqlite"

    if args.export_batch:
        out_csv = Path(args.export_batch).expanduser().resolve()
        summary = export_browser_checklist(db_path, cache_path, out_csv, prefixes=args.doi_prefix)
        print(f"Checklist written: {out_csv}")
        print(f"  Total papers: {summary['total']}  |  APP_OPEN: {summary['app_open']}  |  LOCAL_TDM_BUNDLE: {summary['local_tdm']}")
        if not args.inputs:
            return 0

    if not args.inputs:
        print("Pass input folder/PDF path(s) to triage, or --export-batch <path.csv> to generate a checklist.", file=sys.stderr)
        return 2

    items = discover_input_pdfs(Path(x) for x in args.inputs)
    mode = "COMMIT (writing OPEN -> downloads/, TDM -> paper_bundles/)" if args.commit else "DRY-RUN (read-only preview)"
    print(f"SeismoHub Zotero Compliance Triage | {mode}")
    print(f"  PDF files discovered: {len(items)}")
    if not items:
        return 0

    meta_cache = pw.MetadataFallbackCache(cache_path, readonly=not args.commit)
    http = fb.PoliteSession() if args.online_license_check else None

    ts = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    if args.commit and not args.no_backup:
        bpath = backup_db(db_path, ts)
        print(f"  DB backup: {bpath}")

    session = db_mod.SessionLocal()
    counts: Counter = Counter()
    newly_attached_open: List[Tuple[str, str]] = []
    report_rows: List[Dict[str, Any]] = []

    try:
        lib = LibraryIndex(session=session)
        for idx, (pdf_path, bib_entry) in enumerate(items, 1):
            data = pdf_path.read_bytes()
            gate = fb.inspect_pdf(data)
            if not gate.ok:
                counts[f"rejected:{gate.reason}"] += 1
                print(f"[{idx}/{len(items)}] REJECT  {gate.reason:<24} {pdf_path.name}")
                continue

            sig = extract_match_signals(data, pdf_path.name)
            mres = match_signals(sig, lib, llm_client=None, bib_entry=bib_entry)
            if mres.action == "already_attached":
                counts["already_in_app"] += 1
                print(f"[{idx}/{len(items)}] HAVE    already attached          {pdf_path.name}")
                continue
            if mres.action != "attach" or not mres.paper_id:
                counts[f"match_{mres.action}"] += 1
                print(f"[{idx}/{len(items)}] SKIP    match={mres.action:<18} {pdf_path.name}")
                continue

            paper = session.get(Paper, mres.paper_id)
            if paper is None:
                counts["paper_missing"] += 1
                continue
            if paper.pdf_path:
                counts["already_in_app"] += 1
                continue

            doi = fb.normalize_doi(paper.doi or (bib_entry or {}).get("doi") or (sig.dois[0] if sig.dois else ""))
            work = meta_cache.get("openalex", doi, max_age_days=365.0) if doi else None
            cr_lic = ""
            if work is None and http is not None and doi:
                r_oa = http.get(f"https://api.openalex.org/works/doi:{doi}", headers={"Accept": "application/json"})
                if r_oa.ok:
                    try:
                        work = r_oa.json()
                        meta_cache.put("openalex", doi, work)
                    except Exception:
                        work = None
                r_cr = http.get(f"https://api.crossref.org/works/{doi}", headers={"Accept": "application/json"})
                if r_cr.ok:
                    try:
                        cr_lic = fb.crossref_vor_cc_license((r_cr.json() or {}).get("message") or {})
                    except Exception:
                        cr_lic = ""

            tier, kind, lic_slug, why = classify_pdf_compliance(
                data,
                doi=doi,
                journal=paper.journal or "",
                openalex_work=work,
                crossref_vor_cc=cr_lic,
            )
            hit = fb.PdfHit(
                channel="zotero_triage",
                url=f"local://zotero/{pdf_path.name}",
                data=data,
                pages=gate.pages,
                chars=gate.chars,
                kind=kind,
                version="publishedVersion" if kind != "open_copy" else "submittedVersion",
                license=lic_slug,
                tier=tier,
                identity=f"{mres.method}({mres.score:.2f})",
            )
            ref = fb.PaperRef(
                paper_id=paper.id,
                doi=doi,
                title=paper.title or "",
                journal=paper.journal or "",
                link=paper.link or "",
                source=paper.source or "",
            )

            if tier == fb.TIER_OPEN:
                route_badge = "OPEN->APP   "
                counts["open_to_app" if args.commit else "would_attach_open"] += 1
                if args.commit:
                    out = fb.HarvestOutcome(
                        ref=ref,
                        doi=doi,
                        title=paper.title or "",
                        app_hit=hit,
                        status=fb.SUCCESS,
                        reason=f"zotero_triage:{tier}({why})",
                        attempts=[fb.Attempt("zotero_triage", hit.url, fb.SUCCESS, reason=why, bytes=len(data), started_at=fb._now_iso())],
                    )
                    att = fb.attach_hit_to_paper(session, paper.id, hit, downloads_dir=downloads_dir)
                    fb.record_harvest(session, out, att)
                    session.commit()
                    if att.attached:
                        row_proxy = lib.by_id.get(paper.id)
                        if row_proxy is not None:
                            lib.register_attached(row_proxy, hit.sha256)
                        fb.write_bundle(out, bundle_root, attached_pdf_path=att.pdf_path, attach_reason=att.reason)
                        newly_attached_open.append((paper.id, att.pdf_path))
            else:
                route_badge = "TDM->BUNDLE "
                counts["tdm_to_bundle" if args.commit else "would_hold_tdm_bundle"] += 1
                if args.commit:
                    out = fb.HarvestOutcome(
                        ref=ref,
                        doi=doi,
                        title=paper.title or "",
                        held_hit=hit,
                        status=fb.HELD_TDM_ONLY,
                        reason=f"zotero_triage:{tier}({why}); kept in paper_bundles/ only",
                        attempts=[fb.Attempt("zotero_triage", hit.url, fb.SUCCESS, reason=why, bytes=len(data), started_at=fb._now_iso())],
                    )
                    fb.record_harvest(session, out, fb.AttachResult(False, ""))
                    session.commit()
                    fb.write_bundle(out, bundle_root)

            print(f"[{idx}/{len(items)}] {route_badge} {lic_slug[:22]:<22} {doi or paper.id:<26} {(paper.title or '')[:46]}")
            report_rows.append({
                "file": str(pdf_path),
                "paper_id": paper.id,
                "doi": doi,
                "tier": tier,
                "license": lic_slug,
                "why": why,
                "committed": bool(args.commit),
            })

        if args.commit and report_rows:
            rep_dir = bundle_root / "_reports"
            rep_dir.mkdir(parents=True, exist_ok=True)
            rep_file = rep_dir / f"zotero_triage_{ts}.jsonl"
            with open(rep_file, "w", encoding="utf-8") as fh:
                for r in report_rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"  report: {rep_file}")

        if args.commit and args.analyze_open and newly_attached_open:
            from services.analyze_service import analyze_paper_from_pdf
            print(f"\nAnalyzing {len(newly_attached_open)} newly attached OPEN papers...")
            for pid, ppath in newly_attached_open:
                analyze_paper_from_pdf(pid, ppath, session)
    finally:
        session.close()

    print("\n== triage summary ==")
    for k, v in sorted(counts.items()):
        print(f"  {k:<28} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
