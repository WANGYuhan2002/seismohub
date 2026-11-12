import difflib
import hashlib
import posixpath
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from sqlalchemy import func

from models import Paper
from services.pdf_acquisition.common import PdfValidationError, RetrievalStatus, validate_pdf_bytes
from services.pdf_naming import (
    DOWNLOADS_DIR,
    is_managed_download_path,
    pdf_target_path_for_paper,
    write_pdf_atomic,
)


class ZoteroImportError(ValueError):
    pass


_ENTRY_RE = re.compile(r"@([A-Za-z]+)\s*[{(]", re.MULTILINE)
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", re.IGNORECASE)


def normalize_upload_path(path: str) -> str:
    raw = unquote((path or "").replace("\\", "/")).strip()
    raw = raw.lstrip("./")
    parts = []
    for part in PurePosixPath(raw).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ZoteroImportError(f"Unsafe path in upload: {path}")
        parts.append(part)
    return "/".join(parts)


def _is_noise_path(path: str) -> bool:
    lowered = path.lower()
    return (
        not path
        or "/__macosx/" in f"/{lowered}/"
        or lowered.startswith("__macosx/")
        or lowered.endswith("/.ds_store")
        or lowered.endswith(".ds_store")
    )


def _clean_bib_value(value: str) -> str:
    text = (value or "").strip().strip(",")
    if len(text) >= 2 and ((text[0] == "{" and text[-1] == "}") or (text[0] == '"' and text[-1] == '"')):
        text = text[1:-1]
    text = text.replace(r"\&", "&")
    text = text.replace(r"\_", "_")
    text = text.replace(r"\%", "%")
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_doi(value: str) -> str:
    doi = _clean_bib_value(value)
    doi = _DOI_PREFIX_RE.sub("", doi).strip()
    return doi.lower()


# FN-B3-A05-1 (LOW pass, 2026-05-19): Named constant for the abstract-length
# threshold so the magic 100 below is greppable / tunable. The sentinel value
# "ok" (not "present") is the canonical value the spec calls out.
MIN_ABSTRACT_CHARS = 100


def _abstract_flag(abstract: str) -> str:
    """Return 'ok' if abstract is >= MIN_ABSTRACT_CHARS, 'short' if shorter, 'missing' if empty."""
    abstract = (abstract or "").strip()
    if not abstract:
        return "missing"
    return "ok" if len(abstract) >= MIN_ABSTRACT_CHARS else "short"


def _stable_id(*parts: str) -> str:
    raw = "||".join([part or "" for part in parts]).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def _read_balanced_entry(text: str, start: int) -> Tuple[str, int]:
    opener = text[start]
    closer = "}" if opener == "{" else ")"
    depth = 0
    in_quote = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"' and depth > 0:
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    raise ZoteroImportError("Could not parse BibTeX entry with balanced braces.")


def _split_top_level_fields(body: str) -> Tuple[str, str]:
    comma_index = body.find(",")
    if comma_index < 0:
        return "", ""
    return body[:comma_index].strip(), body[comma_index + 1 :]


def _parse_fields(fields_text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    index = 0
    length = len(fields_text)
    while index < length:
        while index < length and fields_text[index] in " \t\r\n,":
            index += 1
        name_start = index
        while index < length and re.match(r"[A-Za-z0-9_\-]", fields_text[index]):
            index += 1
        name = fields_text[name_start:index].strip().lower()
        if not name:
            break
        while index < length and fields_text[index].isspace():
            index += 1
        if index >= length or fields_text[index] != "=":
            break
        index += 1
        while index < length and fields_text[index].isspace():
            index += 1
        if index >= length:
            break

        if fields_text[index] in "{(":
            opener = fields_text[index]
            closer = "}" if opener == "{" else ")"
            depth = 0
            value_start = index
            while index < length:
                char = fields_text[index]
                if char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            value = fields_text[value_start:index]
        elif fields_text[index] == '"':
            value_start = index
            index += 1
            escaped = False
            while index < length:
                char = fields_text[index]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    index += 1
                    break
                index += 1
            value = fields_text[value_start:index]
        else:
            value_start = index
            while index < length and fields_text[index] != ",":
                index += 1
            value = fields_text[value_start:index]
        fields[name] = _clean_bib_value(value)
    return fields


def parse_bibtex(text: str, bib_path: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    cursor = 0
    while True:
        match = _ENTRY_RE.search(text, cursor)
        if not match:
            break
        opener_index = match.end() - 1
        body, end = _read_balanced_entry(text, opener_index)
        citekey, fields_text = _split_top_level_fields(body)
        fields = _parse_fields(fields_text)
        if fields:
            entries.append(
                {
                    "entry_type": match.group(1).lower(),
                    "citekey": citekey,
                    "fields": fields,
                    "bib_path": bib_path,
                    "bib_dir": posixpath.dirname(bib_path),
                }
            )
        cursor = end
    return entries


def _extract_file_paths(file_field: str) -> List[str]:
    paths: List[str] = []
    for raw_item in (file_field or "").split(";"):
        item = raw_item.strip()
        if not item:
            continue
        mime = ""
        left = item
        if ":" in item:
            left, mime = item.rsplit(":", 1)
        if ":" in left:
            _, candidate = left.split(":", 1)
        else:
            candidate = left
        candidate = _clean_bib_value(candidate)
        if candidate and (candidate.lower().endswith(".pdf") or "pdf" in mime.lower()):
            paths.append(candidate.replace("\\", "/"))
    return paths


def _resolve_attachment(
    file_path: str,
    *,
    bib_dir: str,
    pdf_files: Dict[str, bytes],
    lower_pdf_paths: Dict[str, str],
) -> Tuple[Optional[str], Optional[bytes]]:
    candidates = []
    clean_path = normalize_upload_path(file_path)
    if bib_dir:
        candidates.append(normalize_upload_path(posixpath.normpath(posixpath.join(bib_dir, clean_path))))
    candidates.append(clean_path)
    for candidate in candidates:
        if candidate in pdf_files:
            return candidate, pdf_files[candidate]
        lowered = candidate.lower()
        if lowered in lower_pdf_paths:
            resolved = lower_pdf_paths[lowered]
            return resolved, pdf_files[resolved]
    return None, None


def _parse_authors(value: str) -> List[str]:
    return [_clean_bib_value(author) for author in re.split(r"\s+and\s+", value or "") if _clean_bib_value(author)]


def _parse_keywords(value: str) -> List[str]:
    keywords = []
    seen = set()
    for raw in re.split(r"[,;]", value or ""):
        keyword = _clean_bib_value(raw)
        key = keyword.lower()
        if keyword and key not in seen:
            keywords.append(keyword)
            seen.add(key)
    return keywords


def _entry_date(fields: Dict[str, str]) -> str:
    date_value = _clean_bib_value(fields.get("date") or "")
    if re.match(r"^\d{4}(-\d{2})?(-\d{2})?$", date_value):
        return date_value
    year = _clean_bib_value(fields.get("year") or "")
    if re.match(r"^\d{4}$", year):
        return year
    return ""


def _entry_year(fields: Dict[str, str]) -> str:
    date_value = _entry_date(fields)
    return date_value[:4] if len(date_value) >= 4 else ""


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean_bib_value(value).lower()).strip()


def _title_similarity(a: str, b: str) -> float:
    """Token-set containment ratio: |a∩b| / min(|a|, |b|) over normalized tokens.

    Length-invariant — far better than `SequenceMatcher` for the common
    case where a BibTeX entry uses a shortened/truncated title (e.g. only
    the part before a colon, or just the publication-display form) that the
    publisher uses while the stored Paper carries the full long-form title.
    FN-B3-A04-1 (VERIFY-L, 2026-05-19): `SequenceMatcher` ratio of 0.4688
    for the short Niu title vs the full title rejected the match even at
    threshold 0.92, and plain Jaccard scored 0.25 (since the long title's
    20 unique tokens overwhelm the short title's 6). Containment ratio
    asks instead: of the shorter title's tokens, how many appear in the
    longer one? — which is exactly the BibTeX-truncation question.
    """
    if not a or not b:
        return 0.0
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# Token-set containment threshold. 0.75 means "at least 75% of the shorter
# title's tokens must appear in the longer title", which is forgiving of
# 1-2 missing words (typos, stop-word drops) but rejects unrelated papers
# that happen to share a single common word. FN-B3-A04-1 (VERIFY-L,
# 2026-05-19): replaces 0.92 SequenceMatcher threshold that rejected
# realistic publisher truncations.
TITLE_MATCH_THRESHOLD = 0.75


def _find_existing_paper(db_session, fields: Dict[str, str]) -> Optional[Paper]:
    doi = _normalize_doi(fields.get("doi") or "")
    if doi:
        paper = (
            db_session.query(Paper)
            .filter(func.lower(Paper.doi) == doi.lower())
            .order_by(Paper.updated_at.desc())
            .first()
        )
        if paper is not None:
            return paper

    title_key = _normalized_title(fields.get("title") or "")
    year = _entry_year(fields)
    if not title_key:
        return None

    # Restrict candidate set to the same publication year if year is known,
    # so the O(N) similarity scan stays cheap on large libraries.
    query = db_session.query(Paper)
    if year:
        query = query.filter(Paper.published_date.like(f"{year}%"))
    candidates = query.all()

    best_match = None
    best_ratio = TITLE_MATCH_THRESHOLD
    for cand in candidates:
        cand_key = _normalized_title(cand.title or "")
        if not cand_key:
            continue
        ratio = _title_similarity(title_key, cand_key)
        if ratio >= best_ratio:
            best_ratio = ratio
            best_match = cand
    return best_match


def _allocate_zotero_id(db_session, fields: Dict[str, str]) -> str:
    # FN-B1-A03-1 (LOW pass, 2026-05-19): Prefix ids with `zotero_` so they are
    # self-describing and the documented cleanup SQL (`WHERE id LIKE
    # 'zotero_%'`) matches. Existing pure-hex zotero rows in production keep
    # their ids — no migration. Only NEW imports get the prefix.
    basis = _normalize_doi(fields.get("doi") or "") or fields.get("url") or fields.get("title") or fields.get("citekey") or "zotero"
    for index in range(1, 1000):
        candidate = "zotero_" + _stable_id("zotero", basis if index == 1 else f"{basis}::{index}")
        if db_session.get(Paper, candidate) is None:
            return candidate
    raise ZoteroImportError("Could not allocate a unique paper id for Zotero entry.")


def _venue_for_entry(entry_type: str, fields: Dict[str, str]) -> str:
    """Return the most informative venue string for the entry type."""
    journal = (_clean_bib_value(fields.get("journaltitle") or "")
               or _clean_bib_value(fields.get("journal") or ""))
    if journal:
        return journal
    if entry_type in ("@inproceedings", "inproceedings", "conference", "incollection"):
        return _clean_bib_value(fields.get("booktitle") or "") or "Others"
    if entry_type in ("book",):
        return _clean_bib_value(fields.get("publisher") or "") or "Others"
    if entry_type in ("phdthesis", "mastersthesis"):
        return _clean_bib_value(fields.get("school") or "") or "Others"
    if entry_type == "techreport":
        return _clean_bib_value(fields.get("institution") or "") or "Others"
    if entry_type == "misc":
        return _clean_bib_value(fields.get("howpublished") or "") or "Others"
    return _clean_bib_value(fields.get("booktitle") or "") or "Others"


def _create_paper_from_entry(db_session, entry: Dict[str, Any]) -> Paper:
    fields = entry["fields"]
    title = _clean_bib_value(fields.get("title") or fields.get("booktitle") or "Untitled Zotero Import")
    doi = _normalize_doi(fields.get("doi") or "")
    published_date = _entry_date(fields)
    authors = _parse_authors(fields.get("author") or "")
    keywords = _parse_keywords(fields.get("keywords") or "")
    journal = _venue_for_entry(entry["entry_type"], fields)
    abstract = _clean_bib_value(fields.get("abstract") or "")
    paper = Paper(
        id=_allocate_zotero_id(db_session, fields),
        source="zotero",
        title=title,
        journal=journal,
        topics=keywords or ["Others"],
        institutions=[],
        authors=authors,
        published_date=published_date or None,
        datetime_utc=published_date or None,
        link=_clean_bib_value(fields.get("url") or (f"https://doi.org/{doi}" if doi else "")),
        doi=doi,
        abstract=abstract,
        abstract_flag=_abstract_flag(abstract),
    )
    db_session.add(paper)
    db_session.flush()
    return paper


def _fill_missing_metadata(paper: Paper, fields: Dict[str, str]) -> None:
    if (not paper.authors) and fields.get("author"):
        paper.authors = _parse_authors(fields.get("author") or "")
    if (not paper.published_date) and _entry_date(fields):
        paper.published_date = _entry_date(fields)
    if (not paper.datetime_utc) and paper.published_date:
        paper.datetime_utc = paper.published_date
    if (not paper.doi) and fields.get("doi"):
        paper.doi = _normalize_doi(fields.get("doi") or "")
    if (not paper.link) and fields.get("url"):
        paper.link = _clean_bib_value(fields.get("url") or "")
    if (not paper.abstract) and fields.get("abstract"):
        paper.abstract = _clean_bib_value(fields.get("abstract") or "")
        paper.abstract_flag = _abstract_flag(paper.abstract or "")
    if (not paper.journal or paper.journal == "Others") and (fields.get("journaltitle") or fields.get("journal")):
        paper.journal = _clean_bib_value(fields.get("journaltitle") or fields.get("journal") or "Others")
    if (not paper.topics or paper.topics == ["Others"]) and fields.get("keywords"):
        paper.topics = _parse_keywords(fields.get("keywords") or "") or ["Others"]


def import_zotero_files(
    db_session,
    uploaded_files: Dict[str, bytes],
    *,
    replace_existing: bool = False,
    replace_paper_ids: Optional[set[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    replace_paper_ids = replace_paper_ids or set()
    normalized_files: Dict[str, bytes] = {}
    warnings: List[str] = []
    for path, data in uploaded_files.items():
        try:
            normalized = normalize_upload_path(path)
        except ZoteroImportError as exc:
            warnings.append(str(exc))
            continue
        if _is_noise_path(normalized):
            continue
        normalized_files[normalized] = data

    bib_paths = [path for path in normalized_files if path.lower().endswith(".bib")]
    if not bib_paths:
        raise ZoteroImportError("No .bib file found in the Zotero export.")
    preferred = [path for path in bib_paths if Path(path).name.lower() == "exported items.bib"]
    if preferred:
        bib_path = preferred[0]
    elif len(bib_paths) == 1:
        bib_path = bib_paths[0]
    else:
        raise ZoteroImportError("Multiple .bib files found. Keep one BibTeX export file in the Zotero folder.")

    try:
        bib_text = normalized_files[bib_path].decode("utf-8-sig")
    except UnicodeDecodeError:
        bib_text = normalized_files[bib_path].decode("latin-1")

    entries = parse_bibtex(bib_text, bib_path)
    pdf_files = {path: data for path, data in normalized_files.items() if path.lower().endswith(".pdf")}
    lower_pdf_paths = {path.lower(): path for path in pdf_files}
    referenced_paths = set()
    queued_analysis: List[str] = []

    report: Dict[str, Any] = {
        "entries": len(entries),
        "matched_existing": 0,
        "created": 0,
        "pdf_saved": 0,
        "skipped_existing_pdf": 0,
        "missing_pdf": 0,
        "invalid_pdf": 0,
        "orphan_pdfs": [],
        "queued_analysis": 0,
        "warnings": warnings,
        "created_paper_ids": [],
        "queued_analysis_paper_ids": queued_analysis,
        "missing_pdf_items": [],
        "invalid_pdf_items": [],
        "duplicate_existing_pdf_items": [],
    }

    for entry in entries:
        fields = entry["fields"]
        paper = _find_existing_paper(db_session, fields)
        if paper is None:
            if dry_run:
                continue
            paper = _create_paper_from_entry(db_session, entry)
            report["created"] += 1
            report["created_paper_ids"].append(paper.id)
        else:
            report["matched_existing"] += 1
            if not dry_run:
                _fill_missing_metadata(paper, fields)

        file_paths = _extract_file_paths(fields.get("file") or "")
        if not file_paths:
            continue
        if len(file_paths) > 1:
            report["warnings"].append(
                f"{fields.get('title') or entry.get('citekey')}: multiple PDF attachments found; imported the first valid PDF."
            )

        resolved_attachments: List[Tuple[str, bytes]] = []
        for file_path in file_paths:
            candidate_path, candidate_data = _resolve_attachment(
                file_path,
                bib_dir=entry["bib_dir"],
                pdf_files=pdf_files,
                lower_pdf_paths=lower_pdf_paths,
            )
            if candidate_path:
                referenced_paths.add(candidate_path)
                if candidate_data is not None:
                    resolved_attachments.append((candidate_path, candidate_data))

        if not resolved_attachments:
            report["missing_pdf"] += 1
            report["missing_pdf_items"].append(
                {
                    "title": fields.get("title") or entry.get("citekey") or "",
                    "file": file_paths[0],
                }
            )
            continue

        resolved_path, pdf_data = resolved_attachments[0]

        existing_pdf_path = Path(paper.pdf_path).resolve() if paper.pdf_path else None
        existing_pdf_present = bool(existing_pdf_path and existing_pdf_path.exists())
        if existing_pdf_present and (dry_run or (not replace_existing and paper.id not in replace_paper_ids)):
            report["skipped_existing_pdf"] += 1
            report["duplicate_existing_pdf_items"].append(
                {
                    "paper_id": paper.id,
                    "title": paper.title or fields.get("title") or entry.get("citekey") or "",
                    "doi": paper.doi or _normalize_doi(fields.get("doi") or ""),
                    "journal": paper.journal or "",
                    "published_date": paper.published_date or "",
                    "current_pdf": existing_pdf_path.name if existing_pdf_path else "",
                    "zotero_pdf": resolved_path,
                }
            )
            continue

        if dry_run:
            continue

        try:
            validate_pdf_bytes(
                pdf_data,
                f"zotero://{resolved_path}",
                min_bytes=1024,
                max_bytes=100_000_000,
                content_type="application/pdf",
            )
        except PdfValidationError as exc:
            report["invalid_pdf"] += 1
            report["invalid_pdf_items"].append(
                {
                    "title": fields.get("title") or entry.get("citekey") or "",
                    "file": resolved_path,
                    "reason": exc.detail,
                }
            )
            continue

        target_path = pdf_target_path_for_paper(
            paper,
            DOWNLOADS_DIR,
            current_path=existing_pdf_path if existing_pdf_path and is_managed_download_path(existing_pdf_path) else None,
        )
        saved_path = Path(write_pdf_atomic(target_path, pdf_data)).resolve()
        if existing_pdf_path and existing_pdf_path != saved_path and is_managed_download_path(existing_pdf_path):
            try:
                existing_pdf_path.unlink()
            except OSError:
                report["warnings"].append(f"Could not remove old PDF: {existing_pdf_path.name}")

        paper.pdf_path = str(saved_path)
        paper.retrieval_status = RetrievalStatus.SUCCESS
        paper.retrieval_source = "zotero_import"
        paper.retrieval_source_url = resolved_path
        paper.retrieval_last_stage = "zotero"
        paper.retrieval_error_reason = None
        paper.retrieval_next_retry_at = None
        paper.retrieval_succeeded_at = datetime.utcnow()
        paper.updated_at = datetime.utcnow()
        report["pdf_saved"] += 1
        if paper.id not in queued_analysis:
            queued_analysis.append(paper.id)

    for path, data in sorted(pdf_files.items()):
        if path not in referenced_paths:
            report["orphan_pdfs"].append(
                {
                    "path": path,
                    "filename": Path(path).name,
                    "size_bytes": len(data),
                    "reason": "PDF was present in the upload but was not referenced by any BibTeX file field.",
                }
            )

    report["queued_analysis"] = len(queued_analysis)
    if not dry_run:
        db_session.commit()
    return report


def find_duplicate_existing_pdfs(
    db_session,
    uploaded_files: Dict[str, bytes],
) -> Dict[str, Any]:
    report = import_zotero_files(
        db_session,
        uploaded_files,
        replace_existing=False,
        replace_paper_ids=set(),
        dry_run=True,
    )
    return {
        "entries": report.get("entries", 0),
        "duplicate_existing_pdf_items": report.get("duplicate_existing_pdf_items", []),
        "duplicate_existing_pdf": len(report.get("duplicate_existing_pdf_items", [])),
    }
