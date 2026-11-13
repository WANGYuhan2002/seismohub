"""Contract tests for the "Journal IF" sort (``sort_by=impact_factor``).

The order is SeismoHub Lite's impact-factor sort: journals with an impact factor first (highest first), then
the other journals ("Others" last), then preprints and open platforms. Each journal stays together, whichever
spelling a paper stores, and its papers run newest first.
"""

from __future__ import annotations

import ast
import os
import re
import uuid
from pathlib import Path
from typing import Optional

import pytest

# The API test starts the app; keep its background fetch scheduler off (same guard as test_pdf_match_core.py).
os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")

from services import journal_impact as ji
from services.journal_impact import (
    GROUP_IF,
    GROUP_OTHER,
    GROUP_PREPRINT,
    impact_factor_rank,
    impact_factor_sort_key,
    lookup,
)

INDEX_HTML = Path(__file__).resolve().parents[1] / "templates" / "index.html"
TOOLTIP = "Journal impact factor, high to low; preprints and open platforms last · 期刊影响因子从高到低；预印本与开放平台排在最后"


def _paper(pid: str, journal: Optional[str], date: str = "2024-01-01") -> dict:
    return {"id": pid, "journal": journal, "published_date": date, "authors": [], "title": pid}


def _ids(papers) -> list:
    return [p["id"] for p in papers]


# --------------------------------------------------------------------------------------------- groups and names
def test_groups_follow_lite() -> None:
    assert impact_factor_rank("Nature")[0] == GROUP_IF
    assert impact_factor_rank("Seismica")[0] == GROUP_OTHER  # listed, without an impact factor
    assert impact_factor_rank("Marine Pollution Bulletin")[0] == GROUP_OTHER  # not listed
    for venue in (
        "arXiv",
        "arXiv (Cornell University)",
        "ESS Open Archive",
        "EarthArXiv",
        "Zenodo (CERN European Organization for Nuclear Research)",
        "EGU General Assembly",
        "ETH Zurich (Doctoral Thesis)",
        "Texas Advanced Computing Center",
        "Earth System Science Data Discussions",
        "International Snow Science Workshop (ISSW)",
    ):
        assert impact_factor_rank(venue)[0] == GROUP_PREPRINT, venue


def test_spellings_of_one_journal_share_one_rank() -> None:
    pairs = [
        ("GJI", "Geophysical Journal International"),
        ("GRL", "Geophysical Research Letters"),
        ("EPSL", "Earth and Planetary Science Letters"),
        ("BSSA", "Bulletin of the Seismological Society of America"),
        ("JGR: Solid Earth", "Journal of Geophysical Research: Solid Earth"),
        ("JGR: Solid Earth", "Journal of Geophysical Research Solid Earth"),
        ("JGR: Planets", "Journal of Geophysical Research Planets"),
        ("JGR: Earth Surface", "Journal of Geophysical Research Earth Surface"),
        ("JGR: Machine Learning and Computation", "Journal of Geophysical Research: Machine Learning and Computation"),
        ("G3", "Geochemistry, Geophysics, Geosystems"),
        ("Geophysics (SEG)", "Geophysics"),
        ("arXiv", "arXiv (Cornell University)"),
        ("Zenodo", "Zenodo (CERN European Organization for Nuclear Research)"),
    ]
    for a, b in pairs:
        assert impact_factor_rank(a) == impact_factor_rank(b), (a, b)


def test_every_lite_alias_resolves_like_its_target() -> None:
    for raw, target in ji.LITE_ALIASES.items():
        assert lookup(target) is not None, target
        assert impact_factor_rank(raw) == impact_factor_rank(target), raw


def test_name_variants_normalise() -> None:
    assert lookup("Natural hazards and earth system sciences") == ("NHESS", 4.6)
    assert lookup("INTERNATIONAL JOURNAL OF PHYSICAL MODELLING IN GEOTECHNICS") == ("IJPMG", 1.4)
    assert lookup("Netherlands Journal of Geosciences – Geologie en Mijnbouw") == ("Neth J Geosci", 1.4)
    assert lookup("Netherlands Journal of Geosciences - Geologie en Mijnbouw") == ("Neth J Geosci", 1.4)
    assert lookup("Nature Reviews Earth and Environment") == ("Nat Rev EE", 49.7)  # "and" for "&"
    assert lookup("Leading Edge") == ("TLE", 1.1)  # no leading "The"
    assert lookup("Journal of Hydrology Regional Studies") is None  # whole names only, no prefix match
    assert lookup("") is None and lookup(None) is None


def test_others_and_missing_journal_close_the_other_journals() -> None:
    others = impact_factor_rank("Others")
    assert others == impact_factor_rank("") == impact_factor_rank(None) == impact_factor_rank("   ")
    assert impact_factor_rank("Seismica") < others
    assert impact_factor_rank("Zeitschrift für Gerontologie und Geriatrie") < others
    assert others < impact_factor_rank("arXiv")


def test_impact_factor_runs_high_to_low() -> None:
    names = ["Geophysical Journal International", "Nature", "Seismological Research Letters", "Science", "JGR: Solid Earth"]
    assert sorted(names, key=impact_factor_rank) == [
        "Nature",
        "Science",
        "JGR: Solid Earth",
        "Seismological Research Letters",
        "Geophysical Journal International",
    ]


# --------------------------------------------------------------------------------------------- the sort key
def test_journals_stay_together_and_run_newest_first() -> None:
    papers = [
        _paper("gji-old", "GJI", "2020-05-01"),
        _paper("eps", "Earth Planets and Space", "2023-01-01"),  # same impact factor as GJI
        _paper("gji-new", "Geophysical Journal International", "2024-06-01"),
        _paper("gji-mid", "GJI", "2022-03-01"),
        _paper("arxiv", "arXiv", "2025-01-01"),
        _paper("nature", "Nature", "2019-01-01"),
        _paper("others", "Others", "2025-02-01"),
        _paper("seismica", "Seismica", "2025-03-01"),
    ]
    assert _ids(sorted(papers, key=impact_factor_sort_key)) == [
        "nature", "eps", "gji-new", "gji-mid", "gji-old", "seismica", "others", "arxiv",
    ]


def test_same_journal_and_date_falls_back_to_id() -> None:
    papers = [_paper("b", "GJI"), _paper("a", "Geophysical Journal International")]
    assert _ids(sorted(papers, key=impact_factor_sort_key)) == ["a", "b"]


def test_dates_run_newest_first_like_lite() -> None:
    # Lite: a year-only date goes after the full dates of that year; no date goes last.
    papers = [
        _paper("year", "GJI", "2026"),
        _paper("undated", "GJI", ""),
        _paper("month", "GJI", "2026-11"),
        _paper("old", "GJI", "2025-12-31"),
        _paper("day", "GJI", "2026-11-12"),
    ]
    assert _ids(sorted(papers, key=impact_factor_sort_key)) == ["day", "month", "year", "old", "undated"]


def test_impact_factor_is_a_whitelisted_sort_key() -> None:
    from main import SORT_KEYS, _normalize_sort_by, _sort_papers

    assert "impact_factor" in SORT_KEYS
    assert _normalize_sort_by("impact_factor") == "impact_factor"
    assert _normalize_sort_by("  Impact_Factor ") == "impact_factor"
    papers = [_paper("x", "arXiv"), _paper("y", "Nature"), _paper("z", "GRL"), _paper("w", None)]
    assert _ids(_sort_papers(papers, "impact_factor")) == ["y", "z", "w", "x"]


def _collection_with(paper_ids) -> str:
    """A collection (default order: custom) holding *paper_ids*; Browse only lists papers that are in a project."""
    import db as db_module
    from models import Project, ProjectPaper

    session = db_module.SessionLocal()
    try:
        project = Project(id=str(uuid.uuid4()), name="IF sort", type="static", kind="collection",
                          scope=None, cadence=None, default_sort_by="custom", config={})
        session.add(project)
        session.flush()
        for pid in paper_ids:
            session.add(ProjectPaper(project_id=project.id, paper_id=pid, folder="", source="manual"))
        session.commit()
        return project.id
    finally:
        session.close()


def test_api_papers_sorts_by_impact_factor(client, make_paper) -> None:
    make_paper("p-arxiv", journal="arXiv", published_date="2025-01-01")
    make_paper("p-gji-1", journal="GJI", published_date="2023-01-01")
    make_paper("p-gji-2", journal="Geophysical Journal International", published_date="2024-01-01")
    make_paper("p-nature", journal="Nature", published_date="2020-01-01")
    make_paper("p-seismica", journal="Seismica", published_date="2025-02-01")
    project_id = _collection_with(["p-arxiv", "p-gji-1", "p-gji-2", "p-nature", "p-seismica"])
    expected = ["p-nature", "p-gji-2", "p-gji-1", "p-seismica", "p-arxiv"]

    # Browse (all papers) and inside the collection, where the explicit sort beats its custom default.
    for params in ({"sort_by": "impact_factor"}, {"sort_by": "impact_factor", "project_id": project_id}):
        response = client.get("/api/papers", params=params)
        assert response.status_code == 200
        body = response.json()
        assert body["sort_by"] == "impact_factor"
        assert [p["id"] for p in body["papers"]] == expected, params
        # Like Lite, the impact factor only orders the list; it is never sent to the page.
        assert not any("impact" in key for p in body["papers"] for key in p)


# --------------------------------------------------------------------------------------------- the page
def test_sort_dropdown_offers_journal_if_with_bilingual_tooltip() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    start = source.index('<select id="sortFilter"')
    select = source[start:source.index("</select>", start)]
    assert f'<option value="impact_factor" title="{TOOLTIP}">Journal IF</option>' in select


def test_dropdown_change_resets_column_reverse() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    start = source.index('sortFilter.addEventListener("change"')
    handler = source[start:source.index("});", start)]
    assert "state.filters.sortReverse = false;" in handler
    assert handler.index("state.filters.sortReverse = false;") < handler.index("await loadPapers();")


# --------------------------------------------------------------------------------------------- drift guard
def _lite_root() -> Optional[Path]:
    """A SeismoHub Lite checkout: $SEISMOHUB_LITE_DIR, else a sibling ``seismohub-lite`` folder."""
    env = os.environ.get("SEISMOHUB_LITE_DIR")
    candidates = [Path(env)] if env else [parent / "seismohub-lite" for parent in Path(__file__).resolve().parents]
    for root in candidates:
        if (root / "docs" / "index.html").is_file() and (root / "tools" / "export_lite.py").is_file():
            return root
    return None


@pytest.mark.skipif(_lite_root() is None, reason="no SeismoHub Lite checkout found (set SEISMOHUB_LITE_DIR)")
def test_tables_match_seismohub_lite() -> None:
    root = _lite_root()
    html = (root / "docs" / "index.html").read_text(encoding="utf-8")
    block = re.search(r"const JOURNALS = \{\n(.*?)\n\};", html, re.S).group(1)
    entries = re.findall(r"'([^']+)':\s*\['([^']+)',\s*([\d.]+)\]", block)
    assert len(entries) == block.count("': ["), "unparsed entry in Lite's JOURNALS"
    lite = {name: (short, float(jif)) for name, short, jif in entries}
    message = "SeismoHub Lite's tables changed: copy them into services/journal_impact.py"
    assert lite == ji.JOURNALS, message
    assert re.search(r"const MISC_VENUE = /(.*?)/i;", html).group(1) == ji.MISC_VENUE.pattern, message
    tree = ast.parse((root / "tools" / "export_lite.py").read_text(encoding="utf-8"))
    aliases = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "JOURNAL_ALIASES"
    )
    assert aliases == ji.LITE_ALIASES, message
