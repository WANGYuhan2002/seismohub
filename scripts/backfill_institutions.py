"""Backfill papers.institutions from OpenAlex authorships by DOI.

Migrated zotero/v1 papers often lack institutions. For every paper with a DOI
but no institutions, batch-query OpenAlex (50 DOIs at a time), pull authorship
institutions via the existing extractor, and persist. Mirror of
backfill_keywords.py; run once so institution coverage is even.

    cd seismohub-v2 && DISABLE_TRACKER_SCHEDULER=1 PYTHONPATH=. python3 scripts/backfill_institutions.py
"""
from __future__ import annotations

import time

import requests

import db as dbm
from models import Paper
from services.fetch_service import extract_openalex_institutions

BATCH = 50
OPENALEX = "https://api.openalex.org/works"


def bare_doi(doi):
    d = (doi or "").strip().lower()
    for pre in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pre):
            d = d[len(pre):]
    return d


def main() -> None:
    session = dbm.SessionLocal()
    try:
        papers = session.query(Paper).filter(Paper.doi.isnot(None)).all()
        todo = [p for p in papers if not (p.institutions or [])]
        by_doi: dict = {}
        for p in todo:
            bd = bare_doi(p.doi)
            if bd:
                by_doi.setdefault(bd, []).append(p)
        dois = list(by_doi.keys())
        print(f"{len(todo)} papers lack institutions; {len(dois)} distinct DOIs to query")

        updated = 0
        for i in range(0, len(dois), BATCH):
            chunk = dois[i:i + BATCH]
            params = {
                "filter": "doi:" + "|".join(chunk),
                "per-page": BATCH,
                "select": "doi,authorships",
                "mailto": "example@example.org",
            }
            try:
                r = requests.get(OPENALEX, params=params, timeout=30)
                works = (r.json() or {}).get("results", []) or []
            except Exception as e:  # noqa: BLE001
                print(f"  batch {i}: error {e}; skipping")
                continue
            for w in works:
                insts = extract_openalex_institutions(w)
                if not insts:
                    continue
                for p in by_doi.get(bare_doi(w.get("doi")), []):
                    p.institutions = insts
                    updated += 1
            session.commit()
            time.sleep(0.2)
            print(f"  {min(i + BATCH, len(dois))}/{len(dois)} DOIs · updated {updated}")
        print(f"DONE — backfilled institutions on {updated} papers")
    finally:
        session.close()


if __name__ == "__main__":
    main()
