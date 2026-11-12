"""TDM check for the PDF Q&A chat (added 2026-11-09; owner decision 「加上」).

``main.chat_with_paper`` sends the paper's cached page text (``paper_page_caches.pages``) to Gemini through
``services/qa_service.answer_question``. institution-licensed full text (TDM copies, subscription PDFs) must never reach
hosted AI, so the chat refuses when the text it would send comes from such a copy.

``qa_block_reason(paper, pages)`` returns ``""`` when the chat may go ahead, else a short reason. Any one rule
blocks, and an unexpected error blocks too (fail closed):

1. the paper row: ``retrieval_status == 'held_tdm_only'`` or an Elsevier TDM-XML ``retrieval_source``;
2. the attached PDF's sha256 is a TDM copy recorded in a ``paper_bundles/*/bundle_manifest.json`` or in the
   app-detach manifests (``paper_bundles/_app_detached_tdm_*/DETACHED_FROM_APP.json``);
3. the first cached pages carry the TDM banner of the XML renderer ("LICENSED TDM COPY");
4. a TDM-licensed copy of this paper is on file (its bundle manifest or the detach manifests) and the attached
   PDF is not a known open copy (an OPEN copy in a manifest or in a harvest report) - e.g. another copy of a
   subscription article uploaded by hand;
5. the first two pages say "All rights reserved" with no open-licence statement;
6. the first two pages carry an institution download stamp ("Downloaded from ... by <Institution> user
   on <date>") with no open-licence statement - a subscription PDF saved from a browser on a campus
   network and uploaded by hand.

Rules 5 and 6 skip a known repository / preprint copy (kind ``open_copy``: arXiv, EarthArXiv, ETH Research
Collection, Zenodo, other repositories), because those are public postings and often carry the journal's
copyright line. A subscription PDF of a paper with no bundle and no licence text on its first pages cannot be
recognised; it is let through.

With no cached pages only title / authors / year would be sent, so nothing is blocked then. Manifests and harvest
reports are re-read at most every ``CACHE_SECONDS``. Pure stdlib; reads files only.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE_ROOT = REPO / "paper_bundles"
CACHE_SECONDS = 300.0

TIER_TDM = "LICENSED_TDM_ONLY"
TIER_OPEN = "OPEN_CC_BY_OR_PREPRINT"
TDM_SOURCES = ("elsevier_tdm_xml", "elsevier_xml_rendered")
TDM_BANNERS = ("LICENSED TDM COPY", "DO NOT UPLOAD TO HOSTED AI")

_RIGHTS_RESERVED = re.compile(r"all\s+rights\s+reserved", re.I)
_OPEN_STATEMENT = re.compile(
    r"creativecommons|creative\s+commons|open\s+access\s+article|this\s+is\s+an\s+open\s+access|"
    r"open\s+access\s+this\s+article|©\s*\d{4}\.?\s*the\s+authors?\b|\bthe\s+author\(s\)\s*\d{4}", re.I)
# A publisher's per-download stamp that names the subscribing institution, e.g. "Downloaded from
# https://academic.oup.com/... by <Institution> user on 09 November 2026" or "Downloaded from
# https://www.science.org at <Institution> on November 09, 2026". "by guest" (an anonymous, open download)
# does not count.
_INSTITUTION_STAMP = re.compile(
    r"downloaded\s+from\s+\S+(?:\s+\S+){0,3}?\s+(?:by|at)\s+(?!guest\b)(?:the\s+)?\S.{0,120}?"
    r"\s(?:user\s+)?on\s+(?:\[|\d|[a-z]{3,9}\.?\s+\d)",
    re.I | re.S,
)

REFUSAL = ("PDF 问答已对这篇论文关闭：它的全文属于机构许可内容（TDM 或订阅），只能在本地阅读，不会发给 Gemini。"
           " / PDF Q&A is off for this paper: its full text is institution-licensed (TDM or subscription) and is never "
           "sent to hosted AI.")

_lock = threading.Lock()
_EMPTY: Dict[str, Any] = {"tdm_shas": frozenset(), "tdm_papers": frozenset(), "open_copies": {}}
_index: Dict[str, Any] = {"root": None, "at": 0.0, **_EMPTY}


def _copy_parts(manifest: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for part in (manifest.get("main_manuscript"), (manifest.get("app_attached") or {}).get("copy"),
                 manifest.get("held_back_tdm_copy")):
        if isinstance(part, dict):
            yield part


def _is_tdm(part: Dict[str, Any]) -> bool:
    return part.get("license_tier") == TIER_TDM or part.get("hosted_ai_upload_allowed") is False


def _build_index(root: Path) -> Dict[str, Any]:
    """TDM shas, papers with a TDM copy on file, and known OPEN copies (sha -> kind)."""
    tdm_shas, tdm_papers, open_copies = set(), set(), {}

    def note_open(part: Dict[str, Any]) -> None:
        sha = str(part.get("sha256") or "").lower()
        if sha and part.get("license_tier") == TIER_OPEN and part.get("hosted_ai_upload_allowed") is not False:
            open_copies.setdefault(sha, part.get("kind") or "")

    for mf in root.glob("PaperBundle_*/bundle_manifest.json"):
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = str(manifest.get("paper_id") or "")
        for part in _copy_parts(manifest):
            sha = str(part.get("sha256") or "").lower()
            if _is_tdm(part):
                if sha:
                    tdm_shas.add(sha)
                if pid:
                    tdm_papers.add(pid)
            else:
                note_open(part)
    for detached in root.glob("_app_detached_tdm_*/DETACHED_FROM_APP.json"):
        try:
            data = json.loads(detached.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in list(data.get("papers") or []) + list(data.get("additions") or []):
            if isinstance(row, dict):
                if row.get("sha256"):
                    tdm_shas.add(str(row["sha256"]).lower())
                if row.get("paper_id"):
                    tdm_papers.add(str(row["paper_id"]))
    for report in root.glob("_reports/harvest_*.jsonl"):
        try:
            with open(report, encoding="utf-8") as fh:
                for line in fh:
                    if '"app_copy": {' not in line:
                        continue
                    copy = (json.loads(line).get("app_copy") or {})
                    if isinstance(copy, dict):
                        note_open(copy)
        except Exception:
            continue
    for sha in tdm_shas:  # TDM evidence wins over an OPEN record of the same bytes
        open_copies.pop(sha, None)
    return {"tdm_shas": frozenset(tdm_shas), "tdm_papers": frozenset(tdm_papers), "open_copies": open_copies}


def _get_index(bundle_root: Optional[Path]) -> Dict[str, Any]:
    root = Path(bundle_root) if bundle_root else DEFAULT_BUNDLE_ROOT
    with _lock:
        if _index["root"] == str(root) and time.monotonic() - _index["at"] < CACHE_SECONDS:
            return dict(_index)
    built = _build_index(root)
    with _lock:
        _index.update(root=str(root), at=time.monotonic(), **built)
        return dict(_index)


def clear_cache() -> None:
    with _lock:
        _index.update(root=None, at=0.0, **_EMPTY)


def _first_pages_text(pages: List[Dict[str, Any]], n: int = 2) -> str:
    ordered = sorted((p for p in pages if isinstance(p, dict)), key=lambda p: (p.get("page") or 0))
    return " ".join(str(p.get("text") or "") for p in ordered[:n])


def qa_block_reason(paper: Any, pages: List[Dict[str, Any]], bundle_root: Optional[Path] = None) -> str:
    """``""`` if the PDF Q&A may send *pages* to hosted AI, else a short reason (see the module docstring)."""
    try:
        if not pages:
            return ""
        status = str(getattr(paper, "retrieval_status", "") or "").strip().lower()
        source = str(getattr(paper, "retrieval_source", "") or "").strip().lower()
        if status == "held_tdm_only":
            return "retrieval_status=held_tdm_only"
        if source in TDM_SOURCES:
            return f"retrieval_source={source}"

        index = _get_index(bundle_root)
        sha = str(getattr(paper, "pdf_sha256", "") or "").lower()
        if sha and sha in index["tdm_shas"]:
            return "attached PDF is a TDM copy recorded in paper_bundles"

        head = _first_pages_text(pages)
        if any(banner in head for banner in TDM_BANNERS):
            return "TDM banner on the first page"
        known_kind = index["open_copies"].get(sha) if sha else None
        if known_kind is None and str(getattr(paper, "id", "") or "") in index["tdm_papers"]:
            return "a TDM-licensed copy of this paper is on file and the attached PDF is not a known open copy"
        if known_kind == "open_copy":
            return ""  # repository / preprint copy
        if not _OPEN_STATEMENT.search(head):
            if _RIGHTS_RESERVED.search(head):
                return "first pages say 'All rights reserved' and carry no open-licence statement"
            if _INSTITUTION_STAMP.search(head):
                return "institution download stamp and no open-licence statement"
        return ""
    except Exception as exc:  # fail closed
        return f"licence check failed ({type(exc).__name__})"
