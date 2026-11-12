#!/usr/bin/env python3
"""Re-run Gemini analysis on papers whose stored analysis is an empty fallback
(the "Model output could not be parsed as JSON" case — see analyze_service
_run_analysis). Targets only papers that HAVE a pdf_path so the re-run uses the
full Pro PDF pipeline.

Usage:
    python3 scripts/reanalyze_failed.py            # dry list only
    python3 scripts/reanalyze_failed.py --run      # re-analyze all failed
    python3 scripts/reanalyze_failed.py --run --limit 1   # test on one
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")

from sqlalchemy import text  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import Paper  # noqa: E402


def _failed_paper_ids(session):
    rows = session.execute(text(
        """
        SELECT a.paper_id
        FROM ai_analyses a
        JOIN papers p ON p.id = a.paper_id
        WHERE p.pdf_path IS NOT NULL AND p.pdf_path <> ''
          AND (
            a.evidence_notes LIKE '%could not be parsed%'
            OR (coalesce(a.summary,'') = '' AND coalesce(a.relevance_score,0) = 0)
          )
        ORDER BY a.updated_at
        """
    )).fetchall()
    return [r[0] for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="actually re-analyze (else just list)")
    ap.add_argument("--limit", type=int, default=0, help="cap how many to re-analyze")
    ap.add_argument("--all-with-pdf", action="store_true",
                    help="re-analyze EVERY paper that has a PDF (e.g. after a model switch), "
                         "not just the empty/failed ones")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        if args.all_with_pdf:
            ids = [r[0] for r in session.execute(text(
                "SELECT id FROM papers WHERE pdf_path IS NOT NULL AND pdf_path <> '' "
                "ORDER BY updated_at")).fetchall()]
        else:
            ids = _failed_paper_ids(session)
        if args.limit:
            ids = ids[: args.limit]
        _scope = "with a PDF" if args.all_with_pdf else "with a failed/empty analysis + a PDF"
        print(f"{len(ids)} paper(s) {_scope}:")
        for pid in ids:
            p = session.get(Paper, pid)
            print(f"  {pid}  {(p.title or '')[:70]}")
        if not args.run:
            print("\n(dry list — pass --run to re-analyze)")
            return

        from services.analyze_service import analyze_paper_from_pdf, PDF_ANALYSIS_MODEL
        print(f"\n=== re-analyzing {len(ids)} with {PDF_ANALYSIS_MODEL} "
              f"(max_output_tokens={os.environ.get('SEISMO_ANALYSIS_MAX_TOKENS','65535')}) ===")
        ok = fail = 0
        for pid in ids:
            p = session.get(Paper, pid)
            try:
                print(f"  analyzing {pid} …", flush=True)
                analyze_paper_from_pdf(pid, p.pdf_path, session)
                session.expire_all()
                a = session.get(Paper, pid).ai_analysis
                summ_len = len((a.summary or "")) if a else 0
                rel = (a.relevance_score if a else None)
                note = (a.evidence_notes or "") if a else ""
                parsed = "PARSED" if summ_len > 0 and "could not be parsed" not in note else "STILL-EMPTY"
                print(f"    {parsed}  summary_len={summ_len}  relevance={rel}")
                ok += 1 if parsed == "PARSED" else 0
                fail += 0 if parsed == "PARSED" else 1
            except Exception as exc:
                print(f"    FAILED {pid}: {exc}")
                fail += 1
        print(f"\n=== done: {ok} parsed, {fail} still-empty/failed ===")
    finally:
        session.close()


if __name__ == "__main__":
    main()
