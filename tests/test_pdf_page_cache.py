"""Unit tests for PR-18 paragraph lookup helpers.

These tests stub PaperPageCache via a tiny in-memory `db` shim — no
SQLAlchemy session needed, no PyMuPDF needed (the upstream extractor
from PR-16 is not exercised here; PR-18 reads from already-shaped
`pages` JSON).
"""

from types import SimpleNamespace

from services.pdf_page_cache import (
    DEFAULT_MIN_SCORE,
    find_paragraph_by_opening_sentence,
    verify_anchor,
)


class _StubDB:
    """Minimal SQLAlchemy-session-shaped shim. Only `.get()` is called."""

    def __init__(self, cache):
        self._cache = cache

    def get(self, model, pk):  # pylint: disable=unused-argument
        return self._cache


def _make_cache(pages):
    return SimpleNamespace(pages=pages)


# --- verify_anchor --------------------------------------------------------

def test_verify_anchor_exact_match_returns_true():
    cache = _make_cache([
        {"page": 5, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "we deployed 42 broadband stations"},
            {"para_index": 1, "opening_sentence_norm": "the back arc array operated for 18 months"},
        ]},
    ])
    assert verify_anchor("p1", 5, 1, "The back-arc array operated for 18 months.", _StubDB(cache))


def test_verify_anchor_prefix_match_tolerates_paraphrase():
    cache = _make_cache([
        {"page": 3, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "we extended prior work by combining the h v ratio with array beamforming for crustal imaging"},
        ]},
    ])
    # LLM emitted a 60+ char prefix that matches; tail differs.
    assert verify_anchor("p1", 3, 0, "We extended prior work by combining the H V ratio with array beamforming and totally other words", _StubDB(cache))


def test_verify_anchor_wrong_paragraph_index_returns_false():
    cache = _make_cache([
        {"page": 5, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "we deployed 42 broadband stations"},
            {"para_index": 1, "opening_sentence_norm": "the back arc array operated for 18 months"},
        ]},
    ])
    assert not verify_anchor("p1", 5, 1, "We deployed 42 broadband stations.", _StubDB(cache))


def test_verify_anchor_missing_page_returns_false():
    cache = _make_cache([{"page": 1, "paragraphs": []}])
    assert not verify_anchor("p1", 9, 0, "anything", _StubDB(cache))


def test_verify_anchor_no_cache_returns_false():
    assert not verify_anchor("p1", 1, 0, "anything", _StubDB(None))


def test_verify_anchor_out_of_range_para_returns_false():
    cache = _make_cache([{"page": 1, "paragraphs": [{"opening_sentence_norm": "x"}]}])
    assert not verify_anchor("p1", 1, 5, "x", _StubDB(cache))


def test_verify_anchor_empty_inputs_return_false():
    cache = _make_cache([{"page": 1, "paragraphs": [{"opening_sentence_norm": "x"}]}])
    assert not verify_anchor("p1", 1, 0, "", _StubDB(cache))
    assert not verify_anchor("p1", 0, 0, "x", _StubDB(cache))
    assert not verify_anchor("p1", 1, -1, "x", _StubDB(cache))


# --- find_paragraph_by_opening_sentence ----------------------------------

def test_find_paragraph_returns_best_match_with_score():
    cache = _make_cache([
        {"page": 5, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "we deployed 42 broadband stations across the back arc",
             "opening_sentence": "We deployed 42 broadband stations across the back-arc.",
             "text": "We deployed 42 broadband stations across the back-arc."},
        ]},
    ])
    hit = find_paragraph_by_opening_sentence(
        "p1", 5, "We deployed 42 broadband stations across the back-arc.", _StubDB(cache)
    )
    assert hit is not None
    assert hit["page"] == 5
    assert hit["para_index"] == 0
    assert hit["score"] >= 0.99


def test_find_paragraph_walks_outward_from_hint():
    cache = _make_cache([
        {"page": 4, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "the back arc array operated for 18 months",
             "opening_sentence": "The back-arc array operated for 18 months.",
             "text": "The back-arc array operated for 18 months."},
        ]},
        {"page": 5, "paragraphs": []},
    ])
    hit = find_paragraph_by_opening_sentence(
        "p1", 5, "The back-arc array operated for 18 months.", _StubDB(cache)
    )
    assert hit is not None
    assert hit["page"] == 4


def test_find_paragraph_substring_bonus_for_mid_paragraph_quote():
    cache = _make_cache([
        {"page": 7, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "introduction",
             "opening_sentence": "Introduction.",
             "text": "Introduction. The unique signature of slow slip events lies in their low-frequency tremor envelope."},
        ]},
    ])
    hit = find_paragraph_by_opening_sentence(
        "p1", 7, "The unique signature of slow slip events lies in their low-frequency tremor envelope.", _StubDB(cache)
    )
    assert hit is not None
    assert hit["score"] >= 0.92


def test_find_paragraph_below_threshold_returns_none():
    cache = _make_cache([
        {"page": 1, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "we deployed 42 broadband stations",
             "opening_sentence": "We deployed 42 broadband stations.",
             "text": "We deployed 42 broadband stations."},
        ]},
    ])
    hit = find_paragraph_by_opening_sentence(
        "p1", 1, "Quantum chromodynamics in the early universe.", _StubDB(cache),
        min_score=DEFAULT_MIN_SCORE,
    )
    assert hit is None


def test_find_paragraph_none_hint_scans_natural_order():
    cache = _make_cache([
        {"page": 1, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "alpha",
             "opening_sentence": "Alpha.", "text": "Alpha sentence one."},
        ]},
        {"page": 2, "paragraphs": [
            {"para_index": 0, "opening_sentence_norm": "beta gamma delta epsilon",
             "opening_sentence": "Beta gamma delta epsilon.",
             "text": "Beta gamma delta epsilon — the canonical test phrase."},
        ]},
    ])
    hit = find_paragraph_by_opening_sentence(
        "p1", None, "Beta gamma delta epsilon", _StubDB(cache)
    )
    assert hit is not None
    assert hit["page"] == 2


def test_find_paragraph_empty_inputs_return_none():
    cache = _make_cache([{"page": 1, "paragraphs": []}])
    assert find_paragraph_by_opening_sentence("p1", 1, "", _StubDB(cache)) is None
    assert find_paragraph_by_opening_sentence("p1", 1, "x", _StubDB(None)) is None
