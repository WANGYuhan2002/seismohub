import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Optional


BASE_DIR = Path(__file__).resolve().parents[1]
DOWNLOADS_DIR = BASE_DIR / "downloads"
MAX_FILENAME_STEM = 190


def _clean_component(value: Any, *, fallback: str, max_len: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    text = text.replace("&amp;", "and")
    text = re.sub(r"[\u2010-\u2015\u2212]+", "-", text)
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", text)
    text = re.sub(r"[{}\\]+", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-. ")
    if not text:
        text = fallback
    if len(text) > max_len:
        text = text[:max_len].rstrip("-. ")
    return text or fallback


def _date_component(paper: Any) -> str:
    value = str(getattr(paper, "published_date", "") or "").strip()
    if len(value) >= 7 and re.match(r"^\d{4}-\d{2}", value):
        return value[:7]
    if len(value) >= 4 and re.match(r"^\d{4}", value):
        return value[:4]

    datetime_value = str(getattr(paper, "datetime_utc", "") or "").strip()
    if len(datetime_value) >= 7 and re.match(r"^\d{4}-\d{2}", datetime_value):
        return datetime_value[:7]
    if len(datetime_value) >= 4 and re.match(r"^\d{4}", datetime_value):
        return datetime_value[:4]
    return "Undated"


def _first_author_component(paper: Any) -> str:
    authors = getattr(paper, "authors", None) or []
    if not isinstance(authors, list):
        authors = [str(authors)]
    first = str(authors[0] if authors else "").strip()
    first = re.sub(r"[{}]", "", first)
    if not first:
        return "Unknown"
    if "," in first:
        candidate = first.split(",", 1)[0].strip()
    else:
        pieces = [piece for piece in re.split(r"\s+", first) if piece]
        candidate = pieces[-1] if pieces else first
    return _clean_component(candidate, fallback="Unknown", max_len=36)


def _clip_stem(stem: str, *, suffix: str = "") -> str:
    limit = MAX_FILENAME_STEM - len(suffix)
    if len(stem) <= limit:
        return stem + suffix
    return stem[:limit].rstrip("-. ") + suffix


def build_pdf_filename(paper: Any, *, suffix: Optional[int] = None) -> str:
    paper_id = _clean_component(getattr(paper, "id", "") or "paper", fallback="paper", max_len=64)
    date_part = _clean_component(_date_component(paper), fallback="Undated", max_len=16)
    author = _first_author_component(paper)
    journal = _clean_component(getattr(paper, "journal", "") or "Others", fallback="Others", max_len=42)
    title = _clean_component(getattr(paper, "title", "") or "Untitled", fallback="Untitled", max_len=96)
    stem = f"{paper_id}__{date_part}__{author}__{journal}__{title}"
    suffix_text = f"-{suffix}" if suffix else ""
    return _clip_stem(stem, suffix=suffix_text) + ".pdf"


def is_managed_download_path(path: Path, downloads_dir: Path = DOWNLOADS_DIR) -> bool:
    try:
        resolved = path.resolve()
        root = downloads_dir.resolve()
    except OSError:
        return False
    if hasattr(resolved, "is_relative_to"):
        return resolved.is_relative_to(root)
    return str(resolved).startswith(f"{root}{os.sep}")


def pdf_target_path_for_paper(
    paper: Any,
    downloads_dir: Path = DOWNLOADS_DIR,
    *,
    current_path: Optional[Path] = None,
) -> Path:
    downloads_dir.mkdir(parents=True, exist_ok=True)
    current_resolved = current_path.resolve() if current_path else None
    for suffix in [None] + list(range(2, 1000)):
        candidate = (downloads_dir / build_pdf_filename(paper, suffix=suffix)).resolve()
        if current_resolved and candidate == current_resolved:
            return candidate
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate a PDF filename for paper {getattr(paper, 'id', '')}.")


def write_pdf_atomic(target_path: Path, data: bytes) -> str:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target_path.stem[:48]}-",
        suffix=".pdf.tmp",
        dir=str(target_path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return str(target_path.resolve())


def sha256_of_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of `data`. Used for upload dedup."""
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Streamed SHA-256 of an on-disk PDF, so we don't load big files in RAM."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
