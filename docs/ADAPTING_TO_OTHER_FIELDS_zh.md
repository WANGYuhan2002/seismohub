# 把 SeismoHub 迁移到其他学科

[English](ADAPTING_TO_OTHER_FIELDS.md) · **中文**

代码本身不绑定地震学。地震学相关的内容只集中在少数几个数据文件和提示词里，分三层：

1. **抓什么：** 期刊清单、arXiv 分类、预置追踪器。
2. **留什么：** 领域闸门，由关键词表加一个便宜的 LLM 是/否分类器组成。
3. **怎么读：** 主题分类体系，以及分析和问答提示词里的角色设定。

把这三层换成你的学科（海洋学、材料科学、神经科学……），其余功能（追踪、AI 分析、引用锚点、问答、导出、PDF 抓取）
都不用改。

---

## 清单

| # | 文件 | 对象 | 要改什么 |
|---|---|---|---|
| 1 | `services/journal_registry.py` | `JOURNALS`、`CULLED_JOURNAL_NAMES` | 你的期刊：名称、缩写、颜色、ISSN、RSS、层级 |
| 2 | `services/fetch_service.py` | `ARXIV_DEFAULT_CATEGORIES` | 你的 arXiv 分类，例如 `["cond-mat.mtrl-sci"]` |
| 3 | `services/fetch_service.py` | `_DOMAIN_STRONG_CORE`、`_DOMAIN_MEDIUM`、`EXCLUDE_PATTERNS`、`KEYWORDS`、`BROAD_DOMAIN_KEYWORDS` | 关键词闸门用的学科词汇 |
| 4 | `services/fetch_service.py` | `_llm_domain_classify()` 里的提示词 | 什么算你的学科，以及容易混淆的相近领域 |
| 5 | `services/cos_topics.py` | `PRIMARY_TOPICS`、`METHODS`、`DATA_TYPES`、`TARGETS`、`PAPER_ROLES` | 主题分类体系（每篇论文都会被打上的标签） |
| 6 | `services/analyze_service.py` | `build_prompt()`、`TOPIC_ALIASES` | 审稿人角色；把自由标签映射到你的主题 |
| 7 | `services/qa_service.py` | `_SYSTEM_TMPL` | 问答角色（"helping a seismologist ..."） |
| 8 | `seed_presets.json` | `presets` | 新安装时自带的追踪器 |
| 9 | `services/journal_abbrev.py` | 缩写表 | 可选：列表里显示的简称和颜色 |
| 10 | `templates/index.html` | `<title>`、首页标题文字 | 品牌文字 |
| 11 | `tests/` | `test_domain_gate.py`、`test_journal_registry.py`、断言主题名的测试 | 更新写死了地震学期刊和主题的测试预期 |

改的过程中，用一个空的、不联网抓取的实例，避免写入或抓取任何地震学内容：

```bash
SEISMO_NO_PRESET_SEED=1 SEISMO_NO_AUTOFETCH=1 SEISMO_DB_PATH=./data/myfield.db SEISMO_PORT=8000 python main.py
```

---

## 1. 期刊

`JOURNALS` 中的每一项长这样：

```python
{
    "name": "Journal of Physical Oceanography",   # 规范名称，与 OpenAlex / Crossref 返回的一致
    "abbrev": "JPO",                              # 列表中显示，期刊谓词也按它匹配
    "color": "#0369a1",
    "issn": ["0022-3670", "1520-0485"],           # 按 ISSN 从 OpenAlex / Crossref 抓取
    "rss": "https://...",                          # 可选的 RSS 订阅源
    "tier": PURE,                                  # PURE = 专业期刊；BROAD = Nature、Science、巨型期刊
},
```

任何期刊的论文都仍要通过领域闸门（第 3 节）；层级只用于显示和追踪。永远不想要的期刊放进 `CULLED_JOURNAL_NAMES`。

## 2. arXiv

`ARXIV_DEFAULT_CATEGORIES` 决定追踪器没有自己的 `arxiv_category` 谓词时抓取哪些分类。追踪器也可以显式指定分类（见第 6 节）。

## 3. 领域闸门

标题加摘要命中**一个**强核心词（`_DOMAIN_STRONG_CORE`），或命中**两个**不同的中等词（`_DOMAIN_MEDIUM`），并且没有触发
`EXCLUDE_PATTERNS` 否决，论文才会被保留。强核心词必须是你的学科里不会有歧义的词；中等词是其他领域也常见的词。
函数名仍叫 `in_seismic_domain()`，想改名可以改。

用你确定该过、不该过的摘要检验闸门：

```bash
python -c "from services.fetch_service import in_seismic_domain as keep; print(keep('Mesoscale eddies in the Southern Ocean ...'))"
```

第二道闸门是 LLM（`_llm_domain_classify()`），用来剔除关键词放进来的"形似"论文。把它的提示词改成描述你的学科，以及
那些只是用词相同的相近领域。保持输出格式不变（一个把每个键映射到 `true` / `false` 的 JSON 对象）。调试期间可以用
`SEISMO_LLM_DOMAIN_GATE=0` 关掉它。显式关注的来源（期刊、作者、arXiv 分类追踪器）两道闸门都不经过。

## 4. 主题分类体系

`services/cos_topics.py` 存放 Gemini 给每篇论文打的分面标签。把这些列表换成你自己的；`PRIMARY_TOPICS` 是主分面
（每篇最多 `MAX_PRIMARY_TOPICS` 个）。保留一个类似 `"Off-topic / not <你的学科>"` 的末项，让噪声有处可去。
这个模块必须保持零依赖，因为 `db.py` 启动时会加载它。

已有文献库的话，用 `COS_TOPIC_RENAMES` / `PRIMARY_TOPIC_RENAMES` 映射旧标签；需要全部换成新标签时，重新分析论文。

## 5. 提示词

- `services/analyze_service.py` 中 `build_prompt()` 的开头是审稿人角色（"a research assistant for geophysics and
  computational wave physics"）。把它改掉，并重写 `TOPIC_ALIASES`：它负责把常见关键词和旧标签映射到你的 `PRIMARY_TOPICS`。
- `services/qa_service.py` 中的 `_SYSTEM_TMPL` 决定问答角色。
- 分析输出的格式（双语分节、引用锚点结构）与学科无关，不用动。

## 6. 预置追踪器

`seed_presets.json` 会写入新数据库，也会出现在 **Add from template（从模板添加）** 里：

```json
{
  "name": "🌊 Ocean mixing",
  "type": "topic_monitor",
  "config": {
    "match": "any",
    "predicates": [
      {"type": "cos_topic", "any_of": ["Turbulence & mixing"]},
      {"type": "keyword", "any_of": ["internal waves", "diapycnal mixing"]},
      {"type": "journal", "any_of": ["JPO"]},
      {"type": "semantic", "description": "ocean turbulence and mixing", "threshold": 0.55}
    ]
  },
  "sort_order": 1
}
```

谓词类型：`keyword`、`journal`（按第 1 节的缩写匹配）、`author`、`institution`、`cos_topic`（第 4 节的分类体系）、
`arxiv_category`、`semantic`（与一段描述的向量相似度）和 `date_window`。`match` 取 `any` 或 `all`。

## 7. 不需要改的部分

- 语义向量：默认模型是多语言的，与学科无关。
- 元数据查询（OpenAlex、Crossref、Semantic Scholar、Unpaywall）和标识符导入。
- PDF 抓取：OpenAlex、arXiv、Zenodo、PMC、机构仓储和出版商 TDM 接口对任何学科都适用。如果你的学科有自己的预印本
  服务器，在 `services/fullbundle_harvest.py` 里加一个渠道。
- 许可规则、导出、Obsidian 笔记库、监视文件夹。

## 8. 收尾

```bash
DISABLE_TRACKER_SCHEDULER=1 DISABLE_EAGER_ANALYSIS=1 SEISMO_NO_AUTOFETCH=1 python -m pytest -q
```

更新那些断言地震学期刊、主题或闸门判定的测试，然后去掉 `SEISMO_NO_AUTOFETCH` 启动应用，看看第一次抓取进来了哪些论文。
服务日志会报告 LLM 闸门剔除了多少篇。
