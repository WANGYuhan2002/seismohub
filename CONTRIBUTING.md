# Contributing / 贡献指南

Thanks for helping. Issues and pull requests are welcome in English or Chinese.
欢迎提交 issue 和 pull request，中英文均可。

## Set up / 环境

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DISABLE_TRACKER_SCHEDULER=1 DISABLE_EAGER_ANALYSIS=1 SEISMO_NO_AUTOFETCH=1 python -m pytest -q
SEISMO_NO_AUTOFETCH=1 SEISMO_PORT=8000 python -m uvicorn main:app --reload --port 8000
```

The tests are offline: `tests/conftest.py` gives every test an isolated database, and HTTP calls are mocked with
`responses`. Mark tests that need the network with `@pytest.mark.network` and slow ones with `@pytest.mark.slow`.
测试离线运行：`tests/conftest.py` 为每个测试提供独立的数据库，HTTP 调用用 `responses` 模拟。需要联网的测试标记
`@pytest.mark.network`，慢测试标记 `@pytest.mark.slow`。

## Design constraints / 设计约束

- **SQLite, no Alembic.** Schema changes are additive `ALTER TABLE` steps in `db.py`, guarded by
  `PRAGMA table_info` so `init_db()` can run on every start. To add a column, append `(name, ddl)` to the table's
  `_<TABLE>_COLUMNS` list in `db.py`, mirror it in `models.py`, and add a test.
  表结构变更写成 `db.py` 里的增量 `ALTER TABLE`，用 `PRAGMA table_info` 判断，保证 `init_db()` 每次启动都能安全执行。
- **One template.** The whole UI is `templates/index.html` (vanilla JS + Tailwind). Keep it that way.
  整个界面就是 `templates/index.html`，请保持单文件。
- **Bilingual UI.** Every user-visible string needs English and Chinese.
  所有用户可见的文字都要有中英文。
- **Few dependencies.** Explain any new runtime dependency in the PR.
  新增运行时依赖请在 PR 中说明理由。
- **Licence rules are not optional.** Code that fetches, stores or sends full text must keep the tiers in
  `services/fullbundle_harvest.py` and the guards in `services/tdm_qa_guard.py` and
  `services/tdm_guard_and_xml_renderer.py`. No CAPTCHA solving, no bot-protection workarounds, no browser
  automation against publisher sites.
  抓取、保存或发送全文的代码必须保留许可分层和守卫；不接受破解验证码、绕过反爬或针对出版商网站的浏览器自动化。

## Pull requests / 提交 PR

- One topic per PR, with tests for new behaviour.
- Run the test suite before pushing.
- Never commit `.env`, keys, databases, PDFs or harvest reports. `.gitignore` covers the usual paths; check
  `git status` anyway.
- 每个 PR 只做一件事，新行为要带测试；推送前跑一遍测试；不要提交 `.env`、密钥、数据库、PDF 或抓取报告。
