"""
Resolves seismohub:// URIs to a (page, char_offset, text_snippet) for the frontend.

URI format:  seismohub://paper/{paper_id}/p{page}#sha1={sha1_hex}

This module is a thin orchestration layer on top of the pure-function
helpers in pdf_page_cache.py.  It is the single import point for the
FastAPI route handlers — they call ``parse_seismohub_uri`` (validation
only) and ``resolve_anchor`` (DB lookup) without touching pdf_page_cache
directly.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

from services.pdf_page_cache import (
    _SEISMOHUB_URI_RE,
    build_seismohub_uri,          # re-exported for convenience
    find_paragraph_by_opening_sentence,
    resolve_seismohub_uri,
)

__all__ = [
    "build_seismohub_uri",
    "parse_seismohub_uri",
    "resolve_anchor",
]


def parse_seismohub_uri(uri: str) -> Optional[Dict]:
    """Parse a ``seismohub://`` URI string into its components.

    Returns a dict ``{paper_id, page, sha1}`` on success, or ``None`` if
    the string does not match the expected shape.

    This is a pure-function validator — no DB access.  ``sha1`` is ``None``
    when the URI has no fragment (legacy page-only form).
    """
    m = _SEISMOHUB_URI_RE.match((uri or "").strip())
    if not m:
        return None
    return {
        "paper_id": m.group("paper_id"),
        "page": int(m.group("page")),
        "sha1": (m.group("sha1") or "").lower() or None,
    }


def resolve_anchor(
    db_session,
    paper_id: str,
    page: int,
    sha1: Optional[str],
    *,
    opening_sentence: Optional[str] = None,
) -> Dict:
    """Resolve a paragraph anchor to its current cache location.

    Reconstructs a canonical ``seismohub://`` URI from the parts and
    delegates to ``resolve_seismohub_uri``.

    3-step retry logic:
      1. SHA-1 scan on the cited page  (handled inside resolve_seismohub_uri)
      2. Fuzzy opening-sentence fallback if ``opening_sentence`` is provided
         (uses PR-18's ``find_paragraph_by_opening_sentence``)
      3. Page-only fallback — always returns something useful for the caller

    Returns a dict compatible with the ``POST /api/papers/{id}/locate``
    and ``POST /api/citation/resolve`` response contracts:

      {
        "found": bool,
        "page": int,
        "char_offset": int | None,
        "text_snippet": str | None,
        "paragraph_index": int | None,
        "status": "ok" | "drifted" | "not_found" | "invalid_uri",
        "drift_reason": str | None,
        "recovered_via": str | None,
      }
    """
    uri = build_seismohub_uri(paper_id, page, sha1 or "")
    if not uri:
        # Fallback when build_seismohub_uri rejects the inputs (e.g. page<=0)
        return {
            "found": False, "page": page, "char_offset": 0,
            "text_snippet": None, "paragraph_index": None,
            "status": "invalid_uri", "drift_reason": None, "recovered_via": None,
        }

    result = resolve_seismohub_uri(uri, db_session)

    # Step 2: fuzzy opening-sentence fallback for "drifted" + sha_mismatch.
    if (
        result["status"] == "drifted"
        and result.get("drift_reason") == "sha_mismatch"
        and opening_sentence
        and opening_sentence.strip()
    ):
        fuzzy = find_paragraph_by_opening_sentence(
            paper_id, page, opening_sentence, db_session
        )
        if fuzzy:
            result = dict(result)
            result["status"] = "ok"
            result["paragraph_index"] = fuzzy.get("para_index")
            result["char_offset"] = fuzzy.get("char_offset")
            result["text_snippet"] = (
                fuzzy.get("opening_sentence") or fuzzy.get("text") or ""
            )[:120]
            result["recovered_via"] = "fuzzy_fallback"

    return {
        "found": result["status"] in ("ok",),
        "page": result.get("page") or page,
        "char_offset": result.get("char_offset") or 0,
        "text_snippet": result.get("text_snippet"),
        "paragraph_index": result.get("paragraph_index"),
        "status": result["status"],
        "drift_reason": result.get("drift_reason"),
        "recovered_via": result.get("recovered_via"),
    }
