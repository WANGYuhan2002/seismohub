#!/usr/bin/env python3
"""Standalone Seismology Literature Harvester (`scripts/harvest_literature_list.py`).

Fetches verified full-text PDFs (+ bundles) for ANY list of seismology papers across decades
(1950s–2026) — by DOI, arXiv ID, URL, or citation/title — into a dedicated output folder
WITHOUT touching `seismo_hub.db` (unless `--register-db` is explicitly passed).

Output layout under `--out-dir <DIR>` (default: `./literature_packs/<timestamp>`):
  <DIR>/open_pdfs/             <- ONLY `OPEN_CC_BY_OR_PREPRINT` PDFs (safe to read, share, or send to hosted AI)
  <DIR>/tdm_bundles/           <- `LICENSED_TDM_ONLY` VoR PDFs & Elsevier `<ce:sections>` / `<xocs:rawtext>` XML
                                  + high-res figures (strictly local use; NEVER upload to hosted cloud AI)
  <DIR>/harvest_summary.tsv    <- One row per paper: year, authors, journal, doi, status, open_pdf, tdm_bundle
  <DIR>/pending_zotero.csv     <- Clickable DOI checklist for any remaining bot-walled BSSA/SRL/SEG papers

Examples:
  python scripts/harvest_literature_list.py --doi 10.1029/JB082i020p02981 --doi "10.1016/0031-9201(81)90046-7" --out-dir /tmp/classics
  python scripts/harvest_literature_list.py --query "Shapiro Campillo 2005 High-resolution surface-wave tomography ambient seismic noise"
  python scripts/harvest_literature_list.py --input-file my_reading_list.txt --out-dir ~/Documents/seismo_pack
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")
try:  # pick up settings from the project .env; never overrides variables already set in the shell
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except Exception:  # pragma: no cover - python-dotenv is a hard dependency, but stay importable without it
    pass

from scripts.harvest_pdfs import keep_non_open_in_bundle  # noqa: E402
from scripts.seismohub_upgrade_runner import install_harvester_upgrades  # noqa: E402
from services import fullbundle_harvest as fb  # noqa: E402

_DOI_EXTRACT_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;\"'<>]+)", re.I)
_ARXIV_EXTRACT_RE = re.compile(r"(?:arxiv[:\s/]+|abs/|pdf/)(\d{4}\.\d{4,6}|[a-z-]+/\d{7})(?:v\d+)?", re.I)


def _clean_slug(text: str, max_len: int = 64) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-")
    return s[:max_len] or "paper"


def resolve_citation_to_metadata(http: fb.PoliteSession, raw_spec: str) -> Dict[str, Any]:
    """Resolve a free-form DOI, arXiv ID, URL, or bibliographic query (e.g. 'Aki 1957 space and time spectra')
    into `{doi, arxiv_id, title, journal, year, authors, link}` via Crossref / arXiv."""
    spec = (raw_spec or "").strip()
    if not spec:
        return {}

    # 1. Explicit arXiv ID or URL
    m_ax = _ARXIV_EXTRACT_RE.search(spec)
    if m_ax and "10." not in spec:
        aid = m_ax.group(1)
        return {
            "doi": f"10.48550/arXiv.{aid}" if "." in aid else "",
            "arxiv_id": aid,
            "title": "",
            "journal": "arXiv",
            "year": "",
            "authors": [],
            "link": f"https://arxiv.org/abs/{aid}",
        }

    # 2. Explicit DOI (including old Wiley/Elsevier parentheses DOIs like `10.1016/0031-9201(81)90046-7`)
    m_doi = _DOI_EXTRACT_RE.search(spec)
    doi = fb.normalize_doi(m_doi.group(1).rstrip(".,;]")) if m_doi else ""

    if doi:
        cr = http.get(
            f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/()')}",
            headers={"Accept": "application/json"},
        )
        if cr.ok:
            try:
                msg = (cr.json() or {}).get("message") or {}
                return _meta_from_crossref_msg(msg, fallback_doi=doi)
            except Exception:
                pass
        return {"doi": doi, "arxiv_id": "", "title": "", "journal": "", "year": "", "authors": [], "link": f"https://doi.org/{doi}"}

    # 3. Bibliographic citation / title query via Crossref (`query.bibliographic`)
    q_url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": spec, "rows": "3"})
    res = http.get(q_url, headers={"Accept": "application/json"})
    if res.ok:
        try:
            items = ((res.json() or {}).get("message") or {}).get("items") or []
        except Exception:
            items = []
        for it in items:
            if it.get("type") in ("component", "dataset", "peer-review", "journal-issue"):
                continue
            cand = _meta_from_crossref_msg(it)
            if cand.get("doi") and cand.get("title"):
                return cand
    return {"doi": "", "arxiv_id": "", "title": spec, "journal": "", "year": "", "authors": [], "link": ""}


def _meta_from_crossref_msg(msg: Dict[str, Any], fallback_doi: str = "") -> Dict[str, Any]:
    doi = fb.normalize_doi(str(msg.get("DOI") or fallback_doi))
    title = html_strip(((msg.get("title") or [""])[0] or ""))
    journal = html_strip(((msg.get("container-title") or [""])[0] or ""))
    year = ""
    for k in ("published-print", "published-online", "issued", "created"):
        parts = (((msg.get(k) or {}).get("date-parts") or [[None]])[0]) or [None]
        if parts and parts[0]:
            year = str(parts[0])
            break
    authors: List[str] = []
    for a in (msg.get("author") or []):
        fam = str(a.get("family") or "").strip()
        giv = str(a.get("given") or "").strip()
        if fam:
            authors.append(f"{giv} {fam}".strip())
    return {
        "doi": doi,
        "arxiv_id": "",
        "title": title,
        "journal": journal,
        "year": year,
        "authors": authors,
        "link": f"https://doi.org/{doi}" if doi else "",
    }


def html_strip(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", (text or "")).split())


def load_specs(args: argparse.Namespace) -> List[str]:
    specs: List[str] = []
    for d in args.doi:
        if d.strip():
            specs.append(d.strip())
    for q in args.query:
        if q.strip():
            specs.append(q.strip())
    if args.input_file:
        p = Path(args.input_file).expanduser().resolve()
        for raw in p.read_text(encoding="utf-8").splitlines():
            ln = raw.strip()
            if not ln or ln.startswith("#"):
                continue
            # Support TSV/CSV lines where column 0 or any token has a DOI, or plain citation lines
            specs.append(ln)
    # Deduplicate preserving order
    seen: set[str] = set()
    out: List[str] = []
    for s in specs:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Standalone multi-decade seismology full-text harvester.")
    ap.add_argument("--doi", action="append", default=[], help="DOI or arXiv ID (repeatable)")
    ap.add_argument("--query", action="append", default=[], help="Bibliographic query, e.g. 'Kanamori 1977 energy release great earthquakes' (repeatable)")
    ap.add_argument("--input-file", default="", help="Text/CSV/TSV file with one DOI, arXiv ID, or citation per line")
    ap.add_argument("--out-dir", default="", help="Output directory (default: ./literature_packs/pack_<ts>)")
    ap.add_argument("--no-extras", action="store_true", help="Skip supplements / high-res figures / code-data links")
    ap.add_argument("--dry-run", action="store_true", help="Verify download + compliance tier only; write nothing")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    specs = load_specs(args)
    if not specs:
        print("Provide at least one --doi, --query, or --input-file.", file=sys.stderr)
        return 2

    keys = fb.load_harvest_keys()
    cache_path = REPO / "paper_bundles" / "_metadata_cache.sqlite"
    install_harvester_upgrades(fb, cache_path=cache_path, readonly=args.dry_run)
    harvester = fb.FullBundleHarvester(attach_tdm=False)

    ts = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else (REPO / "literature_packs" / f"pack_{ts}")
    open_dir = out_dir / "open_pdfs"
    tdm_dir = out_dir / "tdm_bundles"

    mode = "DRY-RUN (zero writes)" if args.dry_run else f"WRITE -> {out_dir}"
    print(f"SeismoHub Standalone Literature Harvester (1950s–2026) | {mode}")
    print(f"  keys: wiley={'yes' if keys['WILEY_TDM_TOKEN'] else 'no'} elsevier={'yes' if keys['ELSEVIER_API_KEY'] else 'no'} | targets: {len(specs)}")

    if not args.dry_run:
        open_dir.mkdir(parents=True, exist_ok=True)
        tdm_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: List[Dict[str, str]] = []
    pending_zotero: List[Dict[str, str]] = []
    counts: Counter = Counter()

    for idx, spec in enumerate(specs, 1):
        meta = resolve_citation_to_metadata(harvester.http, spec)
        doi = meta.get("doi") or ""
        title = meta.get("title") or ""
        year = meta.get("year") or ""
        authors = meta.get("authors") or []
        journal = meta.get("journal") or ""
        first_author = authors[0].split()[-1] if authors else "Unknown"

        if not doi and not meta.get("arxiv_id"):
            counts["unresolved_query"] += 1
            print(f"[{idx}/{len(specs)}] UNRESOLVED  {spec[:70]}")
            summary_rows.append({
                "query": spec, "year": year, "first_author": first_author, "journal": journal,
                "doi": "", "title": title, "status": "unresolved_citation",
                "open_pdf": "", "tdm_bundle": "", "channel": "",
            })
            continue

        pid = f"lit_{idx:04d}"
        ref = fb.PaperRef(
            paper_id=pid,
            doi=doi,
            title=title,
            journal=journal,
            link=meta.get("link") or "",
            source="standalone_pack",
            arxiv_id=meta.get("arxiv_id") or "",
        )
        setattr(ref, "authors", json.dumps(authors, ensure_ascii=False))

        out = harvester.harvest(ref)
        # Always enforce the hard 'All rights reserved' publisher seal
        keep_non_open_in_bundle(fb, out)

        open_pdf_saved = ""
        tdm_bundle_saved = ""
        fname_base = f"{year or '0000'}_{_clean_slug(first_author, 20)}_{_clean_slug(out.title or title, 55)}.pdf"

        if out.app_hit is not None:
            counts["open_pdf"] += 1
            if not args.dry_run:
                dst = open_dir / fname_base
                dst.write_bytes(out.app_hit.data)
                open_pdf_saved = str(dst)
        if out.held_hit is not None or out.held_xml is not None:
            if out.app_hit is None:
                counts["tdm_only"] += 1
            else:
                counts["open_plus_tdm_vor"] += 1
            if not args.dry_run:
                bdir = tdm_dir / fb.safe_bundle_name(out.doi or doi, out.arxiv_id, pid)
                extras: List[str] = []
                code_rep: Dict[str, Any] = {}
                if not args.no_extras:
                    bdir.mkdir(parents=True, exist_ok=True)
                    extras, code_rep = harvester.collect_extras(out, bdir)
                written_b = fb.write_bundle(
                    out, tdm_dir,
                    attached_pdf_path=open_pdf_saved,
                    attach_reason="standalone_open_pdf" if open_pdf_saved else "tdm_held_local_only",
                    extras=extras,
                    code_report=code_rep,
                )
                tdm_bundle_saved = str(written_b or "")
        elif out.app_hit is not None and not args.dry_run:
            # Also write bundle metadata/extras for pure OPEN hits when extras exist
            if not args.no_extras:
                bdir = tdm_dir / fb.safe_bundle_name(out.doi or doi, out.arxiv_id, pid)
                bdir.mkdir(parents=True, exist_ok=True)
                extras, code_rep = harvester.collect_extras(out, bdir)
                if extras or code_rep:
                    fb.write_bundle(out, tdm_dir, attached_pdf_path=open_pdf_saved, attach_reason="standalone_open_pdf", extras=extras, code_report=code_rep)

        if out.app_hit is None and out.held_hit is None and out.held_xml is None:
            counts[f"pending:{out.status}"] += 1
            pending_zotero.append({
                "year": year,
                "first_author": first_author,
                "journal": journal,
                "doi": out.doi or doi,
                "open_url": f"https://doi.org/{out.doi or doi}" if (out.doi or doi) else (meta.get("link") or ""),
                "title": out.title or title,
                "status": out.status,
            })

        primary = out.app_hit or out.held_hit
        if out.app_hit is not None:
            badge = f"OPEN      {out.app_hit.channel:<18} {out.app_hit.pages:>3}p"
            ch_used = out.app_hit.channel
        elif out.held_hit is not None:
            badge = f"TDM-PDF   {out.held_hit.channel:<18} {out.held_hit.pages:>3}p"
            ch_used = out.held_hit.channel
        elif out.held_xml is not None:
            badge = f"TDM-XML   {'elsevier_tdm_xml':<18} xml "
            ch_used = "elsevier_tdm_xml"
        else:
            badge = f"PENDING   {out.status:<23}"
            ch_used = ""

        print(f"[{idx}/{len(specs)}] {badge}  {year:<4} {first_author[:14]:<14} {(out.doi or doi)[:28]:<28} {(out.title or title)[:45]}", flush=True)
        summary_rows.append({
            "query": spec,
            "year": year,
            "first_author": first_author,
            "journal": journal,
            "doi": out.doi or doi,
            "title": out.title or title,
            "status": "open_pdf" if out.app_hit else ("held_tdm_only" if (out.held_hit or out.held_xml) else out.status),
            "channel": ch_used,
            "open_pdf": open_pdf_saved,
            "tdm_bundle": tdm_bundle_saved,
        })

    if not args.dry_run:
        tsv_path = out_dir / "harvest_summary.tsv"
        with open(tsv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(summary_rows[0].keys()), delimiter="\t")
            w.writeheader()
            w.writerows(summary_rows)
        if pending_zotero:
            pend_path = out_dir / "pending_zotero.csv"
            with open(pend_path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=list(pending_zotero[0].keys()))
                w.writeheader()
                w.writerows(pending_zotero)
        print(f"\nSaved pack to: {out_dir}")
        print(f"  open_pdfs/         : {counts['open_pdf']} files (safe for hosted AI / sharing)")
        print(f"  tdm_bundles/       : {counts['tdm_only'] + counts['open_plus_tdm_vor']} bundles (local use ONLY; never upload TDM to cloud AI)")
        if pending_zotero:
            print(f"  pending_zotero.csv : {len(pending_zotero)} bot-walled papers for 1-click Zotero Connector")
    else:
        print("\n== dry-run summary ==")
        for k, v in sorted(counts.items()):
            print(f"  {k:<24} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
