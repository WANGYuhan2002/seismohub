# 全文 PDF 抓取

[English](PDF_HARVESTING.md) · **中文**

SeismoHub 可以为文献库里的论文、或任意一份阅读清单抓取全文 PDF：来源是开放渠道，以及（可选）你所在机构的出版商
文本与数据挖掘（TDM）接口。写入任何东西之前，每个 PDF 都要先通过校验并标注许可层级。只有开放副本会挂进应用，
也只有它们可能被发送给云端 AI。

同一套流程也打包成了 Agent 技能：[`.claude/skills/seismohub-pdf-harvest/`](../.claude/skills/seismohub-pdf-harvest/SKILL.md)。

不用 SeismoHub 也想用这个抓取器，见 [paper-pdf-harvest](https://github.com/WANGYuhan2002/paper-pdf-harvest)
（[中文说明](https://github.com/WANGYuhan2002/paper-pdf-harvest/blob/main/README_zh.md)）。它是同一个引擎做成的独立
阅读清单工具，自带 Agent 技能。文档写明了渠道顺序、要注册哪些密钥
（[KEYS_zh.md](https://github.com/WANGYuhan2002/paper-pdf-harvest/blob/main/docs/KEYS_zh.md)）、实测覆盖率
（[RESULTS_zh.md](https://github.com/WANGYuhan2002/paper-pdf-harvest/blob/main/docs/RESULTS_zh.md)），以及如何用到其他学科。

---

## 1. 工具

| 命令 | 作用 | 写入 |
|---|---|---|
| `scripts/harvest_pdfs.py` | 为库里已有的论文补齐 PDF | 数据库、`downloads/`、`paper_bundles/`（先备份数据库） |
| `scripts/harvest_literature_list.py` | 把一份 DOI / arXiv ID / 引文清单抓到一个文件夹 | 只写输出文件夹，从不碰数据库 |
| `scripts/zotero_triage_import.py` | 导入你在浏览器或 Zotero 里下载的 PDF，并按许可分流 | 默认试运行；加 `--commit` 才写入 |
| `scripts/match_zotero_export.py` | 把 Zotero "导出 → 文件" 的文件夹匹配到库里的论文 | 默认试运行；加 `--commit` 才写入 |
| 应用 → **Upload missing PDFs（上传缺失的 PDF）** | 把 PDF 拖进文献库，按 DOI 和标题匹配 | 数据库、`downloads/` |
| `scripts/reanalyze_stale_pdf.py` | 对"分析写于 PDF 挂上之前"的论文重新做 AI 分析 | 默认只预览并估算费用；加 `--run` 会先备份数据库，再调用 Gemini（付费） |
| `.claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py` | 汇总最近一次运行报告（只读） | 无 |

所有命令都在仓库根目录、激活项目的 Python 环境后运行。

---

## 2. 密钥与设置

开放渠道不需要密钥。可选的密钥写在 `.env`，或（推荐）写在仓库之外的 `~/.config/seismo_harvester/keys.env`：

```bash
mkdir -p ~/.config/seismo_harvester && chmod 700 ~/.config/seismo_harvester
cat > ~/.config/seismo_harvester/keys.env <<'EOF'
export ELSEVIER_API_KEY="..."
export WILEY_TDM_TOKEN="..."
export OPENALEX_API_KEY="..."
EOF
chmod 600 ~/.config/seismo_harvester/keys.env
```

| 设置 | 用途 |
|---|---|
| `SEISMO_HARVEST_UA` | User-Agent。加上你自己的 `mailto:` 以进入 OpenAlex / Crossref 礼貌池，例如 `SeismoHub/2.0 (mailto:you@example.org)` |
| `ELSEVIER_API_KEY`、`ELSEVIER_INSTTOKEN` | Elsevier API（10.1016）。在 <https://dev.elsevier.com> 申请密钥；机构令牌可选 |
| `WILEY_TDM_TOKEN` | Wiley TDM API（10.1002、10.1111、10.1029 / AGU）。通过 Wiley 的文本与数据挖掘计划发放给订阅机构 |
| `OPENALEX_API_KEY` | 可选。只发送给 `content.openalex.org` 和单篇查询 |
| `SEISMO_HARVEST_KEYS_FILE` | 另一个要读取的密钥文件 |
| `SEISMO_HARVEST_MAX_BYTES` | 单次下载大小上限（默认 80 MB；升级层会提高到 160 MB） |

抓取器只打印找到了哪些密钥（`keys: wiley=yes elsevier=no openalex=yes`），从不打印密钥的值。`.env` 里
`WILEY_TDM_TOKEN=` 这样的空行按未设置处理，不会盖住密钥文件里的值。

---

## 3. 渠道

抓取器为每篇论文排出一个有序的候选列表，遇到第一个通过全部校验的副本就停下。

| 渠道 | 覆盖范围 | 能拿到什么 |
|---|---|---|
| `openalex` | OpenAlex 中带 CC 许可和 `pdf_url` 的位置（Copernicus、Seismica、Nature OA、Science Advances 等） | 大多是正式发表版 |
| `openalex`（仓储副本） | OpenAlex 列出的大学机构库、HAL、Research Square 等 | 预印本和接受稿 |
| `arxiv` | arXiv ID 和 `10.48550` DOI，经 `export.arxiv.org`（每次请求间隔 3 秒） | 预印本 |
| `eartharxiv` | `10.31223` | 预印本 |
| `copernicus` | `10.5194`（EGU 期刊） | PDF，以及全文 XML、补充材料和公开审稿记录 |
| `nature` | `10.1038` | 开放获取文章挂进应用；订阅文章只留本地 |
| `zenodo` | `10.5281` 中含论文 PDF 的记录 | 多为预印本 |
| `ethrc` | `10.3929` 以及 ETH Research Collection 中 ETH 作者的论文 | 仓储副本 |
| `wiley` | 设置了 `WILEY_TDM_TOKEN` 时的 `10.1002`、`10.1111`、`10.1029` | 完整的正式版 PDF；只有文章本身是 CC 许可时才挂进应用 |
| `elsevier` | 设置了 `ELSEVIER_API_KEY` 时的 `10.1016` | 开放获取 PDF 在任何网络下都能拿到。订阅文章：API 只给 1 页预览 PDF；全文 XML 只在有权限的网络下提供 |

两个抓取命令都会自动加载 `scripts/seismohub_upgrade_runner.py`，它补充了更多开放渠道：PMC 开放获取（官方 S3 数据集）、
Springer 链接 PDF、Research Square、ESSOAr 预印本 → 正式版映射、OSTI、USGS 出版物库、GFZ、HAL，以及 DataCite
仓储查询。

用 `--channels openalex,arxiv,eartharxiv`（逗号分隔）指定渠道。

### 拦截脚本的站点

有些出版商站点会对脚本请求返回 Cloudflare 人机验证。它们列在 `services/fullbundle_harvest.py` 的
`BOT_WALLED_HOSTS` 中，会被直接跳过而不是反复请求：`academic.oup.com`（GJI）、`pubs.geoscienceworld.org`
（BSSA、SRL、TSR）、`essopenarchive.org`、`onlinelibrary.wiley.com`、`www.sciencedirect.com`、`www.science.org`、
`www.pnas.org`、`www.mdpi.com`。只在这些站点上有副本的论文会进入待下载清单（第 6 节）。抓取器从不破解验证码，
也从不绕过反爬保护。

---

## 4. 校验与许可层级

PDF 必须通过四道校验才会被接受：

1. 文件以 `%PDF-` 开头；
2. 大小在上限以内；
3. 至少 3 页、至少 5,000 个字符的文字（排除预览页和登录页）；
4. 前两页能找到论文标题，或 DOI 加较宽松的标题匹配（排除挂错论文）。

许可层级按**实际下载到的那一份**判定，而不是按论文的开放获取状态：

| 层级 | 含义 | 去向 |
|---|---|---|
| `OPEN_CC_BY_OR_PREPRINT` | CC / 公有领域许可，或公开托管的预印本 / 仓储副本 | 挂到论文上（`downloads/`），可以交给 Gemini 分析 |
| `LICENSED_TDM_ONLY` | 通过机构许可取得的出版商版本（Wiley / Elsevier TDM、订阅 PDF） | 只存在 `paper_bundles/`；不挂进应用；不发给云端 AI |

出版商 PDF 只要前几页写着 "All rights reserved"、又没有开放许可声明，无论元数据怎么写，都按 `LICENSED_TDM_ONLY` 处理。
应用本身也遵守同样的规则：论文的 PDF 属于受许可内容时，AI 分析退回到只用元数据，问答会直接拒绝。
`--attach-tdm` 已被封住，运行时什么都不做就退出。

---

## 5. 常用命令

```bash
# 文献库模式：先看看会发生什么，不写入任何东西。
python scripts/harvest_pdfs.py --dry-run --limit 20

# 正式抓取（先自动备份数据库）。
python scripts/harvest_pdfs.py --limit 200

# 只处理某个期刊、某个 DOI 前缀，或单篇论文。
python scripts/harvest_pdfs.py --journal "Geophysical Research Letters"
python scripts/harvest_pdfs.py --doi-prefix 10.5194
python scripts/harvest_pdfs.py --doi 10.1029/2026GL125440 --paper-id <paper-id>

# 重试之前失败或被保留的论文，例如在有权限的网络下重新获取 Elsevier XML。
python scripts/harvest_pdfs.py --doi-prefix 10.1016 --retry-failed

# 阅读清单模式：任意 DOI / arXiv ID / 引文；不碰文献库数据库。
python scripts/harvest_literature_list.py --doi 10.1029/JB082i020p02981 --out-dir ~/seismo_classics
python scripts/harvest_literature_list.py --query "Shapiro Campillo 2005 ambient noise surface-wave tomography"
python scripts/harvest_literature_list.py --input-file reading_list.txt --out-dir ~/seismo_pack
```

其他参数：`--limit N`、`--paper-id ID`、`--no-extras`（跳过 XML、补充材料、审稿记录、代码/数据链接）、
`--download-code`（下载论文引用的 GitHub 代码包，上限 50 MB）、`--db PATH`、`--bundle-root DIR`、
`--downloads-dir DIR`、`--keys-file FILE`、`--no-backup`（仅限一次性的测试数据库）。

抓取器从不覆盖已有的 PDF，从不把别的论文已占用的文件挂上去，也从不触发 AI 分析。

### PDF 挂上之后重新分析

在 PDF 到达之前就分析过的论文，会保留基于摘要的分析；详情面板会注明"PDF 是在分析之后才挂上的"。要基于全文重做这些分析：

```bash
python scripts/reanalyze_stale_pdf.py                         # 免费预览：哪些论文、多少页、估算费用
python scripts/reanalyze_stale_pdf.py --min-relevance 8       # 预览其中一部分
python scripts/reanalyze_stale_pdf.py --run --limit 1         # 付费：先试一篇
python scripts/reanalyze_stale_pdf.py --run --budget-usd 20   # 付费：花到约 20 美元就停
```

受许可（TDM）的 PDF 会被跳过。`--run` 会先备份数据库，并把报告写到 `output/`。费用估算用的是脚本开头的价格常量，
请对照你所用服务商的当前价格核实。

---

## 6. 输出

| 路径 | 内容 |
|---|---|
| `downloads/` | 挂到论文上的开放 PDF（`papers.pdf_path`） |
| `paper_bundles/PaperBundle_<DOI>/` | `bundle_manifest.json`、正文、XML、补充材料、图片、`code_and_data_links.json` |
| `paper_bundles/_reports/harvest_<时间戳>.jsonl` | 每篇论文一行 JSON：状态、原因、挂载 / 保留的副本、每一次尝试 |
| `paper_bundles/_reports/pending_browser_fetch_<时间戳>.tsv` | 需要用浏览器下载的论文，附链接和建议 |
| `_db_backup_<时间戳>_pre_pdf_harvest_run/` | 运行前的数据库备份 |
| `<out-dir>/open_pdfs/`、`<out-dir>/tdm_bundles/`、`harvest_summary.tsv`、`pending_zotero.csv` | 阅读清单模式的输出 |

汇总最近一次报告：

```bash
python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py
python .claude/skills/seismohub-pdf-harvest/scripts/harvest_status.py --report paper_bundles/_reports/harvest_2026-11-12_101500.jsonl --pending
```

### 状态码

| 状态 | 含义 | 怎么办 |
|---|---|---|
| `success` | 开放副本已校验并挂载 | 无需处理 |
| `held_tdm_only` | 受许可副本只保存在 `paper_bundles/` | 在本地阅读；以后可能出现开放副本（`--retry-failed`） |
| `captcha_or_bot_block` | 只有拦截脚本的站点上有副本 | 用浏览器下载，再 **Upload missing PDFs** |
| `no_pdf_but_html` | 站点返回的是 HTML 网页（例如 PMC） | 在浏览器里打开链接并保存 PDF |
| `auth_required` | 需要登录，或返回 401/403 | 在有权限的网络下用浏览器下载 |
| `no_oa_copy` | 没有任何渠道给出可下载的副本 | 用浏览器下载；查查作者主页或机构库 |
| `transient_error` | 超时、HTTP 5xx | 稍后用 `--retry-failed` 重试 |
| `unsupported` / `unsupported_content_type` | 不是论文，或格式没通过校验 | 多半是数据集或软件记录，可跳过 |

---

## 7. 需要浏览器下载的 PDF

1. 生成清单：用某次运行产生的 `pending_browser_fetch_*.tsv`，或
   `python scripts/zotero_triage_import.py --export-batch checklist.csv`。
2. 在有权限的网络下，用浏览器下载这些 PDF（Zotero Connector 很好用）。
3. 导入：
   - 应用 → **Upload missing PDFs**（按 DOI 和标题匹配），或
   - `python scripts/zotero_triage_import.py ~/Zotero/storage`（试运行），确认后加 `--commit`。
     它会把开放副本送进应用，把受许可副本送进 `paper_bundles/`；
   - Zotero "导出 → 文件" 的文件夹用 `python scripts/match_zotero_export.py <导出文件夹>`。

许可检查能识别常见标记（CC 声明、"All rights reserved"、机构下载水印），但无法认出所有订阅 PDF。
你所在机构不允许发送给云端 AI 的受许可 PDF，请不要上传到应用里。

---

## 8. 礼仪与合规

- 对每个站点的请求都有最小间隔（arXiv 3 秒；出版商 API 0.5–1 秒；其他站点 1 秒）。
- User-Agent 如实标明工具身份；可以通过 `SEISMO_HARVEST_UA` 加上你自己的 `mailto:`。
- 有反爬保护的站点直接跳过，不会反复请求。本仓库没有浏览器自动化、没有伪造指纹、也没有验证码破解。
- TDM 密钥属于你所在机构的许可。只在许可条款范围内使用，TDM 副本只留在本地。
