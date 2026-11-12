# 部署 SeismoHub

[English](DEPLOYMENT.md) · **中文**

SeismoHub 是单用户网页应用：一个 Python 进程、一个 SQLite 文件，再加几个存放 PDF 的文件夹。按你想在哪里读论文来选部署方式。

| 方式 | 适合 |
|---|---|
| [本地 Python](#1-本地-python) | 自己的笔记本或工作站 |
| [Docker Compose](#2-docker-compose) | 干净、可复现的安装；服务器 |
| [远程服务器](#3-远程服务器) | 课题组服务器，或通过 SSH 访问的机器 |

---

## 1. 本地 Python

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # 填入密钥
SEISMO_PORT=8000 python main.py
```

- `python main.py` 只绑定 `127.0.0.1`，并会打开一个浏览器标签页（`SEISMO_OPEN_BROWSER=0` 可关闭）。不设 `SEISMO_PORT`
  时端口由文件夹路径推算，所以两个副本不会冲突。
- 也可以用 `python -m uvicorn main:app --port 8000` 启动。
- 保持终端开着，或交给进程管理工具（`launchd`、`systemd --user`、`tmux`）。

要求：macOS、Linux 或 Windows 上的 Python 3.11+。固定版本的 `requirements.txt` 在 CPython 3.11 上验证过。

---

## 2. Docker Compose

```bash
cp .env.example .env
mkdir -p data downloads paper_bundles
docker compose up -d --build
docker compose logs -f seismohub      # 查看日志
```

- 界面地址 <http://localhost:8000>。用 `SEISMOHUB_HOST_PORT=8123 docker compose up -d` 修改宿主机端口。
- 端口只发布在宿主机的 `127.0.0.1` 上。
- 数据卷：`./data`（SQLite 数据库）、`./downloads`（挂载的 PDF）、`./paper_bundles`（全文包和抓取报告）。
- 容器以 UID 1000 运行。在 Linux 上如果这些文件夹属于别的用户：`sudo chown -R 1000:1000 data downloads paper_bundles`。
- 密钥通过 `env_file` 从 `.env` 读取。在宿主机上修改 `.env`，再运行 `docker compose up -d` 生效。应用内设置页面写的是
  容器内部的文件，容器重建后会丢失。
- 在容器里抓取全文：`docker compose exec seismohub python scripts/harvest_pdfs.py --dry-run --limit 20`。

---

## 3. 远程服务器

SeismoHub **没有登录功能**。绝不要把它的端口发布到公网接口上。

**SSH 隧道**（最简单，什么都不暴露）：

```bash
# 在服务器上
SEISMO_PORT=8000 SEISMO_OPEN_BROWSER=0 python main.py      # 或 docker compose up -d
# 在你的电脑上
ssh -N -L 8000:127.0.0.1:8000 you@server
# 然后打开 http://localhost:8000
```

**带认证的反向代理**，例如带 basic auth 的 Caddy：

```
papers.example.org {
    basicauth {
        reader <hashed-password>        # 用 caddy hash-password 生成
    }
    reverse_proxy 127.0.0.1:8000
}
```

任何能加认证的代理都可以（nginx + `auth_basic`、oauth2-proxy、Tailscale、Cloudflare Access）。通过代理的人都能读取文献库，
也能在设置页面修改 API 密钥。

---

## 4. 数据、备份与升级

| 内容 | 位置 |
|---|---|
| 数据库 | `main.py` 旁边的 `seismo_hub.db`，或 `SEISMO_DB_PATH`（Docker：`data/seismo_hub.db`） |
| 挂载的 PDF | `downloads/` |
| 全文包、抓取报告 | `paper_bundles/` |
| Obsidian 笔记库 | 你在应用里选择的文件夹 |
| 密钥 | `.env`、`~/.config/seismo_harvester/keys.env` |

**备份。** SQLite 运行在 WAL 模式下，应用运行时请用备份 API 复制数据库，而不是直接 `cp`：

```bash
sqlite3 seismo_hub.db ".backup 'seismo_hub_$(date +%Y%m%d).db'"
tar czf seismohub-files-$(date +%Y%m%d).tgz downloads paper_bundles
```

**升级。** 拉取新代码后重启即可。`init_db()` 会在启动时应用表结构变更，这些变更只增不删，可重复执行。升级前先备份数据库。

```bash
git pull
pip install -r requirements.txt       # Docker：docker compose up -d --build
```

---

## 5. 常见问题

| 现象 | 解决办法 |
|---|---|
| `pip install` 失败，提示某个包需要 Python >= 3.11 | 用 `python3.11 -m venv .venv` 重建虚拟环境 |
| AI 分析提示 `Missing VERTEX_AI_API_KEY` | 在 `.env` 或设置页面填写密钥；通过设置页面填写后无需重启 |
| 模型 404 / 找不到模型 | 你的密钥或区域没有这个模型。把 `SEISMO_PDF_ANALYSIS_MODEL` / `SEISMO_METADATA_ANALYSIS_MODEL` 设为你能用的模型 |
| 首次启动很慢、API 调用很多 | 第一次抓取会回填近期论文。用 `SEISMO_NO_AUTOFETCH=1` 启动可保持离线 |
| `database is locked` | 有别的进程占用数据库（例如两个服务用同一个文件）。一个数据库只运行一个服务 |
| 端口被占用 | 设置 `SEISMO_PORT`（Docker：`SEISMOHUB_HOST_PORT`） |
| 健康检查 | `curl http://127.0.0.1:8000/api/health` |
