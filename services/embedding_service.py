"""Embedding service — the numerical core for v2's semantic matching (BRIEF v2 §3).

This is the *cheap* title+abstract embedding wrapper (NO PDF, no full Gemini
Pro analysis) plus a numpy brute-force cosine core. At ingest we compute one
small vector per paper; trackers with a ``semantic`` predicate store a query
embedding; matching is ``cosine(query, paper) >= threshold``. The corpus is
~1k–50k papers, so we deliberately use numpy brute force — NO sqlite-vec, NO
vector index (per BRIEF v2 §3).

House style mirrors ``services/llm_client.py`` / ``services/analyze_service.py``:
- Credentials resolve Vertex-first (``VERTEX_AI_API_KEY`` + project/location ->
  ``genai.Client(vertexai=True, ...)``) then fall back to a direct
  ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``.
- The ``google.genai`` SDK is imported *lazily* (inside the client factory and
  the low-level call), exactly like ``GeminiClient._ensure_init`` and
  ``analyze_service._get_vertex_client``, so merely importing this module never
  forces the SDK or a key. That keeps ``db.py`` and pytest collection import-safe.
- Tenacity retry knobs are identical to ``analyze_service.call_gemini``
  (``stop_after_attempt(3)``, ``wait_exponential(min=2, max=12)``).

Graceful degradation contract: every PUBLIC entry point
(``embed_text`` / ``embed_texts``) returns ``None`` (per element) on missing
key, blank input, or any quota / network / auth failure after retries — it
NEVER raises into ingest. The cost of a swallowed failure is that the paper
persists embedding-less and semantic predicates will not match it until a
re-embed/backfill pass (owned by ingest-pipeline) recovers the row.

Consumers
---------
- ingest-pipeline: precompute the corpus vector at intake
  (``text_for_embedding(paper)`` -> ``embed_text(...)`` -> ``encode_vector(...)``
  into the ``Paper.embedding`` TEXT column it owns).
- predicate-model: the ``semantic`` predicate's ``eval(paper)`` via
  ``decode_vector`` + ``cosine_sim``.
- tracker-api: compute the tracker query embedding at create time
  (``embed_text(description, is_query=True)``) and preview matches via
  ``cosine_topk``.

Dependencies
------------
``numpy`` is a NEW runtime dependency. It is declared/pinned in
``requirements.txt`` by the *config-docs* package (``numpy>=1.24``); this leaf
module only imports it. ``tenacity`` and ``google-genai`` are already pinned.

Stability contract
-------------------
Stored vectors are only comparable within one ``(EMBED_MODEL, EMBED_DIM)`` pair.
Both constants are exported so callers can persist the model id alongside the
vector (ingest-pipeline's ``embedding_model`` column) and skip cosine when it
differs. ``cosine_topk`` additionally drops wrong-dimension vectors defensively,
but a *silent* model swap at the same dim would still mis-score — see the module
risks recorded by the package plan.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EMBED_MODEL",
    "EMBED_DIM",
    "get_embedding_client",
    "embed_text",
    "embed_texts",
    "encode_vector",
    "decode_vector",
    "cosine_sim",
    "cosine_topk",
    "text_for_embedding",
    "_reset_client_for_tests",
]


# ---------------------------------------------------------------------------
# Constants (env-overridable, like llm_client's GEMINI_MODEL)
# ---------------------------------------------------------------------------
#
# EMBED_MODEL / EMBED_DIM are the *stability contract*: stored vectors are only
# comparable within one ``(model, dim)`` pair. Callers persist the model id next
# to the vector (ingest-pipeline's ``embedding_model`` column) and must skip
# cosine when it differs. Changing either constant after a bulk-embed pass
# invalidates every stored vector and requires a re-embed.

# Successor to gemini-embedding-001 (EOL 2026-07-14). NOTE: `gemini-embedding-2`
# 404s on the maintainer's Vertex project (location=global) — it is
# NOT available here — so we use `text-multilingual-embedding-002`, which is
# VERIFIED working on this Vertex (768-dim) and is multilingual, fitting the
# zh/en bilingual seismology corpus. Other verified-working ids on this Vertex:
# gemini-embedding-001 (works but EOL 2026-07-14), text-embedding-004/005
# (English/code-leaning). EMBED_DIM stays 768; cosine_* L2-normalise before
# comparing. EMBED_MODEL env overrides without a code change. Switched/verified
# 2026-06-12 — see _audit_2026-06-11/05-CLEANUP-EXECUTED.md.
EMBED_MODEL: str = (os.environ.get("EMBED_MODEL") or "text-multilingual-embedding-002").strip()

# Requested ``output_dimensionality``. 768 is a safe truncation supported by
# gemini-embedding-001 (native 3072) and keeps the stored TEXT cells small for a
# 50k-paper corpus. NOTE: the SDK/model may not honour the request — callers
# must trust the *actual* ``len()`` of the returned/decoded vector, not assume
# EMBED_DIM (cosine_topk groups by actual dim so this stays safe).
EMBED_DIM: int = int(os.environ.get("EMBED_DIM") or "768")

# Gemini retrieval task types. Corpus text is embedded as a DOCUMENT and tracker
# query text as a QUERY so they land in the same retrieval space (asymmetric, as
# Gemini recommends for retrieval).
_EMBED_TASK_DOCUMENT: str = "RETRIEVAL_DOCUMENT"
_EMBED_TASK_QUERY: str = "RETRIEVAL_QUERY"

# Chunk size for embed_texts() native batching (response.embeddings is
# index-parallel to contents, so a list call batches cleanly).
# NOTE: Vertex text-embedding models (text-multilingual-embedding-002,
# text-embedding-004/005) reject a request whose TOTAL input exceeds 20000
# tokens. A 100-abstract chunk (~35k tokens) is rejected with 400
# INVALID_ARGUMENT, silently zeroing embeddings. 20 abstracts (~7k tokens, worst
# case well under 20k incl. CJK) is the safe default; raise EMBED_MAX_BATCH only
# for models with a larger budget. Verified 2026-06-12 — see 05-CLEANUP-EXECUTED.md.
_MAX_BATCH: int = int(os.environ.get("EMBED_MAX_BATCH") or "20")


# ---------------------------------------------------------------------------
# Credential resolution + client singleton
# ---------------------------------------------------------------------------
#
# Structurally copied from llm_client.GeminiClient._ensure_init /
# analyze_service._get_vertex_client so this service shares the exact same
# Vertex-vs-direct convention and needs no new key.

_client: Any = None
_client_lock = threading.Lock()


def _resolve_mode() -> Tuple[str, Dict[str, str]]:
    """Return ``("vertex" | "direct" | "none", kwargs)`` from the environment.

    Vertex wins when ``VERTEX_AI_API_KEY`` is set; otherwise a direct key
    (``GEMINI_API_KEY`` or ``GOOGLE_API_KEY``) is used. Returns ``"none"`` when
    no key is present so callers degrade gracefully instead of raising.
    """
    vertex_key = (os.environ.get("VERTEX_AI_API_KEY") or "").strip()
    if vertex_key:
        return (
            "vertex",
            {
                "api_key": vertex_key,
                "project": (os.environ.get("VERTEX_AI_PROJECT") or "").strip(),
                "location": (os.environ.get("VERTEX_AI_LOCATION") or "global").strip(),
            },
        )
    # Direct mode — accept GOOGLE_API_KEY too (llm_client's env comments allow it).
    direct_key = (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    ).strip()
    if direct_key:
        return ("direct", {"api_key": direct_key})
    return ("none", {})


def get_embedding_client() -> Optional[Any]:
    """Return a shared (lazily built) ``genai.Client`` — or ``None`` when no key.

    Double-checked-locking singleton. NEVER raises: when no credential is
    configured it returns ``None`` so ingest can persist a paper without an
    embedding. The ``google.genai`` import is lazy so importing this module is
    side-effect free (db.py / test-collection safety).
    """
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client

        mode, kwargs = _resolve_mode()
        if mode == "none":
            logger.info(
                "embedding_service: no VERTEX_AI_API_KEY / GEMINI_API_KEY / "
                "GOOGLE_API_KEY set — semantic embedding disabled (returns None)."
            )
            return None

        from google import genai  # type: ignore  # lazy: keep module import-safe

        if mode == "vertex":
            project = kwargs.get("project") or ""
            location = kwargs.get("location") or "global"
            if project:
                os.environ["GOOGLE_CLOUD_PROJECT"] = project
            os.environ["GOOGLE_CLOUD_LOCATION"] = location
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
            _client = genai.Client(vertexai=True, api_key=kwargs["api_key"])
        else:  # "direct"
            _client = genai.Client(api_key=kwargs["api_key"])

        return _client


def _reset_client_for_tests() -> None:
    """Clear the cached client so tests can re-resolve credentials.

    Analogous to clearing ``llm_client._shared``. Test-only hook.
    """
    global _client
    with _client_lock:
        _client = None


def reset_client() -> None:
    """Public alias of the cache reset for RUNTIME use (M8 fix, 2026-06-30): drop
    the cached embedding client after a Settings key change so the next embed call
    re-resolves credentials instead of reusing a stale/bad-key client."""
    _reset_client_for_tests()


# ---------------------------------------------------------------------------
# Low-level retried embedding call
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(Exception),
)
def _embed_call(client: Any, contents: List[str], task_type: str) -> List[Optional[List[float]]]:
    """Embed a chunk of non-blank strings; return one vector (or None) per input.

    Tenacity retries the network call up to 3x; the public wrappers convert a
    final exhausted failure into ``None`` so nothing propagates into ingest.

    The response's ``embeddings`` list is index-parallel to ``contents`` (Gemini
    returns one ContentEmbedding per input). We pad/truncate defensively so the
    output length always equals ``len(contents)``.
    """
    from google.genai import types  # type: ignore  # lazy import

    # ``output_dimensionality`` may be unsupported on older SDK builds — guard
    # exactly like analyze_service._make_generation_config guards
    # response_mime_type, retrying config construction without the kwarg.
    try:
        config = types.EmbedContentConfig(
            task_type=task_type, output_dimensionality=EMBED_DIM
        )
    except TypeError:
        config = types.EmbedContentConfig(task_type=task_type)

    resp = client.models.embed_content(
        model=EMBED_MODEL, contents=contents, config=config
    )

    out: List[Optional[List[float]]] = []
    for emb in (getattr(resp, "embeddings", None) or []):
        vals = getattr(emb, "values", None)
        out.append([float(x) for x in vals] if vals else None)

    # Index-parallel safety: never return more/fewer than we asked for.
    if len(out) < len(contents):
        out.extend([None] * (len(contents) - len(out)))
    elif len(out) > len(contents):
        out = out[: len(contents)]
    return out


# ---------------------------------------------------------------------------
# Public single + batch wrappers (failure -> None, never raise)
# ---------------------------------------------------------------------------


def embed_text(text: str, *, is_query: bool = False) -> Optional[List[float]]:
    """Embed a single string. Returns ``None`` for blank text, no key, or failure.

    ``is_query=True`` selects the ``RETRIEVAL_QUERY`` task type (used for tracker
    description text) so the query lands in the same space as corpus DOCUMENT
    vectors. NEVER raises — a quota/network error is swallowed to ``None``.
    """
    if not text or not text.strip():
        return None
    if get_embedding_client() is None:
        return None
    try:
        result = embed_texts([text], is_query=is_query)
        return result[0] if result else None
    except Exception:  # pragma: no cover — defensive; embed_texts already swallows
        logger.warning("embed_text failed; returning None", exc_info=True)
        return None


def embed_texts(
    texts: List[str], *, is_query: bool = False
) -> List[Optional[List[float]]]:
    """Embed many strings, preserving input length and order.

    Contract:
    - No key -> ``[None] * len(texts)``.
    - Blank/whitespace items are mapped to ``None`` *without* being sent.
    - Sent in ``_MAX_BATCH``-sized chunks; each chunk is wrapped so one failed
      chunk yields ``None`` for only its elements (other chunks still succeed).
    - ``is_query`` toggles ``RETRIEVAL_QUERY`` vs ``RETRIEVAL_DOCUMENT`` so query
      and corpus embeddings are comparable.

    NEVER raises into the caller.
    """
    n = len(texts)
    results: List[Optional[List[float]]] = [None] * n
    if n == 0:
        return results

    client = get_embedding_client()
    if client is None:
        return results

    task_type = _EMBED_TASK_QUERY if is_query else _EMBED_TASK_DOCUMENT

    for start in range(0, n, _MAX_BATCH):
        chunk_indices = list(range(start, min(start + _MAX_BATCH, n)))
        # Filter blanks out of the wire payload; remember where each kept item
        # belongs so we can splice vectors back by original position.
        send_positions: List[int] = []
        send_texts: List[str] = []
        for idx in chunk_indices:
            t = texts[idx]
            if t and t.strip():
                send_positions.append(idx)
                send_texts.append(t)
        if not send_texts:
            continue
        try:
            vectors = _embed_call(client, send_texts, task_type)
        except Exception:
            # Isolated chunk failure: leave this chunk's positions as None.
            logger.warning(
                "embed_texts: chunk [%d:%d] failed after retries; those rows "
                "will have no embedding",
                start,
                start + len(chunk_indices),
                exc_info=True,
            )
            continue
        for pos, vec in zip(send_positions, vectors):
            results[pos] = vec

    return results


# ---------------------------------------------------------------------------
# Vector (de)serialisation for the Paper.embedding TEXT column
# ---------------------------------------------------------------------------
#
# JSON (not a raw float32 BLOB) is chosen deliberately: the brief allows
# BLOB-or-JSON, the column is declared TEXT by the ingest-pipeline/db.py package,
# and JSON keeps the cell human-inspectable and sqlite-greppable. This module
# only round-trips through that column; it does NOT define or migrate it.
# Callers store EMBED_MODEL next to the blob and must not decode-compare vectors
# across models/dims.


def encode_vector(vec: Optional[Sequence[float]]) -> Optional[str]:
    """Serialise a vector to compact JSON for the ``Paper.embedding`` TEXT column.

    ``None`` in -> ``None`` out. 7 significant figures keeps the cell small with
    negligible cosine error.
    """
    if vec is None:
        return None
    return json.dumps([round(float(x), 7) for x in vec], separators=(",", ":"))


def decode_vector(blob: Optional[str]) -> Optional[np.ndarray]:
    """Parse a stored JSON vector back to a 1-D ``float32`` ndarray.

    Returns ``None`` for ``None``/empty/whitespace input and for any corrupt
    cell (bad JSON, non-list, empty, or non-1-D) — a bad cell must NEVER crash a
    sort. Callers must not decode-compare across models/dims.
    """
    if not blob or not blob.strip():
        return None
    try:
        arr = np.asarray(json.loads(blob), dtype=np.float32)
    except (ValueError, TypeError):
        return None
    if arr.ndim != 1 or arr.size == 0:
        return None
    return arr


# ---------------------------------------------------------------------------
# numpy cosine utilities (brute force; no index — corpus 1k–50k per BRIEF §3)
# ---------------------------------------------------------------------------


def cosine_sim(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> float:
    """Cosine similarity of two 1-D vectors.

    Returns ``0.0`` if either is ``None``/empty, shapes differ, or either has
    zero norm — so an unembedded paper simply never matches.
    """
    if a is None or b is None:
        return 0.0
    if getattr(a, "size", 0) == 0 or getattr(b, "size", 0) == 0:
        return 0.0
    if a.shape != b.shape:
        return 0.0
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def cosine_topk(
    query: Optional[np.ndarray],
    id_to_vec: Dict[str, Optional[np.ndarray]],
    k: Optional[int] = None,
    *,
    threshold: Optional[float] = None,
) -> List[Tuple[str, float]]:
    """Rank ``id_to_vec`` entries by cosine similarity to ``query``.

    This is the semantic-predicate / NL-preview engine. ``None`` or wrong-dim
    vectors are skipped (an unembedded paper never matches). The whole corpus is
    a single L2-normalised matmul, so 50k papers is well under ~50 ms — corpus
    1k–50k means we deliberately use brute force, NO sqlite-vec.

    Args:
        query: the query vector; ``None``/empty -> ``[]``.
        id_to_vec: mapping ``paper_id -> vector`` (``None`` entries ignored).
        k: optional truncation to the top-k results.
        threshold: optional minimum cosine score (inclusive).

    Returns:
        ``[(paper_id, score), ...]`` sorted by descending score, scores as plain
        floats and ids as the input keys.
    """
    if query is None or getattr(query, "size", 0) == 0:
        return []
    qdim = int(query.shape[0])

    ids: List[str] = []
    rows: List[np.ndarray] = []
    for pid, vec in id_to_vec.items():
        if vec is None:
            continue
        if getattr(vec, "ndim", None) != 1 or vec.size != qdim:
            continue  # silently skip wrong-dim vectors (model/dim mismatch)
        ids.append(pid)
        rows.append(vec)

    if not rows:
        return []

    mat = np.asarray(np.stack(rows), dtype=np.float32)
    qn = np.asarray(query, dtype=np.float32)
    q_norm = float(np.linalg.norm(qn))
    if q_norm == 0.0:
        return []

    # L2-normalise rows (+epsilon guards zero-vectors) and the query, then dot.
    normed = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
    qn = qn / q_norm
    scores = normed @ qn

    pairs: List[Tuple[str, float]] = [
        (pid, float(score)) for pid, score in zip(ids, scores)
    ]
    if threshold is not None:
        pairs = [p for p in pairs if p[1] >= threshold]
    pairs.sort(key=lambda p: p[1], reverse=True)
    if k is not None:
        pairs = pairs[:k]
    return pairs


# ---------------------------------------------------------------------------
# Shared title+abstract canonicaliser (query/corpus symmetry)
# ---------------------------------------------------------------------------


def text_for_embedding(paper: Any) -> str:
    """Canonicalise a paper's title + abstract into the text we embed.

    Used by both the corpus (ingest-pipeline calls ``text_for_embedding(paper)``
    then ``embed_text(...)`` at intake) and any caller that wants symmetric
    query/corpus text. Accepts either a ``Paper`` ORM object OR a plain
    dict/mapping (predicate-model may pass a lightweight dict). Dependency-free —
    no fetch, no PDF — this is the cheap path the brief mandates.

    Returns ``""`` when nothing usable is present (callers treat ``""`` as
    "skip embedding").
    """

    def _field(name: str) -> str:
        val = getattr(paper, name, None)
        if val is None and isinstance(paper, dict):
            val = paper.get(name)
        return "" if val is None else str(val).strip()

    title = _field("title")
    abstract = _field("abstract")

    if title and abstract:
        joined = f"{title}\n\n{abstract}"
    else:
        joined = title or abstract

    joined = joined.strip()
    if not joined:
        return ""
    # Collapse runs of whitespace so trivially-different spacing yields identical
    # embeddings.
    return " ".join(joined.split())
