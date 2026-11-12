"""Tests for ``services/embedding_service.py`` (BRIEF v2 §3 semantic core).

Mocking style follows ``tests/test_llm_client.py``: no real Gemini calls. We
monkeypatch ``get_embedding_client`` to return a ``MagicMock`` whose
``models.embed_content`` returns a stub ``EmbedContentResponse``-like object
(``resp.embeddings = [obj(values=[...])]``), and inject a fake
``google.genai.types`` module so ``EmbedContentConfig`` is a no-op factory.
Any genuinely live-API test would carry ``@pytest.mark.network`` (skipped in CI
per pytest.ini) — none here are needed.

Coverage targets:
- embed_text happy path + blank short-circuit.
- No-key degradation (client None) -> element-wise None, no exception.
- embed_texts ordering / batching / blank-position handling.
- Per-chunk failure isolation.
- tenacity retry (raise twice then succeed; always-raise -> swallowed None).
- encode/decode round-trip + corrupt-cell tolerance.
- cosine_sim edge cases.
- cosine_topk skipping None/wrong-dim, threshold, k, sort order.
- text_for_embedding for ORM-like, dict, title/abstract-only, empty.
- Import safety with no key set.
"""

from __future__ import annotations

import math
import sys
from typing import Any, List, Optional
from unittest.mock import MagicMock

import numpy as np
import pytest

import services.embedding_service as es
from services.embedding_service import (
    EMBED_DIM,
    EMBED_MODEL,
    cosine_sim,
    cosine_topk,
    decode_vector,
    embed_text,
    embed_texts,
    encode_vector,
    text_for_embedding,
)


# ----------------------------------------------------------------------
# Stub helpers (mirror test_llm_client._make_stub_response style)
# ----------------------------------------------------------------------


def _stub_response(vectors: List[Optional[List[float]]]) -> Any:
    """Build an EmbedContentResponse-like object: resp.embeddings[i].values."""
    resp = MagicMock()
    embeddings = []
    for v in vectors:
        emb = MagicMock()
        emb.values = v
        embeddings.append(emb)
    resp.embeddings = embeddings
    return resp


def _patch_client(monkeypatch, embed_content) -> MagicMock:
    """Inject a fake genai client + a no-op google.genai.types module.

    ``embed_content`` is assigned as the mock's ``models.embed_content``
    (callable / side_effect / return_value already configured by the caller).
    """
    fake_client = MagicMock()
    fake_client.models.embed_content = embed_content
    monkeypatch.setattr(es, "get_embedding_client", lambda: fake_client)

    fake_types = MagicMock()
    fake_types.EmbedContentConfig = lambda **kw: kw  # no-op factory
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    # ``embedding_service._embed_call`` does ``from google.genai import types``,
    # which resolves the ``types`` *attribute* of the already-imported
    # ``google.genai`` package object — NOT ``sys.modules["google.genai.types"]``.
    # If another test (e.g. test_schema_migration) imported google.genai first,
    # that attribute is bound to the REAL types module and the setitem above is
    # ignored, so EmbedContentConfig becomes the real (non-subscriptable) class
    # and ``captured["config"]["task_type"]`` fails. Patch the package attribute
    # too so the fake wins regardless of cross-file import order. monkeypatch
    # restores it after each test.
    try:
        import google.genai as _genai_pkg  # type: ignore

        monkeypatch.setattr(_genai_pkg, "types", fake_types, raising=False)
    except Exception:
        pass
    return fake_client


@pytest.fixture(autouse=True)
def _reset_client():
    """Ensure each test starts/ends with a clean credential singleton."""
    es._reset_client_for_tests()
    yield
    es._reset_client_for_tests()


# ----------------------------------------------------------------------
# embed_text happy path + blank
# ----------------------------------------------------------------------


def test_embed_text_happy_path(monkeypatch):
    vec = [0.1] * EMBED_DIM
    mock = MagicMock(return_value=_stub_response([vec]))
    fake = _patch_client(monkeypatch, mock)
    out = embed_text("hello world")
    assert out == vec
    assert len(out) == EMBED_DIM
    # The model id passed on the wire is EMBED_MODEL.
    assert mock.call_args.kwargs["model"] == EMBED_MODEL


def test_embed_text_blank_returns_none_without_calling_client(monkeypatch):
    mock = MagicMock(return_value=_stub_response([[1.0]]))
    _patch_client(monkeypatch, mock)
    assert embed_text("") is None
    assert embed_text("   ") is None
    mock.assert_not_called()


def test_embed_text_is_query_uses_query_task_type(monkeypatch):
    captured = {}

    def _capture(*, model, contents, config):
        captured["config"] = config
        return _stub_response([[0.5, 0.5]])

    _patch_client(monkeypatch, _capture)
    embed_text("a tracker description", is_query=True)
    # The no-op EmbedContentConfig factory returns its kwargs dict.
    assert captured["config"]["task_type"] == es._EMBED_TASK_QUERY


# ----------------------------------------------------------------------
# No-key degradation
# ----------------------------------------------------------------------


def test_no_key_embed_text_returns_none(monkeypatch):
    monkeypatch.setattr(es, "get_embedding_client", lambda: None)
    assert embed_text("anything") is None


def test_no_key_embed_texts_returns_all_none_no_raise(monkeypatch):
    monkeypatch.setattr(es, "get_embedding_client", lambda: None)
    out = embed_texts(["a", "b"])
    assert out == [None, None]  # caller can still persist embedding-less rows


def test_resolve_mode_none_when_no_env(monkeypatch):
    for var in (
        "VERTEX_AI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    mode, kwargs = es._resolve_mode()
    assert mode == "none"
    assert kwargs == {}
    # And the public factory returns None (never raises) in that state.
    assert es.get_embedding_client() is None


def test_resolve_mode_prefers_vertex(monkeypatch):
    monkeypatch.setenv("VERTEX_AI_API_KEY", "vk")
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    mode, kwargs = es._resolve_mode()
    assert mode == "vertex"
    assert kwargs["api_key"] == "vk"


def test_resolve_mode_direct_accepts_google_api_key(monkeypatch):
    monkeypatch.delenv("VERTEX_AI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "gk2")
    mode, kwargs = es._resolve_mode()
    assert mode == "direct"
    assert kwargs["api_key"] == "gk2"


# ----------------------------------------------------------------------
# embed_texts ordering + batching + blank handling
# ----------------------------------------------------------------------


def test_embed_texts_ordering_batching_and_blank(monkeypatch):
    monkeypatch.setattr(es, "_MAX_BATCH", 2)

    # The stub echoes a recognisable vector per *sent* (non-blank) input so we
    # can assert positional correctness.
    def _embed(*, model, contents, config):
        return _stub_response([[float(len(c))] for c in contents])

    mock = MagicMock(side_effect=_embed)
    _patch_client(monkeypatch, mock)

    texts = ["aa", "", "bbbb", "c", "ddddd"]  # one blank at index 1
    out = embed_texts(texts)

    assert len(out) == len(texts)
    assert out[1] is None  # blank stays None
    assert out[0] == [2.0]
    assert out[2] == [4.0]
    assert out[3] == [1.0]
    assert out[4] == [5.0]
    # 5 inputs, _MAX_BATCH=2 -> ceil(5/2) = 3 chunk calls.
    assert mock.call_count == math.ceil(len(texts) / 2)


def test_embed_texts_empty_list(monkeypatch):
    mock = MagicMock()
    _patch_client(monkeypatch, mock)
    assert embed_texts([]) == []
    mock.assert_not_called()


def test_embed_texts_blank_only_chunk_not_sent(monkeypatch):
    monkeypatch.setattr(es, "_MAX_BATCH", 2)
    mock = MagicMock(side_effect=lambda **kw: _stub_response([[1.0]]))
    _patch_client(monkeypatch, mock)
    # Both items in the single chunk are blank -> nothing is sent.
    out = embed_texts(["", "   "])
    assert out == [None, None]
    mock.assert_not_called()


# ----------------------------------------------------------------------
# Per-chunk failure isolation
# ----------------------------------------------------------------------


def test_embed_texts_chunk_failure_isolated(monkeypatch):
    monkeypatch.setattr(es, "_MAX_BATCH", 2)

    # Key the failure on the chunk's *content* (not a global counter): the
    # second chunk contains "c"/"d" and must fail on every tenacity retry so the
    # chunk is genuinely exhausted, while the first chunk still succeeds.
    def _embed(*, model, contents, config):
        if "c" in contents:  # the [2,3] chunk
            raise RuntimeError("boom on chunk 2")
        return _stub_response([[1.0] for _ in contents])

    mock = MagicMock(side_effect=_embed)
    _patch_client(monkeypatch, mock)

    # 4 inputs -> chunk0 = [0,1], chunk1 = [2,3]; chunk1 fails (all retries).
    out = embed_texts(["a", "b", "c", "d"])
    assert out[0] == [1.0]
    assert out[1] == [1.0]
    assert out[2] is None
    assert out[3] is None


# ----------------------------------------------------------------------
# tenacity retry behaviour
# ----------------------------------------------------------------------


def test_embed_call_retries_then_succeeds(monkeypatch):
    vec = [0.9, 0.1]
    seq = [RuntimeError("t1"), RuntimeError("t2"), _stub_response([vec])]
    mock = MagicMock(side_effect=seq)
    fake = _patch_client(monkeypatch, mock)
    out = embed_text("retry me")
    assert out == vec
    assert mock.call_count == 3  # 2 failures + 1 success


def test_embed_text_swallows_exhausted_retries(monkeypatch):
    mock = MagicMock(side_effect=RuntimeError("always down"))
    _patch_client(monkeypatch, mock)
    # 3 attempts then exhausted -> embed_texts isolates chunk -> None element ->
    # embed_text returns None. No exception escapes.
    assert embed_text("doomed") is None
    assert mock.call_count == 3


# ----------------------------------------------------------------------
# encode / decode round-trip
# ----------------------------------------------------------------------


def test_encode_decode_round_trip():
    blob = encode_vector([0.1, 0.2, 0.3])
    assert isinstance(blob, str)
    arr = decode_vector(blob)
    assert isinstance(arr, np.ndarray)
    assert arr.dtype == np.float32
    assert arr.shape == (3,)
    assert np.allclose(arr, np.array([0.1, 0.2, 0.3], dtype=np.float32), atol=1e-6)


def test_encode_vector_none():
    assert encode_vector(None) is None


def test_encode_vector_compact_json():
    blob = encode_vector([1.0, 2.0])
    assert " " not in blob  # compact separators


@pytest.mark.parametrize("bad", [None, "", "   ", "not json", '{"a":1}'])
def test_decode_vector_corrupt_returns_none(bad):
    assert decode_vector(bad) is None


def test_decode_vector_empty_list_returns_none():
    assert decode_vector("[]") is None


# ----------------------------------------------------------------------
# cosine_sim
# ----------------------------------------------------------------------


def test_cosine_sim_identical_unit_vectors():
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert cosine_sim(a, a) == pytest.approx(1.0, abs=1e-6)


def test_cosine_sim_orthogonal():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert cosine_sim(a, b) == pytest.approx(0.0, abs=1e-6)


def test_cosine_sim_zero_vector():
    a = np.array([0.0, 0.0], dtype=np.float32)
    b = np.array([1.0, 1.0], dtype=np.float32)
    assert cosine_sim(a, b) == 0.0


def test_cosine_sim_mismatched_shapes():
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert cosine_sim(a, b) == 0.0


def test_cosine_sim_none_inputs():
    assert cosine_sim(None, np.array([1.0])) == 0.0
    assert cosine_sim(np.array([1.0]), None) == 0.0
    assert cosine_sim(None, None) == 0.0


# ----------------------------------------------------------------------
# cosine_topk
# ----------------------------------------------------------------------


def test_cosine_topk_skips_none_and_wrong_dim_sorts_and_thresholds():
    query = np.array([1.0, 0.0], dtype=np.float32)
    id_to_vec = {
        "near": np.array([0.9, 0.1], dtype=np.float32),   # high cosine
        "far": np.array([0.0, 1.0], dtype=np.float32),    # ~0 cosine
        "none": None,                                      # skipped
        "wrongdim": np.array([1.0, 0.0, 0.0], dtype=np.float32),  # skipped
    }
    out = cosine_topk(query, id_to_vec)
    ids = [pid for pid, _ in out]
    assert ids == ["near", "far"]  # only valid+matching, sorted desc
    assert all(isinstance(s, float) for _, s in out)
    assert out[0][1] > out[1][1]


def test_cosine_topk_threshold_and_k():
    query = np.array([1.0, 0.0], dtype=np.float32)
    id_to_vec = {
        "a": np.array([1.0, 0.0], dtype=np.float32),    # 1.0
        "b": np.array([0.7, 0.7], dtype=np.float32),    # ~0.707
        "c": np.array([0.0, 1.0], dtype=np.float32),    # 0.0
    }
    thr = cosine_topk(query, id_to_vec, threshold=0.5)
    assert {pid for pid, _ in thr} == {"a", "b"}

    topk = cosine_topk(query, id_to_vec, k=1)
    assert len(topk) == 1
    assert topk[0][0] == "a"


def test_cosine_topk_query_none_or_empty():
    assert cosine_topk(None, {"a": np.array([1.0])}) == []
    assert cosine_topk(np.array([], dtype=np.float32), {"a": np.array([1.0])}) == []


def test_cosine_topk_all_invalid_returns_empty():
    query = np.array([1.0, 0.0], dtype=np.float32)
    out = cosine_topk(query, {"x": None, "y": np.array([1.0, 0.0, 0.0])})
    assert out == []


# ----------------------------------------------------------------------
# text_for_embedding
# ----------------------------------------------------------------------


class _PaperLike:
    def __init__(self, title=None, abstract=None):
        self.title = title
        self.abstract = abstract


def test_text_for_embedding_orm_title_and_abstract():
    p = _PaperLike(title="A   Title", abstract="An  abstract\n\nhere")
    out = text_for_embedding(p)
    # Whitespace collapsed; title + abstract joined.
    assert out == "A Title An abstract here"


def test_text_for_embedding_title_only():
    assert text_for_embedding(_PaperLike(title="Only title")) == "Only title"


def test_text_for_embedding_abstract_only():
    assert text_for_embedding(_PaperLike(abstract="Only abstract")) == "Only abstract"


def test_text_for_embedding_dict_input():
    out = text_for_embedding({"title": "T", "abstract": "A"})
    assert out == "T A"


def test_text_for_embedding_empty_and_none():
    assert text_for_embedding(_PaperLike()) == ""
    assert text_for_embedding({}) == ""
    assert text_for_embedding(_PaperLike(title="", abstract="")) == ""


# ----------------------------------------------------------------------
# Import safety
# ----------------------------------------------------------------------


def test_module_import_safe_without_key(monkeypatch):
    """Importing the module must not require a key or eagerly import the SDK."""
    for var in ("VERTEX_AI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    es._reset_client_for_tests()
    import importlib

    mod = importlib.reload(es)
    assert mod.EMBED_MODEL  # constants present
    assert mod.get_embedding_client() is None  # no key -> None, no raise
