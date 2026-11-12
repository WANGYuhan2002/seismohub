"""Shared PDF-to-library matching core (2026-07-10, Task D).

ONE authoritative matcher used by both the ``match_pdfs`` endpoint (main.py) and
the ``scripts/match_zotero_export.py`` CLI, so the two ladders can never diverge.
FastAPI-free: depends only on fitz + the small pdf_matching / pdf_naming /
analyze_service helpers + the ORM model, so it is unit-testable in isolation.

Ladder (DOI is authoritative; an existing PDF is NEVER overwritten):
  sha256 already attached  →  DOI exact (bib DOI first, then every scraped DOI)
  →  arXiv id  →  exact normalized title (unique)  →  fuzzy title (+optional LLM
  adjudicator)  →  unmatched. Non-attached bytes are stashed & surfaced by the
  caller — never silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.analyze_service import DOI_RE, FITZ_LOCK, normalize_doi
from services.pdf_matching import (
    TITLE_MATCH_AUTO,
    classify_title_match,
    normalize_title,
    rank_title_candidates,
    title_similarity,
)
from services.pdf_naming import sha256_of_bytes

try:  # fitz is a project dependency; guard so imports never hard-fail
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

_ARXIV_RE = re.compile(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,6})", re.I)

# A DOI *scraped from the PDF body* is trusted as an attach key only when the
# resolved paper's title agrees with the PDF's at least this much — a DOI merely
# CITED on page 1-2 (references / "Correction to:" / footnotes) otherwise wins
# and mis-attaches the upload to the cited paper. A .bib sidecar DOI is exempt.
_DOI_TITLE_MIN = 0.5


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@dataclass
class MatchSignals:
    sha256: str
    dois: List[str] = field(default_factory=list)
    title: str = ""            # best working title (trusted meta title, else heuristic)
    meta_title: str = ""       # embedded XMP/Info title (only when it looks real)
    heuristic_title: str = ""  # first-lines-of-page-1 guess
    authors: str = ""
    year: str = ""
    arxiv_id: str = ""
    text: str = ""

    def title_variants(self) -> List[str]:
        seen: set = set()
        out: List[str] = []
        for t in (self.title, self.heuristic_title, self.meta_title):
            t = (t or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out


@dataclass
class MatchResult:
    action: str                    # attach | conflict | already_attached | needs_review | unmatched
    method: Optional[str] = None   # sha256 | doi | arxiv | title_exact | title_fuzzy | title_llm
    score: float = 0.0
    paper_id: Optional[str] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)


def _dedup(seq) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _doi_candidates(d: str):
    """Yield a scraped DOI, then a fallback with a trailing article/elocation id
    stripped: OUP/GJI print the DOI as '10.1093/gji/ggag244/8719014' (real DOI +
    article id), which the greedy regex over-captures so it never resolves."""
    yield d
    m = re.match(r"(10\.\d{4,9}/.+?)/\d+$", d)
    if m:
        yield m.group(1)


def _heuristic_title(page0_text: str, max_chars: int = 240) -> str:
    """First-lines title heuristic — mirrors main._extract_pdf_title(1580)."""
    lines = [ln.strip() for ln in (page0_text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    out = lines[0][:max_chars]
    for ln in lines[1:5]:
        if len(out) >= max_chars:
            break
        low = ln.lower()
        if "://" in ln or "doi.org" in low or "@" in ln or low.startswith("doi"):
            break  # a URL / DOI / email line is not part of the title
        if "," in ln and ln.count(",") >= 2:
            break  # likely the author list
        if len(ln) < 80 and not ln.endswith((".", "?", "!")):
            out = (out + " " + ln).strip()[:max_chars]
        else:
            break
    return out


def _title_agrees(variants: List[str], resolved_title: str) -> bool:
    """Does a PDF (its title variants) plausibly BELONG to *resolved_title*?

    Deliberately tolerant: the extracted PDF title may carry extra banner/URL
    noise, so accept EITHER a symmetric similarity >= _DOI_TITLE_MIN OR the
    resolved title's tokens being mostly contained in a PDF title variant. A DOI
    that resolves to an UNRELATED paper (a citation on the page) fails both.
    """
    if not variants:
        return True
    rt = set(normalize_title(resolved_title).split())
    if not rt:
        return True
    for v in variants:
        if title_similarity(v, resolved_title) >= _DOI_TITLE_MIN:
            return True
        vt = set(normalize_title(v).split())
        if vt and len(rt & vt) / len(rt) >= 0.7:
            return True
    return False


def _looks_titley(t: str) -> bool:
    """Guard against boilerplate embedded titles ("Microsoft Word - doc1", etc.)."""
    t = (t or "").strip()
    return len(t) >= 12 and " " in t and not t.lower().startswith("microsoft word")


def extract_match_signals(pdf_bytes: bytes, filename: str = "", *,
                          max_pages: int = 2, max_chars: int = 12000) -> MatchSignals:
    """Open the PDF ONCE and pull every cheap matching signal.

    Richer than the retired page-1 / 5000-char / first-DOI-only path: embedded
    XMP/Info title+author (usually the clean title on publisher PDFs), first two
    pages of text, ALL DOIs (findall, not first-only), an arXiv id, and a year.
    """
    sha = sha256_of_bytes(pdf_bytes)
    sig = MatchSignals(sha256=sha)
    if fitz is None:  # no text layer available — filename-only fallback
        sig.dois = _dedup(normalize_doi(d) for d in DOI_RE.findall(filename or ""))
        am = _ARXIV_RE.search(filename or "")
        sig.arxiv_id = am.group(1) if am else ""
        return sig

    text = meta_title = meta_author = year = ""
    try:
        with FITZ_LOCK:
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                meta = doc.metadata or {}
                meta_title = (meta.get("title") or "").strip()
                meta_author = (meta.get("author") or "").strip()
                cd = meta.get("creationDate") or meta.get("modDate") or ""
                ym = re.search(r"(\d{4})", cd)
                year = ym.group(1) if ym else ""
                parts: List[str] = []
                total = 0
                for i in range(min(len(doc), max_pages)):
                    t = doc[i].get_text("text") or ""
                    parts.append(t)
                    total += len(t)
                    if total >= max_chars:
                        break
                text = "\n".join(parts)[:max_chars]
    except Exception:
        text = ""

    sig.meta_title = meta_title if _looks_titley(meta_title) else ""
    sig.heuristic_title = _heuristic_title(text)
    sig.title = sig.meta_title or sig.heuristic_title
    sig.authors = meta_author
    sig.year = year
    sig.text = text
    blob = f"{filename}\n{text}"
    sig.dois = _dedup(normalize_doi(d) for d in DOI_RE.findall(blob))
    am = _ARXIV_RE.search(blob)
    sig.arxiv_id = am.group(1) if am else ""
    return sig


# ---------------------------------------------------------------------------
# Library index (loaded once per batch)
# ---------------------------------------------------------------------------

class LibraryIndex:
    """In-memory lookup over the whole library, built once per match batch.

    Pass ``session`` for the live app, or ``rows`` (an iterable of objects with
    id/title/doi/link/pdf_path/pdf_sha256[/authors/published_date]) for tests.
    """

    def __init__(self, session=None, rows=None):
        if rows is None:
            from models import Paper
            rows = session.query(
                Paper.id, Paper.title, Paper.doi, Paper.link,
                Paper.pdf_path, Paper.pdf_sha256, Paper.authors, Paper.published_date,
            ).all()
        self.by_id: Dict[str, Any] = {}
        self.by_doi: Dict[str, Any] = {}
        self.by_sha: Dict[str, Any] = {}
        self.by_arxiv: Dict[str, Any] = {}
        self.by_norm_title: Dict[str, List[str]] = {}
        self.title_pairs: List = []
        # papers that gained a PDF during THIS batch (so a later file matching the
        # same paper is a conflict, not a silent overwrite).
        self._attached_ids: set = set()
        self._attached_sha: Dict[str, str] = {}
        for r in rows:
            self.by_id[r.id] = r
            if getattr(r, "doi", None):
                self.by_doi.setdefault(r.doi.lower(), r)
            if getattr(r, "pdf_sha256", None):
                self.by_sha.setdefault(r.pdf_sha256, r)
            link = getattr(r, "link", None)
            if link:
                am = _ARXIV_RE.search(link)
                if am:
                    self.by_arxiv.setdefault(am.group(1), r)
            self.title_pairs.append((r.id, r.title))
            nt = normalize_title(r.title)
            if nt:
                self.by_norm_title.setdefault(nt, []).append(r.id)

    def register_attached(self, row, sha: str) -> None:
        """Record an attach done this batch so intra-batch duplicates behave."""
        if not sha:
            return
        self._attached_ids.add(row.id)
        self._attached_sha[row.id] = sha
        self.by_sha.setdefault(sha, row)

    def decide(self, row, sha: str) -> str:
        """attach | conflict | already_attached — NEVER overwrite an existing PDF."""
        if getattr(row, "pdf_sha256", None) and row.pdf_sha256 == sha:
            return "already_attached"
        if self._attached_sha.get(row.id) == sha:
            return "already_attached"
        if getattr(row, "pdf_path", None) or row.id in self._attached_ids:
            return "conflict"
        return "attach"


# ---------------------------------------------------------------------------
# The one ladder
# ---------------------------------------------------------------------------

def match_signals(sig: MatchSignals, lib: LibraryIndex, *,
                  llm_client=None, bib_entry: Optional[Dict[str, Any]] = None) -> MatchResult:
    sha = sig.sha256

    # ① exact same file already attached somewhere
    if sha in lib.by_sha:
        return MatchResult(action="already_attached", method="sha256", score=1.0,
                           paper_id=lib.by_sha[sha].id)

    # ② DOI exact. The .bib DOI (trusted sidecar) is authoritative. A DOI
    # SCRAPED from the PDF is accepted only when the resolved paper's title
    # agrees with the PDF's — otherwise a DOI cited in the body would attach the
    # upload to the cited paper (and re-analyze the wrong document).
    if bib_entry and bib_entry.get("doi"):
        r = lib.by_doi.get(normalize_doi(bib_entry["doi"]).lower())
        if r is not None:
            return MatchResult(action=lib.decide(r, sha), method="doi", score=1.0, paper_id=r.id)
    variants = sig.title_variants()
    for d in _dedup(sig.dois):
        for i, cand in enumerate(_doi_candidates(d)):
            r = lib.by_doi.get(cand.lower())
            if r is None:
                continue
            # i>0 means it resolved ONLY after stripping a trailing /article-id.
            # That suffix is the publisher's own internal id (a citation would use
            # the clean DOI), so this is the paper's OWN DOI → trust without the
            # title guard (publisher first pages often yield a garbage title).
            if i > 0 or _title_agrees(variants, r.title):
                return MatchResult(action=lib.decide(r, sha), method="doi", score=1.0, paper_id=r.id)
            break  # clean DOI resolved but title disagreed → likely cited; next DOI

    # ③ arXiv id (no-DOI preprints) — bib sidecar id first, then the scraped one
    arxiv_ids: List[str] = []
    if bib_entry and bib_entry.get("arxiv_id"):
        arxiv_ids.append(bib_entry["arxiv_id"])
    if sig.arxiv_id:
        arxiv_ids.append(sig.arxiv_id)
    for aid in _dedup(arxiv_ids):
        if aid in lib.by_arxiv:
            r = lib.by_arxiv[aid]
            return MatchResult(action=lib.decide(r, sha), method="arxiv", score=1.0, paper_id=r.id)

    # ④ exact normalized title (bib > working titles), unique hit only
    titles: List[str] = []
    if bib_entry and bib_entry.get("title"):
        titles.append(bib_entry["title"])
    titles.extend(sig.title_variants())
    for t in titles:
        nt = normalize_title(t)
        ids = lib.by_norm_title.get(nt, []) if nt else []
        if len(ids) == 1:
            r = lib.by_id[ids[0]]
            return MatchResult(action=lib.decide(r, sha), method="title_exact", score=1.0, paper_id=r.id)

    # ⑤ fuzzy title
    query = (bib_entry.get("title") if bib_entry else "") or sig.title
    cands = rank_title_candidates(query, lib.title_pairs)
    cand_list = [{"paper_id": cid, "title": lib.by_id[cid].title, "score": cs} for cid, cs in cands]
    if cands:
        if classify_title_match(cands) == "auto":
            r = lib.by_id[cands[0][0]]
            act = lib.decide(r, sha)
            if act == "attach":
                return MatchResult(action="attach", method="title_fuzzy", score=cands[0][1],
                                   paper_id=r.id, candidates=cand_list)
            # unique high title but the target already has a PDF -> human confirm
            return MatchResult(action="needs_review", method="title_fuzzy", score=cands[0][1],
                               paper_id=r.id, candidates=cand_list)
        # mid-confidence / ambiguous -> optional Gemini adjudication, else confirm
        if llm_client is not None:
            picked = llm_adjudicate(sig, cands, lib, llm_client, bib_entry=bib_entry)
            if picked:
                r = lib.by_id[picked]
                return MatchResult(action=lib.decide(r, sha), method="title_llm",
                                   score=cands[0][1], paper_id=r.id, candidates=cand_list)
        return MatchResult(action="needs_review", method="title_fuzzy",
                           score=cands[0][1], candidates=cand_list)

    # ⑥ no match — caller still stashes the bytes
    return MatchResult(action="unmatched", candidates=cand_list)


def llm_adjudicate(sig: MatchSignals, cands, lib: LibraryIndex, client,
                   *, bib_entry: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Ask Gemini which candidate (or none) is the SAME paper. Returns id or None."""
    import json as _json
    from services.analyze_service import (
        QUICK_ANALYSIS_FALLBACK, QUICK_ANALYSIS_MODEL, call_gemini,
    )

    lines = []
    for i, (cid, cs) in enumerate(cands[:5], 1):
        r = lib.by_id[cid]
        lines.append(f'{i}. id={cid} | title="{(r.title or "")[:180]}" | doi={getattr(r, "doi", "") or ""}')
    up_title = (bib_entry.get("title") if bib_entry else "") or sig.title
    up_authors = (bib_entry.get("authors") if bib_entry else "") or sig.authors
    prompt = (
        "You match ONE uploaded PDF to AT MOST ONE library paper. They match ONLY "
        "if they are the SAME article (same authors + same core title meaning; word "
        "reordering / added subtitle / translation / typos are OK). A merely shared "
        "topic is NOT a match. If unsure, return null.\n"
        'Reply ONLY as JSON: {"matched_paper_id": <id-string|null>, "confidence": <0..1>}\n\n'
        f'UPLOADED_PDF: title="{up_title}", authors="{(up_authors or "")[:200]}", '
        f'year={sig.year}, dois={sig.dois}\n'
        "CANDIDATES:\n" + "\n".join(lines)
    )
    try:
        text, _model = call_gemini(client, prompt, model_name=QUICK_ANALYSIS_MODEL,
                                   fallback_model=QUICK_ANALYSIS_FALLBACK)
        m = re.search(r"\{.*\}", text, re.S)
        data = _json.loads(m.group(0) if m else text)
        pid = data.get("matched_paper_id")
        conf = float(data.get("confidence") or 0)
        if pid in {cid for cid, _ in cands} and conf >= 0.80:
            return pid
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Zotero .bib sidecar (used by the CLI; endpoint may pass bib_entry=None)
# ---------------------------------------------------------------------------

_BIB_FIELD = {
    "doi": re.compile(r"\bdoi\s*=\s*\{([^}]+)\}", re.I),
    "title": re.compile(r"\btitle\s*=\s*\{(.+?)\}\s*,\s*\n", re.I | re.S),
    "url": re.compile(r"\burl\s*=\s*\{([^}]+)\}", re.I),
    "date": re.compile(r"\bdate\s*=\s*\{([^}]+)\}", re.I),
    "abstract": re.compile(r"\babstract\s*=\s*\{(.+?)\}\s*,\s*\n", re.I | re.S),
}
_BIB_AUTHOR = re.compile(r"\bauthor\s*=\s*\{(.+?)\}\s*,\s*\n", re.I | re.S)
_BIB_FILE = re.compile(r"\bfile\s*=\s*\{[^}]*?(files/\d+/[^:}]+\.pdf)", re.I)
_BIB_KEY = re.compile(r"^@\w+\{([^,]+),")


def _debrace(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("{", "").replace("}", "")).strip()


def parse_bib(path) -> List[Dict[str, Any]]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    chunks = re.split(r"\n(?=@)", raw)
    entries: List[Dict[str, Any]] = []
    for chunk in chunks:
        if not chunk.lstrip().startswith("@"):
            continue
        key = _BIB_KEY.search(chunk.lstrip())
        title = _BIB_FIELD["title"].search(chunk)
        doi = _BIB_FIELD["doi"].search(chunk)
        url = _BIB_FIELD["url"].search(chunk)
        date = _BIB_FIELD["date"].search(chunk)
        abstract = _BIB_FIELD["abstract"].search(chunk)
        author = _BIB_AUTHOR.search(chunk)
        fil = _BIB_FILE.search(chunk)
        url_v = url.group(1).strip() if url else ""
        arxiv = _ARXIV_RE.search(url_v) or _ARXIV_RE.search(chunk)
        entries.append({
            "citekey": key.group(1) if key else "?",
            "title": _debrace(title.group(1)) if title else "",
            "doi": normalize_doi(doi.group(1).strip()) if doi else "",
            "url": url_v,
            "arxiv_id": arxiv.group(1) if arxiv else "",
            "year": (date.group(1)[:4] if date else ""),
            "authors": _debrace(author.group(1)) if author else "",
            "abstract": _debrace(abstract.group(1)) if abstract else "",
            "file_rel": fil.group(1) if fil else "",
        })
    return entries
