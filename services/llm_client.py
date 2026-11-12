"""Multi-LLM router (M2 G7).

Provides a thin ``LLMClient`` ABC and a ``GeminiClient`` concrete
implementation so new features (G5 QA chat, future Claude/GPT support)
share a consistent calling convention.

Usage::

    from services.llm_client import get_llm_client, Message

    client = get_llm_client()          # honours LLM_BACKEND env var
    reply = client.chat(
        [Message("user", "What is the main finding?")],
        system="You are a helpful research assistant.",
    )

The existing ``services/analyze_service.py`` keeps its own Gemini wiring
untouched for now; this module is used by ``qa_service.py`` and any future
feature that needs multi-turn conversation or wants to swap backends.

Supported backends
------------------
- ``"gemini"`` (default) — ``google.generativeai``, model from
  ``GEMINI_MODEL`` env var (default ``gemini-2.0-flash``).

Telemetry
---------
Every ``chat()`` call increments ``_call_counter`` (accessible via
``llm_call_count()``).  The counter resets on process restart and is
intentionally *not* persisted to keep G7 MVP simple.  A future revision
can push it to ``AppState`` or Prometheus.
"""

from __future__ import annotations

import os
import re
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

__all__ = [
    "Message",
    "LLMClient",
    "GeminiClient",
    "get_llm_client",
    "llm_call_count",
    "MODEL_DISPLAY_NAMES",
    "display_name_for_model",
]


# ---------------------------------------------------------------------------
# Display-name normalisation (BRIEF C — QA model identity badge)
# ---------------------------------------------------------------------------
#
# Wire IDs like ``gemini-3.1-pro-preview`` are not user-facing labels.  The
# dict below maps each wire ID to the canonical marketing name vendors use in
# their own UIs (see 04-feature-design/11-qa-model-badge.md §2 for the source
# survey).  Unknown IDs fall back through ``display_name_for_model()`` to a
# title-cased, hyphen-split rendering so a new preview model that ships before
# the dict is updated still renders a non-empty pill.

MODEL_DISPLAY_NAMES: Dict[str, str] = {
    # Gemini family
    "gemini-3.8-flash":         "Gemini 3.8 Flash",
    "gemini-3.5-flash":         "Gemini 3.5 Flash",
    "gemini-3.5-flash-lite":    "Gemini 3.5 Flash-Lite",
    "gemini-3.1-pro-preview":   "Gemini 3.1 Pro",
    "gemini-3.1-flash-preview": "Gemini 3.1 Flash",
    "gemini-2.5-pro":           "Gemini 2.5 Pro",
    "gemini-2.5-flash":         "Gemini 2.5 Flash",
    "gemini-2.0-flash":         "Gemini 2.0 Flash",
    # Future Claude / OpenAI entries land here when LLM_BACKEND grows
    "claude-sonnet-4-6":        "Claude Sonnet 4.6",
    "claude-opus-4-7":          "Claude Opus 4.7",
    "gpt-5.5-instant":          "GPT-5.5 Instant",
    "gpt-5.5-thinking":         "GPT-5.5 Thinking",
}


def display_name_for_model(model_id: str) -> str:
    """Return a human-friendly label for *model_id*.

    Unknown IDs fall back to a title-cased, hyphen-split rendering so the
    pill never appears blank.  E.g. ``gemini-4.0-ultra`` -> ``Gemini 4.0 Ultra``;
    ``gpt-6-mini`` -> ``GPT 6 Mini``.
    """
    if not model_id:
        return "Unknown model"
    if model_id in MODEL_DISPLAY_NAMES:
        return MODEL_DISPLAY_NAMES[model_id]
    parts = [p.capitalize() for p in re.split(r"[-_]", model_id) if p]
    return " ".join(parts).replace("Gpt", "GPT").replace("Ai", "AI")


# ---------------------------------------------------------------------------
# Simple call counter (G7 telemetry — in-process only)
# ---------------------------------------------------------------------------

_call_counter: int = 0
_counter_lock = threading.Lock()


def _inc_counter() -> int:
    global _call_counter
    with _counter_lock:
        _call_counter += 1
        return _call_counter


def llm_call_count() -> int:
    """Return the total number of LLM chat() calls since process start."""
    return _call_counter


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Message:
    """Immutable (role, text) pair representing one conversation turn.

    ``role`` must be ``"user"`` or ``"model"`` — the two roles Gemini
    recognises.  Future backends may translate ``"model"`` to ``"assistant"``
    internally.
    """

    __slots__ = ("role", "text")

    def __init__(self, role: str, text: str) -> None:
        if role not in {"user", "model"}:
            raise ValueError(f"role must be 'user' or 'model', got {role!r}")
        self.role = role
        self.text = str(text)

    def __repr__(self) -> str:  # pragma: no cover
        snippet = self.text[:60].replace("\n", " ")
        return f"Message(role={self.role!r}, text={snippet!r})"


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class LLMClient(ABC):
    """Base interface every LLM backend must implement."""

    @abstractmethod
    def chat(
        self,
        history: List[Message],
        *,
        system: str = "",
        temperature: float = 0.4,
        max_output_tokens: int = 2048,
    ) -> str:
        """Send *history* (list of user/model turns) and return the reply.

        Args:
            history: Conversation turns in chronological order.  The last
                entry should be the latest user message.
            system: Optional system-level instruction prepended before the
                conversation turns.
            temperature: Sampling temperature (0 = deterministic).
            max_output_tokens: Hard cap on the response length.

        Returns:
            The assistant's reply as a plain string (stripped of leading /
            trailing whitespace).
        """


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------


class GeminiClient(LLMClient):
    """Wraps ``google.genai`` (Vertex AI) for multi-turn chat.

    Uses the same ``VERTEX_AI_API_KEY`` / ``VERTEX_AI_PROJECT`` credentials
    as ``analyze_service.py`` so no extra API key is needed.
    Falls back to ``GEMINI_API_KEY`` direct mode when Vertex creds are absent.
    """

    # Same primary as analyze_service.py's PDF path.
    # 2026-10-16: Gemini 2.5 retires on Vertex 2026-10-20, so the fallback moves
    # from gemini-2.5-pro to gemini-3.8-flash (GA, retires >= 2027-09-02). The
    # Pro-tier GA successor gemini-4-argon 404s through this API-key client (it
    # routes to europe-west1, where Argon is not served), so it cannot be used here.
    PRIMARY_MODEL = "gemini-3.1-pro-preview"
    FALLBACK_MODEL = "gemini-3.8-flash"

    def __init__(self, model_name: Optional[str] = None) -> None:
        self._model_name: str = model_name or os.environ.get(
            "GEMINI_MODEL", self.PRIMARY_MODEL
        )
        # Written by chat() after every call so callers see the ID that
        # actually produced the reply.  This is the *honest* model name —
        # the primary if it succeeded, ``FALLBACK_MODEL`` if the primary
        # raised and the fallback path fired.  Initialised to ``_model_name``
        # so a caller that reads it before the first chat() still gets a
        # plausible value.
        self._last_model_used: str = self._model_name
        self._client: Any = None  # populated lazily in _ensure_init()

    def _ensure_init(self) -> Any:
        if self._client is not None:
            return self._client

        from google import genai  # type: ignore
        from google.genai import types as _gtypes  # noqa: F401 — ensure package present

        vertex_key = (os.environ.get("VERTEX_AI_API_KEY") or "").strip()
        project = (os.environ.get("VERTEX_AI_PROJECT") or "").strip()
        location = (os.environ.get("VERTEX_AI_LOCATION") or "global").strip()

        if vertex_key:
            if project:
                os.environ["GOOGLE_CLOUD_PROJECT"] = project
            os.environ["GOOGLE_CLOUD_LOCATION"] = location
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
            self._client = genai.Client(vertexai=True, api_key=vertex_key)
        else:
            # Fallback: direct Gemini API key mode
            gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
            self._client = genai.Client(api_key=gemini_key)

        return self._client

    def chat(
        self,
        history: List[Message],
        *,
        system: str = "",
        temperature: float = 0.4,
    ) -> str:
        from google.genai import types as gtypes  # type: ignore

        client = self._ensure_init()

        # Build contents list — prepend system as a synthetic user/model exchange
        contents: List[Dict[str, Any]] = []
        if system:
            contents.append({"role": "user", "parts": [{"text": system}]})
            contents.append(
                {"role": "model", "parts": [{"text": "Understood. I'll follow those instructions."}]}
            )
        for msg in history:
            contents.append({"role": msg.role, "parts": [{"text": msg.text}]})

        config = gtypes.GenerateContentConfig(
            temperature=temperature,
        )
        # Try primary model; fall back to FALLBACK_MODEL on any error.
        # IMPORTANT: ``_last_model_used`` MUST be written in BOTH branches so
        # the badge surfaced upstream (qa_service -> main.py -> template) is
        # the model that actually answered, not the one we *intended* to call.
        # Without the except-branch write the badge would lie whenever the
        # primary model rate-limited / 5xx'd — see
        # 04-feature-design/11-qa-model-badge.md §6 finding.
        try:
            resp = client.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=config,
            )
            self._last_model_used = self._model_name
        except Exception:
            resp = client.models.generate_content(
                model=self.FALLBACK_MODEL,
                contents=contents,
                config=config,
            )
            self._last_model_used = self.FALLBACK_MODEL
        _inc_counter()
        # Extract text robustly
        if hasattr(resp, "text") and resp.text:
            return resp.text.strip()
        if hasattr(resp, "candidates") and resp.candidates:
            parts = getattr(resp.candidates[0].content, "parts", []) or []
            texts = [p.text for p in parts if getattr(p, "text", None)]
            if texts:
                return "\n".join(texts).strip()
        return ""


# ---------------------------------------------------------------------------
# Factory & shared instances
# ---------------------------------------------------------------------------

_shared: Dict[str, LLMClient] = {}
_shared_lock = threading.Lock()


def get_llm_client(backend: Optional[str] = None) -> LLMClient:
    """Return a shared (lazily constructed) LLM client instance.

    The backend is resolved in this order:
    1. The ``backend`` argument (if provided).
    2. The ``LLM_BACKEND`` environment variable.
    3. ``"gemini"`` (hard-coded default).

    Currently supported backend keys: ``"gemini"``.
    """
    key = backend or os.environ.get("LLM_BACKEND", "gemini")
    with _shared_lock:
        if key not in _shared:
            if key == "gemini":
                _shared[key] = GeminiClient()
            else:
                raise ValueError(
                    f"Unknown LLM_BACKEND: {key!r}. Supported values: 'gemini'."
                )
        return _shared[key]


def reset_shared_clients() -> None:
    """Drop all cached LLM client singletons so the NEXT get_llm_client() rebuilds
    from the CURRENT environment (M8 fix, 2026-06-30).

    A Settings save mirrors a new key into os.environ, but GeminiClient caches its
    genai.Client and reads the key only at construction — so without this the
    stale (possibly wrong/expired-key) client is reused until a full process
    restart. Safe to call any time; the next get_llm_client() lazily rebuilds.
    """
    with _shared_lock:
        _shared.clear()
