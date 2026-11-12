"""PDF page-text cache service  (M2 G6 + Q3-PR-17 paragraph anchors).

Extracts per-page text from a PDF with PyMuPDF and stores it in the
``paper_page_caches`` table.  Provides a fast "get or create" helper used
by the structured AI card (G4) and the future PDF QA chat (G5).

Cache schema  (``PaperPageCache.pages`` JSON column):
  [{"page": 1, "text": "...", "chars": 3218, "paragraphs": [...]}, ...]

After PR-17 each page also carries a ``paragraphs`` array shaped per
``05-technical-architecture/05-paragraph-anchor-extraction.md`` §4:
  paragraphs: [{"para_index": 0, "char_offset": 0,
                "opening_sentence": "...", "opening_sentence_norm": "...",
                "text": "...", "sha": "...",
                "prefix32": "...", "suffix32": "..."}]

Pages are 1-indexed to match reader page numbers shown in the UI.
"""

from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None  # type: ignore[assignment]

# PyMuPDF does NOT guarantee thread safety: MuPDF keeps a global context, so
# concurrent fitz calls — even on different files — can corrupt it (worst case
# a segfault that takes down the whole process). Every fitz section in the app
# must hold this lock. Extraction is ms-scale next to a Gemini round-trip, so
# serializing it costs nothing observable.
FITZ_LOCK = threading.Lock()

from models import PaperPageCache


# Maximum characters per page (excess is silently truncated).
PAGE_TEXT_MAX_CHARS = 8_000
# Maximum pages to extract (keeps cache size sane for long papers).
MAX_PAGES = 60

# ---------------------------------------------------------------------------
# Paragraph-anchor extraction  (Q3-PR-17, BRIEF §1.3.H foundation)
# ---------------------------------------------------------------------------
# Pure-function building block. No I/O, no DB, no LLM.

PARA_SPLIT_RE = re.compile(r"\n\s*\n+")
OPENING_SENTENCE_RE = re.compile(r"^.+?[.!?](?=\s+[A-Z])", re.DOTALL)
FIG_CAPTION_RE = re.compile(r"^(Figure|Fig\.?|Table|Tab\.?)\s+\d+[\.:]", re.I)
PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")
PROSE_TOKEN_RE = re.compile(r"\b[A-Za-z]{4,}\b")
WS_RUN_RE = re.compile(r"\s+")
NONWORD_EDGE_RE = re.compile(r"^\W+|\W+$")

OPENING_MAX_CHARS = 200
TAB_DENSITY_THRESHOLD = 0.04   # ≥4 % tab chars ⇒ probably a table fragment
PREFIX_SUFFIX_LEN = 32         # Hypothesis-style fuzzy-fallback window

# Common PyMuPDF ligature artefacts. Normalised away before hashing so an
# anchor stored from one extraction matches a later re-extraction that
# happens to decompose the ligatures differently.
_LIGATURES = (
    ("ﬁ", "fi"),  # ﬁ
    ("ﬂ", "fl"),  # ﬂ
    ("ﬀ", "ff"),  # ﬀ
    ("ﬃ", "ffi"), # ﬃ
    ("ﬄ", "ffl"), # ﬄ
)


def _is_real_paragraph(chunk: str) -> bool:
    """Step 2 — filter figure captions, table fragments, page numbers,
    and prose-less crumbs.
    """
    if PAGE_NUMBER_RE.match(chunk):
        return False
    if FIG_CAPTION_RE.match(chunk):
        return False
    # Table density — pymupdf flattens table cells as col1\tcol2\tcol3\n…
    if chunk.count("\t") / max(len(chunk), 1) >= TAB_DENSITY_THRESHOLD:
        return False
    # Must contain at least one 4+ letter word — kills "(1)" / "et al." crumbs.
    if not PROSE_TOKEN_RE.search(chunk):
        return False
    return True


def _extract_opening_sentence(para_text: str) -> str:
    """Step 3 — first sentence, ≤200 chars, original casing preserved."""
    m = OPENING_SENTENCE_RE.match(para_text)
    if m:
        return m.group(0)[:OPENING_MAX_CHARS]
    return para_text[:OPENING_MAX_CHARS]


def _normalize_opening(opening_sentence: str) -> str:
    """Step 4 — produce the stable lookup key. Idempotent."""
    s = opening_sentence.lower()
    for src, dst in _LIGATURES:
        if src in s:
            s = s.replace(src, dst)
    s = WS_RUN_RE.sub(" ", s).strip()
    s = NONWORD_EDGE_RE.sub("", s)
    return s


def _sha1_of(norm: str) -> str:
    """Step 5 — SHA-1 of the normalized key."""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _compute_context(page_text: str, char_offset: int,
                     para_len: int) -> tuple[str, str]:
    """05-tech-arch/05 §6 — Hypothesis-style fuzzy fallback."""
    start = max(0, char_offset - PREFIX_SUFFIX_LEN)
    end = min(len(page_text), char_offset + para_len + PREFIX_SUFFIX_LEN)
    prefix = page_text[start:char_offset]
    suffix = page_text[char_offset + para_len:end]
    return prefix, suffix


def extract_paragraph_anchors(page_text: str) -> List[Dict]:
    """Run Steps 1–5 (+ §6 fallback) on a single page's text.

    Returns a list of anchor dicts ready to embed under
    ``PaperPageCache.pages[i]["paragraphs"]``. Pure function.
    """
    if not page_text or not page_text.strip():
        return []

    out: List[Dict] = []
    cursor = 0
    raw_chunks = PARA_SPLIT_RE.split(page_text)

    kept_index = 0
    for raw in raw_chunks:
        chunk = raw.strip()
        if not chunk:
            cursor += len(raw) + 2
            continue

        if not _is_real_paragraph(chunk):
            cursor += len(raw) + 2
            continue

        char_offset = page_text.find(chunk, cursor)
        if char_offset < 0:
            char_offset = cursor

        opening = _extract_opening_sentence(chunk)
        norm = _normalize_opening(opening)
        sha = _sha1_of(norm)
        prefix, suffix = _compute_context(page_text, char_offset, len(chunk))

        out.append({
            "para_index": kept_index,
            "char_offset": char_offset,
            "opening_sentence": opening,
            "opening_sentence_norm": norm,
            "text": chunk,
            "sha": sha,
            "prefix32": prefix,
            "suffix32": suffix,
        })
        kept_index += 1
        cursor = char_offset + len(chunk)

    return out


def get_paragraphs_by_page(paper_id: str, db_session) -> Dict[int, List[Dict]]:
    """Return ``{page_number: [paragraph_dict, ...]}`` for a paper.

    Returns an empty dict if the cache row is missing or if no paragraphs
    have been extracted yet.  Defensive `.get("paragraphs", [])` per the
    PR-16 contract.
    """
    cache: Optional[PaperPageCache] = db_session.get(PaperPageCache, paper_id)
    if cache is None or not cache.pages:
        return {}
    out: Dict[int, List[Dict]] = {}
    for entry in cache.pages:
        if not isinstance(entry, dict):
            continue
        page_num = entry.get("page")
        if not isinstance(page_num, int):
            continue
        out[page_num] = list(entry.get("paragraphs") or [])
    return out


def _needs_paragraph_refresh(cache: PaperPageCache) -> bool:
    """Return True when any page in *cache* still lacks paragraphs[]."""
    if not cache or not cache.pages:
        return False
    for entry in cache.pages:
        if not isinstance(entry, dict):
            continue
        if "paragraphs" not in entry:
            return True
        if entry.get("paragraphs") is None:
            return True
    return False


def extract_paragraphs(
    page_text: str,
    *,
    page_number: int,
    total_pages: int,
) -> List[Dict]:
    """Return ``paragraphs`` list for one page.

    PR-17 wires this stub to the real ``extract_paragraph_anchors``
    pure-function helper above.  Kept as a wrapper with the
    ``(page_number, total_pages)`` kwargs so future cross-page state
    (the references-sweep) can layer on top without re-plumbing
    ``extract_pages``.
    """
    return extract_paragraph_anchors(page_text)


def extract_pages(pdf_path: str | Path, *, max_pages: int = MAX_PAGES) -> List[Dict]:
    """Return a list of ``{page, text, chars}`` dicts from *pdf_path*.

    Returns an empty list if PyMuPDF is unavailable or extraction fails.
    Pages are 1-indexed.
    """
    if fitz is None:
        return []
    path = str(pdf_path)
    with FITZ_LOCK:
        try:
            doc = fitz.open(path)
        except Exception:
            return []

        pages: List[Dict] = []
        try:
            total = min(len(doc), max_pages)
            for i in range(total):
                raw = doc[i].get_text("text") or ""
                # Collapse runs of whitespace but preserve paragraph breaks
                import re
                cleaned = re.sub(r"[ \t]+", " ", raw).strip()
                cleaned = cleaned[:PAGE_TEXT_MAX_CHARS]
                pages.append({
                    "page": i + 1,
                    "text": cleaned,
                    "chars": len(cleaned),
                    # PR-16: shape extension; PR-17 fills via extract_paragraphs.
                    "paragraphs": extract_paragraphs(
                        cleaned,
                        page_number=i + 1,
                        total_pages=total,
                    ),
                })
        except Exception:
            pass
        finally:
            try:
                doc.close()
            except Exception:
                pass
    return pages


def get_or_create_cache(
    paper_id: str,
    pdf_path: str | Path,
    db_session,
    *,
    force_refresh: bool = False,
) -> Optional[PaperPageCache]:
    """Return (and create if missing) the page-text cache for *paper_id*.

    If a cache row already exists and ``force_refresh`` is False, the
    cached row is returned immediately (no re-extraction).

    Returns None if extraction yields no pages (e.g. PyMuPDF unavailable
    or PDF is image-only).
    """
    existing: Optional[PaperPageCache] = db_session.get(PaperPageCache, paper_id)
    if existing and not force_refresh:
        return existing

    pages = extract_pages(pdf_path)
    if not pages:
        return None

    if existing:
        existing.pages = pages
        existing.page_count = len(pages)
        existing.extracted_at = datetime.utcnow()
        db_session.add(existing)
    else:
        cache = PaperPageCache(
            paper_id=paper_id,
            pages=pages,
            page_count=len(pages),
        )
        db_session.add(cache)

    db_session.commit()
    return db_session.get(PaperPageCache, paper_id)


# ---------------------------------------------------------------------------
# Paragraph lookup helpers  (BRIEF §1.3.H — PR-18)
#
# Read-side counterparts to extract_paragraphs() (PR-16/17). Side-effect free.
# ---------------------------------------------------------------------------

DEFAULT_MIN_SCORE = 0.78
DEFAULT_MAX_PAGES_AWAY = 2
_SUBSTRING_BONUS_SCORE = 0.92
_EARLY_EXIT_SCORE = 0.99


def _normalize_for_match(s: str) -> str:
    """Normalize a free-form sentence for fuzzy comparison.

    Mirrors the normalization extract_paragraphs() applies when it
    computes ``opening_sentence_norm``, so the two strings live in the
    same space.
    """
    import unicodedata

    s = unicodedata.normalize("NFKC", s or "").lower()
    s = (s.replace("ﬁ", "fi").replace("ﬂ", "fl")
           .replace("ﬀ", "ff").replace("ﬃ", "ffi")
           .replace("ﬄ", "ffl"))
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def verify_anchor(
    paper_id_or_envelope,
    page=None,
    paragraph_index=None,
    opening_sentence=None,
    db=None,
    *,
    paragraphs_by_page=None,
):
    """Confirm a ``[[p.{page}¶{paragraph_index}]]`` anchor against the cache.

    PR-18 signature: ``verify_anchor(paper_id, page, paragraph_index,
    opening_sentence, db) -> bool``.

    PR-23 also calls this with a slightly different shape:
    ``verify_anchor(envelope_dict, paragraphs_by_page) -> (bool, reason)``.
    This wrapper supports both call signatures so the QA-side code can
    use the envelope form while analyze-side code uses the positional
    form.
    """
    # --- Dispatch on call signature ---
    if isinstance(paper_id_or_envelope, dict):
        envelope = paper_id_or_envelope
        ppp = page if isinstance(page, dict) else paragraphs_by_page
        if ppp is None:
            return (False, "no_cache")
        try:
            page_int = int(envelope.get("page"))
            para_int = int(envelope.get("paragraph_index"))
        except (TypeError, ValueError):
            return (False, "empty_anchor")
        if page_int <= 0 or para_int < 0:
            return (False, "page_out_of_range")
        page_paras = ppp.get(page_int)
        if not page_paras:
            return (False, "page_out_of_range")
        # PR-23 envelope uses 1-indexed paragraph_index, our cache is 0-indexed
        # Try both interpretations to be tolerant.
        candidates = []
        if 0 <= para_int < len(page_paras):
            candidates.append(page_paras[para_int])
        if 0 < para_int <= len(page_paras):
            candidates.append(page_paras[para_int - 1])
        if not candidates:
            return (False, "page_out_of_range")
        target_raw = (envelope.get("opening_sentence") or envelope.get("cited_text") or "")
        target_norm = _normalize_for_match(target_raw)
        if not target_norm:
            return (False, "empty_anchor")
        for para in candidates:
            cached_norm = (para.get("opening_sentence_norm") or "").strip()
            if not cached_norm:
                continue
            if cached_norm == target_norm:
                return (True, None)
            prefix_len = min(60, len(cached_norm), len(target_norm))
            if prefix_len > 0 and cached_norm[:prefix_len] == target_norm[:prefix_len]:
                return (True, None)
        return (False, "no_match")

    # Positional form: PR-18 original signature.
    paper_id = paper_id_or_envelope
    if not opening_sentence:
        return False
    try:
        page_int = int(page)
        para_int = int(paragraph_index)
    except (TypeError, ValueError):
        return False
    if page_int <= 0 or para_int < 0:
        return False

    cache: Optional[PaperPageCache] = db.get(PaperPageCache, paper_id)
    if not cache or not cache.pages:
        return False

    entry = next((p for p in cache.pages if p.get("page") == page_int), None)
    if not entry:
        return False
    paragraphs = entry.get("paragraphs") or []
    if not (0 <= para_int < len(paragraphs)):
        return False

    cached_norm = (paragraphs[para_int].get("opening_sentence_norm") or "").strip()
    target_norm = _normalize_for_match(opening_sentence)
    if not cached_norm or not target_norm:
        return False

    if cached_norm == target_norm:
        return True

    prefix_len = min(60, len(cached_norm), len(target_norm))
    if prefix_len <= 0:
        return False
    if cached_norm[:prefix_len] == target_norm[:prefix_len]:
        return True

    return False


def _walk_outward(
    pages: List[Dict],
    page_hint: Optional[int],
    max_pages_away: int,
) -> List[Dict]:
    """Yield cache page entries in distance-from-hint order."""
    if not pages:
        return []
    by_num = {p.get("page"): p for p in pages if p.get("page") is not None}
    if not by_num:
        return list(pages)
    if page_hint is None or page_hint not in by_num:
        return list(pages)

    ordered: List[Dict] = [by_num[page_hint]]
    seen = {page_hint}
    for delta in range(1, max_pages_away + 1):
        for candidate in (page_hint - delta, page_hint + delta):
            if candidate in by_num and candidate not in seen:
                ordered.append(by_num[candidate])
                seen.add(candidate)
    for entry in pages:
        if entry.get("page") not in seen:
            ordered.append(entry)
            seen.add(entry.get("page"))
    return ordered


def find_paragraph_by_opening_sentence(
    paper_id_or_paragraphs,
    page_hint_or_sentence=None,
    sentence=None,
    db=None,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    max_pages_away: int = DEFAULT_MAX_PAGES_AWAY,
) -> Optional[Dict]:
    """Best-effort fuzzy lookup of a paragraph by its opening sentence.

    Two call shapes are accepted (matches PR-18 + PR-23 contracts):

    Shape A — PR-18:
        find_paragraph_by_opening_sentence(paper_id, page_hint, sentence, db)

    Shape B — PR-23 alt:
        find_paragraph_by_opening_sentence(paragraphs_list, sentence)
    """
    # --- Shape B: paragraphs list + sentence ---
    if isinstance(paper_id_or_paragraphs, list):
        paragraphs = paper_id_or_paragraphs
        target_text = page_hint_or_sentence or ""
        if not target_text:
            return None
        target = _normalize_for_match(target_text)
        if not target:
            return None
        best: Optional[Dict] = None
        best_score = 0.0
        for para in paragraphs:
            cached_norm = (para.get("opening_sentence_norm") or "").strip()
            if not cached_norm:
                continue
            score = SequenceMatcher(None, target, cached_norm).ratio()
            if target and target in _normalize_for_match(para.get("text") or ""):
                if score < _SUBSTRING_BONUS_SCORE:
                    score = _SUBSTRING_BONUS_SCORE
            if score > best_score:
                best_score = score
                best = para
                # PERFORMANCE.md Bottleneck #3 — early-exit at near-perfect
                # match. Mirrors the same guard Shape A uses (line 541) so
                # one-shot exact-match callers don't pay for the full
                # SequenceMatcher loop over 49 remaining paragraphs.
                if best_score >= _EARLY_EXIT_SCORE:
                    return best
        if best is None or best_score < min_score:
            return None
        return best

    # --- Shape A: paper_id, page_hint, sentence, db ---
    paper_id = paper_id_or_paragraphs
    page_hint = page_hint_or_sentence
    if not sentence:
        return None
    target = _normalize_for_match(sentence)
    if not target:
        return None

    cache: Optional[PaperPageCache] = db.get(PaperPageCache, paper_id)
    if not cache or not cache.pages:
        return None

    best_page: Optional[int] = None
    best_para: Optional[Dict] = None
    best_score: float = 0.0

    for entry in _walk_outward(cache.pages, page_hint, max_pages_away):
        page_no = entry.get("page")
        for para in entry.get("paragraphs") or []:
            cached_norm = (para.get("opening_sentence_norm") or "").strip()
            if not cached_norm:
                continue
            score = SequenceMatcher(None, target, cached_norm).ratio()
            if target and target in _normalize_for_match(para.get("text") or ""):
                if score < _SUBSTRING_BONUS_SCORE:
                    score = _SUBSTRING_BONUS_SCORE
            if score > best_score:
                best_score = score
                best_page = page_no
                best_para = para
                if score >= _EARLY_EXIT_SCORE:
                    result = {"page": page_no, "score": score}
                    result.update(para)
                    return result

    if best_para is None or best_score < min_score:
        return None
    result = {"page": best_page, "score": best_score}
    result.update(best_para)
    return result


# ---------------------------------------------------------------------------
# seismohub:// URI builder + resolver  (PR-51 — BRIEF §1.3.H round-trip half)
#
# URI shape (per RECOMMENDATIONS.md cross-cutting #4 + ai-safety-50 vote):
#     seismohub://paper/{paper_id}/p{page}#sha1={paragraph_sha1}
#
# The path carries the COARSE coordinates (paper + page) and the fragment
# carries the FINE anchor (SHA-1 of paragraph opening_sentence_norm).
# ---------------------------------------------------------------------------

_SEISMOHUB_URI_RE = re.compile(
    r"^seismohub://paper/(?P<paper_id>[A-Za-z0-9_\-]+)/p(?P<page>\d+)"
    r"(?:#sha1=(?P<sha1>[a-f0-9]{40}))?$"
)


def build_seismohub_uri(paper_id: str, page: int, paragraph_sha: str) -> str:
    """Compose a ``seismohub://`` URI for a single citation anchor.

    Pure string-builder — no DB lookups.  Returns ``""`` when any
    required field is missing rather than emitting a half-formed URI.
    """
    pid = (paper_id or "").strip()
    sha = (paragraph_sha or "").strip().lower()
    try:
        page_i = int(page)
    except (TypeError, ValueError):
        return ""
    if not pid or page_i <= 0:
        return ""
    base = f"seismohub://paper/{pid}/p{page_i}"
    if sha and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        base = f"{base}#sha1={sha}"
    return base


def resolve_seismohub_uri(uri: str, db) -> Dict:
    """Resolve a ``seismohub://`` URI to the current cache location.

    Returns a dict with keys:
      status          : "ok" | "drifted" | "not_found" | "invalid_uri"
      paper_id        : str | None
      page            : int | None
      paragraph_index : int | None
      char_offset     : int | None
      text_snippet    : str | None
      expected_sha    : str | None
      found_sha       : str | None
      drift_reason    : str | None  ("sha_mismatch" | "page_out_of_range" | "no_cache")
      recovered_via   : str | None  ("sha_lookup" | None)

    Semantics:
      * "ok"          — SHA-1 matched (or page-only URI).  Scroll to result.
      * "drifted"     — page exists but SHA not found on it.  Page-only jump.
      * "not_found"   — no cache row for paper.
      * "invalid_uri" — string did not parse.
    """
    m = _SEISMOHUB_URI_RE.match((uri or "").strip())
    if not m:
        return {
            "status": "invalid_uri", "paper_id": None, "page": None,
            "paragraph_index": None, "char_offset": None, "text_snippet": None,
            "expected_sha": None, "found_sha": None,
            "drift_reason": None, "recovered_via": None,
        }

    paper_id = m.group("paper_id")
    page = int(m.group("page"))
    expected_sha = (m.group("sha1") or "").lower() or None

    cache: Optional[PaperPageCache] = db.get(PaperPageCache, paper_id)
    if not cache or not cache.pages:
        return {
            "status": "not_found", "paper_id": paper_id, "page": page,
            "paragraph_index": None, "char_offset": None, "text_snippet": None,
            "expected_sha": expected_sha, "found_sha": None,
            "drift_reason": "no_cache", "recovered_via": None,
        }

    entry = next((p for p in cache.pages if p.get("page") == page), None)
    if not entry:
        return {
            "status": "drifted", "paper_id": paper_id, "page": page,
            "paragraph_index": None, "char_offset": None, "text_snippet": None,
            "expected_sha": expected_sha, "found_sha": None,
            "drift_reason": "page_out_of_range", "recovered_via": None,
        }

    paragraphs = entry.get("paragraphs") or []

    if not expected_sha:
        # Legacy URI without a SHA fragment — page-only jump, always "ok".
        return {
            "status": "ok", "paper_id": paper_id, "page": page,
            "paragraph_index": None, "char_offset": None, "text_snippet": None,
            "expected_sha": None, "found_sha": None,
            "drift_reason": None, "recovered_via": None,
        }

    # Recovery #1: scan paragraphs on the cited page for matching SHA.
    # This handles the common case where the paragraph moved slots after
    # a re-extraction but still exists on the same page.
    for idx, para in enumerate(paragraphs):
        if (para.get("sha") or "").lower() == expected_sha:
            snippet = (para.get("opening_sentence") or para.get("text") or "")[:120]
            return {
                "status": "ok", "paper_id": paper_id, "page": page,
                "paragraph_index": idx,
                "char_offset": para.get("char_offset"),
                "text_snippet": snippet,
                "expected_sha": expected_sha, "found_sha": expected_sha,
                "drift_reason": None, "recovered_via": "sha_lookup",
            }

    # No SHA hit on the cited page → genuine drift.
    found_sha = ((paragraphs[0].get("sha") if paragraphs else None) or "").lower() or None
    return {
        "status": "drifted", "paper_id": paper_id, "page": page,
        "paragraph_index": None, "char_offset": None, "text_snippet": None,
        "expected_sha": expected_sha, "found_sha": found_sha,
        "drift_reason": "sha_mismatch", "recovered_via": None,
    }
