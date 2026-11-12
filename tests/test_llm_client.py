"""Tests for ``services/llm_client.py`` (was 40 % coverage).

Covers PR-06 (``_last_model_used`` honesty contract) and the BRIEF-C model
badge normaliser used by the QA dialog. No real Gemini calls — the
``GeminiClient._ensure_init`` step is short-circuited and
``client.models.generate_content`` is replaced with a stub so we can
deterministically exercise both the happy path and the fallback branch.

Coverage targets:
- ``display_name_for_model``: known wire IDs → marketing names; unknown
  wire IDs → title-cased hyphen-split fallback that still renders.
- ``_inc_counter`` / ``llm_call_count`` increment monotonically across
  ``chat()`` calls and span the lock.
- ``Message`` validator rejects roles other than ``user`` / ``model``.
- ``GeminiClient.chat`` writes ``_last_model_used`` on BOTH the happy
  path AND when the primary raises and we fall back (PR-06 contract).
- ``get_llm_client`` returns a shared instance, rejects unknown backends.
"""

from __future__ import annotations

import threading
from typing import Any, List
from unittest.mock import MagicMock

import pytest

import services.llm_client as llm_client
from services.llm_client import (
    GeminiClient,
    LLMClient,
    MODEL_DISPLAY_NAMES,
    Message,
    display_name_for_model,
    get_llm_client,
    llm_call_count,
)


# ----------------------------------------------------------------------
# display_name_for_model
# ----------------------------------------------------------------------


def test_display_name_known_gemini_3_1_pro():
    assert display_name_for_model("gemini-3.1-pro-preview") == "Gemini 3.1 Pro"


def test_display_name_known_gemini_2_5_pro():
    assert display_name_for_model("gemini-2.5-pro") == "Gemini 2.5 Pro"


def test_display_name_known_gemini_2_0_flash():
    assert display_name_for_model("gemini-2.0-flash") == "Gemini 2.0 Flash"


def test_display_name_known_claude_opus_4_7():
    assert display_name_for_model("claude-opus-4-7") == "Claude Opus 4.7"


def test_display_name_known_gpt_5_5_thinking():
    assert display_name_for_model("gpt-5.5-thinking") == "GPT-5.5 Thinking"


def test_display_name_empty_returns_unknown_placeholder():
    assert display_name_for_model("") == "Unknown model"


def test_display_name_unknown_id_falls_back_to_title_case():
    """A brand-new preview ID not yet in MODEL_DISPLAY_NAMES still renders."""
    out = display_name_for_model("gemini-4.0-ultra")
    assert "Gemini" in out
    assert "4.0" in out
    assert "Ultra" in out


def test_display_name_unknown_gpt_special_cases_uppercased():
    """The fallback path uppercases 'gpt' → 'GPT' because title-casing
    'Gpt' would render incorrectly in the pill."""
    out = display_name_for_model("gpt-6-mini")
    assert "GPT" in out
    assert "Gpt" not in out


def test_display_name_unknown_ai_special_cased():
    out = display_name_for_model("ai-bot-v2")
    assert "AI" in out
    assert "Ai " not in out


def test_display_name_for_every_known_key_in_dict_is_consistent():
    """Sanity check: every entry in MODEL_DISPLAY_NAMES round-trips."""
    for wire, marketing in MODEL_DISPLAY_NAMES.items():
        assert display_name_for_model(wire) == marketing


# ----------------------------------------------------------------------
# Call counter (PR-06 / G7)
# ----------------------------------------------------------------------


def test_inc_counter_monotonic_and_thread_safe():
    """Concurrent ``_inc_counter`` calls must never lose increments."""
    start = llm_call_count()
    NUM_THREADS = 8
    PER_THREAD = 50
    threads: List[threading.Thread] = []
    for _ in range(NUM_THREADS):
        threads.append(threading.Thread(target=lambda: [llm_client._inc_counter() for _ in range(PER_THREAD)]))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    end = llm_call_count()
    assert end - start == NUM_THREADS * PER_THREAD


# ----------------------------------------------------------------------
# Message dataclass
# ----------------------------------------------------------------------


def test_message_accepts_user_role():
    m = Message("user", "Hello")
    assert m.role == "user"
    assert m.text == "Hello"


def test_message_accepts_model_role():
    m = Message("model", "Hi")
    assert m.role == "model"
    assert m.text == "Hi"


def test_message_rejects_invalid_role():
    with pytest.raises(ValueError, match="role must be"):
        Message("system", "anything")
    with pytest.raises(ValueError):
        Message("assistant", "anything")
    with pytest.raises(ValueError):
        Message("", "x")


def test_message_coerces_text_to_string():
    m = Message("user", 42)  # type: ignore[arg-type]
    assert m.text == "42"


# ----------------------------------------------------------------------
# GeminiClient.chat — happy path + fallback (the PR-06 contract)
# ----------------------------------------------------------------------


def _make_stub_response(text: str = "stubbed reply") -> Any:
    """Build a minimal object that satisfies ``GeminiClient.chat`` extraction logic."""
    resp = MagicMock()
    resp.text = text
    return resp


def _patch_gemini_with_response(monkeypatch, response, fallback_response=None):
    """Bypass ``_ensure_init`` and inject a stub client that records the
    model name passed to each ``generate_content`` invocation."""
    fake_client = MagicMock()

    if fallback_response is None:
        fake_client.models.generate_content.return_value = response
    else:
        # First call returns the primary response or raises; the call site
        # decides via ``side_effect``.
        pass
    monkeypatch.setattr(
        GeminiClient, "_ensure_init", lambda self: fake_client
    )

    # Stub the gtypes import inside chat() — we only need a no-op factory
    # for the GenerateContentConfig.
    fake_gtypes = MagicMock()
    fake_gtypes.GenerateContentConfig = lambda **kw: kw
    monkeypatch.setitem(
        __import__("sys").modules, "google.genai.types", fake_gtypes
    )
    return fake_client


def test_gemini_client_records_primary_model_on_happy_path(monkeypatch):
    client = GeminiClient(model_name="gemini-3.1-pro-preview")
    fake = _patch_gemini_with_response(monkeypatch, _make_stub_response("hi"))
    out = client.chat([Message("user", "hello")])
    assert out == "hi"
    assert client._last_model_used == "gemini-3.1-pro-preview"
    # The primary was the only model requested
    call = fake.models.generate_content.call_args
    assert call.kwargs["model"] == "gemini-3.1-pro-preview"


def test_gemini_client_falls_back_and_records_fallback_model(monkeypatch):
    """When the primary raises, the fallback model must be tried AND
    ``_last_model_used`` must reflect the fallback (PR-06 honesty)."""
    client = GeminiClient(model_name="gemini-3.1-pro-preview")
    fake = MagicMock()
    monkeypatch.setattr(GeminiClient, "_ensure_init", lambda self: fake)
    # Side-effect: first call raises, second returns
    fake.models.generate_content.side_effect = [
        RuntimeError("primary 429"),
        _make_stub_response("fallback answer"),
    ]
    fake_gtypes = MagicMock()
    fake_gtypes.GenerateContentConfig = lambda **kw: kw
    monkeypatch.setitem(
        __import__("sys").modules, "google.genai.types", fake_gtypes
    )
    out = client.chat([Message("user", "hello")])
    assert out == "fallback answer"
    assert client._last_model_used == GeminiClient.FALLBACK_MODEL
    # Both attempts happened
    assert fake.models.generate_content.call_count == 2
    second_call = fake.models.generate_content.call_args_list[1]
    assert second_call.kwargs["model"] == GeminiClient.FALLBACK_MODEL


def test_gemini_client_prepends_system_as_synthetic_user_model_turns(monkeypatch):
    client = GeminiClient(model_name="gemini-3.1-pro-preview")
    fake = _patch_gemini_with_response(monkeypatch, _make_stub_response())
    client.chat([Message("user", "question")], system="You are a researcher.")
    call = fake.models.generate_content.call_args
    contents = call.kwargs["contents"]
    # First two turns are the synthetic system pair (user / model)
    assert contents[0]["role"] == "user"
    assert "researcher" in contents[0]["parts"][0]["text"]
    assert contents[1]["role"] == "model"
    # Then the real user turn
    assert contents[2]["role"] == "user"
    assert contents[2]["parts"][0]["text"] == "question"


def test_gemini_client_increments_call_counter(monkeypatch):
    """A successful chat() bumps the global LLM call counter."""
    client = GeminiClient(model_name="gemini-2.5-pro")
    _patch_gemini_with_response(monkeypatch, _make_stub_response())
    before = llm_call_count()
    client.chat([Message("user", "x")])
    after = llm_call_count()
    assert after == before + 1


def test_gemini_client_extracts_text_from_candidates_when_resp_text_missing(monkeypatch):
    """The robust extraction path: if resp.text is empty, fall back to
    ``candidates[0].content.parts[*].text``."""
    client = GeminiClient(model_name="gemini-3.1-pro-preview")
    resp = MagicMock()
    resp.text = ""
    part1 = MagicMock()
    part1.text = "first"
    part2 = MagicMock()
    part2.text = "second"
    resp.candidates = [MagicMock(content=MagicMock(parts=[part1, part2]))]
    _patch_gemini_with_response(monkeypatch, resp)
    out = client.chat([Message("user", "x")])
    assert "first" in out and "second" in out


def test_gemini_client_returns_empty_string_when_no_text_extractable(monkeypatch):
    client = GeminiClient(model_name="gemini-3.1-pro-preview")
    resp = MagicMock()
    resp.text = ""
    resp.candidates = []
    _patch_gemini_with_response(monkeypatch, resp)
    out = client.chat([Message("user", "x")])
    assert out == ""


def test_gemini_client_initial_last_model_used_matches_model_name():
    """Before any chat() call the field must be plausible — see comment in
    GeminiClient.__init__ — so badge readers don't see ``None``."""
    client = GeminiClient(model_name="gemini-2.5-pro")
    assert client._last_model_used == "gemini-2.5-pro"


# ----------------------------------------------------------------------
# get_llm_client (factory)
# ----------------------------------------------------------------------


def test_get_llm_client_default_returns_gemini():
    """No backend arg + no LLM_BACKEND env var → 'gemini' default."""
    # Clear cached instances so the test doesn't depend on prior runs
    with llm_client._shared_lock:
        llm_client._shared.clear()
    c = get_llm_client()
    assert isinstance(c, GeminiClient)
    assert isinstance(c, LLMClient)


def test_get_llm_client_explicit_gemini():
    with llm_client._shared_lock:
        llm_client._shared.clear()
    c = get_llm_client("gemini")
    assert isinstance(c, GeminiClient)


def test_get_llm_client_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown LLM_BACKEND"):
        get_llm_client("not-a-real-backend")


def test_get_llm_client_caches_shared_instance():
    """Repeated calls with the same key return the SAME object (not new)."""
    with llm_client._shared_lock:
        llm_client._shared.clear()
    a = get_llm_client("gemini")
    b = get_llm_client("gemini")
    assert a is b


def test_get_llm_client_respects_env_var(monkeypatch):
    with llm_client._shared_lock:
        llm_client._shared.clear()
    monkeypatch.setenv("LLM_BACKEND", "gemini")
    c = get_llm_client()
    assert isinstance(c, GeminiClient)
