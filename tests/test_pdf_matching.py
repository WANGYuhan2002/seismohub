"""Unit tests for the title-similarity matcher (上传遗失 PDF, 2026-06-16).

Pure-function tests (no DB, no PDF). The DOI exact-match + endpoint wiring live
in main.py and are exercised separately; these lock the fuzzy-fallback bar that
owner set: >=0.92 unique → auto, mid/ambiguous → confirm, <0.75 → none.
"""
from services.pdf_matching import (
    TITLE_MATCH_AUTO,
    TITLE_MATCH_MIN,
    classify_title_match,
    normalize_title,
    rank_title_candidates,
    title_similarity,
)


def test_normalize_strips_punctuation_and_case():
    assert normalize_title("Full-Waveform Inversion: A Review!") == "full waveform inversion a review"
    assert normalize_title(None) == ""
    assert normalize_title("   ") == ""


def test_similarity_identical_variant_and_unrelated():
    assert title_similarity("Full Waveform Inversion", "full-waveform inversion") > 0.9
    assert title_similarity("anything", "") == 0.0
    assert title_similarity("DAS fibre-optic seismology", "machine learning earthquake catalogs") < 0.5


def test_rank_filters_below_floor_and_sorts_desc():
    papers = [
        ("p1", "Full Waveform Inversion of the Andes"),
        ("p2", "Ambient noise tomography of the crust"),
        ("p3", "Full-waveform inversion of the Andes!"),
    ]
    out = rank_title_candidates("Full Waveform Inversion of the Andes", papers)
    assert out, "expected at least one candidate"
    assert out[0][1] >= TITLE_MATCH_AUTO
    assert out[0][0] in ("p1", "p3")
    assert all(score >= TITLE_MATCH_MIN for _, score in out)
    assert "p2" not in [pid for pid, _ in out]  # unrelated filtered out
    # descending order
    assert out == sorted(out, key=lambda x: x[1], reverse=True)


def test_rank_empty_title_returns_empty():
    assert rank_title_candidates("", [("p1", "whatever")]) == []
    assert rank_title_candidates("   ", [("p1", "whatever")]) == []


def test_classify_auto_unique_high():
    assert classify_title_match([("p1", 0.96)]) == "auto"
    assert classify_title_match([("p1", 0.95), ("p2", 0.80)]) == "auto"  # runner-up below AUTO


def test_classify_confirm_ambiguous_two_high():
    assert classify_title_match([("p1", 0.95), ("p2", 0.93)]) == "confirm"


def test_classify_confirm_mid_confidence():
    assert classify_title_match([("p1", 0.82)]) == "confirm"


def test_classify_none_when_empty():
    assert classify_title_match([]) == "none"
