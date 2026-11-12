"""Tests for POST /api/trackers/preview_config (B1 — save-search-as-tracker).

Covers the search->predicate MAPPING and the HONEST dry-run preview:
  * q -> keyword predicate; the honest count reflects word-boundary matching,
    NOT the looser substring search (the whole reason this endpoint exists).
  * journal facet -> upper-cased abbreviation predicate (round-trips).
  * CJK keyword stays substring (consistent with search).
  * orphan_hits counts matched papers not yet in any project.
  * empty search -> 400.
  * want_semantic with no embedding key degrades gracefully (no dead predicate).

All deterministic: _embed_tracker_description is monkeypatched to None so the
suite never depends on a live embedding key (semantic path = graceful-degrade).
"""
import pytest


@pytest.fixture(autouse=True)
def _no_embedding_key(monkeypatch):
    """Force the semantic embed to be unavailable so tests are hermetic."""
    import main as main_module
    monkeypatch.setattr(main_module, "_embed_tracker_description", lambda _desc: None)


def _preview(client, **body):
    r = client.post("/api/trackers/preview_config", json=body)
    return r


def test_q_maps_to_keyword_and_matches(client, make_paper):
    make_paper("p1", title="Slow slip events in Cascadia", abstract="")
    r = _preview(client, q="slow slip")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["match"] == "all"
    assert data["predicate_types"] == ["keyword"]
    assert data["total_hits"] >= 1
    # config must be directly usable by create_project
    assert data["config"]["predicates"][0]["type"] == "keyword"
    assert data["config"]["predicates"][0]["any_of"] == ["slow slip"]


def test_substring_vs_wordboundary_is_reported_honestly(client, make_paper):
    """"quake" (>3 ASCII chars) is word-boundary in the keyword predicate, so it
    matches "Quake detection" but NOT "Earthquake early warning" — even though a
    substring SEARCH would show both. The preview must report the true (1), not
    the search count (2)."""
    make_paper("word", title="Quake detection with DAS", abstract="")
    make_paper("substr", title="Earthquake early warning", abstract="")
    r = _preview(client, q="quake")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_hits"] == 1, f"expected only the word-boundary hit, got {data['total_hits']}"
    ids = {s["id"] for s in data["sample"]}
    assert "word" in ids and "substr" not in ids


def test_journal_facet_maps_to_abbrev_and_roundtrips(client, make_paper):
    from services.journal_abbrev import abbreviate as journal_abbreviate

    name = "Geophysical Research Letters"
    make_paper("j1", title="A paper", journal=name)
    make_paper("j2", title="Another", journal="Some Other Journal")
    r = _preview(client, journal=name)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["predicate_types"] == ["journal"]
    # predicate carries the UPPER-cased abbreviation the JournalPredicate compares
    assert data["config"]["predicates"][0]["any_of"] == [journal_abbreviate(name).upper()]
    # only the paper in that journal matches
    assert data["total_hits"] == 1
    assert data["sample"][0]["id"] == "j1"


def test_cjk_keyword_is_substring_consistent(client, make_paper):
    make_paper("cjk", title="地震预警系统的研究进展", abstract="")
    r = _preview(client, q="地震")
    assert r.status_code == 200, r.text
    assert r.json()["total_hits"] >= 1


def test_orphan_hits_counts_papers_in_no_project(client, make_paper):
    # two papers both match; put ONE into a collection so it is not an orphan
    make_paper("o1", title="Tremor near the trench", abstract="")
    make_paper("o2", title="Tremor beneath the arc", abstract="")
    cr = client.post("/api/projects", json={"name": "My collection", "kind": "collection"})
    assert cr.status_code == 200, cr.text
    pid = cr.json()["id"]
    add = client.post(f"/api/projects/{pid}/papers/o1")
    assert add.status_code in (200, 201), add.text

    r = _preview(client, q="tremor")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_hits"] == 2
    assert data["orphan_hits"] == 1  # only o2 is still orphaned


def test_empty_search_is_rejected(client):
    r = _preview(client)
    assert r.status_code == 400
    r2 = _preview(client, q="   ", topic="", journal="")
    assert r2.status_code == 400


def test_want_semantic_without_key_degrades_without_dead_predicate(client, make_paper):
    """With no embedding key, want_semantic must NOT leave a query-less semantic
    predicate that (under match='all') would zero out every hit."""
    make_paper("s1", title="Ambient noise tomography", abstract="")
    r = _preview(client, q="tomography", want_semantic=True)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["predicate_types"] == ["keyword"], "semantic must be dropped, not kept dead"
    assert any(s["kind"] == "semantic_unavailable" for s in data["suggestions"])
    assert data["total_hits"] >= 1


def test_journal_typed_in_q_yields_a_suggestion(client, make_paper):
    """If q is an exact known journal title, offer a journal predicate suggestion
    (search scans the journal field; the keyword predicate does not)."""
    from services.journal_registry import all_journals, abbrev_for

    known = next((j["name"] for j in all_journals() if abbrev_for(j["name"])), None)
    if not known:
        pytest.skip("no registry journal with an abbreviation available")
    make_paper("k1", title="unrelated title", abstract="")
    r = _preview(client, q=known)
    assert r.status_code == 200, r.text
    kinds = {s["kind"] for s in r.json()["suggestions"]}
    assert "journal_from_q" in kinds
