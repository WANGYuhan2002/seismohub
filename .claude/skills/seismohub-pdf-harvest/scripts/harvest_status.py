#!/usr/bin/env python3
"""Summarise SeismoHub PDF-harvest reports. Read-only: never writes, never touches the network or the database.

Library runs (``scripts/harvest_pdfs.py``) write ``paper_bundles/_reports/harvest_<ts>.jsonl`` (one JSON line per
paper) and ``pending_browser_fetch_<ts>.tsv``. Reading-list runs (``scripts/harvest_literature_list.py``) write
``<out-dir>/harvest_summary.tsv``.

Examples::

    python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py                # latest library report
    python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --all-reports  # latest status per paper
    python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --pending      # browser-download list
    python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --doi 10.1029/2026GL125440 --attempts
    python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --pack ~/seismo_pack
    python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --json

Only metadata from the reports is shown. The script never opens PDFs or XML, so licensed full text is not read.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

NEXT_STEP = {
    "success": "nothing to do",
    "held_tdm_only": "licensed copy kept in paper_bundles/ (local reading only); retry later for an open copy",
    "captcha_or_bot_block": "publisher blocks scripts: download in a browser, then 'Upload missing PDFs'",
    "no_pdf_but_html": "host served HTML (e.g. PMC): open the link in a browser and save the PDF",
    "auth_required": "login wall: browser download from a network with access",
    "no_oa_copy": "no open copy found: browser download, author page or repository",
    "transient_error": "timeouts / HTTP 5xx: rerun with --retry-failed later",
    "unsupported": "not attachable (duplicate, not a paper, or rejected format)",
    "unsupported_content_type": "rejected format: usually a dataset or software record",
}


def find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "main.py").is_file() and (candidate / "services" / "fullbundle_harvest.py").is_file():
            return candidate
    return Path.cwd()


def host_path(url: str) -> str:
    """Host + path only. Query strings are dropped so no token-bearing parameter is ever printed."""
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.netloc}{parts.path}" if parts.netloc else url.split("?", 1)[0]


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def matches(row: Dict[str, Any], args: argparse.Namespace) -> bool:
    if args.doi and (row.get("doi") or "").lower() not in {d.lower() for d in args.doi}:
        return False
    if args.paper_id and str(row.get("paper_id") or "") not in set(args.paper_id):
        return False
    if args.journal and not any(j.lower() in (row.get("journal") or "").lower() for j in args.journal):
        return False
    return True


def outcome_channel(row: Dict[str, Any]) -> str:
    for key in ("app_copy", "held_copy", "held_xml"):
        part = row.get(key)
        if isinstance(part, dict) and part.get("channel"):
            return str(part["channel"])
    return ""


# Outcome of one report row, best first. Merging several reports keeps the best outcome per paper
# (a paper attached in an earlier run stays attached even if a later run skipped it).
OUTCOMES = (
    ("attached", "attached (open copy)"),
    ("already_had_pdf", "already had a PDF"),
    ("duplicate", "same PDF already on another paper"),
    ("held", "held licensed copy (local only)"),
    ("not_attached", "verified copy not attached"),
    ("no_full_text", "no full text yet"),
)
RANK = {key: len(OUTCOMES) - i for i, (key, _label) in enumerate(OUTCOMES)}


def outcome(row: Dict[str, Any]) -> str:
    reason = str(row.get("attach_reason") or "")
    if row.get("attached"):
        return "attached"
    if reason.startswith("already_has_pdf"):
        return "already_had_pdf"
    if reason.startswith(("same_pdf_already_attached_to", "inferred_doi_already_attached_to")):
        return "duplicate"
    if row.get("held_copy") or row.get("held_xml"):
        return "held"
    if row.get("app_copy"):
        return "not_attached"
    return "no_full_text"


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    outcomes = Counter(outcome(r) for r in rows)
    missing = [r for r in rows if outcome(r) == "no_full_text"]
    missing_status = Counter(r.get("status") or "unknown" for r in missing)
    attached = Counter(outcome_channel(r) for r in rows if outcome(r) == "attached")
    held = Counter(outcome_channel(r) or "unknown" for r in rows if outcome(r) == "held")
    missing_journals = Counter((r.get("journal") or "unknown") for r in missing)
    failures: Counter = Counter()
    for r in missing:
        for a in r.get("attempts") or []:
            if a.get("outcome") and a.get("outcome") != "success":
                host = urllib.parse.urlsplit(a.get("url") or "").netloc or a.get("channel") or "?"
                failures[f"{a['outcome']} @ {host}"] += 1
    return {
        "papers": len(rows),
        "outcomes": {key: outcomes.get(key, 0) for key, _label in OUTCOMES},
        "no_full_text_by_status": dict(missing_status.most_common()),
        "attached_by_channel": dict(attached.most_common()),
        "held_by_channel": dict(held.most_common()),
        "no_full_text_by_journal": dict(missing_journals.most_common(10)),
        "top_failures": dict(failures.most_common(8)),
    }


def print_summary(label: str, summary: Dict[str, Any]) -> None:
    print(f"{label}  ({summary['papers']} papers)")
    labels = dict(OUTCOMES)
    for key, value in summary["outcomes"].items():
        if value:
            print(f"  {labels[key]:<34} {value:>5}")
    if summary["attached_by_channel"]:
        print("attached open copies by channel: "
              + ", ".join(f"{k}={v}" for k, v in summary["attached_by_channel"].items()))
    if summary["held_by_channel"]:
        print("held licensed copies (local only) by channel: "
              + ", ".join(f"{k}={v}" for k, v in summary["held_by_channel"].items()))
    if summary["no_full_text_by_status"]:
        print("no full text yet, by status:")
        for key, value in summary["no_full_text_by_status"].items():
            print(f"  {key:<26} {value:>5}   {NEXT_STEP.get(key, '')}")
        print("  by journal: " + ", ".join(f"{k} ({v})" for k, v in summary["no_full_text_by_journal"].items()))
        if summary["top_failures"]:
            print("  most common failed attempts:")
            for key, value in summary["top_failures"].items():
                print(f"    {value:>4}  {key}")


def print_attempts(rows: Iterable[Dict[str, Any]]) -> None:
    for r in rows:
        print(f"\n{r.get('doi') or r.get('paper_id')}  [{r.get('status')}]  {(r.get('title') or '')[:90]}")
        if r.get("reason"):
            print(f"  reason: {r['reason']}")
        for key in ("app_copy", "held_copy"):
            part = r.get(key)
            if isinstance(part, dict):
                print(f"  {key}: {part.get('channel')} {part.get('license_tier')} {part.get('pages')}p "
                      f"licence={part.get('license')} from {host_path(part.get('source_url') or '')}")
        for a in r.get("attempts") or []:
            http = f" HTTP {a['http_status']}" if a.get("http_status") else ""
            print(f"  - {a.get('channel', ''):<14} {a.get('outcome', ''):<24}{http}  {host_path(a.get('url') or '')}"
                  + (f"  ({a['reason'][:120]})" if a.get("reason") else ""))


def latest(paths: List[Path]) -> Optional[Path]:
    return sorted(paths, key=lambda p: p.name)[-1] if paths else None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Summarise SeismoHub PDF-harvest reports (read-only).")
    ap.add_argument("--bundle-root", default="", help="paper_bundles folder (default: <repo>/paper_bundles)")
    ap.add_argument("--report", default="", help="a specific harvest_<ts>.jsonl (default: the latest)")
    ap.add_argument("--all-reports", action="store_true", help="merge all reports; the latest entry per paper wins")
    ap.add_argument("--doi", action="append", default=[], help="only this DOI (repeatable)")
    ap.add_argument("--paper-id", action="append", default=[], help="only this paper id (repeatable)")
    ap.add_argument("--journal", action="append", default=[], help="only journals containing this text")
    ap.add_argument("--attempts", action="store_true", help="list every attempt for the selected papers")
    ap.add_argument("--pending", action="store_true", help="print the browser-download list of the report")
    ap.add_argument("--pack", default="", help="summarise a reading-list output folder (harvest_summary.tsv)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.pack:
        tsv = Path(args.pack).expanduser() / "harvest_summary.tsv"
        if not tsv.is_file():
            print(f"no harvest_summary.tsv in {tsv.parent}", file=sys.stderr)
            return 2
        with open(tsv, encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh, delimiter="\t")]
        status = Counter(r.get("status") or "unknown" for r in rows)
        channels = Counter(r.get("channel") or "-" for r in rows if r.get("status") == "open_pdf")
        result = {"pack": str(tsv.parent), "papers": len(rows), "status": dict(status.most_common()),
                  "open_by_channel": dict(channels.most_common())}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            print(f"{tsv}  ({len(rows)} papers)")
            for key, value in status.most_common():
                print(f"  {key:<26} {value:>5}")
            if channels:
                print("open PDFs by channel: " + ", ".join(f"{k}={v}" for k, v in channels.most_common()))
        return 0

    repo = find_repo_root(Path(__file__).resolve().parent)
    bundle_root = Path(args.bundle_root).expanduser() if args.bundle_root else repo / "paper_bundles"
    reports_dir = bundle_root / "_reports"
    reports = sorted(reports_dir.glob("harvest_*.jsonl"), key=lambda p: p.name) if reports_dir.is_dir() else []

    if args.report:
        chosen = [Path(args.report).expanduser()]
    elif args.all_reports:
        chosen = reports
    else:
        chosen = [p for p in [latest(reports)] if p]
    if not chosen:
        print(f"no harvest reports in {reports_dir} - run scripts/harvest_pdfs.py first "
              "(a --dry-run writes no report)", file=sys.stderr)
        return 2

    merged: Dict[str, Dict[str, Any]] = {}
    for path in chosen:
        for row in read_jsonl(path):
            key = str(row.get("paper_id") or row.get("doi") or f"row{len(merged)}")
            previous = merged.get(key)
            # keep the best outcome per paper; on a tie the later report wins
            if previous is None or RANK[outcome(row)] >= RANK[outcome(previous)]:
                merged[key] = row
    rows = [r for r in merged.values() if matches(r, args)]
    label = str(chosen[0]) if len(chosen) == 1 else f"{len(chosen)} reports in {reports_dir}"
    summary = summarise(rows)

    if args.json:
        summary["reports"] = [str(p) for p in chosen]
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print_summary(label, summary)
        if args.attempts:
            print_attempts(rows)

    if args.pending:
        stamp = chosen[-1].stem.replace("harvest_", "", 1)
        pending_tsv = reports_dir / f"pending_browser_fetch_{stamp}.tsv"
        if pending_tsv.is_file():
            with open(pending_tsv, encoding="utf-8") as fh:
                pend = [r for r in csv.DictReader(fh, delimiter="\t") if matches(r, args)]
            print(f"\npending browser downloads: {pending_tsv} ({len(pend)} rows)")
            for r in pend:
                print(f"  {r.get('status', ''):<22} {r.get('doi') or r.get('paper_id', ''):<34} "
                      f"{(r.get('journal') or '')[:24]:<24} {r.get('open_url', '')}")
        else:
            print(f"\nno pending list for this report ({pending_tsv.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
