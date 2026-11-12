"""Unit tests for services.pdf_page_cache.extract_paragraph_anchors.

The function is pure and stdlib-only, so the test suite needs no DB
fixture, no PyMuPDF, no LLM mock — straight input/output assertions
against the algorithm in 05-tech-arch/05 §2-§6.
"""
from __future__ import annotations

import hashlib

from services.pdf_page_cache import (
    PREFIX_SUFFIX_LEN,
    extract_paragraph_anchors,
    _normalize_opening,
)


# ---------------------------------------------------------------------- #
# Step 1 — segmentation
# ---------------------------------------------------------------------- #

def test_splits_on_blank_lines():
    page = (
        "We extended prior work by combining the H/V ratio with array "
        "beamforming. The result is a hybrid method.\n\n"
        "Synthetic tests recover the input model to 5% RMS. Real-data "
        "tests follow in Section 4.\n\n"
        "The misfit is dominated by shallow heterogeneity. We mitigate "
        "with a 2-D regularisation."
    )
    out = extract_paragraph_anchors(page)
    assert len(out) == 3
    assert [a["para_index"] for a in out] == [0, 1, 2]


def test_empty_input_returns_empty():
    assert extract_paragraph_anchors("") == []
    assert extract_paragraph_anchors("   \n\n  ") == []


def test_tolerates_trailing_whitespace_in_separator():
    page = "First paragraph here.\n   \n\nSecond paragraph here. It has a sentence."
    out = extract_paragraph_anchors(page)
    assert len(out) == 2


# ---------------------------------------------------------------------- #
# Step 2 — discard non-paragraphs
# ---------------------------------------------------------------------- #

def test_drops_figure_captions():
    page = (
        "Figure 1. H/V spectral ratio at station ANMO. The peak at 0.5 Hz "
        "indicates a sediment-basement interface.\n\n"
        "We extended prior work by combining the H/V ratio with array "
        "beamforming. The result is hybrid."
    )
    out = extract_paragraph_anchors(page)
    assert len(out) == 1
    assert out[0]["opening_sentence"].startswith("We extended prior work")


def test_drops_table_label():
    page = "Table 2: Site response amplitudes.\n\nThe peak at 0.5 Hz indicates a sediment-basement interface. We extended prior work."
    out = extract_paragraph_anchors(page)
    assert len(out) == 1


def test_drops_page_number():
    page = "42\n\nWe extended prior work by combining the H/V ratio. The result is a hybrid method."
    out = extract_paragraph_anchors(page)
    assert len(out) == 1


def test_drops_table_fragment_by_tab_density():
    page = "col1\tcol2\tcol3\nrow1\t1\t2\nrow2\t3\t4\n\nWe extended prior work. The result is hybrid."
    out = extract_paragraph_anchors(page)
    assert len(out) == 1


def test_drops_chunk_with_no_prose_token():
    page = "(1)\n\nWe extended prior work by combining the H/V ratio. The result is hybrid."
    out = extract_paragraph_anchors(page)
    assert len(out) == 1


# ---------------------------------------------------------------------- #
# Step 3 — opening-sentence extraction
# ---------------------------------------------------------------------- #

def test_opening_sentence_stops_at_first_period():
    page = "We extended prior work. Synthetic tests recover the input."
    out = extract_paragraph_anchors(page)
    assert out[0]["opening_sentence"] == "We extended prior work."


def test_opening_sentence_handles_line_wrap():
    page = "We extended prior work by combining\nthe H/V ratio with array beamforming. Next sentence here."
    out = extract_paragraph_anchors(page)
    assert "combining" in out[0]["opening_sentence"]
    assert "Next sentence" not in out[0]["opening_sentence"]


def test_opening_sentence_caps_at_200_chars():
    long_first = "x" * 400 + " B"
    out = extract_paragraph_anchors(long_first + "\n\nSecond paragraph here. With prose.")
    assert len(out[0]["opening_sentence"]) == 200


def test_opening_sentence_fallback_when_no_terminator():
    page = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do"
    out = extract_paragraph_anchors(page)
    assert out[0]["opening_sentence"] == page


# ---------------------------------------------------------------------- #
# Step 4 + 5 — normalize + SHA
# ---------------------------------------------------------------------- #

def test_normalize_is_idempotent():
    raw = "We Extended Prior Work."
    once = _normalize_opening(raw)
    twice = _normalize_opening(once)
    assert once == twice == "we extended prior work"


def test_normalize_collapses_ligatures():
    assert _normalize_opening("efﬁcient") == "efficient"
    assert _normalize_opening("ﬂux") == "flux"


def test_sha_matches_normalized_opening():
    page = "We extended prior work. Next."
    out = extract_paragraph_anchors(page)
    expected = hashlib.sha1(
        out[0]["opening_sentence_norm"].encode("utf-8")
    ).hexdigest()
    assert out[0]["sha"] == expected
    assert len(out[0]["sha"]) == 40  # SHA-1 hex


def test_same_input_same_sha():
    page = "We extended prior work by combining H/V. Synthetic tests recover the input."
    a = extract_paragraph_anchors(page)
    b = extract_paragraph_anchors(page)
    assert a[0]["sha"] == b[0]["sha"]


# ---------------------------------------------------------------------- #
# §6 — Hypothesis prefix32 / suffix32 fallback
# ---------------------------------------------------------------------- #

def test_prefix_suffix_lengths():
    p1 = "A" * 100 + "."
    p2 = "We extended prior work. Next sentence here for prose content."
    p3 = "B" * 100 + "."
    page = "\n\n".join((p1, p2, p3))
    out = extract_paragraph_anchors(page)
    middle = out[1]
    assert len(middle["prefix32"]) == PREFIX_SUFFIX_LEN
    assert len(middle["suffix32"]) == PREFIX_SUFFIX_LEN


def test_prefix_suffix_at_page_edges():
    page = "We extended prior work. Next sentence."
    out = extract_paragraph_anchors(page)
    assert out[0]["prefix32"] == ""
    assert out[0]["suffix32"] == ""


def test_char_offset_locates_paragraph_in_page():
    page = "Header noise.\n\nWe extended prior work. Next sentence."
    out = extract_paragraph_anchors(page)
    assert len(out) == 2
    assert page[out[1]["char_offset"]:].startswith("We extended prior work.")


def test_kept_para_index_is_sequential_after_skips():
    page = (
        "Figure 1. A caption.\n\n"
        "First real paragraph here. With prose.\n\n"
        "(1)\n\n"
        "Second real paragraph here. With more prose."
    )
    out = extract_paragraph_anchors(page)
    assert [a["para_index"] for a in out] == [0, 1]
    assert out[0]["opening_sentence"].startswith("First real paragraph")
    assert out[1]["opening_sentence"].startswith("Second real paragraph")
