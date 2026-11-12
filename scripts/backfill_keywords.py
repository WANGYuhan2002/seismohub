"""Backfill papers.keywords from OpenAlex concepts (cheap, no Gemini).

For every paper with a DOI but no keywords yet, batch-query OpenAlex by DOI
(50 at a time), extract keywords via the same fetch-time extractor, and persist.
Run once after the v2 keywords column lands; new fetches populate keywords inline.

    cd seismohub-v2 && DISABLE_TRACKER_SCHEDULER=1 python3 scripts/backfill_keywords.py
"""
from __future__ import annotations

import sys
import time

import requests

import db as dbm
from models import Paper
from services.fetch_service import extract_openalex_keywords

BATCH = 50
OPENALEX = "https://api.openalex.org/works"


def bare_doi(doi: str | None) -> str:
    d = (doi or "").strip().lower()
    for pre in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pre):
            d = d[len(pre):]
    return d


def main() -> None:
    session = dbm.SessionLocal()
    try:
        papers = session.query(Paper).filter(Paper.doi.isnot(None)).all()
        todo = [p for p in papers if not (p.keywords or [])]
        by_doi: dict[str, list[Paper]] = {}
        for p in todo:
            bd = bare_doi(p.doi)
            if bd:
                by_doi.setdefault(bd, []).append(p)
        dois = list(by_doi.keys())
        print(f"{len(todo)} papers need keywords; {len(dois)} distinct DOIs to query")

        updated = 0
        for i in range(0, len(dois), BATCH):
            chunk = dois[i:i + BATCH]
            params = {
                "filter": "doi:" + "|".join(chunk),
                "per-page": BATCH,
                "select": "doi,concepts,keywords",
                "mailto": "example@example.org",
            }
            try:
                r = requests.get(OPENALEX, params=params, timeout=30)
                works = (r.json() or {}).get("results", []) or []
            except Exception as e:  # noqa: BLE001
                print(f"  batch {i}: error {e}; skipping")
                continue
            for w in works:
                kws = extract_openalex_keywords(w)
                if not kws:
                    continue
                for p in by_doi.get(bare_doi(w.get("doi")), []):
                    p.keywords = kws
                    updated += 1
            session.commit()
            time.sleep(0.2)  # polite to OpenAlex
            print(f"  {min(i + BATCH, len(dois))}/{len(dois)} DOIs · updated {updated}")
        print(f"DONE — backfilled keywords on {updated} papers")
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
