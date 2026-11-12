# Deploying SeismoHub

**English** · [中文](DEPLOYMENT_zh.md)

SeismoHub is a single-user web app: one Python process, one SQLite file, and folders of PDFs. Pick the setup that
matches where you want to read.

| Setup | Good for |
|---|---|
| [Local Python](#1-local-python) | Your own laptop or workstation |
| [Docker Compose](#2-docker-compose) | A clean, reproducible install; servers |
| [Remote server](#3-remote-server) | A group server or a machine you reach over SSH |

---

## 1. Local Python

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add your keys
SEISMO_PORT=8000 python main.py
```

- `python main.py` binds to `127.0.0.1` and opens a browser tab (`SEISMO_OPEN_BROWSER=0` to skip). Without
  `SEISMO_PORT` the port is derived from the folder path, so two checkouts never collide.
- `python -m uvicorn main:app --port 8000` works too.
- Keep the terminal open, or run it under a process manager (`launchd`, `systemd --user`, `tmux`).

Requirements: Python 3.11+ on macOS, Linux or Windows. The pinned `requirements.txt` was verified on CPython 3.11.

---

## 2. Docker Compose

```bash
cp .env.example .env
mkdir -p data downloads paper_bundles
docker compose up -d --build
docker compose logs -f seismohub      # follow the logs
```

- The UI is at <http://localhost:8000>. Change the host port with `SEISMOHUB_HOST_PORT=8123 docker compose up -d`.
- The port is published on the host's `127.0.0.1` only.
- Volumes: `./data` (SQLite database), `./downloads` (attached PDFs), `./paper_bundles` (full-text bundles and
  harvest reports).
- The container runs as UID 1000. On Linux, if the folders belong to another user:
  `sudo chown -R 1000:1000 data downloads paper_bundles`.
- Keys come from `.env` through `env_file`. Edit `.env` on the host and run `docker compose up -d` to apply.
  The in-app Settings page writes inside the container, so those changes are lost when the container is
  recreated.
- Harvesting inside the container:
  `docker compose exec seismohub python scripts/harvest_pdfs.py --dry-run --limit 20`.

---

## 3. Remote server

SeismoHub has **no login**. Never publish its port on a public interface.

**SSH tunnel** (simplest, nothing exposed):

```bash
# on the server
SEISMO_PORT=8000 SEISMO_OPEN_BROWSER=0 python main.py      # or docker compose up -d
# on your laptop
ssh -N -L 8000:127.0.0.1:8000 you@server
# then open http://localhost:8000
```

**Reverse proxy with authentication**, for example Caddy with basic auth:

```
papers.example.org {
    basicauth {
        reader <hashed-password>        # caddy hash-password
    }
    reverse_proxy 127.0.0.1:8000
}
```

Any proxy that adds authentication works (nginx + `auth_basic`, oauth2-proxy, Tailscale, Cloudflare Access).
Anyone who passes the proxy can read the library and change the API keys on the Settings page.

---

## 4. Data, backup and upgrades

| What | Where |
|---|---|
| Database | `seismo_hub.db` next to `main.py`, or `SEISMO_DB_PATH` (Docker: `data/seismo_hub.db`) |
| Attached PDFs | `downloads/` |
| Full-text bundles, harvest reports | `paper_bundles/` |
| Obsidian vault | the folder you choose in the app |
| Keys | `.env`, `~/.config/seismo_harvester/keys.env` |

**Backup.** SQLite runs in WAL mode, so copy the database with the backup API instead of `cp` while the app runs:

```bash
sqlite3 seismo_hub.db ".backup 'seismo_hub_$(date +%Y%m%d).db'"
tar czf seismohub-files-$(date +%Y%m%d).tgz downloads paper_bundles
```

**Upgrade.** Pull the new code and restart. `init_db()` applies schema changes on start; they are additive and
idempotent. Back up the database first.

```bash
git pull
pip install -r requirements.txt       # Docker: docker compose up -d --build
```

---

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `pip install` fails, a package needs Python >= 3.11 | Recreate the venv with `python3.11 -m venv .venv` |
| AI analysis says `Missing VERTEX_AI_API_KEY` | Add the key to `.env` or the Settings page; restart is not needed after Settings |
| Model 404 / not found | The model is not available for your key or region. Set `SEISMO_PDF_ANALYSIS_MODEL` / `SEISMO_METADATA_ANALYSIS_MODEL` to one you can use |
| First start is slow, many API calls | The first fetch backfills recent papers. Start with `SEISMO_NO_AUTOFETCH=1` to stay offline |
| `database is locked` | Another process holds the DB (for example two servers on one file). Run one server per database |
| Port already in use | Set `SEISMO_PORT` (Docker: `SEISMOHUB_HOST_PORT`) |
| Health check | `curl http://127.0.0.1:8000/api/health` |
