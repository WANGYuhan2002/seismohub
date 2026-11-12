# SeismoHub

[English](README.md) · **中文**

面向地震学研究者的自托管、本地优先的文献中心。SeismoHub 按你关注的主题、作者和期刊追踪新论文，用 Google
Gemini 精读，并让每一条 AI 结论都能链接回原文的页码和段落。它在你自己的电脑上以一个 FastAPI 进程加一个 SQLite
数据库运行：没有账号，没有遥测，没有云同步，文献库就是你自己的一个文件。

> SeismoHub 是单用户软件，**没有登录功能**。请让它只监听 `127.0.0.1`（默认就是这样），或放在带认证的反向代理后面。
> 详见[安全与受许可内容](#安全与受许可内容)。

---

## 功能

**追踪**
- 按主题、作者、期刊或机构建立追踪器，按天、周或月从 OpenAlex、Crossref、arXiv 和期刊 RSS 更新
  （可选 ETH Zurich Research Collection）。
- 首次启动自带 11 个地震学预置追踪器：DAS / 光纤传感、全波形反演、背景噪声、震源物理、地震学机器学习、
  地震目录与检测、环境地震学等。
- 用 Gemini 向量做语义匹配；收件箱会标出上次访问以来的新论文。

**AI 精读**
- 基于整篇 PDF 生成中英双语的结构化分析。
- 引用锚点：每条 AI 结论都带页码、段落和原文引文，点击即跳到对应段落。
- 每篇论文都有问答对话，历史会保存，支持 Markdown 和 LaTeX 公式。

**导入与导出**
- 用 DOI、arXiv ID、PMID / PMC ID、网址或任意文字添加论文。
- 上传 PDF（单篇或批量），按 DOI 和标题自动匹配到库里的条目。
- 支持监视文件夹、Zotero 导入，可导出 BibTeX、RIS、CSL-JSON、PDF 压缩包和 Obsidian 笔记库。

**全文抓取**（命令行 + Agent 技能）
- 从开放渠道抓取并校验全文 PDF：OpenAlex、arXiv、EarthArXiv、Copernicus / EGU、Zenodo、PMC、
  Nature 开放获取、机构仓储。
- 可选使用你所在机构的 Elsevier 和 Wiley 文本与数据挖掘（TDM）密钥。
- 每个 PDF 入库前都要过校验（确实是 PDF、至少 3 页、前几页能找到标题或 DOI），并标注许可层级。
  TDM 许可的副本只留在你的硬盘上，不会发送给云端 AI。详见 [docs/PDF_HARVESTING.zh-CN.md](docs/PDF_HARVESTING.zh-CN.md)。

---

## 快速开始

需要 **Python 3.11 或更新版本**，AI 功能需要一个 Google Vertex AI API 密钥。

```bash
git clone https://github.com/WANGYuhan2002/seismohub.git
cd seismohub
python3.11 -m venv .venv
source .venv/bin/activate            # Windows：.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # 然后填写 VERTEX_AI_API_KEY、VERTEX_AI_PROJECT、PIPELINE_CONTACT_EMAIL
SEISMO_PORT=8000 python main.py
```

浏览器打开 <http://127.0.0.1:8000>。首次启动会创建 `seismo_hub.db`、写入预置追踪器，并在后台开始抓取近期论文。
如果想要一个空的、不联网抓取的实例：

```bash
SEISMO_NO_AUTOFETCH=1 SEISMO_NO_PRESET_SEED=1 SEISMO_PORT=8000 python main.py
```

密钥也可以稍后在应用内的 **Settings（设置）** 页面填写，它会以 0600 权限写入 `.env`。

### Docker

```bash
cp .env.example .env                 # 填入密钥
mkdir -p data downloads paper_bundles
docker compose up -d --build
```

然后访问 <http://localhost:8000>。端口只发布在宿主机的 `127.0.0.1` 上。数据库、PDF 和全文包都保存在宿主机的
`./data`、`./downloads` 和 `./paper_bundles`。远程服务器、备份等更多部署方式见 [docs/DEPLOYMENT.zh-CN.md](docs/DEPLOYMENT.zh-CN.md)。

---

## 配置

所有设置都是环境变量，完整说明见 [`.env.example`](.env.example)。主要的几项：

| 变量 | 用途 | 获取方式 |
|---|---|---|
| `VERTEX_AI_API_KEY` | AI 分析（必需）；问答和语义向量 | Google Cloud 控制台 → Vertex AI → API 密钥 |
| `VERTEX_AI_PROJECT` | 经你的项目路由的模型 | 你的 Google Cloud 项目 ID |
| `GEMINI_API_KEY` | 仅作问答和语义向量的回退 | Google AI Studio |
| `PIPELINE_CONTACT_EMAIL` | 进入 OpenAlex / Crossref / Unpaywall 的礼貌池 | 你自己的邮箱 |
| `GOOGLE_CUSTOM_SEARCH_API_KEY`、`GOOGLE_CSE_CX` | 可选：AI 分析时补充网络检索结果 | Google 可编程搜索 |
| `ETH_RC_API_KEY` | 可选：ETH Research Collection 来源 | ETH 图书馆 API 门户 |
| `ELSEVIER_API_KEY`、`WILEY_TDM_TOKEN`、`OPENALEX_API_KEY` | 可选：全文抓取渠道 | 各出版商的开发者门户 |

**API 费用。** 服务可以在每篇新论文到达时就在后台分析它。`.env.example` 默认关闭了这一功能
（`DISABLE_EAGER_ANALYSIS=1`），论文会在你打开时再分析。删掉这一行即可恢复后台全量分析。

---

## 全文 PDF 抓取

```bash
python scripts/harvest_pdfs.py --dry-run --limit 20     # 先看会抓到什么，不写入任何东西
python scripts/harvest_pdfs.py --limit 100              # 抓取、校验，并把开放副本挂到论文上
python scripts/harvest_literature_list.py --input-file reading_list.txt --out-dir ~/seismo_pack
```

- `harvest_pdfs.py` 为库里已有的论文补齐 PDF。写入前会自动备份数据库。
- `harvest_literature_list.py` 把任意一份 DOI、arXiv ID 或引文清单抓到一个文件夹里，不碰数据库。
- 出版商不允许脚本下载的论文会写进一份清单。用浏览器下载它们，再用应用里的 **Upload missing PDFs（上传缺失的 PDF）**。
- 抓取从不触发 AI 分析。`scripts/reanalyze_stale_pdf.py` 会重做那些"写于 PDF 挂上之前"的分析；不加 `--run` 时只预览并估算费用。

渠道、密钥、许可层级和运行报告的说明见 [docs/PDF_HARVESTING.zh-CN.md](docs/PDF_HARVESTING.zh-CN.md)。

### Agent 技能

仓库自带一个 [Claude Code](https://claude.com/claude-code) 技能：
[`.claude/skills/seismohub-pdf-harvest/`](.claude/skills/seismohub-pdf-harvest/SKILL.md)。用 Claude Code 打开仓库，
直接说"帮我把 GJI 论文缺的 PDF 抓下来"或"下载这份 DOI 清单的全文"即可。技能会先试运行，遵守许可规则，
并用 `harvest_status.py` 汇总运行报告。其他编程 Agent 也可以照着同一份 `SKILL.md` 执行。

---

## 安全与受许可内容

- **没有认证。** `python main.py` 只监听 `127.0.0.1`。远程使用请通过 SSH 隧道或带认证的反向代理。不要把端口暴露到
  公网：任何能访问它的人都能读取你的文献库、修改你的 API 密钥。
- **密钥**保存在 `.env`（已被 git 忽略，权限 0600），或仓库外的 `~/.config/seismo_harvester/keys.env`。
  设置页面只显示打码后的值。
- **哪些内容会发给 Google。** AI 分析会把挂载的 PDF 上传给 Gemini；问答发送缓存的页面文字；语义向量发送标题和摘要。
  除此之外，只有对上述学术 API 的元数据查询会离开你的电脑。
- **受许可的全文只留在本地。** 通过机构 TDM 许可取得的副本，以及首页写有 "All rights reserved" 或带机构下载水印、
  却没有开放许可声明的 PDF，都只保存在 `paper_bundles/`：不挂进应用，不发给云端 AI。这类论文的问答会直接拒绝。
- **出版商条款。** 抓取器使用如实的 User-Agent，对每个站点的请求都有间隔，遇到对脚本返回人机验证的站点就跳过。
  它不会破解验证码，也不会绕过 Cloudflare。TDM 密钥请只在你所在机构签署的条款范围内使用。

安全问题的报告方式见 [SECURITY.md](SECURITY.md)。

---

## 目录结构

```
main.py                 FastAPI 应用（85 个路由）和后台调度
db.py, models.py        SQLite 表结构、幂等迁移、SQLAlchemy 1.4 模型
services/               抓取、AI 分析、问答、向量、引用锚点、导出、Obsidian 笔记库、
                        Zotero 导入、PDF 匹配、全文抓取器、许可守卫
templates/index.html    整个网页界面（原生 JS + Tailwind）
scripts/                抓取与维护用的命令行工具
seed_presets.json       11 个预置追踪器
tests/                  pytest 测试（离线运行，使用内存数据库）
docs/                   部署与抓取指南（中英文）
.claude/skills/         PDF 抓取的 Agent 技能
```

## 开发

```bash
pip install -r requirements.txt
DISABLE_TRACKER_SCHEDULER=1 DISABLE_EAGER_ANALYSIS=1 SEISMO_NO_AUTOFETCH=1 python -m pytest -q
```

测试离线运行，使用内存数据库，不需要任何 API 密钥。参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)。论文元数据来自 OpenAlex（CC0）、Crossref、arXiv 和出版商自己的订阅源；全文的版权仍归各出版商所有。
