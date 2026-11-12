---
name: seismohub-pdf-harvest
description: >-
  Fetch and verify full-text PDFs with SeismoHub's harvester. Use it to fill in missing PDFs for papers in the
  library (scripts/harvest_pdfs.py), to download a reading list of DOIs, arXiv IDs or citations into a folder
  (scripts/harvest_literature_list.py), to import PDFs downloaded in a browser or Zotero with licence triage, to
  summarise harvest reports, or to explain why a paper has no PDF. Trigger on requests such as "fetch / download /
  harvest / retry the PDFs", "get the full text for these DOIs", "why is there no PDF for ...", "抓取 PDF",
  "下载全文", "补齐缺失的 PDF", "为什么这篇没有 PDF". The skill enforces the licence rules: TDM-licensed copies stay
  local and are never read into a hosted AI, and bot-protected sites are never bypassed.
---

# SeismoHub PDF harvesting / PDF 抓取

Work from the repository root with the project's Python environment active. The full user guide is
`docs/PDF_HARVESTING.md` (Chinese: `docs/PDF_HARVESTING.zh-CN.md`).

## Ground rules (non-negotiable)

1. **Licensed full text never leaves the machine, and you are a hosted AI.** Do not open, read, extract or summarise
   files under `paper_bundles/` whose `bundle_manifest.json` says `LICENSED_TDM_ONLY` or
   `"hosted_ai_upload_allowed": false`, nor anything in a reading-list `tdm_bundles/` folder. Work from report
   metadata only (status, channel, tier, page count). `harvest_status.py` follows this rule.
2. **Never pass `--attach-tdm`** (it is sealed and exits with code 3). Never copy files from `paper_bundles/` into
   `downloads/`, never set `papers.pdf_path` by hand, and never upload a licensed PDF through the app.
3. **No bypassing.** Do not write browser automation (Playwright, Selenium, CDP), do not imitate a browser's
   User-Agent, do not edit `BOT_WALLED_HOSTS` or the rate limits to force requests, and never solve CAPTCHAs or
   work around logins. Bot-walled papers go to the user as a browser-download checklist.
4. **Dry run before writes.** Run library harvests with `--dry-run` first, show the user what would happen, then
   run for real. Writes back up the database automatically; keep that (`--no-backup` is for throwaway DBs only).
5. **Keys are presence-only.** Report whether a key is configured, never its value. Do not print `.env` or
   `keys.env`.
6. **One harvest at a time** per database. Start large runs with `--limit`, and run long ones in the background.
   Ask the user before an unlimited run over a big library (rate limits make it take hours).

## Step 0: orient

```bash
test -f main.py && test -f services/fullbundle_harvest.py && echo "repo root OK"
python -c "import fitz, requests, sqlalchemy; print('deps OK')"
python - <<'EOF'
import os, sqlite3
from pathlib import Path
db = Path(os.environ.get("SEISMO_DB_PATH") or "seismo_hub.db")
print("db:", db, "exists" if db.is_file() else "MISSING (start the app once with python main.py)")
if db.is_file():
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    total, with_pdf = con.execute(
        "select count(*), sum(coalesce(pdf_path, '') != '') from papers").fetchone()
    print(f"papers: {total}, with PDF: {with_pdf or 0}")
EOF
python - <<'EOF'
from dotenv import load_dotenv
load_dotenv(".env", override=False)
from services import fullbundle_harvest as fb
print("keys present:", fb.load_harvest_keys())   # booleans only
EOF
```

No keys is fine: the open channels (OpenAlex, arXiv, EarthArXiv, Copernicus, Zenodo, PMC, Nature OA, repositories)
need none. Elsevier and Wiley channels are skipped without keys.

## Workflow A: fill in missing PDFs for the library

```bash
python scripts/harvest_pdfs.py --dry-run --limit 20            # preview; writes nothing (still queries the network)
python scripts/harvest_pdfs.py --limit 200                     # real run: DB backup, attach OPEN copies
python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --pending
```

Narrow the selection with `--journal TEXT`, `--doi-prefix 10.5194`, `--doi DOI`, `--paper-id ID` (all repeatable),
or `--channels openalex,arxiv`. Use `--retry-failed` to include papers that failed or were held before (for example
Elsevier XML once the user is on an entitled network, or Wiley HTTP 500s a few days later). `--no-extras` skips
XML, supplements, reviews and code/data links.

Each line of output is one paper: `ATTACH` (open copy attached), `HELD` (licensed copy kept in `paper_bundles/`),
`PENDING` (no usable copy), `SEALED` (a non-open copy was held back), `SKIP` (verified but not attached, for
example because the paper already has a PDF). The summary ends with the report and pending-list paths.

Harvesting never starts an AI analysis, so newly attached papers keep their abstract-based analysis. To redo
them from the full text, preview first (free), show the user the paper count and cost estimate, and run the paid
step only after they agree:

```bash
python scripts/reanalyze_stale_pdf.py                          # free preview + cost estimate
python scripts/reanalyze_stale_pdf.py --run --limit 1          # paid; then --budget-usd N for a batch
```

## Workflow B: a reading list, without the library

```bash
python scripts/harvest_literature_list.py --input-file reading_list.txt --out-dir ~/seismo_pack --dry-run
python scripts/harvest_literature_list.py --input-file reading_list.txt --out-dir ~/seismo_pack
python scripts/harvest_literature_list.py --doi 10.1029/JB082i020p02981 --query "Aki 1957 space and time spectra"
python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --pack ~/seismo_pack
```

One DOI, arXiv ID, URL or citation per line. Output: `open_pdfs/` (shareable), `tdm_bundles/` (local only,
rule 1), `harvest_summary.tsv`, and `pending_zotero.csv` for browser downloads.

## Workflow C: PDFs the user downloaded in a browser

1. Give the user the checklist: the `pending_browser_fetch_<ts>.tsv` from Workflow A, or
   `python scripts/zotero_triage_import.py --export-batch checklist.csv`.
2. The user downloads them (Zotero Connector, on a network with access).
3. Import with licence triage, dry run first:
   ```bash
   python scripts/zotero_triage_import.py ~/Zotero/storage            # prints OPEN -> APP vs TDM -> BUNDLE
   python scripts/zotero_triage_import.py ~/Zotero/storage --commit   # after the user agrees
   ```
   For a Zotero "Export → files" folder use `python scripts/match_zotero_export.py <folder>` (add `--commit`).
   The in-app **Upload missing PDFs** also works for open copies.

## Workflow D: "why is there no PDF for X?"

```bash
python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --all-reports --doi 10.xxxx/yyyy --attempts
python - <<'EOF'
import sqlite3
con = sqlite3.connect("file:seismo_hub.db?mode=ro", uri=True)
q = "select id, journal, pdf_path, retrieval_status, retrieval_source from papers where lower(doi) = lower(?)"
for row in con.execute(q, ("10.xxxx/yyyy",)):
    print(row)
EOF
```

Explain the status with the table below, name the host that blocked or failed, and give the next step.

| Status | Meaning | Next step |
|---|---|---|
| `success` | Open copy verified and attached | none |
| `held_tdm_only` | Licensed copy kept local (rule 1) | local reading only; retry later for an open copy |
| `captcha_or_bot_block` | Only copies on script-blocking sites (OUP/GJI, GeoScienceWorld/BSSA-SRL-TSR, ESSOAr, Wiley web, ScienceDirect, Science, PNAS, MDPI) | browser download → Workflow C |
| `no_pdf_but_html` | Host served HTML (often PMC, nature.com) | open the link in a browser, save the PDF |
| `auth_required` | Login wall / 401 / 403 | browser download from an entitled network |
| `no_oa_copy` | No channel returned a copy | browser download; author page or repository |
| `transient_error` | Timeout, HTTP 5xx (e.g. Wiley TDM 500) | `--retry-failed` later |
| `unsupported`, `unsupported_content_type` | Duplicate, not a paper (dataset, software, review comment) or rejected format | usually skip |

The four acceptance checks are: `%PDF-` header; size cap; at least 3 pages and 5,000 characters; title or DOI on
the first two pages. The tier comes from the copy actually downloaded: `OPEN_CC_BY_OR_PREPRINT` is attached,
`LICENSED_TDM_ONLY` is held. A publisher PDF that says "All rights reserved" with no open licence is held.

## Reporting back

Tell the user, briefly: papers processed; attached open copies by channel; held licensed copies (local only);
papers still without full text, grouped by journal and reason; the path of the pending checklist; and the next
action (browser downloads, `--retry-failed` later, or adding a key). Say plainly if a run was a dry run, was
interrupted, or skipped channels for missing keys. Never paste key values or text from held licensed copies.
