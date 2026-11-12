# SeismoHub

**English** · [中文](README.zh-CN.md)

A self-hosted, local-first literature hub for seismology. SeismoHub tracks new papers on the topics, authors and
journals you follow, reads them with Google Gemini, and keeps every AI statement linked to the page and paragraph
it came from. It runs on your own machine as one FastAPI process with a SQLite database. There is no account, no
telemetry and no cloud sync; your library is a file you own.

> SeismoHub is single-user software and has **no login**. Run it on `127.0.0.1` (the default) or behind a reverse
> proxy that handles authentication. See [Security](#security-and-licensed-content).

---

## Features

**Tracking**
- Trackers by topic, author, journal or institution, refreshed daily, weekly or monthly from OpenAlex, Crossref,
  arXiv and journal RSS feeds (optionally ETH Zurich Research Collection).
- 11 preset seismology trackers on first start: DAS / fibre-optic sensing, full-waveform inversion, ambient noise,
  earthquake source physics, ML in seismology, catalogs and detection, environmental seismology and more.
- Semantic matching with Gemini embeddings, plus an Inbox that shows what is new since your last visit.

**Reading with AI**
- Bilingual (English / Chinese) structured analysis of each paper from its full PDF.
- Citation anchors: every AI claim carries page, paragraph and quote, and clicking it opens that paragraph.
- Per-paper Q&A chat with saved history, Markdown and LaTeX rendering.

**Getting papers in and out**
- Add a paper from a DOI, arXiv ID, PMID / PMC ID, URL or free text.
- Upload PDFs (single or bulk). They are matched to library entries by DOI and title.
- Watch folders, Zotero import, and export to BibTeX, RIS, CSL-JSON, PDF zip and an Obsidian vault.

**Full-text harvesting** (command line + agent skill)
- Fetches verified full-text PDFs from open channels: OpenAlex, arXiv, EarthArXiv, Copernicus / EGU, Zenodo,
  PMC, Nature open access, institutional repositories.
- Optionally uses your institution's Elsevier and Wiley text-and-data-mining (TDM) keys.
- Every PDF is checked before it is accepted (real PDF, at least 3 pages, title or DOI on the first pages) and
  labelled with a licence tier. TDM-licensed copies stay on your disk and are never sent to hosted AI.
  See [docs/PDF_HARVESTING.md](docs/PDF_HARVESTING.md).

---

## Quick start

Requirements: Python **3.11 or newer**, and a Google Vertex AI API key for the AI features.

```bash
git clone https://github.com/WANGYuhan2002/seismohub.git
cd seismohub
python3.11 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then set VERTEX_AI_API_KEY, VERTEX_AI_PROJECT, PIPELINE_CONTACT_EMAIL
SEISMO_PORT=8000 python main.py
```

Open <http://127.0.0.1:8000>. On the first start SeismoHub creates `seismo_hub.db`, seeds the preset trackers and
starts fetching recent papers in the background. To start with an empty, offline instance instead:

```bash
SEISMO_NO_AUTOFETCH=1 SEISMO_NO_PRESET_SEED=1 SEISMO_PORT=8000 python main.py
```

You can also enter the keys later on the in-app **Settings** page. It writes them to `.env` with file mode 0600.

### Docker

```bash
cp .env.example .env                 # add your keys
mkdir -p data downloads paper_bundles
docker compose up -d --build
```

The app is then at <http://localhost:8000>, published on the host's `127.0.0.1` only. The database, PDFs and
full-text bundles stay on the host in `./data`, `./downloads` and `./paper_bundles`. More options, including
remote servers and backups, are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Configuration

All settings are environment variables, documented in [`.env.example`](.env.example). The main ones:

| Variable | Needed for | Where to get it |
|---|---|---|
| `VERTEX_AI_API_KEY` | AI analysis (required); Q&A and embeddings | Google Cloud console → Vertex AI → API keys |
| `VERTEX_AI_PROJECT` | Models that are routed through your project | Your Google Cloud project ID |
| `GEMINI_API_KEY` | Fallback for Q&A and embeddings only | Google AI Studio |
| `PIPELINE_CONTACT_EMAIL` | Polite-pool access to OpenAlex / Crossref / Unpaywall | Your own e-mail address |
| `GOOGLE_CUSTOM_SEARCH_API_KEY`, `GOOGLE_CSE_CX` | Optional web context in the AI analysis | Google Programmable Search |
| `ETH_RC_API_KEY` | Optional ETH Research Collection source | ETH Library API portal |
| `ELSEVIER_API_KEY`, `WILEY_TDM_TOKEN`, `OPENALEX_API_KEY` | Optional full-text harvesting channels | Publisher developer portals |

**API cost.** The server can analyse every new paper in the background as soon as it arrives. `.env.example`
turns this off (`DISABLE_EAGER_ANALYSIS=1`), so papers are analysed when you open them. Remove that line to
analyse everything ahead of time.

---

## Full-text PDF harvesting

```bash
python scripts/harvest_pdfs.py --dry-run --limit 20     # check what would be fetched; writes nothing
python scripts/harvest_pdfs.py --limit 100              # fetch, verify and attach open copies
python scripts/harvest_literature_list.py --input-file reading_list.txt --out-dir ~/seismo_pack
```

- `harvest_pdfs.py` fills in missing PDFs for papers already in your library. It backs up the database before it
  writes anything.
- `harvest_literature_list.py` fetches any list of DOIs, arXiv IDs or citations into a folder, without touching
  the database.
- Papers that publishers block for scripts are written to a checklist. Download those in your browser, then use
  **Upload missing PDFs** in the app.
- Harvesting never starts an AI analysis. `scripts/reanalyze_stale_pdf.py` redoes the analyses that were written
  before a paper's PDF arrived; it previews and estimates the cost unless you pass `--run`.

Channels, keys, licence tiers and reports are described in [docs/PDF_HARVESTING.md](docs/PDF_HARVESTING.md).

### Agent skill

The repository ships a [Claude Code](https://claude.com/claude-code) skill at
[`.claude/skills/seismohub-pdf-harvest/`](.claude/skills/seismohub-pdf-harvest/SKILL.md). Open the repository in
Claude Code and ask, for example, *"fetch the missing PDFs for my GJI papers"* or *"download full texts for this
DOI list"*. The skill runs a dry run first, applies the licence rules, and summarises the run report with
`harvest_status.py`. Other coding agents can follow the same `SKILL.md`.

---

## Security and licensed content

- **No authentication.** `python main.py` listens on `127.0.0.1`. For remote use, connect through an SSH tunnel
  or a reverse proxy with authentication. Do not expose the port to the internet: anyone who can reach it can
  read your library and change your API keys.
- **Keys** live in `.env` (git-ignored, mode 0600) or in `~/.config/seismo_harvester/keys.env` outside the
  repository. The Settings page only ever shows masked values.
- **What is sent to Google.** The AI analysis uploads the attached PDF to Gemini. Q&A sends cached page text.
  Embeddings send titles and abstracts. Nothing else leaves your machine except metadata queries to the
  scholarly APIs listed above.
- **Licensed full text stays local.** Copies obtained through institutional TDM licences, and PDFs that carry
  "All rights reserved" or an institutional download stamp without an open licence, are kept in `paper_bundles/`,
  never attached to the app and never sent to hosted AI. The Q&A chat refuses for such papers.
- **Publisher terms.** The harvester sends an honest User-Agent, waits between requests to each host, and skips
  sites that answer scripts with a bot challenge. It never solves CAPTCHAs or works around Cloudflare. Use TDM
  keys only within the terms your institution signed.

Report security issues as described in [SECURITY.md](SECURITY.md).

---

## Project layout

```
main.py                 FastAPI app (85 routes) and background schedulers
db.py, models.py        SQLite schema, idempotent migrations, SQLAlchemy 1.4 models
services/               fetching, AI analysis, Q&A, embeddings, citation anchors, exports, Obsidian vault,
                        Zotero import, PDF matching, full-text harvester, licence guards
templates/index.html    the whole web UI (vanilla JS + Tailwind)
scripts/                harvesting and maintenance command-line tools
seed_presets.json       the 11 preset trackers
tests/                  pytest suite (offline; in-memory database)
docs/                   deployment and harvesting guides (English and Chinese)
.claude/skills/         agent skill for PDF harvesting
```

## Development

```bash
pip install -r requirements.txt
DISABLE_TRACKER_SCHEDULER=1 DISABLE_EAGER_ANALYSIS=1 SEISMO_NO_AUTOFETCH=1 python -m pytest -q
```

The tests run offline against an in-memory database and need no API keys. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). Paper metadata comes from OpenAlex (CC0), Crossref, arXiv and the publishers' own feeds. Full
texts remain under their publishers' licences.
