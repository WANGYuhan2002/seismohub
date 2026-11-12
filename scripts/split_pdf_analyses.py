#!/usr/bin/env python3
"""Give papers whose only analysis read the PDF a title/abstract analysis, keeping the PDF one for display.

Added 2026-11-13. The owner's rule: a paper is kept and classified from its title and abstract (plus public
metadata) only; reading the PDF only adds detail to the paper view. Since then an analysis that reads the PDF is
stored as a display layer (``payload["pdf_analysis"]``, see ``services.analyze_service.PDF_LAYER_KEY``).

Rows written before that change by an analysis that read the PDF (``ai_analyses.pdf_used = 1`` and no PDF layer)
still classify the paper from the PDF: their topics, relevance, keywords and summary came from the full text. They
also stop SeismoHub Lite from publishing, because Lite refuses any AI analysis made from a PDF.

For each such paper this tool runs the normal title/abstract analysis
(``analyze_service.analyze_paper_from_metadata``, the quick model). Storing it moves the old PDF analysis into the
display layer and puts the new title/abstract analysis in its place: topics, facets, relevance, keywords and the
summary columns now come from title + abstract, and the paper is re-matched to topic trackers.

Tracker links that the old PDF-based topics created are not removed (assignments only grow); review them in the
app if needed.

Usage:
    python3 scripts/split_pdf_analyses.py                 # preview: which papers, how many model calls (free)
    python3 scripts/split_pdf_analyses.py --run --limit 1 # paid: one paper first
    python3 scripts/split_pdf_analyses.py --run           # paid: all of them

``--run`` backs up the database first (``_db_backup_<ts>_pre_split_pdf_analyses/``) and writes one JSON line per
paper to ``output/split_pdf_analyses_<ts>.jsonl``.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")


def legacy_rows(session):
    """Papers whose analysis read the PDF and that have no title/abstract analysis yet."""
    from models import AIAnalysis, Paper
    from services.analyze_service import pdf_layer_of

    rows = (session.query(Paper).join(AIAnalysis, AIAnalysis.paper_id == Paper.id)
            .filter(AIAnalysis.pdf_used.is_(True))
            .order_by(Paper.published_date.desc()).all())
    return [p for p in rows if pdf_layer_of(p.ai_analysis) is None]


def backup_db(tag: str) -> Path:
    import db as _db

    src = Path(_db.DATABASE_PATH).resolve()
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    dest_dir = BASE_DIR / f"_db_backup_{stamp}_{tag}"
    dest_dir.mkdir()
    dest = dest_dir / f"seismo_hub_{tag}_{stamp}.db"
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    source.backup(target)
    source.close()
    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    target.close()
    (dest_dir / "BACKUP_INFO.json").write_text(json.dumps({
        "source_db": str(src), "backup": dest.name, "taken_at": now.isoformat(),
        "method": "sqlite3 online backup API from a mode=ro source connection",
        "integrity_check": integrity, "reason": "before scripts/split_pdf_analyses.py --run",
        "restore": "stop the app, then copy this file over the source DB (remove any -wal/-shm next to it first)",
    }, indent=2, ensure_ascii=False))
    if integrity != "ok":
        raise SystemExit(f"DB backup integrity check failed: {integrity}")
    return dest_dir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="call the model (paid); default is a free preview")
    ap.add_argument("--limit", type=int, default=0, help="handle at most N papers")
    args = ap.parse_args()

    from db import SessionLocal
    session = SessionLocal()
    papers = legacy_rows(session)
    if args.limit:
        papers = papers[: args.limit]
    print(f"{len(papers)} paper(s) are classified from a PDF analysis and have no title/abstract analysis")
    print(f"--run makes {len(papers)} call(s) to the quick model (title + abstract + public metadata, no PDF)")
    for p in papers[:10]:
        print(f"  {p.id}  {(p.journal or '')[:22]:<22} {(p.title or '')[:70]}")
    if len(papers) > 10:
        print(f"  … and {len(papers) - 10} more")
    if not args.run or not papers:
        if not args.run:
            print("\n(preview only — nothing was sent to AI; pass --run to analyse)")
        session.close()
        return

    import services.analyze_service as A

    backup_dir = backup_db("pre_split_pdf_analyses")
    print(f"DB backed up to {backup_dir.name}")
    out_dir = BASE_DIR / "output"
    out_dir.mkdir(exist_ok=True)
    report = out_dir / f"split_pdf_analyses_{datetime.now():%Y-%m-%d_%H%M%S}.jsonl"
    ids = [p.id for p in papers]
    session.close()

    done = errors = consecutive_errors = 0
    for n, pid in enumerate(ids, 1):
        session = SessionLocal()
        started = time.time()
        row = {"paper_id": pid, "started_at": datetime.now().isoformat()}
        try:
            A.analyze_paper_from_metadata(pid, session, progress_callback=lambda *a, **k: None)
            session.expire_all()
            from models import Paper
            analysis = session.get(Paper, pid).ai_analysis
            ok = A.has_abstract_layer(analysis) and A.pdf_layer_of(analysis) is not None
            row.update({"ok": ok, "model": analysis.analysis_model, "pdf_used": bool(analysis.pdf_used),
                        "kept_pdf_layer": A.pdf_layer_of(analysis) is not None})
            done += 1 if ok else 0
            errors += 0 if ok else 1
            consecutive_errors = 0 if ok else consecutive_errors + 1
            print(f"[{n}/{len(ids)}] {pid} {'ok' if ok else 'CHECK'}  model={analysis.analysis_model}  "
                  f"({time.time() - started:.0f}s)", flush=True)
        except Exception as exc:
            session.rollback()
            errors += 1
            consecutive_errors += 1
            row.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            print(f"[{n}/{len(ids)}] {pid} FAILED: {row['error']}", flush=True)
        finally:
            session.close()
        row["secs"] = round(time.time() - started, 1)
        with report.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if consecutive_errors >= 3:
            print("3 failures in a row; stopping")
            break

    print(f"\ndone: {done} split, {errors} failed; report {report.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
