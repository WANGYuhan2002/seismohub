# Full-text PDF harvesting

**English** · [中文](PDF_HARVESTING_zh.md)

SeismoHub can fetch full-text PDFs for the papers in your library, or for any reading list, from open channels and
(optionally) through your institution's publisher text-and-data-mining (TDM) APIs. Every PDF is verified and
labelled with a licence tier before anything is written. Only open copies are attached to the app and may reach
hosted AI.

The same workflow is packaged as an agent skill in
[`.claude/skills/seismohub-pdf-harvest/`](../.claude/skills/seismohub-pdf-harvest/SKILL.md).

To use the harvester without SeismoHub, see [paper-pdf-harvest](https://github.com/WANGYuhan2002/paper-pdf-harvest).
It is the same engine as a standalone tool for reading lists, with its own agent skill. Its documentation covers
the channel order, which keys to register
([KEYS.md](https://github.com/WANGYuhan2002/paper-pdf-harvest/blob/main/docs/KEYS.md)), the measured coverage
([RESULTS.md](https://github.com/WANGYuhan2002/paper-pdf-harvest/blob/main/docs/RESULTS.md)) and use in other fields.

---

## 1. Tools

| Command | What it does | Writes |
|---|---|---|
| `scripts/harvest_pdfs.py` | Fills in missing PDFs for papers already in the library | DB rows, `downloads/`, `paper_bundles/` (backs up the DB first) |
| `scripts/harvest_literature_list.py` | Fetches a list of DOIs / arXiv IDs / citations into a folder | Only the output folder; never the DB |
| `scripts/zotero_triage_import.py` | Imports PDFs you downloaded in a browser or Zotero, sorted by licence | Dry run by default; `--commit` writes |
| `scripts/match_zotero_export.py` | Matches a Zotero "Export → files" folder to library papers | Dry run by default; `--commit` writes |
| App → **Upload missing PDFs** | Drag PDFs onto the library; matched by DOI and title | DB rows, `downloads/` |
| `scripts/reanalyze_stale_pdf.py` | Re-runs the AI analysis for papers whose analysis was written before their PDF arrived | Preview and cost estimate by default; `--run` calls Gemini (paid) after a DB backup |
| `.claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py` | Summarises the latest run report (read-only) | Nothing |

Run everything from the repository root with the project's Python environment active.

---

## 2. Keys and settings

Open channels need no key. Put optional keys in `.env`, or in `~/.config/seismo_harvester/keys.env` (outside the
repository, recommended):

```bash
mkdir -p ~/.config/seismo_harvester && chmod 700 ~/.config/seismo_harvester
cat > ~/.config/seismo_harvester/keys.env <<'EOF'
export ELSEVIER_API_KEY="..."
export WILEY_TDM_TOKEN="..."
export OPENALEX_API_KEY="..."
EOF
chmod 600 ~/.config/seismo_harvester/keys.env
```

| Setting | Purpose |
|---|---|
| `SEISMO_HARVEST_UA` | User-Agent. Add your own `mailto:` to join the OpenAlex / Crossref polite pools, e.g. `SeismoHub/2.0 (mailto:you@example.org)` |
| `ELSEVIER_API_KEY`, `ELSEVIER_INSTTOKEN` | Elsevier API (10.1016). Get a key at <https://dev.elsevier.com>. An institution token is optional |
| `WILEY_TDM_TOKEN` | Wiley TDM API (10.1002, 10.1111, 10.1029 / AGU). Issued to subscribers through Wiley's text-and-data-mining programme |
| `OPENALEX_API_KEY` | Optional. Sent only to `content.openalex.org` and single-work lookups |
| `SEISMO_HARVEST_KEYS_FILE` | Another keys file to read |
| `SEISMO_HARVEST_MAX_BYTES` | Size cap per download (default 80 MB; the upgrade layer raises it to 160 MB) |

The harvester prints which keys it found (`keys: wiley=yes elsevier=no openalex=yes`), never their values. An empty
line such as `WILEY_TDM_TOKEN=` in `.env` counts as unset, so it does not hide the value in a keys file.

---

## 3. Channels

The harvester builds an ordered candidate list per paper and stops at the first copy that passes all checks.

| Channel | Covers | What you get |
|---|---|---|
| `openalex` | OpenAlex locations with a CC licence and a `pdf_url` (Copernicus, Seismica, Nature OA, Science Advances, ...) | Mostly the published version |
| `openalex` (repository copies) | University repositories, HAL, Research Square, ... listed by OpenAlex | Preprints and accepted manuscripts |
| `arxiv` | arXiv IDs and `10.48550` DOIs, via `export.arxiv.org` (3 s between requests) | Preprints |
| `eartharxiv` | `10.31223` | Preprints |
| `copernicus` | `10.5194` (EGU journals) | PDF plus full-text XML, supplements and open reviews |
| `nature` | `10.1038` | Open-access articles are attached; subscription articles are kept local |
| `zenodo` | `10.5281` records that contain a paper PDF | Usually preprints |
| `ethrc` | `10.3929` and ETH-authored papers in the ETH Research Collection | Repository copies |
| `wiley` | `10.1002`, `10.1111`, `10.1029` with `WILEY_TDM_TOKEN` | Full published PDF; attached only if the article itself is CC-licensed |
| `elsevier` | `10.1016` with `ELSEVIER_API_KEY` | Open-access PDFs anywhere. Subscribed articles: the API serves only a 1-page preview PDF; full-text XML only from an entitled network |

`scripts/seismohub_upgrade_runner.py` is loaded automatically by both harvest CLIs and adds more open channels:
PMC open-access (official S3 dataset), Springer link PDFs, Research Square, ESSOAr → published-version mapping,
OSTI, USGS Publications Warehouse, GFZ, HAL and DataCite repository lookups.

Choose channels with `--channels openalex,arxiv,eartharxiv` (comma list).

### Sites that block scripts

Some publisher sites answer scripted requests with a Cloudflare challenge. They are listed in
`BOT_WALLED_HOSTS` in `services/fullbundle_harvest.py` and are skipped, not retried: `academic.oup.com` (GJI),
`pubs.geoscienceworld.org` (BSSA, SRL, TSR), `essopenarchive.org`, `onlinelibrary.wiley.com`,
`www.sciencedirect.com`, `www.science.org`, `www.pnas.org`, `www.mdpi.com`. Papers that only live there go to the
pending checklist (section 6). The harvester never solves CAPTCHAs and never works around bot protection.

---

## 4. Checks and licence tiers

A PDF is accepted only after four checks:

1. the file starts with `%PDF-`;
2. it is within the size cap;
3. it has at least 3 pages and at least 5,000 characters of text (rules out previews and login pages);
4. the paper's title, or its DOI plus a looser title match, appears on the first two pages (rules out wrong papers).

The licence tier is decided from **the copy actually downloaded**, not from the paper's open-access status:

| Tier | Meaning | Where it goes |
|---|---|---|
| `OPEN_CC_BY_OR_PREPRINT` | CC / public-domain licence, or an openly hosted preprint / repository copy | Attached to the paper (`downloads/`); may be analysed by Gemini |
| `LICENSED_TDM_ONLY` | Publisher version obtained through an institutional licence (Wiley / Elsevier TDM, subscription PDFs) | Kept in `paper_bundles/` only; never attached; never sent to hosted AI |

A publisher PDF whose first pages say "All rights reserved" with no open-licence statement is treated as
`LICENSED_TDM_ONLY`, whatever the metadata claims. The same rules protect the app: the AI analysis falls back to
metadata only, and the Q&A chat refuses, when a paper's PDF is licensed content. `--attach-tdm` is sealed and
exits without doing anything.

---

## 5. Typical runs

```bash
# Library mode: see what would happen first. Writes nothing.
python scripts/harvest_pdfs.py --dry-run --limit 20

# Fetch for real (the DB is backed up first).
python scripts/harvest_pdfs.py --limit 200

# One journal, one DOI prefix, or single papers.
python scripts/harvest_pdfs.py --journal "Geophysical Research Letters"
python scripts/harvest_pdfs.py --doi-prefix 10.5194
python scripts/harvest_pdfs.py --doi 10.1029/2026GL125440 --paper-id <paper-id>

# Retry papers that failed or were held earlier, e.g. Elsevier XML from an entitled network.
python scripts/harvest_pdfs.py --doi-prefix 10.1016 --retry-failed

# Reading-list mode: any DOIs / arXiv IDs / citations; the library DB is not touched.
python scripts/harvest_literature_list.py --doi 10.1029/JB082i020p02981 --out-dir ~/seismo_classics
python scripts/harvest_literature_list.py --query "Shapiro Campillo 2005 ambient noise surface-wave tomography"
python scripts/harvest_literature_list.py --input-file reading_list.txt --out-dir ~/seismo_pack
```

Other flags: `--limit N`, `--paper-id ID`, `--no-extras` (skip XML, supplements, reviews, code/data links),
`--download-code` (GitHub tarballs up to 50 MB cited as code), `--db PATH`, `--bundle-root DIR`,
`--downloads-dir DIR`, `--keys-file FILE`, `--no-backup` (throwaway databases only).

The harvester never overwrites an existing PDF, never attaches a file that another paper already owns, and never
starts an AI analysis.

### Re-analysing papers once their PDF is attached

A paper analysed before its PDF arrived keeps its abstract-based analysis; the detail panel then says the PDF was
attached after the analysis. To redo those analyses from the full text:

```bash
python scripts/reanalyze_stale_pdf.py                         # free preview: which papers, pages, estimated cost
python scripts/reanalyze_stale_pdf.py --min-relevance 8       # preview a subset
python scripts/reanalyze_stale_pdf.py --run --limit 1         # paid: one paper first
python scripts/reanalyze_stale_pdf.py --run --budget-usd 20   # paid: stop at about $20
```

Licensed (TDM) PDFs are skipped. `--run` backs up the database and writes a report to `output/`. The cost
estimate uses the price constants at the top of the script; check them against your provider's current pricing.

---

## 6. Outputs

| Path | Contents |
|---|---|
| `downloads/` | Open PDFs attached to papers (`papers.pdf_path`) |
| `paper_bundles/PaperBundle_<DOI>/` | `bundle_manifest.json`, the main manuscript, XML, supplements, figures, `code_and_data_links.json` |
| `paper_bundles/_reports/harvest_<timestamp>.jsonl` | One JSON line per paper: status, reason, attached / held copies, every attempt |
| `paper_bundles/_reports/pending_browser_fetch_<timestamp>.tsv` | Papers that need a browser download, with a link and a suggestion |
| `_db_backup_<timestamp>_pre_pdf_harvest_run/` | Database backup taken before the run |
| `<out-dir>/open_pdfs/`, `<out-dir>/tdm_bundles/`, `harvest_summary.tsv`, `pending_zotero.csv` | Reading-list mode output |

Summarise the latest report:

```bash
python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py
python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --report paper_bundles/_reports/harvest_2026-11-12_101500.jsonl --pending
```

### Statuses

| Status | Meaning | What to do |
|---|---|---|
| `success` | Open copy verified and attached | Nothing |
| `held_tdm_only` | Licensed copy kept in `paper_bundles/` only | Read it locally; an open copy may appear later (`--retry-failed`) |
| `captcha_or_bot_block` | Only copies on sites that block scripts | Download in a browser, then **Upload missing PDFs** |
| `no_pdf_but_html` | The host served an HTML page (e.g. PMC) | Open the link in a browser and save the PDF |
| `auth_required` | Login wall or 401/403 | Browser download from a network with access |
| `no_oa_copy` | No channel returned a downloadable copy | Browser download; check the author's page or repository |
| `transient_error` | Timeouts, HTTP 5xx | Retry later with `--retry-failed` |
| `unsupported` / `unsupported_content_type` | Not a paper, or a format the checks reject | Usually a dataset or software record; skip |

---

## 7. PDFs that need a browser

1. Make a checklist: the `pending_browser_fetch_*.tsv` from a run, or
   `python scripts/zotero_triage_import.py --export-batch checklist.csv`.
2. Download the PDFs in your browser (Zotero Connector works well), from a network that has access.
3. Bring them in:
   - app → **Upload missing PDFs** (matched by DOI and title), or
   - `python scripts/zotero_triage_import.py ~/Zotero/storage` (dry run), then add `--commit`.
     The triage sends open copies to the app and licensed copies to `paper_bundles/`.
   - `python scripts/match_zotero_export.py <export-folder>` for a Zotero "Export → files" folder.

The licence checks catch common markers (CC statements, "All rights reserved", institutional download stamps),
but they cannot recognise every subscription PDF. Do not upload licensed PDFs that your institution does not allow
you to send to a cloud AI service.

---

## 8. Etiquette

- Each host has a minimum interval between requests (arXiv: 3 s; publisher APIs: 0.5–1 s; other hosts: 1 s).
- The User-Agent is honest and identifies the tool; add your own `mailto:` through `SEISMO_HARVEST_UA`.
- Bot-protected sites are skipped, not hammered. There is no browser automation, no fingerprint spoofing and no
  CAPTCHA solving in this repository.
- TDM keys belong to your institution's licence. Use them only within its terms, and keep TDM copies local.
