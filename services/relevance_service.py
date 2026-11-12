"""Per-tracker relevance scoring — 相关度 MVP (2026-06-16).

``context_relevance`` answers "inside THIS tracker, how on-topic is this paper",
as a continuous 0..1 cosine — replacing the flat ``match_score = 1.0`` that
keyword/journal/author trackers get (every deterministic predicate returns 1.0
on a hit, so today every member ranks identically).

Two sources, per 计划_相关度模型_2026-06-15.md §3.1:
  1. a PURE semantic tracker (every predicate is semantic) → reuse
     ``ProjectPaper.match_score`` (the matcher already wrote the semantic
     cosine there). A *mixed* config is NOT reused: a cheap deterministic
     predicate can short-circuit ``match=any`` to a flat 1.0, or dominate
     ``best_hit_score`` under ``match=all`` — so mixed trackers fall back to
     the centroid below.
  2. otherwise (keyword/journal/author/mixed) → cosine to the member centroid:
     L2-normalise each member embedding, average → centroid, then score each
     paper by ``cosine(paper, centroid)``.

This is a PURE READ path: computed on demand at tracker-view load. No DB column,
no migration, no write to ``project.config``. A cross-tracker global profile and
behaviour signals (❤️/notes) are deliberately OUT of this MVP (planned 阶段 2).
``dismiss`` is NOT a signal here (owner 2026-06-16: hide-only, never relevance).

Known MVP limitation (per design, not fixed here): the centroid includes the
papers being scored, so a member is ~1/N of its own centroid; for tiny trackers
(near the MIN_CENTROID_MEMBERS floor) this mildly inflates/flattens scores. The
live trackers (13–113 members) are large enough for this to be negligible.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from services.embedding_service import cosine_sim, decode_vector

# A centroid built from fewer than this many embedded members is too noisy to
# trust; such a tracker is left unscored (context_relevance None) and the view
# falls back to date ordering.
MIN_CENTROID_MEMBERS = 3


def _clamp01(x: Any) -> float:
    """Clamp to [0, 1]. cosine is [-1, 1]; a (rare) negative or >1 must not
    render as a negative/over-100 relevance nor sort oddly."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def build_tracker_centroid(
    member_vectors: List[Optional[np.ndarray]],
) -> Optional[np.ndarray]:
    """Mean of the L2-normalised member vectors, or ``None`` if too few usable.

    Per-vector L2 normalisation BEFORE averaging so a large-magnitude paper
    cannot dominate the centroid (``text-multilingual-embedding-002`` does not
    guarantee unit length). Vectors that are None / wrong-shape / zero-norm —
    OR whose dimension differs from the first kept vector — are skipped, never
    raise (mirrors the wrong-dim guard in ``embedding_service.cosine_topk`` so a
    model/dim switch degrades gracefully instead of raising in ``np.stack``).
    Returns ``None`` when fewer than ``MIN_CENTROID_MEMBERS`` usable vectors
    remain.
    """
    units: List[np.ndarray] = []
    dim: Optional[int] = None
    for v in member_vectors:
        if v is None or getattr(v, "ndim", None) != 1 or v.size == 0:
            continue
        if dim is None:
            dim = int(v.shape[0])
        elif int(v.shape[0]) != dim:
            continue  # mixed-dim member (model/dim drift) — skip, don't crash
        n = float(np.linalg.norm(v))
        if n == 0.0:
            continue
        units.append(v / n)
    if len(units) < MIN_CENTROID_MEMBERS:
        return None
    return np.mean(np.stack(units), axis=0)


def _is_pure_semantic_tracker(project: Any) -> bool:
    """True only when EVERY predicate is semantic.

    Then ``ProjectPaper.match_score`` IS the semantic cosine and is safe to
    reuse as ``context_relevance``. Any deterministic predicate
    (keyword/journal/author/date) can short-circuit ``match=any`` to a flat 1.0
    or dominate ``best_hit_score`` under ``match=all``, so a mixed config falls
    back to the member centroid instead.
    """
    try:
        from services.predicates import build_predicates, _config_of
        preds = build_predicates(_config_of(project))
        return bool(preds) and all(
            getattr(p, "type", None) == "semantic" for p in preds
        )
    except Exception:
        return False


def score_tracker_context_relevance(
    papers: List[Any],
    project: Any,
    project_paper_index: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[float]]:
    """Return ``{paper_id: context_relevance in 0..1 (or None)}`` for a tracker.

    ``papers`` are ORM ``Paper`` rows (carry the ``.embedding`` TEXT column).
    A pure-semantic tracker reuses the already-discriminative ``match_score``;
    every other tracker scores each paper against the member centroid. ``None``
    per paper means "no usable signal" (no centroid, no match_score, an
    unembedded paper, or a wrong-dim embedding) — callers fall back to date
    ordering and rank such papers below the scored ones.
    """
    # Pure-semantic tracker: the matcher already wrote a meaningful cosine.
    if _is_pure_semantic_tracker(project) and project_paper_index:
        out: Dict[str, Optional[float]] = {}
        for p in papers:
            pp = project_paper_index.get(p.id)
            ms = getattr(pp, "match_score", None) if pp is not None else None
            out[p.id] = _clamp01(ms) if ms is not None else None
        return out

    # Keyword/journal/author/mixed tracker: member-centroid cosine.
    vecs: Dict[str, Optional[np.ndarray]] = {
        p.id: decode_vector(getattr(p, "embedding", None)) for p in papers
    }
    centroid = build_tracker_centroid(list(vecs.values()))
    if centroid is None:
        return {pid: None for pid in vecs}
    cdim = int(centroid.shape[0])
    out = {}
    for pid, v in vecs.items():
        # Skip unembedded / wrong-dim papers → None (unscored), NOT 0.0 (which
        # cosine_sim returns on a shape mismatch and would bottom-rank them as a
        # genuine low score). Keeps the "no usable signal → None" contract.
        if v is None or getattr(v, "ndim", None) != 1 or v.size != cdim:
            out[pid] = None
        else:
            out[pid] = _clamp01(cosine_sim(v, centroid))
    return out
