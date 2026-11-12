# Changelog / 更新日志

## 2.0.0 — first open-source release / 首个开源版本

- Core app: FastAPI + SQLite + single-page UI; trackers with 11 seismology presets; Gemini analysis with
  paragraph-anchored citations; per-paper Q&A; imports (identifier, upload, watch folder, Zotero) and exports
  (BibTeX, RIS, CSL-JSON, PDF zip, Obsidian vault).
- Full-text harvester (`scripts/harvest_pdfs.py`, `scripts/harvest_literature_list.py`) with licence tiers and
  an agent skill (`.claude/skills/seismohub-pdf-harvest`).
- The detail panel says why an analysis did not use the PDF (attached later, licence-restricted, ...), and
  `scripts/reanalyze_stale_pdf.py` redoes analyses written before the PDF arrived (preview by default).
- Institution-neutral licence guards: TDM-licensed copies and PDFs with institutional download stamps stay local.
- Docker image runs as a non-root user; Compose publishes on `127.0.0.1` only.
- `.env.example` turns off proactive background analysis by default (`DISABLE_EAGER_ANALYSIS=1`).
- The retired browser-based crawler is not included.
- Docs: README screenshots and demo GIF; a guide for adapting SeismoHub to other fields.

- 核心应用：FastAPI + SQLite + 单页界面；带 11 个地震学预置的追踪器；带段落锚点引用的 Gemini 分析；单篇问答；
  导入（标识符、上传、监视文件夹、Zotero）与导出（BibTeX、RIS、CSL-JSON、PDF 压缩包、Obsidian 笔记库）。
- 全文抓取器与许可分层，以及配套的 Agent 技能。
- 详情面板会说明分析为什么没用上 PDF（PDF 后来才挂上、受许可限制等）；`scripts/reanalyze_stale_pdf.py`
  可重做写于 PDF 挂上之前的分析（默认只预览）。
- 与机构无关的许可守卫：TDM 许可副本和带机构下载水印的 PDF 只留在本地。
- Docker 镜像以非 root 用户运行；Compose 只发布在 `127.0.0.1`。
- `.env.example` 默认关闭后台主动分析（`DISABLE_EAGER_ANALYSIS=1`）。
- 已退役的浏览器爬虫不包含在内。
- 文档：README 截图和演示 GIF；把 SeismoHub 迁移到其他学科的指南。
