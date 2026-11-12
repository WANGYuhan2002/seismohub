"""Unit tests for the per-tracker relevance MVP (2026-06-16).

Covers ``build_tracker_centroid`` (the L2-normalise-then-average centroid, the
<MIN_CENTROID_MEMBERS guard, bad-vector skipping, normalise-before-mean) and
``score_tracker_context_relevance`` (semantic→match_score reuse vs keyword
centroid cosine, the too-few-members → all-None fallback).

Pure-function tests with synthetic 768-d vectors + SimpleNamespace fakes — no DB,
no FastAPI client. End-to-end injection/sort is verified live on the preview.
See 计划_相关度模型_2026-06-15.md §3.1.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from services.embedding_service import EMBED_DIM, encode_vector
from services.relevance_service import (
    MIN_CENTROID_MEMBERS,
    build_tracker_centroid,
    score_tracker_context_relevance,
)


def _axis_vec(axis: int, jitter_idx: int = 0, jitter: float = 0.0) -> np.ndarray:
    v = np.zeros(EMBED_DIM, dtype=np.float32)
    v[axis] = 1.0
    if jitter:
        v[100 + jitter_idx] = jitter
    return v


# ---------------------------------------------------------------------------
# build_tracker_centroid
# ---------------------------------------------------------------------------

def test_centroid_too_few_members_returns_none():
    # Exactly MIN_CENTROID_MEMBERS - 1 usable vectors -> None.
    vecs = [_axis_vec(0) for _ in range(MIN_CENTROID_MEMBERS - 1)]
    assert build_tracker_centroid(vecs) is None


def test_centroid_at_threshold_builds():
    vecs = [_axis_vec(0) for _ in range(MIN_CENTROID_MEMBERS)]
    cen = build_tracker_centroid(vecs)
    assert cen is not None
    assert cen.shape == (EMBED_DIM,)


def test_centroid_skips_bad_vectors():
    good = [_axis_vec(0) for _ in range(MIN_CENTROID_MEMBERS)]
    bad = [None, np.zeros(EMBED_DIM, dtype=np.float32), np.ones((2, 2), dtype=np.float32)]
    cen = build_tracker_centroid(good + bad)
    assert cen is not None and cen.shape == (EMBED_DIM,)


def test_centroid_too_few_after_skipping_bad_returns_none():
    good = [_axis_vec(0), _axis_vec(0)]  # only 2 usable
    bad = [None, np.zeros(EMBED_DIM, dtype=np.float32)]
    assert build_tracker_centroid(good + bad) is None


def test_centroid_normalises_before_averaging():
    # A huge-magnitude vector along e0 must NOT dominate: after per-vector L2
    # normalisation it equals a unit e0, so the centroid is mean([e0, e1, e0]).
    a = _axis_vec(0)
    b = _axis_vec(1)
    big = _axis_vec(0) * 1000.0
    cen = build_tracker_centroid([a, b, big])
    assert cen is not None
    # Normalised: e0 component = 2/3 ≈ 0.667 (NOT ~333 if magnitude leaked in).
    assert abs(float(cen[0]) - (2.0 / 3.0)) < 0.02
    assert abs(float(cen[1]) - (1.0 / 3.0)) < 0.02


# ---------------------------------------------------------------------------
# score_tracker_context_relevance
# ---------------------------------------------------------------------------

def _paper(pid: str, vec) -> SimpleNamespace:
    blob = encode_vector(vec.tolist()) if vec is not None else None
    return SimpleNamespace(id=pid, embedding=blob)


def test_keyword_tracker_scores_by_centroid_with_spread():
    # 3 papers near axis-0 (the centroid leans there), 1 near axis-7 (off-topic).
    papers = [_paper(f"a{i}", _axis_vec(0, i, 0.15)) for i in range(3)]
    papers.append(_paper("b0", _axis_vec(7)))
    project = SimpleNamespace(config={"keywords": ["seismic"]})  # no semantic predicate

    out = score_tracker_context_relevance(papers, project, None)

    assert all(v is not None for v in out.values())
    # Off-topic paper is the least relevant, and there IS spread (not flat 1.0).
    assert out["b0"] == min(out.values())
    assert min(out[f"a{i}"] for i in range(3)) > out["b0"]


def test_semantic_tracker_reuses_match_score():
    papers = [_paper("p1", None), _paper("p2", None)]
    project = SimpleNamespace(
        config={"predicates": [{"type": "semantic",
                                "query_embedding": [0.1] * EMBED_DIM,
                                "threshold": 0.5}]}
    )
    ppidx = {"p1": SimpleNamespace(match_score=0.83),
             "p2": SimpleNamespace(match_score=None)}

    out = score_tracker_context_relevance(papers, project, ppidx)

    assert out["p1"] == 0.83
    assert out["p2"] is None


def test_too_few_members_all_none():
    papers = [_paper("p1", _axis_vec(0))]  # 1 member < MIN_CENTROID_MEMBERS
    project = SimpleNamespace(config={"keywords": ["x"]})
    out = score_tracker_context_relevance(papers, project, None)
    assert out == {"p1": None}


def test_unembedded_paper_scores_none_among_embedded():
    papers = [_paper(f"a{i}", _axis_vec(0, i, 0.1)) for i in range(3)]
    papers.append(_paper("noemb", None))
    project = SimpleNamespace(config={"keywords": ["x"]})
    out = score_tracker_context_relevance(papers, project, None)
    assert out["noemb"] is None
    assert all(out[f"a{i}"] is not None for i in range(3))


# ---------------------------------------------------------------------------
# Dimension drift (review BLOCKER + LOW): mixed-dim must not crash; wrong-dim
# papers score None, not a bottom-ranked 0.0.
# ---------------------------------------------------------------------------

def test_centroid_skips_mixed_dim_without_crashing():
    v768 = [_axis_vec(0) for _ in range(3)]
    v512 = np.zeros(512, dtype=np.float32)
    v512[0] = 1.0
    cen = build_tracker_centroid(v768 + [v512])
    assert cen is not None and cen.shape == (EMBED_DIM,)  # 512 skipped, no ValueError


def test_centroid_mixed_dim_below_floor_returns_none():
    # First kept vector is 768-d → dim locks to 768; the three 512-d are
    # skipped, leaving only 2 usable < MIN_CENTROID_MEMBERS.
    v768 = [_axis_vec(0), _axis_vec(0)]
    v512 = [np.eye(512, dtype=np.float32)[0] for _ in range(3)]
    assert build_tracker_centroid(v768 + v512) is None


def test_score_wrong_dim_paper_is_none_not_zero():
    papers = [_paper(f"a{i}", _axis_vec(0, i, 0.1)) for i in range(3)]
    v512 = np.zeros(512, dtype=np.float32)
    v512[0] = 1.0
    papers.append(SimpleNamespace(id="wd", embedding=encode_vector(v512.tolist())))
    project = SimpleNamespace(config={"keywords": ["x"]})
    out = score_tracker_context_relevance(papers, project, None)
    assert out["wd"] is None  # wrong-dim → unscored, NOT 0.0 bottom-rank
    assert all(out[f"a{i}"] is not None for i in range(3))


def test_negative_cosine_clamped_to_zero():
    # 3 members along +e0 plus one along -e0: the -e0 paper has cosine ≈ -1 to
    # the (mostly +e0) centroid and must clamp to 0, never a negative relevance.
    papers = [_paper(f"p{i}", _axis_vec(0)) for i in range(3)]
    papers.append(_paper("neg", -_axis_vec(0)))
    project = SimpleNamespace(config={"keywords": ["x"]})
    out = score_tracker_context_relevance(papers, project, None)
    assert out["neg"] == 0.0
    assert all(out[f"p{i}"] > 0 for i in range(3))


# ---------------------------------------------------------------------------
# Mixed predicate config (review HIGH): a flat match_score=1.0 must NOT be
# reused; the tracker must fall back to the discriminative centroid.
# ---------------------------------------------------------------------------

def test_mixed_config_falls_back_to_centroid_not_flat_match_score():
    papers = [_paper(f"a{i}", _axis_vec(0, i, 0.1)) for i in range(3)]
    papers.append(_paper("b", _axis_vec(7)))
    project = SimpleNamespace(config={
        "match": "any",
        "predicates": [
            {"type": "keyword", "any_of": ["seismic"]},
            {"type": "semantic", "query_embedding": [0.1] * EMBED_DIM, "threshold": 0.5},
        ],
    })
    # Matcher would have stored a flat 1.0 (cheap keyword hit short-circuits).
    ppidx = {p.id: SimpleNamespace(match_score=1.0) for p in papers}
    out = score_tracker_context_relevance(papers, project, ppidx)
    assert not all(v == 1.0 for v in out.values())  # NOT the flat reuse
    assert out["b"] == min(out.values())             # centroid gives real spread


# ---------------------------------------------------------------------------
# Sort builder (review MEDIUM): scored papers rank above unscored ones; a
# stale legacy relevance_score must not outrank a real per-tracker cosine.
# ---------------------------------------------------------------------------

def test_relevance_sort_scored_above_unscored_legacy():
    import main
    papers = [
        {"id": "hi", "context_relevance": 0.92, "relevance_score": 0,
         "published_date": "2020-01-01", "updated_at": "2020-01-01T00:00:00"},
        {"id": "lo", "context_relevance": 0.75, "relevance_score": 0,
         "published_date": "2021-01-01", "updated_at": "2021-01-01T00:00:00"},
        {"id": "legacy95", "relevance_score": 95,
         "published_date": "2022-01-01", "updated_at": "2022-01-01T00:00:00"},
    ]
    out = main._sort_papers(list(papers), "relevance")
    ids = [p["id"] for p in out]
    # Both scored papers rank above the high-legacy-score unscored one...
    assert ids.index("hi") < ids.index("legacy95")
    assert ids.index("lo") < ids.index("legacy95")
    # ...and higher context_relevance ranks first within the scored group.
    assert ids.index("hi") < ids.index("lo")
