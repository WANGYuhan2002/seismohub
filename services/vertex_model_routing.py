"""Per-model Vertex routing for models the express endpoint does not serve.

Added 2026-11-09 for the switch of the PDF-analysis model to ``gemini-4-argon``.

Why this file exists
--------------------
``analyze_service._get_vertex_client()`` builds the genai client in Vertex
*express mode* (``genai.Client(vertexai=True, api_key=...)``). In that mode the
SDK drops VERTEX_AI_PROJECT / VERTEX_AI_LOCATION and sends requests to
``aiplatform.googleapis.com/v1beta1/publishers/google/models/<id>``; the
express endpoint then routes this project to ``europe-west1``, where
``gemini-4-argon`` is not served (404 NOT_FOUND, probed 2026-11-09 with the
free countTokens API; the 2026-10-16 note in ``llm_client.py`` saw the same).

The SAME API key on the project-scoped path
``projects/<VERTEX_AI_PROJECT>/locations/global/publishers/google/models/gemini-4-argon``
does resolve (countTokens 200 for text and for a PDF part, 2026-11-09).
google-genai passes a model name that starts with ``projects/`` through
unchanged, so the existing express client reaches it with no new client and no
change to how the key is sent (``x-goog-api-key`` header).

``wire_model_name()`` turns a short model ID into that resource path only for
the IDs listed in ``PROJECT_ROUTED_MODELS``; every other ID is returned
unchanged, so all other calls keep their current route. Callers must record
the SHORT ID in the database, never the resource path (it holds the project ID).

Rollback: see ``_code_backup_2026-11-09_151822_pre_gemini4_argon_pdf/ROLLBACK.md``.
"""

from __future__ import annotations

import os
import re
from typing import Dict

__all__ = ["PROJECT_ROUTED_MODELS", "wire_model_name"]

# Model ID -> Vertex location that serves it for this project (verified by probe).
PROJECT_ROUTED_MODELS: Dict[str, str] = {
    "gemini-4-argon": "global",
}

# GCP project IDs: 6-30 chars, lowercase letters, digits and hyphens.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def wire_model_name(model_id: str) -> str:
    """Return the model name to send on the wire for *model_id*.

    For IDs in ``PROJECT_ROUTED_MODELS`` this is the full publisher-model
    resource path under ``projects/<VERTEX_AI_PROJECT>/locations/<location>``.
    Anything else, or a missing / malformed project ID, returns *model_id*
    unchanged (the plain express route, i.e. today's behaviour).
    """
    mid = (model_id or "").strip()
    location = PROJECT_ROUTED_MODELS.get(mid)
    if not location:
        return model_id
    project = (os.getenv("VERTEX_AI_PROJECT") or "").strip()
    if not _PROJECT_ID_RE.match(project):
        return model_id
    return f"projects/{project}/locations/{location}/publishers/google/models/{mid}"
