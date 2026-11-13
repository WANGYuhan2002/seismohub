#!/usr/bin/env python3
"""Harvest full-text PDFs (+ Full-Bundle extras) for papers already in the SeismoHub library.

Additive tool (2026-11-08); usage: docs/PDF_HARVESTING.md. Safety rails:

* ``--dry-run`` downloads + verifies but writes NOTHING (no DB rows, no files).
* Before the first DB write a consistent SQLite backup is taken through the SQLite backup API into
  ``_db_backup_<ts>_pre_pdf_harvest_run/`` next to the DB (``--no-backup`` only for throwaway copies).
* Never overwrites an existing PDF, never attaches bytes another paper owns, never triggers AI analysis.
* TDM-licensed copies (Wiley / Elsevier publisher versions without a CC licence) are kept in
  ``paper_bundles/`` and never attached to the app: the app's analyze step and its PDF Q&A send attached
  PDFs / their page text to Gemini, which institutional licence terms do not allow for TDM content.
  ``--attach-tdm`` is SEALED since 2026-11-09 (owner decision): it refuses to run, and a non-OPEN copy is
  never attached even if the harvester is patched to offer one.
* ``--db PATH`` runs against another DB file (sets SEISMO_DB_PATH before db.py is imported).

Examples::

    python scripts/harvest_pdfs.py --dry-run --limit 20
    python scripts/harvest_pdfs.py --limit 100
    python scripts/harvest_pdfs.py --doi 10.1029/2026GL125440
    python scripts/harvest_pdfs.py --doi-prefix 10.1016 --retry-failed   # on institutional VPN

Elsevier (verified 2026-11-08 on the institutional VPN): open-access PDFs come through anywhere; for subscribed
articles the API serves only a 1-page PDF preview, but full-text XML. Those papers get an XML bundle
(``main_manuscript.xml`` + high-res figures + supplements, TDM-only, never attached) and status
``held_tdm_only``. A readable PDF of a subscribed article still needs a browser on the institutional network.
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
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
try:  # pick up settings from the project .env; never overrides variables already set in the shell
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except Exception:  # pragma: no cover - python-dotenv is a hard dependency, but stay importable without it
    pass

# Sealed 2026-11-09 (owner: 「封住」). Re-opening needs the owner's OK *and* a TDM guard on every path that sends an
# attached PDF or its text to hosted AI (analyze step, PDF Q&A chat, manual upload / watch-folder flows).
ATTACH_TDM_SEALED_EXIT = 3
ATTACH_TDM_SEALED_MESSAGE = (
    "--attach-tdm is sealed (owner decision, 2026-11-09). TDM-licensed copies stay in paper_bundles/ and are\n"
    "never attached to the app: the app's PDF Q&A (main.chat_with_paper -> services/qa_service.py) sends page text\n"
    "to Gemini without a TDM check. Nothing was read or written. Run without --attach-tdm to attach OPEN copies only.\n"
    "--attach-tdm 已封住（2026-11-09，所有者决定）：TDM 许可的全文只留在 paper_bundles/，不再挂进 App。\n"
    "本次没有读写任何东西。去掉 --attach-tdm 再运行，只会挂开放许可的副本。"
)


# A publisher copy whose first two pages say "All rights reserved" with no open-licence statement is licensed
# content, whatever the licence metadata says. Found 2026-11-09: OpenAlex + Unpaywall called 10.1029/2026JB034130
# (JGR Solid Earth) cc-by, while its Crossref deposit (Wiley terms + TDM licence) and the PDF itself
# ("(c) 2026 American Geophysical Union. All rights reserved") say it is a subscription article.
_RIGHTS_RESERVED = re.compile(r"all\s+rights\s+reserved", re.I)
_OPEN_STATEMENT = re.compile(
    r"creativecommons|creative\s+commons|open\s+access\s+article|this\s+is\s+an\s+open\s+access|"
    r"open\s+access\s+this\s+article|©\s*\d{4}\.?\s*the\s+authors?\b|\bthe\s+author\(s\)\s*\d{4}", re.I)


def pdf_says_rights_reserved(hit) -> bool:
    """True for a publisher copy (not a repository / preprint copy) whose first two pages say 'All rights
    reserved' and carry no open-licence statement, or whose text cannot be read."""
    if getattr(hit, "kind", "") == "open_copy":
        return False
    try:
        import fitz  # PyMuPDF; the harvester already uses it to verify every PDF

        with fitz.open(stream=hit.data, filetype="pdf") as doc:
            text = " ".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
    except Exception:
        return True  # cannot confirm the licence -> keep it out of the app
    return bool(_RIGHTS_RESERVED.search(text)) and not _OPEN_STATEMENT.search(text)


def keep_non_open_in_bundle(fb, out) -> bool:
    """Last line of the seal: only an OPEN copy is ever attached, whatever patched the harvester and whatever
    the licence metadata claims.

    A non-OPEN copy, or a publisher copy whose PDF says 'All rights reserved' without an open-licence statement
    (re-tiered to TDM), moves to ``held_hit`` (bundle only) and the outcome becomes ``held_tdm_only``.
    Returns True if it acted.
    """
    hit = out.app_hit
    if hit is None:
        return False
    if hit.tier == fb.TIER_OPEN:
        if not pdf_says_rights_reserved(hit):
            return False
        hit.tier = fb.TIER_TDM
        hit.license = f"publisher-licensed (pdf: all rights reserved; metadata said {hit.license or 'open'})"
    if out.held_hit is None:
        out.held_hit = hit
    out.app_hit = None
    out.status = fb.HELD_TDM_ONLY
    out.reason = (f"{hit.channel} copy is {hit.tier} ({hit.license}); kept in bundle only "
                  "(sealed 2026-11-09: TDM content never goes to the app)")
    return True


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Harvest verified full-text PDFs into the SeismoHub library.")
    p.add_argument("--db", default="", help="SQLite DB (default: $SEISMO_DB_PATH or ./seismo_hub.db)")
    p.add_argument("--limit", type=int, default=0, help="max papers this run (0 = all eligible)")
    p.add_argument("--doi", action="append", default=[], help="only this DOI (repeatable)")
    p.add_argument("--doi-prefix", action="append", default=[], help="only DOIs with this prefix, e.g. 10.1016")
    p.add_argument("--paper-id", action="append", default=[], help="only this paper id (repeatable)")
    p.add_argument("--journal", action="append", default=[], help="only journals containing this text")
    p.add_argument("--channels", default="",
                   help="comma list: openalex,copernicus,nature,ethrc,wiley,elsevier,arxiv,eartharxiv,zenodo")
    p.add_argument("--attach-tdm", action="store_true",
                   help="SEALED since 2026-11-09: refuses to run. TDM-licensed copies stay in paper_bundles/ "
                        "(the app's PDF Q&A sends page text to Gemini without a TDM check)")
    p.add_argument("--no-extras", action="store_true", help="skip XML / supplements / reviews / code-data links")
    p.add_argument("--download-code", action="store_true", help="download GitHub tarballs (<=50 MB) cited as code")
    p.add_argument("--retry-failed", action="store_true", help="include previously failed / held papers")
    p.add_argument("--dry-run", action="store_true", help="fetch + verify only; write nothing")
    p.add_argument("--bundle-root", default="", help="Full-Bundle folder (default: <repo>/paper_bundles)")
    p.add_argument("--downloads-dir", default="", help="managed PDF folder (default: the app's downloads/)")
    p.add_argument("--no-backup", action="store_true", help="skip the automatic DB backup (throwaway DBs only)")
    p.add_argument("--keys-file", default="", help="extra env-style keys file")
    return p.parse_args(argv)


def install_upgrades_and_load_keys(fb, *, keys_file: str, cache_path: Path, readonly: bool) -> Dict[str, bool]:
    """Install the upgrade layer and read the keys, ``--keys-file`` included. Returns which keys are set.

    The upgrade layer adds OPENALEX_API_KEY to ``fb.KEY_NAMES`` and reads the key files again; it must get
    ``--keys-file`` too, or an OPENALEX_API_KEY given only there is never loaded. Every key then follows the
    ``load_harvest_keys`` order: ``$SEISMO_HARVEST_KEYS_FILE``, ``--keys-file``, ``~/.config/seismo_harvester/keys.env``.
    """
    from scripts.seismohub_upgrade_runner import install_harvester_upgrades

    extra = [keys_file] if keys_file else []
    install_harvester_upgrades(fb, cache_path=cache_path, readonly=readonly, keys_files=extra)
    return fb.load_harvest_keys(extra)


def backup_db(db_path: Path, ts: str) -> Path:
    """Consistent copy via the SQLite online-backup API (safe while the app is running)."""
    out_dir = db_path.parent / f"_db_backup_{ts}_pre_pdf_harvest_run"
    out_dir.mkdir(parents=True, exist_ok=False)
    dst_path = out_dir / f"{db_path.stem}_pre_pdf_harvest_run_{ts}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        papers = dst.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        with_pdf = dst.execute("SELECT COUNT(*) FROM papers WHERE pdf_path IS NOT NULL AND pdf_path != ''").fetchone()[0]
        dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm sidecars
    finally:
        dst.close()
        src.close()
    if integrity != "ok":
        raise SystemExit(f"DB backup integrity_check failed ({integrity}); aborting before any write.")
    (out_dir / "BACKUP_INFO.json").write_text(json.dumps({
        "source_db": str(db_path), "backup": dst_path.name, "taken_at": dt.datetime.now().astimezone().isoformat(),
        "method": "sqlite3 online backup API from a mode=ro source connection",
        "integrity_check": integrity, "papers": papers, "papers_with_pdf": with_pdf,
        "restore": "stop the app, then copy this file over the source DB (remove any -wal/-shm next to it first)",
    }, indent=2), encoding="utf-8")
    return dst_path


def select_papers(db, Paper, fb, args) -> List[Any]:
    from sqlalchemy import and_, func, or_

    session = db.SessionLocal()
    try:
        q = session.query(Paper).filter(or_(Paper.pdf_path.is_(None), Paper.pdf_path == ""))
        targeted = bool(args.doi or args.paper_id)
        if args.doi:
            q = q.filter(func.lower(Paper.doi).in_([fb.normalize_doi(d).lower() for d in args.doi]))
        if args.paper_id:
            q = q.filter(Paper.id.in_(args.paper_id))
        if not targeted:
            q = q.filter(or_(
                and_(Paper.doi.isnot(None), Paper.doi != ""),
                Paper.link.ilike("%arxiv.org/%"),
                Paper.link.ilike("%research-collection.ethz.ch/%"),
                Paper.link.ilike("%20.500.11850/%"),
                Paper.link.ilike("%hal.science/%"),
                Paper.link.ilike("%archives-ouvertes.fr/%"),
                Paper.link.ilike("%nature.com/articles/%"),
            ))
            if args.retry_failed:
                q = q.filter(or_(Paper.retrieval_status.is_(None), Paper.retrieval_status != fb.SUCCESS))
            else:
                due = [Paper.retrieval_status.is_(None), Paper.retrieval_status == "not_attempted",
                       and_(Paper.retrieval_next_retry_at.isnot(None),
                            Paper.retrieval_next_retry_at <= dt.datetime.utcnow())]
                if args.attach_tdm:
                    due.append(and_(Paper.retrieval_status == fb.HELD_TDM_ONLY, or_(Paper.retrieval_source.is_(None), Paper.retrieval_source != "elsevier_tdm_xml")))
                q = q.filter(or_(*due))
        if args.doi_prefix:
            q = q.filter(or_(*[func.lower(Paper.doi).like(p.strip().lower().rstrip("/") + "/%") for p in args.doi_prefix]))
        if args.journal:
            q = q.filter(or_(*[Paper.journal.ilike(f"%{j}%") for j in args.journal]))
        q = q.order_by(Paper.published_date.desc(), Paper.id)
        if args.limit:
            q = q.limit(args.limit)
        refs_out: List[Any] = []
        for p in q.all():
            r = fb.PaperRef(
                paper_id=p.id, doi=p.doi or "", title=p.title or "",
                journal=p.journal or "", link=p.link or "", source=p.source or "",
            )
            setattr(r, "authors", getattr(p, "authors", "") or "")
            setattr(r, "institutions", getattr(p, "institutions", "") or "")
            refs_out.append(r)
        return refs_out
    finally:
        session.close()


def suggestion(fb, out) -> str:
    reasons = " ".join(a.reason for a in out.pdf_attempts)
    outcomes = {a.outcome for a in out.pdf_attempts}
    if out.doi.lower().startswith("10.1016/") and "elsevier" in reasons.lower():
        return ("Elsevier full text (XML) needs an institutional network: on an institutional network / VPN run "
                "python scripts/harvest_pdfs.py --doi-prefix 10.1016 --retry-failed ; the API never serves "
                "subscribed PDFs, so a readable PDF needs a browser on the institutional network")
    if fb.NO_PDF_BUT_HTML in outcomes:
        return ("an open copy exists but its host serves scripts an HTML page (e.g. PMC): open open_url in a "
                "browser, save the PDF, then the app's 'upload missing PDFs'")
    if fb.CAPTCHA_OR_BOT_BLOCK in outcomes:
        return ("publisher blocks scripts: download in a browser (Zotero Connector, institutional network), then the app's "
                "'upload missing PDFs' matches them by DOI/title")
    if out.status == fb.NO_OA_COPY:
        return "no open copy known (OpenAlex / arXiv / ETH RC); browser download + 'upload missing PDFs'"
    return "see attempts in the run report; retry later with --retry-failed"


def short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.attach_tdm:  # sealed: refuse before touching the environment, the DB, the network or any file
        print(ATTACH_TDM_SEALED_MESSAGE, file=sys.stderr)
        return ATTACH_TDM_SEALED_EXIT
    if args.db:
        os.environ["SEISMO_DB_PATH"] = str(Path(args.db).expanduser().resolve())
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    import db  # noqa: E402  (after SEISMO_DB_PATH)
    from models import Paper  # noqa: E402
    from services import fullbundle_harvest as fb  # noqa: E402
    from services import pdf_naming  # noqa: E402

    db_path = Path(db.DATABASE_PATH)
    if not db_path.is_file():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2
    bundle_root = Path(args.bundle_root).expanduser() if args.bundle_root else REPO / "paper_bundles"
    keys = install_upgrades_and_load_keys(fb, keys_file=args.keys_file,
                                          cache_path=bundle_root / "_metadata_cache.sqlite", readonly=args.dry_run)
    channels = [c.strip() for c in args.channels.split(",") if c.strip()] or None
    harvester = fb.FullBundleHarvester(channels=channels, attach_tdm=False,  # sealed 2026-11-09
                                       download_code=args.download_code)
    downloads_dir = Path(args.downloads_dir).expanduser() if args.downloads_dir else pdf_naming.DOWNLOADS_DIR

    refs = select_papers(db, Paper, fb, args)
    mode = "DRY-RUN (nothing is written)" if args.dry_run else "WRITE"
    print(f"SeismoHub Full-Bundle harvester {fb.HARVEST_VERSION} | {mode}")
    print(f"  DB: {db_path}")
    print(f"  keys: wiley={'yes' if keys['WILEY_TDM_TOKEN'] else 'no'} "
          f"elsevier={'yes' if keys['ELSEVIER_API_KEY'] else 'no'} "
          f"openalex={'yes' if os.environ.get('OPENALEX_API_KEY') else 'no'} "
          f"| channels: {','.join(sorted(harvester.channels))}")
    print("  attach TDM-licensed copies to the app: no (bundle only; --attach-tdm sealed 2026-11-09)")
    print(f"  papers selected: {len(refs)}")
    if not refs:
        return 0

    ts = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    reports_dir = bundle_root / "_reports"
    backup_path: Optional[Path] = None
    if not args.dry_run:
        if not args.no_backup:
            backup_path = backup_db(db_path, ts)
            print(f"  DB backup: {backup_path}")
        reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"harvest_{ts}.jsonl"
    pending_path = reports_dir / f"pending_browser_fetch_{ts}.tsv"

    counts: Counter = Counter()
    by_channel: Counter = Counter()
    pending: List[Dict[str, str]] = []
    started = time.monotonic()
    width = len(str(len(refs)))
    try:
        for index, ref in enumerate(refs, 1):
            try:
                out = harvester.harvest(ref)
            except Exception as exc:  # one paper must never kill the run
                print(f"[{index:>{width}}/{len(refs)}] ERROR   {ref.doi or ref.paper_id}: {type(exc).__name__}: {exc}")
                counts["error"] += 1
                continue
            if keep_non_open_in_bundle(fb, out):
                print(f"[{index:>{width}}/{len(refs)}] SEALED  {out.reason}", flush=True)

            attach = fb.AttachResult(False, "")
            bundle_dir: Optional[Path] = None
            extras: List[str] = []
            if not args.dry_run:
                session = db.SessionLocal()
                written = ""
                try:
                    inferred_doi = getattr(out, "inferred_doi", "") or ""
                    dup_doi_owner = None
                    if inferred_doi and not (ref.doi or "").strip():
                        from sqlalchemy import func as _sa_func
                        existing_owner = (
                            session.query(Paper.id, Paper.pdf_path, Paper.retrieval_status)
                            .filter(_sa_func.lower(Paper.doi) == inferred_doi.lower(), Paper.id != ref.paper_id)
                            .first()
                        )
                        if existing_owner is None:
                            p_obj = session.get(Paper, ref.paper_id)
                            if p_obj is not None and not (p_obj.doi or "").strip():
                                p_obj.doi = inferred_doi
                        elif existing_owner[1] or existing_owner[2] == fb.HELD_TDM_ONLY:
                            dup_doi_owner = existing_owner[0]
                    if dup_doi_owner is not None:
                        attach = fb.AttachResult(False, f"inferred_doi_already_attached_to:{dup_doi_owner}({inferred_doi})")
                        out.status = fb.UNSUPPORTED
                    elif out.app_hit is not None:
                        attach = fb.attach_hit_to_paper(session, ref.paper_id, out.app_hit, downloads_dir=downloads_dir)
                        written = attach.pdf_path if attach.attached else ""
                    fb.record_harvest(session, out, attach)
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    if written:
                        Path(written).unlink(missing_ok=True)
                    attach = fb.AttachResult(False, f"db_error:{type(exc).__name__}: {exc}"[:240])
                finally:
                    session.close()
                if out.app_hit is not None or out.held_hit is not None or out.held_xml is not None:
                    bundle_dir = bundle_root / fb.safe_bundle_name(out.doi, out.arxiv_id, ref.paper_id)
                    code: Dict[str, Any] = {}
                    if not args.no_extras:
                        bundle_dir.mkdir(parents=True, exist_ok=True)
                        extras, code = harvester.collect_extras(out, bundle_dir)
                    fb.write_bundle(out, bundle_root, attached_pdf_path=attach.pdf_path if attach.attached else "",
                                    attach_reason=attach.reason, extras=extras, code_report=code)

            hit = out.app_hit or out.held_hit
            if out.app_hit is not None and (attach.attached or args.dry_run):
                label = "DRY-OK " if args.dry_run else "ATTACH "
                counts["attached" if not args.dry_run else "would_attach"] += 1
                by_channel[out.app_hit.channel] += 1
            elif out.app_hit is not None:
                label = "SKIP   "
                counts["not_attached:" + attach.reason.split(":")[0]] += 1
            elif out.held_hit is not None:
                label = "HELD   "
                counts["held_tdm_only"] += 1
                by_channel[out.held_hit.channel + "(held)"] += 1
            elif out.held_xml is not None:
                label = "HELD   "
                counts["held_tdm_only (full-text XML)"] += 1
                by_channel[out.held_xml.channel + "(held)"] += 1
            else:
                label = "PENDING"
                counts["pending:" + out.status] += 1
                html_copy = next((a.url for a in out.pdf_attempts if a.outcome == fb.NO_PDF_BUT_HTML and a.url), "")
                pending.append({
                    "paper_id": ref.paper_id, "doi": out.doi, "journal": ref.journal,
                    "title": short(out.title, 160), "status": out.status,
                    "open_url": html_copy or (f"https://doi.org/{out.doi}" if out.doi else ref.link),
                    "suggestion": suggestion(fb, out),
                })
            if hit is not None:
                detail = f"{hit.channel:<13} {'OPEN' if hit.tier == fb.TIER_OPEN else 'TDM ':<4} {hit.pages:>3}p"
            elif out.held_xml is not None:
                detail = f"{'elsevier_xml':<13} TDM  xml "
            else:
                detail = f"{out.status:<23}"
            extra_note = f" +{len(extras)} extras" if extras else ""
            print(f"[{index:>{width}}/{len(refs)}] {label} {detail}  {short(out.doi or ref.paper_id, 34):<34} "
                  f"{short(ref.journal, 18):<18} {short(out.title, 50)}{extra_note}"
                  + (f"  ({attach.reason})" if label == "SKIP   " else ""), flush=True)

            if not args.dry_run:
                record = {
                    "paper_id": ref.paper_id, "doi": out.doi, "title": out.title, "journal": ref.journal,
                    "status": out.status, "reason": out.reason, "attached": attach.attached,
                    "pdf_path": attach.pdf_path or None, "attach_reason": attach.reason,
                    "app_copy": out.app_hit.describe() if out.app_hit else None,
                    "held_copy": out.held_hit.describe() if out.held_hit else None,
                    "held_xml": out.held_xml.describe() if out.held_xml else None,
                    "bundle": str(bundle_dir) if bundle_dir else None, "extras": extras,
                    "attempts": [a.as_dict() for a in out.attempts],
                }
                with open(report_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except KeyboardInterrupt:
        print("\ninterrupted - summarising what was done so far")

    if pending and not args.dry_run:
        with open(pending_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(pending[0].keys()), delimiter="\t")
            writer.writeheader()
            writer.writerows(pending)

    elapsed = time.monotonic() - started
    print("\n== summary ==")
    print(f"papers processed: {sum(counts.values())} in {elapsed / 60:.1f} min")
    for key, value in sorted(counts.items()):
        print(f"  {key:<34} {value}")
    if by_channel:
        print("  by channel: " + ", ".join(f"{k}={v}" for k, v in by_channel.most_common()))
    if harvester.elsevier_disabled_reason:
        print(f"  elsevier pdf: {harvester.elsevier_disabled_reason}")
    if harvester.elsevier_xml_disabled_reason:
        print(f"  elsevier xml: {harvester.elsevier_xml_disabled_reason}")
    if not args.dry_run:
        print(f"report:  {report_path}")
        if pending:
            print(f"pending: {pending_path}")
        if backup_path:
            print(f"backup:  {backup_path}")
    print("No AI analysis was triggered. Only OPEN-tier PDFs were attached; TDM-tier copies stay in paper_bundles/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
