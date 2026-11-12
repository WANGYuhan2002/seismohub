# Adapting SeismoHub to another field

**English** · [中文](ADAPTING_TO_OTHER_FIELDS_zh.md)

The code is not specific to seismology. The seismology lives in a few data files and prompts, in three layers:

1. **What is fetched:** the journal list, the arXiv categories and the preset trackers.
2. **What is kept:** the domain gate, made of keyword lists plus a cheap LLM yes/no classifier.
3. **How papers are read:** the topic taxonomy and the persona in the analysis and Q&A prompts.

Change those for your field (oceanography, materials science, neuroscience, ...) and the rest (tracking, AI
analysis, citation anchors, Q&A, exports, PDF harvesting) works unchanged.

---

## Checklist

| # | File | Object | What to change |
|---|---|---|---|
| 1 | `services/journal_registry.py` | `JOURNALS`, `CULLED_JOURNAL_NAMES` | Your journals: name, abbreviation, colour, ISSNs, RSS feed, tier |
| 2 | `services/fetch_service.py` | `ARXIV_DEFAULT_CATEGORIES` | Your arXiv categories, e.g. `["cond-mat.mtrl-sci"]` |
| 3 | `services/fetch_service.py` | `_DOMAIN_STRONG_CORE`, `_DOMAIN_MEDIUM`, `EXCLUDE_PATTERNS`, `KEYWORDS`, `BROAD_DOMAIN_KEYWORDS` | Your field's vocabulary for the keyword gate |
| 4 | `services/fetch_service.py` | the prompt in `_llm_domain_classify()` | What counts as your field, and its look-alikes |
| 5 | `services/cos_topics.py` | `PRIMARY_TOPICS`, `METHODS`, `DATA_TYPES`, `TARGETS`, `PAPER_ROLES` | Your topic taxonomy (the facets every paper is tagged with) |
| 6 | `services/analyze_service.py` | `build_prompt()`, `TOPIC_ALIASES` | Reviewer persona; mapping of free-form labels to your topics |
| 7 | `services/qa_service.py` | `_SYSTEM_TMPL` | Q&A persona ("helping a seismologist ...") |
| 8 | `seed_presets.json` | `presets` | The trackers a new install starts with |
| 9 | `services/journal_abbrev.py` | abbreviation table | Optional: short names and colours in the list view |
| 10 | `templates/index.html` | `<title>`, hero text | Branding |
| 11 | `tests/` | `test_domain_gate.py`, `test_journal_registry.py`, topic-name assertions | Update the expectations that name seismology journals and topics |

While you work, start an empty, offline instance so nothing seismology-specific is seeded or fetched:

```bash
SEISMO_NO_PRESET_SEED=1 SEISMO_NO_AUTOFETCH=1 SEISMO_DB_PATH=./data/myfield.db SEISMO_PORT=8000 python main.py
```

---

## 1. Journals

Each entry in `JOURNALS` looks like this:

```python
{
    "name": "Journal of Physical Oceanography",   # canonical name, as OpenAlex / Crossref report it
    "abbrev": "JPO",                              # shown in the list and used by journal predicates
    "color": "#0369a1",
    "issn": ["0022-3670", "1520-0485"],           # used to fetch by ISSN from OpenAlex / Crossref
    "rss": "https://...",                          # optional RSS feed
    "tier": PURE,                                  # PURE = dedicated journal, BROAD = Nature, Science, mega-journals
},
```

Every paper, from any journal, still has to pass the domain gate (section 3); the tier is used for display and
tracking. Put venues you never want in `CULLED_JOURNAL_NAMES`.

## 2. arXiv

`ARXIV_DEFAULT_CATEGORIES` sets the categories fetched when a tracker has no `arxiv_category` predicate of its own.
Trackers can also name categories explicitly (see section 6).

## 3. Domain gate

A paper is kept when its title + abstract matches **one** strong-core term (`_DOMAIN_STRONG_CORE`) or **two**
different medium terms (`_DOMAIN_MEDIUM`), and no `EXCLUDE_PATTERNS` veto. Strong-core terms must be unambiguous
for your field; medium terms are words that are common elsewhere too. The function is still called
`in_seismic_domain()`; rename it if you like.

Check the gate with abstracts you know should pass and fail:

```bash
python -c "from services.fetch_service import in_seismic_domain as keep; print(keep('Mesoscale eddies in the Southern Ocean ...'))"
```

A second, LLM-based gate (`_llm_domain_classify()`) removes look-alikes the keywords let through. Rewrite its
prompt to describe your field and the topics that merely share words with it. Keep the output contract (a JSON
object mapping each key to `true` / `false`). Turn it off while tuning with `SEISMO_LLM_DOMAIN_GATE=0`.
Explicitly followed sources (journal, author and arXiv-category trackers) bypass both gates.

## 4. Topic taxonomy

`services/cos_topics.py` holds the facets Gemini assigns to every paper. Replace the lists with your own;
`PRIMARY_TOPICS` is the main one (up to `MAX_PRIMARY_TOPICS` per paper). Keep a last entry like
`"Off-topic / not <your field>"` so noise has somewhere to go. The module must stay import-free, because `db.py`
loads it at start-up.

On an existing library, map old labels with `COS_TOPIC_RENAMES` / `PRIMARY_TOPIC_RENAMES` and re-analyse papers
if you need the new labels everywhere.

## 5. Prompts

- `build_prompt()` in `services/analyze_service.py` opens with the reviewer persona ("a research assistant for
  geophysics and computational wave physics"). Change it, and rewrite `TOPIC_ALIASES`, which maps common keywords
  and old labels onto your `PRIMARY_TOPICS`.
- `_SYSTEM_TMPL` in `services/qa_service.py` sets the Q&A persona.
- The analysis output format (bilingual sections, citation envelope) is field-neutral; leave it alone.

## 6. Preset trackers

`seed_presets.json` is loaded into a new database and offered under **Add from template**:

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

Predicate types: `keyword`, `journal` (matches the abbreviations from section 1), `author`, `institution`,
`cos_topic` (your taxonomy from section 4), `arxiv_category`, `semantic` (embedding similarity to a description)
and `date_window`. `match` is `any` or `all`.

## 7. What does not need changes

- Embeddings: the default model is multilingual and field-neutral.
- Metadata lookups (OpenAlex, Crossref, Semantic Scholar, Unpaywall) and identifier import.
- PDF harvesting: OpenAlex, arXiv, Zenodo, PMC, repositories and the publisher TDM APIs work for any field.
  If your field has its own preprint server, add a channel in `services/fullbundle_harvest.py`.
- Licence rules, exports, Obsidian vault, watch folders.

## 8. Finish

```bash
DISABLE_TRACKER_SCHEDULER=1 DISABLE_EAGER_ANALYSIS=1 SEISMO_NO_AUTOFETCH=1 python -m pytest -q
```

Update the tests that assert seismology journals, topics or gate decisions, then start the app without
`SEISMO_NO_AUTOFETCH` and check what the first fetch brings in. The server log reports how many papers the LLM gate
dropped.
