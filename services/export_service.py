"""Bibliographic export (M1 must-do #2).

Single source of truth: a paper dict (the same shape `_paper_to_dict()` emits)
is normalized to CSL-JSON, then rendered to BibTeX / RIS / Markdown / a PDF
ZIP. All exporters take a `List[Dict]` and a `target` stream; no exporter
loads the whole library into memory.

Public entrypoints used by `main.py`:

- :func:`papers_to_csl_json` — list of paper dicts -> CSL-JSON list
- :func:`papers_to_bibtex_text` — list of paper dicts -> BibTeX string
- :func:`papers_to_ris_text` — list of paper dicts -> RIS string
- :func:`stream_pdfs_zip` — generator yielding ZIP chunks of all PDFs
- :func:`stream_all_zip` — generator yielding ZIP chunks of bib + ris + PDFs
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


# ----------------------------------------------------------------------
# Normalization (paper dict -> CSL-JSON)
# ----------------------------------------------------------------------

# Author name patterns: "Last, First" / "First Last" / "Last F." / "Surname".
_NAME_COMMA_RE = re.compile(r"^\s*([^,]+),\s*(.+)\s*$")


def _split_author(raw: str) -> Dict[str, str]:
    """Best-effort split of a single author string into family + given parts.

    Handles "Wang, Xiaoming", "Xiaoming Wang", "X. Wang", "王小明" (CJK) and falls
    back to `literal` for unparseable strings (e.g. consortium names).
    """
    raw = (raw or "").strip()
    if not raw:
        return {"literal": "Unknown"}

    # "Last, First" form
    m = _NAME_COMMA_RE.match(raw)
    if m:
        return {"family": m.group(1).strip(), "given": m.group(2).strip()}

    # CJK names — keep as literal (no space-split) so 王小明 stays intact.
    if re.search(r"[一-鿿]", raw):
        return {"family": raw, "given": ""}

    # "First Last" / "Y. Wang" — assume the LAST whitespace-token is family.
    tokens = raw.split()
    if len(tokens) >= 2:
        return {"family": tokens[-1], "given": " ".join(tokens[:-1])}

    # Single token
    return {"literal": raw}


def _csl_authors(paper: Dict[str, Any]) -> List[Dict[str, str]]:
    raw = paper.get("authors") or []
    if isinstance(raw, str):
        raw = [raw]
    out: List[Dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, dict):
            # Already in CSL-shape (rare for this codebase)
            out.append({k: str(v) for k, v in entry.items() if v is not None})
        else:
            out.append(_split_author(str(entry)))
    return out or [{"literal": "Unknown"}]


def _csl_issued(paper: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert `published_date` (YYYY[-MM[-DD]]) -> CSL `issued.date-parts`."""
    raw = (paper.get("published_date") or "").strip()
    if not raw:
        return None
    parts = re.split(r"[-/]", raw)
    nums: List[int] = []
    for piece in parts[:3]:
        try:
            nums.append(int(piece))
        except ValueError:
            break
    if not nums:
        return None
    return {"date-parts": [nums]}


_CITEKEY_BAD = re.compile(r"[^A-Za-z0-9]+")


def _citekey(paper: Dict[str, Any], existing: set[str]) -> str:
    """Generate an AGU-style citekey: `Surname2024_first-keyword`.

    Falls back to `Anon2024` when authors missing. Dedup-suffixes `a/b/...`
    when collisions occur within the current export set.
    """
    authors = _csl_authors(paper)
    first = authors[0]
    surname = (first.get("family") or first.get("literal") or "Anon").strip()
    surname = _CITEKEY_BAD.sub("", surname) or "Anon"

    year = "n.d."
    issued = _csl_issued(paper)
    if issued and issued.get("date-parts") and issued["date-parts"][0]:
        year = str(issued["date-parts"][0][0])

    keywords = paper.get("keywords") or []
    kw = ""
    if isinstance(keywords, list) and keywords:
        first_kw = str(keywords[0])
        kw_clean = _CITEKEY_BAD.sub("", first_kw)
        if kw_clean:
            kw = "_" + kw_clean[:18]

    base = f"{surname}{year}{kw}"
    candidate = base
    suffix_idx = 0
    while candidate in existing:
        candidate = base + chr(ord("a") + suffix_idx)
        suffix_idx += 1
        if suffix_idx > 25:
            candidate = f"{base}_{suffix_idx}"
    existing.add(candidate)
    return candidate


def paper_to_csl(paper: Dict[str, Any], *, citekey: Optional[str] = None) -> Dict[str, Any]:
    """Convert a paper dict (from `_paper_to_dict()`) into a CSL-JSON entry."""
    csl: Dict[str, Any] = {
        "id": citekey or paper.get("id") or "unknown",
        "type": "article-journal",
        "title": (paper.get("title") or "Untitled").strip(),
        "author": _csl_authors(paper),
    }
    if paper.get("journal"):
        csl["container-title"] = paper["journal"]
    if paper.get("doi"):
        csl["DOI"] = paper["doi"]
    if paper.get("link"):
        csl["URL"] = paper["link"]
    if paper.get("abstract"):
        csl["abstract"] = paper["abstract"]
    issued = _csl_issued(paper)
    if issued:
        csl["issued"] = issued
    keywords = paper.get("keywords") or []
    if isinstance(keywords, list) and keywords:
        csl["keyword"] = ", ".join(str(k) for k in keywords if k)
    institutions = paper.get("institutions") or []
    if isinstance(institutions, list) and institutions:
        csl["publisher"] = institutions[0]
    return csl


def papers_to_csl_json(papers: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    return [paper_to_csl(p, citekey=_citekey(p, seen)) for p in papers]


# ----------------------------------------------------------------------
# BibTeX
# ----------------------------------------------------------------------

# LaTeX-special character escapes for inside a `{...}` BibTeX value.
# We do NOT escape `{ }` because they are used for grouping in our output.
_LATEX_ESCAPES = [
    ("\\", r"\textbackslash{}"),  # must run before any other backslash insertion
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("^", r"\^{}"),
    ("~", r"\~{}"),
]


def _bibtex_escape(value: str) -> str:
    if not value:
        return ""
    out = value
    for src, dst in _LATEX_ESCAPES:
        out = out.replace(src, dst)
    return out


def _format_bibtex_authors(authors: List[Dict[str, str]]) -> str:
    parts: List[str] = []
    for a in authors:
        if "literal" in a and a["literal"]:
            parts.append(f"{{{_bibtex_escape(a['literal'])}}}")
        else:
            fam = _bibtex_escape(a.get("family", ""))
            giv = _bibtex_escape(a.get("given", ""))
            if fam and giv:
                parts.append(f"{fam}, {giv}")
            elif fam:
                parts.append(fam)
    return " and ".join(parts) or "Unknown"


def paper_to_bibtex(paper: Dict[str, Any], *, citekey: Optional[str] = None) -> str:
    csl = paper_to_csl(paper, citekey=citekey)
    fields: List[tuple[str, str]] = []
    fields.append(("author", _format_bibtex_authors(csl["author"])))
    fields.append(("title", _bibtex_escape(csl["title"])))
    if csl.get("container-title"):
        fields.append(("journal", _bibtex_escape(csl["container-title"])))
    issued = csl.get("issued") or {}
    if issued.get("date-parts") and issued["date-parts"][0]:
        fields.append(("year", str(issued["date-parts"][0][0])))
    if csl.get("DOI"):
        fields.append(("doi", _bibtex_escape(csl["DOI"])))
    if csl.get("URL"):
        fields.append(("url", _bibtex_escape(csl["URL"])))
    if csl.get("abstract"):
        # Truncate huge abstracts so the .bib stays parseable by older tools.
        abstract = csl["abstract"]
        if len(abstract) > 4000:
            abstract = abstract[:4000].rstrip() + "…"
        fields.append(("abstract", _bibtex_escape(abstract)))
    if csl.get("keyword"):
        fields.append(("keywords", _bibtex_escape(csl["keyword"])))
    if csl.get("publisher"):
        fields.append(("institution", _bibtex_escape(csl["publisher"])))

    body = ",\n".join(f"  {key} = {{{value}}}" for key, value in fields if value)
    return f"@article{{{csl['id']},\n{body}\n}}"


def papers_to_bibtex_text(papers: Iterable[Dict[str, Any]]) -> str:
    seen: set[str] = set()
    chunks: List[str] = [
        "% BibTeX export generated by SWP Paper Hub",
        f"% Exported at {datetime.utcnow().isoformat(timespec='seconds')}Z",
        "",
    ]
    for paper in papers:
        ck = _citekey(paper, seen)
        chunks.append(paper_to_bibtex(paper, citekey=ck))
        chunks.append("")
    return "\n".join(chunks)


# ----------------------------------------------------------------------
# RIS (delegated to existing main._build_ris_text via a small shim)
# ----------------------------------------------------------------------

def papers_to_ris_text(papers: List[Dict[str, Any]]) -> str:
    """Render RIS via main's existing helper (kept here so /api/export/all
    has a single import point and a future migration can move it locally
    without touching the route handlers)."""
    from main import _build_ris_text  # local import: avoid circular

    return _build_ris_text(papers)


# ----------------------------------------------------------------------
# ZIP streaming
# ----------------------------------------------------------------------


class _ZipBuffer(io.RawIOBase):
    """Tiny stream that lets a `ZipFile(stream)` push bytes into a list."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:  # type: ignore[override]
        self._buf.extend(data)
        return len(data)

    def take(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


def _safe_archive_name(name: str) -> str:
    """Sanitize a filename for inclusion inside a ZIP.

    Forward slashes are preserved so callers can route entries into
    subdirectories (e.g. ``pdfs/foo.pdf``). Backslashes and control bytes
    are stripped because they can confuse legacy unzip tools.
    """
    name = name.replace("\\", "_")
    name = re.sub(r"[\x00-\x1f]", "", name)
    # Reject `..` traversal at any segment boundary.
    parts = [seg for seg in name.split("/") if seg and seg != "."]
    parts = [seg for seg in parts if seg != ".."]
    return "/".join(parts) or "untitled.pdf"


def _iter_zip_entries(
    entries: List[tuple[str, bytes | Path]],
    *,
    flush_every_bytes: int = 1 << 20,
) -> Iterator[bytes]:
    """Stream a ZIP containing `entries` (name, bytes_or_path) tuples.

    Bytes entries are added in-memory (small files like bib/ris/manifest);
    Path entries are streamed in chunks so 100MB PDFs don't blow RAM.
    """
    buf = _ZipBuffer()
    seen_names: dict[str, int] = {}
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for raw_name, payload in entries:
            name = _safe_archive_name(raw_name)
            # Dedup-suffix collisions inside the ZIP
            base = name
            i = 2
            while name in seen_names:
                stem = Path(base).stem
                suffix = Path(base).suffix
                name = f"{stem}_{i}{suffix}"
                i += 1
            seen_names[name] = 1

            if isinstance(payload, (bytes, bytearray)):
                zf.writestr(name, bytes(payload))
                if len(buf._buf) >= flush_every_bytes:
                    yield buf.take()
            else:
                path = Path(payload)
                if not path.is_file():
                    continue
                with zf.open(name, mode="w", force_zip64=True) as zip_entry:
                    with open(path, "rb") as fh:
                        for chunk in iter(lambda: fh.read(64 * 1024), b""):
                            zip_entry.write(chunk)
                            if len(buf._buf) >= flush_every_bytes:
                                yield buf.take()
    # Yield the central directory + any tail bytes
    tail = buf.take()
    if tail:
        yield tail


def stream_pdfs_zip(papers: List[Dict[str, Any]], downloads_dir: Path) -> Iterator[bytes]:
    """Generator that streams a ZIP of all PDFs from `papers`.

    Only papers with `has_pdf=True` AND a readable file inside `downloads_dir`
    are included. Missing PDFs are silently skipped.
    """
    entries: List[tuple[str, bytes | Path]] = []
    for p in papers:
        if not p.get("has_pdf"):
            continue
        pdf_url = p.get("pdf_url") or ""
        filename = Path(pdf_url).name if pdf_url else ""
        if not filename:
            continue
        local = downloads_dir / filename
        if local.is_file():
            entries.append((filename, local))
    yield from _iter_zip_entries(entries)


def stream_all_zip(
    papers: List[Dict[str, Any]],
    downloads_dir: Path,
    *,
    filter_snapshot: Optional[Dict[str, Any]] = None,
) -> Iterator[bytes]:
    """Stream a ZIP containing `papers.bib`, `papers.ris`, `manifest.json`,
    and a `pdfs/` folder with every available PDF."""
    bib_text = papers_to_bibtex_text(papers).encode("utf-8")
    ris_text = papers_to_ris_text(papers).encode("utf-8")
    csl_json = json.dumps(papers_to_csl_json(papers), ensure_ascii=False, indent=2).encode("utf-8")

    manifest = {
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "paper_count": len(papers),
        "papers_with_pdf": sum(1 for p in papers if p.get("has_pdf")),
        "filter_snapshot": filter_snapshot or {},
        "files": ["papers.bib", "papers.ris", "papers.csl.json", "pdfs/"],
        "generator": "SWP Paper Hub Export v1",
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    entries: List[tuple[str, bytes | Path]] = [
        ("papers.bib", bib_text),
        ("papers.ris", ris_text),
        ("papers.csl.json", csl_json),
        ("manifest.json", manifest_bytes),
    ]
    for p in papers:
        if not p.get("has_pdf"):
            continue
        pdf_url = p.get("pdf_url") or ""
        filename = Path(pdf_url).name if pdf_url else ""
        if not filename:
            continue
        local = downloads_dir / filename
        if local.is_file():
            entries.append((f"pdfs/{filename}", local))
    yield from _iter_zip_entries(entries)


__all__ = [
    "paper_to_csl",
    "papers_to_csl_json",
    "paper_to_bibtex",
    "papers_to_bibtex_text",
    "papers_to_ris_text",
    "stream_pdfs_zip",
    "stream_all_zip",
]
