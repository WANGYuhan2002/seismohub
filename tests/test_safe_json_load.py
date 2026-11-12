"""Regression tests for analyze_service.safe_json_load.

2026-07-10: gemini-3.1-pro-preview intermittently emits a valid analysis object
followed by a STRAY trailing '}' (or prose). The old greedy `{.*}` span reached
the last brace and swallowed the junk, so json.loads rejected an otherwise
perfect analysis and every such paper fell back to an empty result. The parser
now extracts the first BALANCED object.
"""
import os

os.environ.setdefault("SEISMO_NO_AUTOFETCH", "1")
os.environ.setdefault("SEISMO_DB_PATH", "/tmp/test_safe_json_load.db")

import pytest

from services.analyze_service import safe_json_load, _extract_balanced_json


def test_stray_trailing_brace_is_dropped():
    # the exact failure shape: complete object + one extra closing brace
    assert safe_json_load('{"a": 1, "b": {"c": 2}}\n}') == {"a": 1, "b": {"c": 2}}


def test_trailing_prose_is_ignored():
    assert safe_json_load('{"ok": true}\n\nNote: analysis complete.') == {"ok": True}


def test_leading_prose_is_ignored():
    assert safe_json_load('Here is the JSON:\n{"x": 5}') == {"x": 5}


def test_markdown_fence_is_stripped():
    assert safe_json_load('```json\n{"a": 1, "b": [2, 3]}\n```') == {"a": 1, "b": [2, 3]}


def test_brace_inside_string_literal_not_miscounted():
    # a '}' inside a string must not close the object early
    assert safe_json_load('{"note": "close } brace", "n": 7}')["n"] == 7


def test_escaped_quote_inside_string():
    assert safe_json_load('{"q": "a \\" b } c", "n": 3}')["n"] == 3


def test_nested_objects():
    assert safe_json_load('{"x": {"y": {"z": 1}}}')["x"]["y"]["z"] == 1


def test_balanced_extractor_returns_none_on_truncation():
    # genuinely truncated (no matching close) -> no complete object
    assert _extract_balanced_json('{"a": 1, "b": [1, 2, 3') is None


def test_no_object_raises():
    with pytest.raises(ValueError):
        safe_json_load("no json here at all")
