#!/usr/bin/env python3
"""Re-analyse papers whose AI analysis was written BEFORE their PDF was attached.

Added 2026-11-12. ``scripts/harvest_pdfs.py`` attaches PDFs but never triggers AI
analysis, so a harvested paper keeps its old abstract+web analysis
(``ai_analyses.pdf_used = 0``) and the detail panel says the PDF was not used.
This tool finds those papers and re-runs the normal PDF analysis on them
(``services.analyze_service.analyze_paper_from_pdf``: gemini-4-argon, with the
TDM guard inside ``_run_analysis``). It changes no existing source file.

Selection: a PDF file is on disk, an analysis exists with ``pdf_used = 0``, and the
TDM guard (``is_tdm_restricted_paper``) does not block the PDF. Ordered by the
current relevance score (high first), then newest, so a budget cap stops at the
least relevant papers. Re-running is safe: finished papers have ``pdf_used = 1``
and drop out of the selection, so an interrupted run resumes where it stopped.

Usage:
    python3 scripts/reanalyze_stale_pdf.py                        # preview + cost estimate (free)
    python3 scripts/reanalyze_stale_pdf.py --min-relevance 8      # preview a subset
    python3 scripts/reanalyze_stale_pdf.py --run --limit 1        # paid: one paper
    python3 scripts/reanalyze_stale_pdf.py --run --budget-usd 50  # paid: stop at ~$50

``--run`` backs up the DB first (``_db_backup_<ts>_pre_reanalyze_stale_pdf/``) and
writes one JSON line per paper to ``output/reanalyze_stale_pdf_<ts>.jsonl``.

Prices (USD per 1M tokens, gemini-4-argon, Google Cloud blog 2026-10-14 and the
Gemini API pricing page, checked 2026-11-12): $2 in / $10 out through 2026-11-30,
then $4 / $20. Thinking tokens bill as output. Token estimates are calibrated on
the 2026-11-12 test (a 16-page paper: 32,491 in / 8,434 out):
~3,400 prompt tokens + ~1,820 tokens per PDF page, ~8,450 output tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")

INTRO_LAST_DAY = date(2026, 11, 30)
PRICE_INTRO = (2.00, 10.00)      # (input, output) USD per 1M tokens, through INTRO_LAST_DAY
PRICE_STANDARD = (4.00, 20.00)   # from 2026-12-01

EST_PROMPT_TOKENS = 3_400
EST_TOKENS_PER_PAGE = 1_820
EST_OUTPUT_TOKENS = 8_450
EST_TEXT_PATH_TOKENS = 30_000    # >30 MB PDFs send extracted text (<=30 pages, <=120k chars)


def price_for(day: date) -> tuple:
    return PRICE_INTRO if day <= INTRO_LAST_DAY else PRICE_STANDARD


def cost_usd(tokens_in: int, tokens_out: int, price: tuple) -> float:
    return tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1]


def estimate_tokens(pages: int, size_mb: float, max_pdf_mb: float = 30.0) -> tuple:
    """(input, output) token estimate for one paper."""
    if size_mb > max_pdf_mb:
        tokens_in = EST_PROMPT_TOKENS + min(EST_TEXT_PATH_TOKENS, pages * 1_000)
    else:
        tokens_in = EST_PROMPT_TOKENS + pages * EST_TOKENS_PER_PAGE
    return tokens_in, EST_OUTPUT_TOKENS


def select_candidates(session, *, min_relevance=None, max_pages=None, ids=None):
    import fitz
    from models import AIAnalysis, Paper
    from services.tdm_guard_and_xml_renderer import is_tdm_restricted_paper

    query = (
        session.query(Paper)
        .join(AIAnalysis, AIAnalysis.paper_id == Paper.id)
        .filter(Paper.pdf_path.isnot(None), Paper.pdf_path != "", AIAnalysis.pdf_used.is_(False))
    )
    if ids:
        query = query.filter(Paper.id.in_(ids))
    if min_relevance is not None:
        query = query.filter(AIAnalysis.relevance_score >= min_relevance)
    papers = query.order_by(AIAnalysis.relevance_score.desc(), Paper.published_date.desc()).all()

    picked, skipped = [], {"missing_file": 0, "tdm_blocked": 0, "too_many_pages": 0, "unreadable": 0}
    for paper in papers:
        path = Path(paper.pdf_path)
        if not path.is_file():
            skipped["missing_file"] += 1
            continue
        try:
            blocked = is_tdm_restricted_paper(paper, pdf_path=str(path))[0]
        except Exception:
            blocked = True  # fail closed
        if blocked:
            skipped["tdm_blocked"] += 1
            continue
        try:
            with fitz.open(str(path)) as doc:
                pages = doc.page_count
        except Exception:
            skipped["unreadable"] += 1
            continue
        if max_pages and pages > max_pages:
            skipped["too_many_pages"] += 1
            continue
        picked.append({
            "id": paper.id,
            "title": paper.title or "",
            "pdf_path": str(path),
            "pages": pages,
            "size_mb": path.stat().st_size / 1e6,
            "relevance": paper.ai_analysis.relevance_score,
        })
    return picked, skipped


def backup_db(tag: str) -> Path:
    # Back up the DB the analysis will actually write: db.py honours SEISMO_DB_PATH
    # (Docker / throwaway instances), so take the path from there, not BASE_DIR.
    from db import DATABASE_PATH
    src = Path(DATABASE_PATH).resolve()
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
        "integrity_check": integrity, "reason": "before scripts/reanalyze_stale_pdf.py --run",
        "restore": "stop the app, then copy this file over the source DB (remove any -wal/-shm next to it first)",
    }, indent=2, ensure_ascii=False))
    if integrity != "ok":
        raise SystemExit(f"DB backup integrity check failed: {integrity}")
    return dest_dir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="actually re-analyse (paid); default is a free preview")
    ap.add_argument("--limit", type=int, default=0, help="re-analyse at most N papers")
    ap.add_argument("--min-relevance", type=int, default=None, help="only papers whose current relevance score >= N (0-10)")
    ap.add_argument("--max-pages", type=int, default=0, help="skip PDFs longer than N pages")
    ap.add_argument("--ids", default="", help="comma-separated paper ids to restrict to")
    ap.add_argument("--budget-usd", type=float, default=0.0, help="stop once measured spend reaches this amount")
    ap.add_argument("--allow-fallback", action="store_true",
                    help="keep going when a call falls back from the PDF model (default: stop)")
    args = ap.parse_args()

    from db import SessionLocal
    session = SessionLocal()
    ids = [x.strip() for x in args.ids.split(",") if x.strip()] or None
    picked, skipped = select_candidates(session, min_relevance=args.min_relevance,
                                        max_pages=args.max_pages or None, ids=ids)
    if args.limit:
        picked = picked[: args.limit]

    today = date.today()
    est_in = est_out = 0
    for item in picked:
        t_in, t_out = estimate_tokens(item["pages"], item["size_mb"])
        est_in += t_in
        est_out += t_out
    print(f"{len(picked)} paper(s) to re-analyse with their PDF; skipped: {skipped}")
    print(f"pages: {sum(i['pages'] for i in picked)} | estimated tokens: {est_in / 1e6:.1f}M in, {est_out / 1e6:.1f}M out")
    print(f"estimated cost (Argon standard tier): ${cost_usd(est_in, est_out, PRICE_INTRO):,.0f} at the intro rate "
          f"(through {INTRO_LAST_DAY}), ${cost_usd(est_in, est_out, PRICE_STANDARD):,.0f} from 2026-12-01")
    for item in picked[:10]:
        print(f"  rel={item['relevance']:>2}  {item['pages']:>3}p  {item['id']}  {item['title'][:70]}")
    if len(picked) > 10:
        print(f"  … and {len(picked) - 10} more")
    if not args.run:
        print("\n(preview only — nothing was sent to AI; pass --run to re-analyse)")
        session.close()
        return
    if not picked:
        session.close()
        return

    import services.analyze_service as A
    from models import Paper

    backup_dir = backup_db("pre_reanalyze_stale_pdf")
    print(f"DB backed up to {backup_dir.name}")
    out_dir = BASE_DIR / "output"
    out_dir.mkdir(exist_ok=True)
    report = out_dir / f"reanalyze_stale_pdf_{datetime.now():%Y-%m-%d_%H%M%S}.jsonl"

    client = A._get_vertex_client()
    original_call = client.models.generate_content
    last_usage: dict = {}

    def logged_call(*c_args, **c_kwargs):
        last_usage.clear()
        response = original_call(*c_args, **c_kwargs)
        meta = getattr(response, "usage_metadata", None)
        last_usage["in"] = int(getattr(meta, "prompt_token_count", 0) or 0)
        last_usage["out"] = int(getattr(meta, "candidates_token_count", 0) or 0) + int(getattr(meta, "thoughts_token_count", 0) or 0)
        return response

    client.models.generate_content = logged_call

    spent = 0.0
    done = errors = consecutive_errors = 0
    for n, item in enumerate(picked, 1):
        if args.budget_usd and spent >= args.budget_usd:
            print(f"budget reached (${spent:.2f} >= ${args.budget_usd:.2f}); stopping")
            break
        started = time.time()
        row = {"paper_id": item["id"], "pages": item["pages"], "started_at": datetime.now().isoformat()}
        try:
            A.analyze_paper_from_pdf(item["id"], item["pdf_path"], session, progress_callback=lambda *a, **k: None)
            session.expire_all()
            analysis = session.get(Paper, item["id"]).ai_analysis
            price = price_for(date.today())
            call_cost = cost_usd(last_usage.get("in", 0), last_usage.get("out", 0), price)
            spent += call_cost
            row.update({"ok": True, "model": analysis.analysis_model, "pdf_used": bool(analysis.pdf_used),
                        "evidence": analysis.evidence_based_on, "tokens_in": last_usage.get("in"),
                        "tokens_out": last_usage.get("out"), "cost_usd": round(call_cost, 4)})
            done += 1
            consecutive_errors = 0
            print(f"[{n}/{len(picked)}] {item['id']} ok  model={analysis.analysis_model}  pdf_used={bool(analysis.pdf_used)}  "
                  f"${call_cost:.3f}  total ${spent:.2f}  ({time.time() - started:.0f}s)", flush=True)
        except Exception as exc:
            session.rollback()
            errors += 1
            consecutive_errors += 1
            row.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            print(f"[{n}/{len(picked)}] {item['id']} FAILED: {row['error']}", flush=True)
        row["secs"] = round(time.time() - started, 1)
        with report.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if row.get("ok") and row.get("model") != A.PDF_ANALYSIS_MODEL and not args.allow_fallback:
            print(f"call fell back to {row.get('model')} instead of {A.PDF_ANALYSIS_MODEL}; stopping (see --allow-fallback)")
            break
        if consecutive_errors >= 3:
            print("3 failures in a row; stopping")
            break

    session.close()
    print(f"\ndone: {done} re-analysed, {errors} failed, measured spend ${spent:.2f}; report {report.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
