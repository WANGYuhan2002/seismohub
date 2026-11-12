"""PDF QA chat service (M2 G5).

Provides stateless question-answering grounded in the per-page text cache
built by :mod:`services.pdf_page_cache`.  Each AI reply contains
structured citation envelopes (filled in by PR-23 / T-204); for now the
``citations`` slot is always an empty list while the schema is in flight.

Public API
----------
``answer_question(*, question, history, pages, paper_meta, client)``
    Single call that accepts the full conversation history and returns
    ``{reply, citations, model, model_label, raw_reply}``.

Design decisions
~~~~~~~~~~~~~~~~
* **Stateless** — history is passed in / out of every request; nothing is
  persisted server-side in G5.  Persistence (save chats to DB) is deferred
  to a later milestone.
* **Page-ref format** — the model is instructed to write ``[[p.N]]`` (double
  brackets).  The cleaner reply strips these markers; the page-ref list is
  returned separately so the UI can render chips without parsing text.
* **Token budget** — at most ``QA_MAX_PAGES`` pages are stuffed into the
  context.  For a typical 60-page paper (8 000 chars/page) that is ~480 K
  chars, well inside Gemini 2.0 Flash's 1 M token window.
* **Language detection** — the system prompt asks the model to match the
  question language (Chinese or English) so bilingual users get answers in
  the language they asked.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from services.llm_client import LLMClient, Message, display_name_for_model, get_llm_client
from services.pdf_page_cache import (
    verify_anchor,                        # PR-18
    find_paragraph_by_opening_sentence,   # PR-18 fuzzy fallback
)

__all__ = ["answer_question"]

_log = logging.getLogger(__name__)

# Regex carves the trailing ``<citations>…</citations>`` block off the reply.
_CITATIONS_BLOCK_RE = re.compile(
    r"<citations>\s*(?P<json>.+?)\s*</citations>\s*$",
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum pages fed into a single QA call.  Adjust downward if you observe
# Gemini token-limit errors on very long papers.
QA_MAX_PAGES = 40

# System prompt shown to the LLM before the conversation starts.
# v2 (PR-22, BRIEF item H): replaces the legacy
# ``(§{section} {title} — {hint}; p. {N})`` plain-text annotation rule with
# the ``[[N]]`` inline sentinel + trailing ``<citations>`` JSON block.
_SYSTEM_TMPL = """\
You are a research assistant helping a seismologist understand the paper:

Title: {title}
Authors: {authors}
Year: {year}

The paper's text is provided below, divided by page number.

--- BEGIN PAPER TEXT ---
{page_text}
--- END PAPER TEXT ---

CITATION RULES (mandatory — follow exactly):
- Every factual claim you make MUST be supported by a verbatim quote from the paper text above.
- Mark each supporting claim in the prose with an inline sentinel of the form [[N]] (double square brackets, integer N starting at 1, incremented for each new citation, no whitespace inside). The sentinel goes IMMEDIATELY after the sentence it supports — for example:
    The receiver function images reveal a low-velocity zone at 12 km depth.[[1]] We further identify partial melt beneath the Wangtian'e volcano.[[2]]
- Do NOT write parenthetical source annotations like "(§3.2 Receiver functions — second paragraph; p. 4)". Do NOT use the legacy "[[p.N]]" form. Use ONLY the new [[N]] sentinel.
- After the body of your answer, emit EXACTLY ONE block delimited by the literal tags <citations> and </citations>. Inside the tags place a JSON array. Each entry corresponds to one [[N]] sentinel in the prose (id == N) and has this shape:
    {{
      "id": 1,
      "page": 5,
      "paragraph_index": 8,
      "opening_sentence": "When the receiver function inverts the wave equation across the Wangtian'e crust, ...",
      "quote": "low-velocity zone at 12 km depth",
      "section_title": "§3.2 Receiver functions"
    }}
  Optional sibling fields you MAY include when applicable: "sentence_index" (1-indexed sentence within the paragraph).
- Required keys per entry: id, page, paragraph_index, opening_sentence, quote. The "section_title" key is strongly preferred when a heading is visible on the page; omit it if not. Do not add other keys.
- Field semantics (read carefully — these are non-negotiable):
    • "page" — the integer that appears in the [Page N] header of the source block you took the quote from. 1-indexed. Required.
    • "paragraph_index" — 1-indexed paragraph number within that page, counting from the top of the page. Required.
    • "opening_sentence" — copy VERBATIM the first sentence (≤ 240 characters) of the paragraph the quote is in. Do not paraphrase. Do not summarise. Do not translate. If the paper is in Chinese, the opening_sentence is in Chinese; if English, English.
    • "quote" — copy VERBATIM the supporting span of text (≤ 320 characters) from that paragraph. Character-for-character, including punctuation and capitalisation. **The quote MUST stay in the paper's original language. NEVER translate it, even when your answer is in a different language than the paper.** A Chinese paper passage stays in Chinese; an English passage stays in English. If you must reference a translated paraphrase in the answer prose, write the translation in the prose and place the verbatim original-language span in the JSON "quote" field.
    • "section_title" — the §-numbered heading visible on the page if there is one (e.g. "§3.2 Receiver functions"); leave the key out if no heading is visible.
- The <citations> block is the LAST thing in your reply. Nothing after </citations>. No trailing prose.
- If you cannot find a verbatim span in the paper that supports a claim, do NOT fabricate a quote. Either drop the claim, or rewrite the claim as a hedge ("the paper does not state X directly") and omit the corresponding [[N]] sentinel and citation entry.
- Only cite paragraphs whose text you actually used. Every [[N]] in the prose MUST have a matching entry in the JSON block (same id), and every JSON entry MUST be referenced by at least one [[N]] in the prose.
- If the paper text does not contain enough information to answer, say so honestly without fabricating, and emit "<citations>[]</citations>" with an empty array.

Worked example (illustrative — your output will use the actual paper above):
  Q: What does the receiver function imaging show beneath Wangtian'e?
  A: The receiver function images reveal a low-velocity zone at 12 km depth.[[1]] The authors interpret this as partial melt in the lower crust.[[2]]
  <citations>[
    {{"id": 1, "page": 4, "paragraph_index": 2, "opening_sentence": "We compute receiver functions using the iterative time-domain deconvolution of Ligorria and Ammon (1999).", "quote": "the receiver function images reveal a low-velocity zone at 12 km depth", "section_title": "§3.2 Receiver functions"}},
    {{"id": 2, "page": 8, "paragraph_index": 1, "opening_sentence": "The 12-km low-velocity zone is most parsimoniously interpreted as ...", "quote": "we identify partial melt beneath the Wangtian'e", "section_title": "§4 Results"}}
  ]</citations>

Answer style:
- Reply in the same language as the question (Chinese or English).
- Lead with a direct answer, then support every claim with quoted evidence + source.
- Be as thorough as needed — do not cut corners to save space.
- The reader VERIFIES by clicking the [[N]] chip — the chip jumps to page + paragraph in the embedded PDF viewer and highlights the verbatim quote.
- Important reminder about language: the answer prose follows the question language (CN question -> CN answer, EN question -> EN answer); the "opening_sentence" and "quote" fields in <citations> follow the PAPER language, not the answer language. NEVER translate either field.
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_page_text(pages: List[Dict], max_pages: int = QA_MAX_PAGES) -> str:
    """Concatenate page entries into one labelled block."""
    parts: List[str] = []
    for entry in pages[:max_pages]:
        page_num = entry.get("page", "?")
        text = (entry.get("text") or "").strip()
        if text:
            parts.append(f"[Page {page_num}]\n{text}")
    return "\n\n".join(parts) if parts else ""


def _extract_citations(
    raw_reply: str,
    *,
    paper_id: Optional[str],
    pages: List[Dict],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Split the trailing ``<citations>…</citations>`` block off *raw_reply*,
    validate each envelope through PR-18's verifier, and return
    ``(clean_reply, citations_list)``.
    """
    clean_reply = raw_reply
    citations_raw: List[Any] = []

    matches = list(_CITATIONS_BLOCK_RE.finditer(raw_reply))
    if matches:
        last = matches[-1]
        clean_reply = raw_reply[: last.start()].rstrip()
        block = last.group("json").strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, list):
                citations_raw = parsed
            else:
                _log.warning(
                    "qa_service: <citations> block is not a JSON array "
                    "(got %s) — dropping all citations",
                    type(parsed).__name__,
                )
        except json.JSONDecodeError as exc:
            _log.warning(
                "qa_service: <citations> JSON parse failed for paper %s: %s",
                paper_id, exc,
            )
    else:
        _log.info(
            "qa_service: model emitted no <citations> block for paper %s "
            "(may indicate prompt regression or short factual answer)",
            paper_id,
        )

    # Build a {page_number -> [paragraph_dict]} index from pages.
    paragraphs_by_page: Dict[int, List[Dict[str, Any]]] = {}
    for p in pages:
        if not isinstance(p, dict):
            continue
        pg = p.get("page")
        if not isinstance(pg, int):
            continue
        paragraphs_by_page[pg] = list(p.get("paragraphs") or [])

    out: List[Dict[str, Any]] = []
    for idx, env in enumerate(citations_raw, start=1):
        if not isinstance(env, dict):
            _log.warning(
                "qa_service: citation #%d is not a dict (%s) — dropped",
                idx, type(env).__name__,
            )
            continue
        if not env.get("page") or not (env.get("opening_sentence") or env.get("cited_text") or env.get("quote")):
            _log.warning(
                "qa_service: citation #%d missing required fields — dropped "
                "(keys=%s)",
                idx, sorted(env.keys()),
            )
            continue

        ok, reason = verify_anchor(env, paragraphs_by_page)
        if ok:
            env["verified"] = True
        else:
            try:
                page_num = int(env.get("page"))
            except (TypeError, ValueError):
                page_num = 0
            recovered = find_paragraph_by_opening_sentence(
                paragraphs_by_page.get(page_num, []),
                env.get("opening_sentence", "") or env.get("quote", ""),
            )
            if recovered is not None:
                env["paragraph_index"] = recovered.get("para_index", env.get("paragraph_index"))
                env["verified"] = True
                env["anchor_recovery"] = "opening_sentence"
            else:
                env["verified"] = False
                env["anchor_miss_reason"] = reason
                _log.info(
                    "qa_service: citation #%d unverified for paper %s "
                    "(reason=%s, page=%s, opening=%r)",
                    idx, paper_id, reason, env.get("page"),
                    (env.get("opening_sentence") or "")[:60],
                )
        out.append(env)

    return clean_reply, out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def answer_question(
    *,
    question: str,
    history: List[Dict],
    pages: List[Dict],
    paper_meta: Dict,
    client: Optional[LLMClient] = None,
    paper_id: Optional[str] = None,
) -> Dict:
    """Answer *question* grounded in *pages* text.

    Parameters
    ----------
    question:
        The user's current question (non-empty string).
    history:
        Prior conversation turns as a list of dicts:
        ``[{"role": "user"|"model", "text": "..."}]``.
        May be empty for the first question.
    pages:
        Page dicts from ``PaperPageCache.pages``:
        ``[{"page": 1, "text": "...", "chars": 3218}]``.
        May be empty for image-only PDFs.
    paper_meta:
        ``{"title": str, "authors": str, "year": str}`` for the system
        prompt header.
    client:
        Override the LLM backend.  Pass a mock here in unit tests.

    Returns
    -------
    dict with keys:
        ``reply``       — cleaned reply string (no ``[[p.N]]`` markers).
        ``citations``   — list of citation envelopes.  Empty for now;
                          PR-23 (T-204) parses the model's
                          ``--- CITATIONS ---`` block into this slot.
        ``model``       — wire-level ID of the model that actually
                          produced the reply (e.g. ``"gemini-3.1-pro-preview"``).
                          Sourced from ``GeminiClient._last_model_used``
                          (PR-06) so silent primary→fallback degradation
                          is recorded honestly, not optimistically.
        ``model_label`` — human-friendly label for ``model``
                          (e.g. ``"Gemini 3.1 Pro"``), via
                          ``display_name_for_model()``.  Denormalised
                          here so PR-08's persistence write can freeze
                          the historical label on the ``qa_messages``
                          row without re-deriving on every fetch.
        ``raw_reply``   — the model output before marker stripping.
    """
    if client is None:
        client = get_llm_client()

    page_text = _build_page_text(pages)
    if not page_text:
        page_text = "(No extractable text — this paper may be image-based.)"

    system = _SYSTEM_TMPL.format(
        title=paper_meta.get("title") or "Unknown",
        authors=paper_meta.get("authors") or "",
        year=paper_meta.get("year") or "",
        page_text=page_text,
    )

    msgs: List[Message] = []
    for turn in history:
        role = str(turn.get("role", "user"))
        text = str(turn.get("text") or "").strip()
        if role in {"user", "model"} and text:
            msgs.append(Message(role=role, text=text))
    msgs.append(Message(role="user", text=question))

    raw = client.chat(msgs, system=system)
    reply, citations = _extract_citations(raw, paper_id=paper_id, pages=pages)

    # PR-06 contract: ``GeminiClient`` sets ``_last_model_used`` in BOTH
    # the primary and fallback branches of ``chat()`` so the badge never
    # lies about which model produced the reply.  Fall back to
    # ``_model_name`` only for clients that pre-date PR-06 (e.g. test
    # doubles); fall back to "" so ``display_name_for_model`` returns
    # "Unknown model" rather than crashing.
    model_id = (
        getattr(client, "_last_model_used", "")
        or getattr(client, "_model_name", "")
        or ""
    )
    model_label = display_name_for_model(model_id)

    return {
        "reply": reply,
        "citations": citations,  # PR-23 populates from <citations>...</citations>
        "model": model_id,
        "model_label": model_label,
        "raw_reply": raw,
    }
