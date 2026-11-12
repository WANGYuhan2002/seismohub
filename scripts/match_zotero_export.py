#!/usr/bin/env python3
"""Match a Zotero "Export → files" folder's PDFs to existing library papers,
then optionally attach the PDF and trigger a Gemini-Pro analysis.

Uses the SAME matcher as the live ``match_pdfs`` endpoint
(``services.pdf_match_core``) — DOI (from the .bib) is authoritative, title is a
clean fallback, an optional Gemini adjudicator breaks ambiguous ties, and an
existing PDF is never overwritten.

Folder layout expected:
    <folder>/<name>.bib
    <folder>/files/<itemId>/<Author> - <Year> - <Title>.pdf

Safety: default is a DRY RUN (no writes, no network, no LLM). ``--commit``
attaches matched PDFs; ``--analyze`` (implies commit) runs analyze_paper_from_pdf
on each newly-attached paper; ``--llm`` lets Gemini adjudicate ambiguous ties.

Usage:
    python3 scripts/match_zotero_export.py <folder> [<folder2> ...]
    python3 scripts/match_zotero_export.py <folder> --commit --analyze
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")

from db import SessionLocal, DATABASE_PATH  # noqa: E402
from models import Paper  # noqa: E402
from services.pdf_match_core import (  # noqa: E402  (one shared matcher)
    LibraryIndex,
    extract_match_signals,
    match_signals,
    parse_bib,
)
from services.pdf_naming import (  # noqa: E402
    DOWNLOADS_DIR,
    pdf_target_path_for_paper,
    write_pdf_atomic,
)


def _short(s: str, n: int = 52) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def process_folder(folder: Path, lib: LibraryIndex, session, args, llm_client):
    bibs = sorted(folder.glob("*.bib"))
    if not bibs:
        print(f"  !! no .bib found in {folder}")
        return {"tally": {}, "attached": []}
    entries = parse_bib(bibs[0])
    have_pdf = [e for e in entries if e["file_rel"]]
    print(f"\n=== {folder.name}  ({bibs[0].name}: {len(entries)} entries, "
          f"{len(have_pdf)} with a PDF) ===")
    tally: Dict[str, int] = {}
    attached: List[Tuple[str, str]] = []
    for e in have_pdf:
        pdf_path = folder / e["file_rel"]
        if not pdf_path.exists():
            print(f"  MISSING FILE  {e['file_rel']}")
            tally["missing_file"] = tally.get("missing_file", 0) + 1
            continue
        data = pdf_path.read_bytes()
        sig = extract_match_signals(data, pdf_path.name)
        res = match_signals(sig, lib, llm_client=llm_client, bib_entry=e)
        tally[res.action] = tally.get(res.action, 0) + 1
        row = lib.by_id.get(res.paper_id) if res.paper_id else None
        tgt = f"{row.id} {_short(row.title, 46)}" if row is not None else "—"
        badge = {"attach": "ATTACH ", "conflict": "CONFLICT", "already_attached": "HAVE   ",
                 "needs_review": "REVIEW ", "unmatched": "MISS   "}.get(res.action, res.action)
        print(f"  [{badge}] {res.method or '-':13} {res.score:.2f}  "
              f"{_short(e['title'], 44):45}  -> {tgt}")
        if res.action == "needs_review":
            for c in res.candidates[:3]:
                print(f"            cand {c['score']:.2f}  {c['paper_id']}  {_short(c['title'], 50)}")

        if res.action == "attach" and (args.commit or args.analyze) and row is not None:
            paper = session.get(Paper, row.id)
            target = pdf_target_path_for_paper(paper, DOWNLOADS_DIR)
            write_pdf_atomic(target, data)
            paper.pdf_path = str(target.resolve())
            paper.pdf_sha256 = sig.sha256
            session.commit()
            lib.register_attached(row, sig.sha256)  # keep the shared index in sync
            attached.append((paper.id, paper.pdf_path))
    return {"tally": tally, "attached": attached}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folders", nargs="+", help="Zotero export folder(s)")
    ap.add_argument("--commit", action="store_true", help="attach matched PDFs (writes DB)")
    ap.add_argument("--analyze", action="store_true",
                    help="run Gemini-Pro analysis on newly-attached papers (implies --commit)")
    ap.add_argument("--llm", action="store_true", help="let Gemini adjudicate ambiguous title ties")
    args = ap.parse_args()
    writing = args.commit or args.analyze

    print(f"DB: {DATABASE_PATH}")
    if not writing:
        print(">>> DRY RUN — no DB writes, no network, no LLM (pass --commit to attach)\n")

    llm_client = None
    if args.llm:
        try:
            from services.analyze_service import _get_vertex_client
            llm_client = _get_vertex_client()
        except Exception as exc:
            print(f"!! --llm requested but no Vertex client ({exc}); ambiguous ties -> review")

    session = SessionLocal()
    all_attached: List[Tuple[str, str]] = []
    grand: Dict[str, int] = {}
    try:
        lib = LibraryIndex(session=session)
        print(f"Library: {len(lib.by_id)} papers "
              f"({len(lib.by_doi)} with DOI, {len(lib.by_sha)} with a PDF)")
        for f in args.folders:
            out = process_folder(Path(f).expanduser(), lib, session, args, llm_client)
            for k, v in out["tally"].items():
                grand[k] = grand.get(k, 0) + v
            all_attached.extend(out["attached"])

        print("\n=== summary ===")
        for k in sorted(grand):
            print(f"  {k:16} {grand[k]}")

        if args.analyze and all_attached:
            from services.analyze_service import analyze_paper_from_pdf, PDF_ANALYSIS_MODEL
            print(f"\n=== analyzing {len(all_attached)} newly-attached papers with {PDF_ANALYSIS_MODEL} ===")
            for pid, ppath in all_attached:
                try:
                    print(f"  analyzing {pid} …", flush=True)
                    analyze_paper_from_pdf(pid, ppath, session)
                    print(f"    ok {pid}")
                except Exception as exc:
                    print(f"    FAILED {pid}: {exc}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
