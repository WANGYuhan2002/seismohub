"""Predicate-based tracker model unit tests (BRIEF v2 §1, §2, §3).

Covers, with NO DB / NO network (semantic vectors are constructed in-process
and encoded with the embedding service's own JSON codec):

* Each predicate type's ``eval(paper) -> (hit, score)``:
  keyword, journal, author, cos_topic, institution, date_window, semantic.
* The across-predicate combiner via :func:`match_tracker`:
  OR-within a predicate's ``any_of``; AND-across with ``match="all"``;
  OR-across with ``match="any"``.
* SOURCE vs MATCH (`is_source_only_config`): journal-only / author-only
  configs are explicit follows; a config that also filters is not.
* Tiered eval: the built predicate list orders cheap predicates before the
  expensive semantic one, and ``match="any"`` short-circuits on a cheap hit.
* LEGACY ``{topics, keywords, journals}`` config (and its ``config.query.*``
  mirror) still translates into predicates and matches.

Papers are represented as lightweight ``SimpleNamespace`` objects (the
predicate field readers accept either an ORM Paper or a plain object/dict),
so these tests are fast and dependency-light.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from services import predicates as P
from services.embedding_service import encode_vector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_paper(**kwargs):
    """A minimal paper object the predicate field readers understand."""
    defaults = dict(
        title="",
        abstract="",
        authors=[],
        institutions=[],
        topics=[],
        journal="",
        published_date="",
        embedding="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _unit(vec) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    return arr / (np.linalg.norm(arr) + 1e-12)


# ---------------------------------------------------------------------------
# KeywordPredicate
# ---------------------------------------------------------------------------


def test_keyword_predicate_hits_on_phrase_in_title():
    pred = P.KeywordPredicate(["slow slip"])
    paper = make_paper(title="Detecting slow slip events with arrays")
    hit, score = pred.eval(paper)
    assert hit is True
    assert score == 1.0


def test_keyword_predicate_word_boundary_avoids_substring_false_positive():
    """A >3-char keyword is word-boundary aware: 'ice' must not fire inside
    'police' (legacy _keyword_hits semantics)."""
    pred = P.KeywordPredicate(["police"])
    # 'police' present -> hit
    assert pred.eval(make_paper(title="police report"))[0] is True
    # the short-token path: a <=3 char term is also boundary-checked.
    short = P.KeywordPredicate(["das"])
    assert short.eval(make_paper(title="A DAS deployment"))[0] is True
    assert short.eval(make_paper(title="ideas about nothing"))[0] is False


def test_keyword_predicate_or_within_any_of():
    pred = P.KeywordPredicate(["nonexistent term", "ambient noise"])
    paper = make_paper(abstract="We use ambient noise tomography.")
    assert pred.eval(paper)[0] is True


def test_keyword_predicate_searches_abstract_and_authors():
    pred_auth = P.KeywordPredicate(["Fichtner"])
    assert pred_auth.eval(make_paper(authors=["Andreas Fichtner"]))[0] is True


def test_keyword_predicate_cjk_substring():
    pred = P.KeywordPredicate(["地震"])
    assert pred.eval(make_paper(title="地震波形反演研究"))[0] is True
    assert pred.eval(make_paper(title="ambient noise"))[0] is False


def test_keyword_predicate_empty_terms_never_hits():
    assert P.KeywordPredicate([]).eval(make_paper(title="anything"))[0] is False


# ---------------------------------------------------------------------------
# JournalPredicate  (SOURCE)
# ---------------------------------------------------------------------------


def test_journal_predicate_matches_by_abbreviation():
    pred = P.JournalPredicate(["GRL"])
    paper = make_paper(journal="Geophysical Research Letters")
    hit, score = pred.eval(paper)
    assert hit is True
    assert score == 1.0


def test_journal_predicate_matches_when_paper_stores_abbrev():
    pred = P.JournalPredicate(["Nature"])
    assert pred.eval(make_paper(journal="Nature"))[0] is True


def test_journal_predicate_is_a_source_type():
    pred = P.JournalPredicate(["GRL"])
    assert pred.is_source is True
    assert pred.type == "journal"


def test_journal_predicate_no_match_for_other_journal():
    pred = P.JournalPredicate(["GRL"])
    assert pred.eval(make_paper(journal="Seismica"))[0] is False


# ---------------------------------------------------------------------------
# AuthorPredicate  (SOURCE)
# ---------------------------------------------------------------------------


def test_author_predicate_substring_match():
    pred = P.AuthorPredicate(["Fichtner"])
    assert pred.eval(make_paper(authors=["Andreas Fichtner", "J. Doe"]))[0] is True


def test_author_predicate_case_insensitive():
    pred = P.AuthorPredicate(["beroza"])
    assert pred.eval(make_paper(authors=["Gregory C. Beroza"]))[0] is True


def test_author_predicate_is_source_and_no_match():
    pred = P.AuthorPredicate(["Nobody"])
    assert pred.is_source is True
    assert pred.eval(make_paper(authors=["Andreas Fichtner"]))[0] is False


# ---------------------------------------------------------------------------
# CosTopicPredicate  (filter)
# ---------------------------------------------------------------------------


def test_cos_topic_predicate_exact_and_substring_overlap():
    pred = P.CosTopicPredicate(["Fibre-optic seismology"])
    # exact
    assert pred.eval(make_paper(topics=["Fibre-optic seismology"]))[0] is True
    # bidirectional substring (AI label drift)
    assert pred.eval(make_paper(topics=["Fibre-optic seismology / DAS"]))[0] is True


def test_cos_topic_predicate_prefers_matched_topic_names_stash():
    """When the matcher stashes ``_matched_topic_names`` on the paper, the
    predicate uses those over the paper's stored topics."""
    paper = make_paper(topics=["Something Else"])
    paper._matched_topic_names = ["Inverse theory"]
    pred = P.CosTopicPredicate(["Inverse theory"])
    assert pred.eval(paper)[0] is True


def test_cos_topic_predicate_no_overlap():
    pred = P.CosTopicPredicate(["DAS"])
    assert pred.eval(make_paper(topics=["Ambient Noise"]))[0] is False


def test_cos_topic_predicate_is_not_a_source():
    assert P.CosTopicPredicate(["DAS"]).is_source is False


# ---------------------------------------------------------------------------
# InstitutionPredicate  (filter)
# ---------------------------------------------------------------------------


def test_institution_predicate_substring():
    pred = P.InstitutionPredicate(["ETH"])
    assert pred.eval(make_paper(institutions=["ETH Zurich"]))[0] is True


def test_institution_predicate_no_match():
    pred = P.InstitutionPredicate(["Caltech"])
    assert pred.eval(make_paper(institutions=["ETH Zurich"]))[0] is False


# ---------------------------------------------------------------------------
# DateWindowPredicate  (filter)
# ---------------------------------------------------------------------------


def test_date_window_inclusive_bounds():
    pred = P.DateWindowPredicate(after="2024-01-01", before="2024-12-31")
    assert pred.eval(make_paper(published_date="2024-06-15"))[0] is True
    assert pred.eval(make_paper(published_date="2024-01-01"))[0] is True  # inclusive
    assert pred.eval(make_paper(published_date="2024-12-31"))[0] is True  # inclusive


def test_date_window_excludes_outside_and_undated():
    pred = P.DateWindowPredicate(after="2024-01-01", before="2024-12-31")
    assert pred.eval(make_paper(published_date="2023-12-31"))[0] is False
    assert pred.eval(make_paper(published_date="2025-01-01"))[0] is False
    # undated -> conservative non-match
    assert pred.eval(make_paper(published_date=""))[0] is False


def test_date_window_open_ended_after_only():
    pred = P.DateWindowPredicate(after="2024-01-01", before=None)
    assert pred.eval(make_paper(published_date="2030-01-01"))[0] is True
    assert pred.eval(make_paper(published_date="2020-01-01"))[0] is False


def test_date_window_empty_bounds_never_admits():
    pred = P.DateWindowPredicate(after=None, before=None)
    assert pred.eval(make_paper(published_date="2024-06-15"))[0] is False


# ---------------------------------------------------------------------------
# SemanticPredicate  (expensive; cosine)
# ---------------------------------------------------------------------------


def test_semantic_predicate_hits_above_threshold():
    """A paper embedding nearly parallel to the query embedding scores ~1.0 and
    matches; an orthogonal one scores ~0.0 and does not."""
    query = [1.0, 0.0, 0.0, 0.0]
    pred = P.SemanticPredicate(query_embedding=query, threshold=0.62)

    parallel = make_paper(embedding=encode_vector([0.9, 0.1, 0.0, 0.0]))
    hit, score = pred.eval(parallel)
    assert hit is True
    assert score > 0.62

    orthogonal = make_paper(embedding=encode_vector([0.0, 1.0, 0.0, 0.0]))
    hit2, score2 = pred.eval(orthogonal)
    assert hit2 is False
    assert score2 < 0.62


def test_semantic_predicate_missing_embedding_scores_zero():
    pred = P.SemanticPredicate(query_embedding=[1.0, 0.0, 0.0], threshold=0.5)
    # No paper embedding -> (False, 0.0), never raises.
    assert pred.eval(make_paper(embedding=""))[0] is False
    # No query embedding -> always (False, 0.0).
    pred_noq = P.SemanticPredicate(query_embedding=None, threshold=0.5)
    assert pred_noq.eval(make_paper(embedding=encode_vector([1.0, 0.0])))[0] is False


def test_semantic_predicate_corrupt_embedding_is_swallowed():
    pred = P.SemanticPredicate(query_embedding=[1.0, 0.0], threshold=0.5)
    # garbage in the embedding cell must not raise into the matcher.
    hit, score = pred.eval(make_paper(embedding="not-json"))
    assert hit is False
    assert score == 0.0


def test_semantic_predicate_threshold_default_and_coercion():
    pred = P.SemanticPredicate(query_embedding=[1.0, 0.0])
    assert pred.threshold == P.DEFAULT_SEMANTIC_THRESHOLD
    # Bad threshold -> falls back to default, does not raise.
    pred_bad = P.SemanticPredicate(query_embedding=[1.0, 0.0], threshold="oops")
    assert pred_bad.threshold == P.DEFAULT_SEMANTIC_THRESHOLD


def test_semantic_predicate_is_the_expensive_one():
    pred = P.SemanticPredicate(query_embedding=[1.0])
    assert pred.is_cheap is False
    assert pred.is_source is False


# ---------------------------------------------------------------------------
# build_predicates: dispatch + tiering
# ---------------------------------------------------------------------------


def test_build_predicates_dispatches_each_type():
    config = {
        "match": "any",
        "predicates": [
            {"type": "keyword", "any_of": ["DAS"]},
            {"type": "journal", "any_of": ["GRL"]},
            {"type": "author", "any_of": ["Fichtner"]},
            {"type": "cos_topic", "any_of": ["Inverse theory"]},
            {"type": "institution", "any_of": ["ETH"]},
            {"type": "date_window", "after": "2024-01-01"},
            {"type": "semantic", "description": "slow slip", "query_embedding": [1.0, 0.0]},
        ],
    }
    preds = P.build_predicates(config)
    types = [p.type for p in preds]
    assert set(types) == {
        "keyword", "journal", "author", "cos_topic", "institution",
        "date_window", "semantic",
    }


def test_build_predicates_tiers_semantic_last():
    """The expensive semantic predicate must sort after every cheap one."""
    config = {
        "match": "all",
        "predicates": [
            {"type": "semantic", "query_embedding": [1.0, 0.0]},
            {"type": "keyword", "any_of": ["DAS"]},
            {"type": "journal", "any_of": ["GRL"]},
        ],
    }
    preds = P.build_predicates(config)
    assert preds[-1].type == "semantic", "semantic must be evaluated last"
    assert all(p.is_cheap for p in preds[:-1])


def test_build_predicates_skips_unknown_type():
    """An unknown predicate type is dropped (forward-compat) not fatal."""
    config = {"match": "any", "predicates": [{"type": "wormhole", "any_of": ["x"]}]}
    assert P.build_predicates(config) == []


def test_build_predicates_tolerates_malformed_single_predicate():
    config = {
        "match": "any",
        "predicates": [
            {"type": "keyword", "any_of": ["DAS"]},
            "not-a-dict",  # ignored by normalize_config
        ],
    }
    preds = P.build_predicates(config)
    assert [p.type for p in preds] == ["keyword"]


# ---------------------------------------------------------------------------
# match_tracker: combiner semantics (OR within / AND across / OR across)
# ---------------------------------------------------------------------------


def test_match_any_returns_on_first_cheap_hit_without_semantic():
    """match='any': a journal hit short-circuits before the semantic cosine,
    so a missing/garbage paper embedding does not matter."""
    config = {
        "match": "any",
        "predicates": [
            {"type": "journal", "any_of": ["GRL"]},
            {"type": "semantic", "query_embedding": [1.0, 0.0], "threshold": 0.99},
        ],
    }
    paper = make_paper(journal="Geophysical Research Letters", embedding="")
    belongs, score = P.match_tracker(paper, {"config": config})
    assert belongs is True
    assert score == 1.0  # the cheap deterministic hit


def test_match_all_requires_every_predicate():
    config = {
        "match": "all",
        "predicates": [
            {"type": "journal", "any_of": ["GRL"]},
            {"type": "keyword", "any_of": ["slow slip"]},
        ],
    }
    # journal hits, keyword misses -> overall miss under AND.
    miss = make_paper(journal="Geophysical Research Letters", title="ambient noise")
    assert P.match_tracker(miss, {"config": config})[0] is False

    # both hit -> match.
    hit = make_paper(
        journal="Geophysical Research Letters",
        title="slow slip on the megathrust",
    )
    assert P.match_tracker(hit, {"config": config})[0] is True


def test_match_any_or_across_predicates():
    config = {
        "match": "any",
        "predicates": [
            {"type": "journal", "any_of": ["Nature"]},      # miss
            {"type": "keyword", "any_of": ["interferometry"]},  # hit
        ],
    }
    paper = make_paper(journal="Seismica", abstract="seismic interferometry study")
    assert P.match_tracker(paper, {"config": config})[0] is True


def test_match_empty_config_never_matches():
    assert P.match_tracker(make_paper(title="anything"), {"config": {}})[0] is False
    assert P.match_tracker(make_paper(title="anything"), {})[0] is False


def test_match_tracker_score_is_semantic_cosine_when_semantic_only():
    """A semantic-only tracker returns the cosine as its match score (the
    binding hit). Cross-checks the brief's "best score, esp. semantic cosine"
    requirement that drives ProjectPaper.match_score / tracker sort."""
    qvec = [1.0, 0.0, 0.0]
    config = {
        "match": "any",
        "predicates": [
            {"type": "semantic", "query_embedding": qvec, "threshold": 0.5},
        ],
    }
    paper = make_paper(embedding=encode_vector([0.8, 0.2, 0.0]))
    belongs, score = P.match_tracker(paper, {"config": config})
    assert belongs is True
    # cosine of [0.8,0.2,0] with [1,0,0]
    expected = float(np.dot(_unit([0.8, 0.2, 0.0]), _unit([1.0, 0.0, 0.0])))
    assert math.isclose(score, expected, rel_tol=1e-4)


def test_match_tracker_score_is_best_hit_score_across_predicates():
    """``score`` is the MAX score among predicates that hit. A deterministic
    keyword hit (1.0) therefore dominates a lower semantic cosine under
    match='all' (both must hit, best score wins)."""
    config = {
        "match": "all",
        "predicates": [
            {"type": "keyword", "any_of": ["DAS"]},
            {"type": "semantic", "query_embedding": [1.0, 0.0, 0.0], "threshold": 0.5},
        ],
    }
    paper = make_paper(
        title="A DAS experiment",
        embedding=encode_vector([0.8, 0.2, 0.0]),  # cosine ~0.97 < 1.0
    )
    belongs, score = P.match_tracker(paper, {"config": config})
    assert belongs is True
    assert score == 1.0  # the deterministic keyword hit is the best score


def test_match_tracker_accepts_orm_like_project_object():
    """match_tracker reads .config off a project-like object too."""
    config = {"match": "any", "predicates": [{"type": "keyword", "any_of": ["DAS"]}]}
    project = SimpleNamespace(config=config)
    assert P.match_tracker(make_paper(title="DAS array"), project)[0] is True


# ---------------------------------------------------------------------------
# is_source_only_config  (explicit follow detection)
# ---------------------------------------------------------------------------


def test_journal_only_config_is_source_only():
    config = {"match": "any", "predicates": [{"type": "journal", "any_of": ["GRL"]}]}
    assert P.is_source_only_config(config) is True


def test_author_only_config_is_source_only():
    config = {"match": "any", "predicates": [{"type": "author", "any_of": ["Fichtner"]}]}
    assert P.is_source_only_config(config) is True


def test_mixed_config_is_not_source_only():
    config = {
        "match": "any",
        "predicates": [
            {"type": "journal", "any_of": ["GRL"]},
            {"type": "keyword", "any_of": ["DAS"]},  # a FILTER -> not pure follow
        ],
    }
    assert P.is_source_only_config(config) is False


def test_empty_config_is_not_source_only():
    assert P.is_source_only_config({}) is False
    assert P.is_source_only_config({"match": "any", "predicates": []}) is False


def test_source_predicate_types_constant():
    assert P.SOURCE_PREDICATE_TYPES == frozenset({"journal", "author", "arxiv_category"})


# ---------------------------------------------------------------------------
# LEGACY config still matches  (backward compatibility)
# ---------------------------------------------------------------------------


def test_legacy_flat_topics_config_matches():
    """The flat legacy ``{topics: [...]}`` shape translates to a cos_topic
    predicate under match='any' and still matches."""
    legacy = {"topics": ["DAS"]}
    paper = make_paper(topics=["DAS"])
    assert P.match_tracker(paper, {"config": legacy})[0] is True


def test_legacy_keywords_config_matches():
    legacy = {"keywords": ["slow slip"]}
    paper = make_paper(title="Detecting slow slip events")
    assert P.match_tracker(paper, {"config": legacy})[0] is True


def test_legacy_journals_config_matches():
    legacy = {"journals": ["GRL"]}
    paper = make_paper(journal="Geophysical Research Letters")
    assert P.match_tracker(paper, {"config": legacy})[0] is True


def test_legacy_config_query_nested_mirror_matches():
    """The contract tests store both top-level and ``config.query`` mirrors;
    the nested mirror alone must still translate and match."""
    legacy = {"query": {"keywords": ["interferometry"]}}
    paper = make_paper(abstract="seismic interferometry results")
    assert P.match_tracker(paper, {"config": legacy})[0] is True


def test_legacy_config_is_or_across_families():
    """Legacy semantics: topic OR keyword OR journal is sufficient (match=any).
    A topic-only hit suffices even when keyword/journal would miss."""
    legacy = {"topics": ["DAS"], "keywords": ["slow slip"], "journals": ["Nature"]}
    # Only the topic matches.
    paper = make_paper(topics=["DAS"], title="unrelated", journal="Seismica")
    assert P.match_tracker(paper, {"config": legacy})[0] is True


def test_normalize_config_legacy_uses_any_match():
    norm = P.normalize_config({"topics": ["DAS"], "keywords": ["x"]})
    assert norm["match"] == "any"
    types = {s["type"] for s in norm["predicates"]}
    assert types == {"cos_topic", "keyword"}


def test_normalize_config_new_shape_passthrough():
    cfg = {"match": "all", "predicates": [{"type": "keyword", "any_of": ["DAS"]}]}
    norm = P.normalize_config(cfg)
    assert norm["match"] == "all"
    assert norm["predicates"] == [{"type": "keyword", "any_of": ["DAS"]}]


def test_normalize_config_non_dict_is_safe():
    assert P.normalize_config(None) == {"match": "any", "predicates": []}
    assert P.normalize_config("garbage") == {"match": "any", "predicates": []}
