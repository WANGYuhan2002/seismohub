"""Vault-folder writer — one `.md` per paper to the user's Obsidian vault.

Per Roadmap V2 item 4 (Q2 decision: separate `~/Obsidian/SWP-vault/` folder,
not next to the PDF in `downloads/`).

Design contract
---------------
- SWP **owns** the YAML frontmatter and every section *above* the
  ``## My Notes`` marker.
- The user **owns** ``## My Notes`` and everything *below* it.
- On re-write (re-analysis or paper edit), SWP preserves the user's
  ``## My Notes`` block verbatim and replaces only the SWP-owned portion.
- File never deleted automatically — even when a paper is deleted in SWP,
  the Markdown stays on disk (the Long Now principle, T15).

Vault path is stored in ``AppState['vault_path']``. If unset, the writer
silently no-ops (the rest of SWP still works without an Obsidian vault).

Filename convention: ``{citekey}.md`` if a citekey is derivable, else
``{paper_id}--{slug}.md``. Always within the configured vault directory.

See BRAINSTORM_ROADMAP_V2.md item 4 + agent #10 (Obsidian) for rationale.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models import AIAnalysis, AppState, Paper
from services.export_service import _citekey


__all__ = [
    "VaultWriterError",
    "get_vault_path",
    "set_vault_path",
    "clear_vault_path",
    "write_paper_md",
    "write_all_papers",
    "vault_filename_for_paper",
    "_resolve_collision",
]


VAULT_PATH_KEY = "vault_path"

# Markers — never change these strings. The "My Notes" marker is the
# user-owned boundary; everything below survives re-writes.
MY_NOTES_HEADING = "## My Notes"
SWP_HEADER_MARKER = "<!-- SWP:OWNED — do not edit above; SWP overwrites on re-analysis -->"
SWP_FOOTER_MARKER = "<!-- SWP:END — user-owned notes below -->"


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


class VaultWriterError(Exception):
    """Raised when the vault folder cannot be used safely."""


# ----------------------------------------------------------------------
# Vault path management (AppState-backed)
# ----------------------------------------------------------------------


def get_vault_path(db_session) -> Optional[Path]:
    """Return the configured vault path, or None if not set."""
    row = db_session.query(AppState).filter_by(key=VAULT_PATH_KEY).first()
    if not row or not row.value:
        return None
    return Path(row.value).expanduser()


def set_vault_path(db_session, path: str | Path) -> Path:
    """Validate the path and persist it to AppState.

    Creates the directory if it doesn't exist. Raises VaultWriterError
    if the path is empty or a file (not a directory) already exists there.
    """
    if not path:
        raise VaultWriterError("Vault path cannot be empty.")

    resolved = Path(path).expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise VaultWriterError(f"Vault path exists but is not a directory: {resolved}")

    # FN-B5-A02-1: turn OS-level mkdir failures (permission denied on
    # /etc, read-only FS, etc.) into VaultWriterError so the route can
    # return a clean 400 instead of leaking a 500.
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise VaultWriterError(
            f"Cannot create or access vault path {resolved}: permission denied"
        ) from exc
    except OSError as exc:
        raise VaultWriterError(
            f"Cannot create or access vault path {resolved}: {exc}"
        ) from exc

    # Idempotent upsert
    row = db_session.query(AppState).filter_by(key=VAULT_PATH_KEY).first()
    if row is None:
        row = AppState(key=VAULT_PATH_KEY, value=str(resolved))
        db_session.add(row)
    else:
        row.value = str(resolved)
    db_session.commit()
    return resolved


def clear_vault_path(db_session) -> None:
    """Unset the vault path. SWP will stop writing Markdown."""
    row = db_session.query(AppState).filter_by(key=VAULT_PATH_KEY).first()
    if row is not None:
        db_session.delete(row)
        db_session.commit()


# ----------------------------------------------------------------------
# Filename derivation
# ----------------------------------------------------------------------


_FS_SAFE_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def _slug(text: str, *, max_len: int = 80) -> str:
    """ASCII-friendly, filesystem-safe slug. Mirrors `pdf_naming._clean_component`."""
    s = unicodedata.normalize("NFKC", str(text or "").strip())
    s = s.replace("&amp;", "and").replace("&", "and")
    s = re.sub(r"[‐-―−]+", "-", s)
    s = _FS_SAFE_RE.sub(" ", s)
    s = re.sub(r"[{}\\]+", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-. ")
    if not s:
        s = "untitled"
    if len(s) > max_len:
        s = s[:max_len].rstrip("-. ") or "untitled"
    return s


def _paper_dict_for_citekey(paper: Paper) -> Dict[str, Any]:
    """Minimal dict that satisfies export_service._citekey's expectations."""
    authors = paper.authors if isinstance(paper.authors, list) else []
    return {
        "authors": authors,
        "published_date": paper.published_date or "",
        "title": paper.title or "",
    }


def _resolve_collision(base_path: Path, paper_id: str) -> Path:
    """If *base_path* exists with a DIFFERENT ``swp_paper_id``, return a
    collision-suffixed path. Idempotent: the same ``paper_id`` always maps to
    the same suffix so the colliding paper always lands on the same file.

    See FN-B5-A01-1: two papers with the same citekey (e.g. ``Raudsepp2026``
    from different sources) were silently overwriting each other's
    SWP-owned section. The user-owned ``## My Notes`` was preserved per the
    split contract, but the SWP-owned portion ended up belonging to a
    different paper. We avoid that here by suffixing the new filename with
    the first 4 hex chars of the colliding ``paper_id``.

    Note: existing colliding files in the user's vault are NOT renamed —
    they may carry user notes. The first paper keeps the un-suffixed file;
    subsequent collisions land on ``<stem>-<4hex>.md``.
    """
    if not base_path.exists():
        return base_path
    try:
        text = base_path.read_text(encoding="utf-8")
    except OSError:
        return base_path
    # Parse swp_paper_id from frontmatter (simple regex; tolerant of quoting).
    m = re.search(r'^swp_paper_id:\s*"?([a-f0-9]+)"?', text, re.MULTILINE)
    existing_id = m.group(1) if m else None
    if existing_id == paper_id:
        return base_path  # no collision — same paper

    # FIX 2026-05-19 (FN-B5-A04-1): the previous 4-hex-char suffix collided
    # when multiple papers shared the same first 4 chars (e.g. smoke-test
    # papers all starting with "smok"). Escalate suffix length deterministically
    # and verify ownership of any pre-existing candidate before falling back
    # to a timestamped slot.
    pid = paper_id or "unknown"
    for prefix_len in (4, 6, 8, 12, len(pid)):
        if prefix_len <= 0:
            continue
        suffix = pid[:prefix_len]
        candidate = base_path.with_name(f"{base_path.stem}-{suffix}{base_path.suffix}")
        if not candidate.exists():
            return candidate
        # Candidate already exists — if it's THIS paper, reuse it; else try a longer prefix.
        try:
            ctext = candidate.read_text(encoding="utf-8")
            cm = re.search(r'^swp_paper_id:\s*"?([a-f0-9]+)"?', ctext, re.MULTILINE)
            if cm and cm.group(1) == paper_id:
                return candidate  # already ours
        except OSError:
            pass
    # Worst case: all prefix lengths collided with foreign papers — append a timestamp.
    import time
    return base_path.with_name(
        f"{base_path.stem}-{pid[:8]}-{int(time.time())}{base_path.suffix}"
    )


def vault_filename_for_paper(paper: Paper, *, existing: Optional[set] = None) -> str:
    """Return the `.md` filename for *paper* (just the stem + extension).

    Prefers a citekey-derived name (`niu2024crustal.md`); falls back to
    `{paper_id}--{slug}.md` so duplicates from `_citekey` collisions cannot
    overwrite each other.

    Note: this returns the *base* filename without applying collision
    resolution against the vault. Callers that have a vault directory in
    hand (e.g. ``write_paper_md``) should additionally call
    ``_resolve_collision(vault / filename, paper.id)`` before writing.
    """
    existing = existing or set()
    try:
        key = _citekey(_paper_dict_for_citekey(paper), existing)
        if key and key != "anon":
            return f"{_slug(key, max_len=120)}.md"
    except Exception:
        pass
    short_id = (paper.id or "unknown")[:16]
    return f"{short_id}--{_slug(paper.title or 'untitled')}.md"


# ----------------------------------------------------------------------
# Markdown rendering
# ----------------------------------------------------------------------


def _yaml_escape(value: Any) -> str:
    """Render a scalar as a safe single-line YAML value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    # Always quote to avoid YAML "yes"/"no"/":" ambiguity. Escape backslash + quote.
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _yaml_list(values: List[Any]) -> str:
    if not values:
        return "[]"
    lines = ["", *[f"  - {_yaml_escape(v)}" for v in values]]
    return "\n".join(lines)


def _build_frontmatter(paper: Paper, analysis: Optional[AIAnalysis]) -> str:
    """YAML frontmatter — keep small and Obsidian-Dataview-friendly."""
    payload = (analysis.payload if (analysis and isinstance(analysis.payload, dict)) else {}) or {}
    authors = paper.authors if isinstance(paper.authors, list) else []
    topics = paper.topics if isinstance(paper.topics, list) else []

    try:
        citekey = _citekey(_paper_dict_for_citekey(paper), set())
    except Exception:
        citekey = ""

    year = (paper.published_date or "")[:4] if paper.published_date else ""

    fields: List[Tuple[str, str]] = [
        ("swp_paper_id", _yaml_escape(paper.id)),
        ("citekey", _yaml_escape(citekey)),
        ("title", _yaml_escape(paper.title or "")),
        ("authors", _yaml_list(authors)),
        ("year", _yaml_escape(year)),
        ("journal", _yaml_escape(paper.journal or "")),
        ("doi", _yaml_escape(paper.doi or "")),
        ("url", _yaml_escape(paper.link or "")),
        ("topics", _yaml_list(topics)),
        ("tags", "[]"),  # user-owned via Obsidian; we never set tags
        ("swp_pdf_path", _yaml_escape(paper.pdf_path or "")),
        ("swp_has_pdf", _yaml_escape(bool(paper.pdf_path))),
        ("swp_relevance", _yaml_escape(getattr(analysis, "relevance_score", None) if analysis else None)),
        ("swp_evidence_based_on", _yaml_escape(getattr(analysis, "evidence_based_on", "") if analysis else "")),
        ("swp_pdf_used", _yaml_escape(getattr(analysis, "pdf_used", False) if analysis else False)),
        ("swp_last_analyzed", _yaml_escape(
            (analysis.updated_at.isoformat() if (analysis and analysis.updated_at) else "")
        )),
        # Reading-state fields — surface to Obsidian Dataview / queries.
        ("swp_reading_intent", _yaml_escape(getattr(paper, "reading_intent", "") or "")),
        ("swp_takeaway_at", _yaml_escape(
            paper.user_takeaway_at.isoformat()
            if getattr(paper, "user_takeaway_at", None) else ""
        )),
        ("swp_follow_up", _yaml_escape(bool(getattr(paper, "follow_up", False)))),
        ("swp_last_synced", _yaml_escape(datetime.utcnow().isoformat() + "Z")),
    ]
    body = "\n".join(f"{k}: {v}" for k, v in fields)
    return f"---\n{body}\n---\n"


def _md_escape(text: str) -> str:
    """Light escaping — Markdown is permissive; we only fix obvious breakers."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").strip()


def _bullets(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "_Not available._"
    return "\n".join(f"- {_md_escape(str(it))}" for it in items if str(it).strip())


def _section(heading: str, body: str) -> str:
    body = (body or "").rstrip()
    if not body:
        body = "_Not available._"
    return f"\n## {heading}\n\n{body}\n"


def _format_source_annotation(cell: Dict[str, Any]) -> str:
    """Build a plain-text source annotation for a structured-analysis cell.

    Prefers the new ``source`` string ("§3.2 Receiver functions — second
    paragraph; p. 4"); falls back to legacy ``page_refs: [int]`` rendered
    as ``see p. 3, 4`` when only that is available. Returns "" when both
    are empty so the caller can skip the suffix entirely.
    """
    source = str(cell.get("source") or "").strip()
    if source:
        return source
    refs = cell.get("page_refs") or []
    pages = [str(int(n)) for n in refs if isinstance(n, int) or str(n).isdigit()]
    if pages:
        return "see p. " + ", ".join(pages)
    return ""


def _structured_block(payload: Dict[str, Any]) -> str:
    sa = payload.get("structured_analysis") or {}
    if not isinstance(sa, dict) or not sa:
        return ""
    keys = [
        ("method", "Method"),
        ("data", "Data"),
        ("results", "Results"),
        ("limitations", "Limitations"),
        ("novelty", "Novelty"),
    ]
    parts: List[str] = []
    for key, label in keys:
        cell = sa.get(key)
        if not isinstance(cell, dict):
            continue
        text = _md_escape(str(cell.get("text") or ""))
        if not text:
            continue
        annotation = _format_source_annotation(cell)
        suffix = f"  \n  _{_md_escape(annotation)}_" if annotation else ""
        parts.append(f"**{label}.** {text}{suffix}")
    if not parts:
        return ""
    return "\n\n".join(parts)


def _figures_block(payload: Dict[str, Any]) -> str:
    figs = payload.get("figure_analyses") or []
    if not isinstance(figs, list) or not figs:
        return ""
    out: List[str] = []
    for fig in figs:
        if not isinstance(fig, dict):
            continue
        fid = _md_escape(str(fig.get("figure_id") or fig.get("figure_label") or ""))
        title = _md_escape(str(fig.get("title") or ""))
        # Accept either canonical (`key_finding`) or legacy (`description`)
        # name for the "what the figure shows / key takeaway" field.
        # See FN-B4-A01-1 (normalizer emits both as aliases).
        desc = _md_escape(str(fig.get("description") or fig.get("key_finding") or ""))
        interp = _md_escape(str(fig.get("interpretation") or ""))
        source = _md_escape(str(fig.get("source") or "").strip())
        head = f"**{fid}**" + (f" — {title}" if title else "")
        body_lines = [head]
        if desc:
            body_lines.append(desc)
        if interp:
            body_lines.append(f"_Interpretation._ {interp}")
        if source:
            body_lines.append(f"_Source._ {source}")
        out.append("\n".join(body_lines))
    return "\n\n".join(out)


def _build_swp_body(paper: Paper, analysis: Optional[AIAnalysis]) -> str:
    payload = (analysis.payload if (analysis and isinstance(analysis.payload, dict)) else {}) or {}

    authors = paper.authors if isinstance(paper.authors, list) else []
    author_str = ", ".join(authors) if authors else "Unknown authors"
    year = (paper.published_date or "")[:4] if paper.published_date else ""
    journal = paper.journal or ""
    doi = paper.doi or ""
    pdf_path = paper.pdf_path or ""

    parts: List[str] = []
    parts.append(SWP_HEADER_MARKER)
    parts.append(f"\n# {_md_escape(paper.title or 'Untitled')}\n")

    meta_line = f"> **Authors.** {_md_escape(author_str)}"
    if year:
        meta_line += f"  ·  **Year.** {year}"
    if journal:
        meta_line += f"  ·  **Journal.** {_md_escape(journal)}"
    if doi:
        meta_line += f"  ·  **DOI.** [{doi}](https://doi.org/{doi})"
    parts.append(meta_line)

    if pdf_path:
        parts.append(f"\n> **PDF.** [`{Path(pdf_path).name}`](file://{pdf_path})\n")

    # Takeaway
    takeaway_en = _md_escape(str(payload.get("one_sentence_takeaway_en") or ""))
    takeaway_zh = _md_escape(str(payload.get("one_sentence_takeaway_zh") or ""))
    if takeaway_en or takeaway_zh:
        body = takeaway_en
        if takeaway_zh:
            body += ("\n\n" if body else "") + f"**中文.** {takeaway_zh}"
        parts.append(_section("TL;DR", body))

    # Abstract (paper-provided, not AI)
    abstract = _md_escape(paper.abstract or "")
    if abstract:
        parts.append(_section("Abstract", abstract))

    # Summary
    summ_en = _md_escape(str(payload.get("summary_150w_en") or ""))
    summ_zh = _md_escape(str(payload.get("summary_150w_zh") or ""))
    if summ_en or summ_zh:
        block = summ_en
        if summ_zh:
            block += ("\n\n" if block else "") + "### 中文摘要\n\n" + summ_zh
        parts.append(_section("Summary", block))

    # Structured analysis (G4)
    sb = _structured_block(payload)
    if sb:
        parts.append(_section("Structured Analysis", sb))

    # Lists
    if payload.get("key_contributions"):
        parts.append(_section("Key Contributions", _bullets(payload.get("key_contributions"))))
    if payload.get("methods"):
        parts.append(_section("Methods", _bullets(payload.get("methods"))))
    if payload.get("data_or_experiments"):
        parts.append(_section("Data & Experiments", _bullets(payload.get("data_or_experiments"))))
    if payload.get("limitations"):
        parts.append(_section("Limitations", _bullets(payload.get("limitations"))))
    if payload.get("followups_for_our_group"):
        parts.append(_section("Follow-ups", _bullets(payload.get("followups_for_our_group"))))

    # Figures
    fb = _figures_block(payload)
    if fb:
        parts.append(_section("Figures", fb))

    # Roadmap V2 item 41 — Why Strip externalized intent
    intent = _md_escape(getattr(paper, "reading_intent", "") or "")
    if intent:
        parts.append(_section("Why I opened this", f"> {intent}"))

    # Roadmap V2 item 1 — Forced two-sentence takeaway (user-authored)
    what = _md_escape(getattr(paper, "user_takeaway_what", "") or "")
    why = _md_escape(getattr(paper, "user_takeaway_why", "") or "")
    if what or why:
        body = ""
        if what:
            body += f"**What did the paper argue?**\n\n{what}\n"
        if why:
            body += ("\n" if body else "") + f"**Why does it matter to me?**\n\n{why}\n"
        parts.append(_section("Two-sentence takeaway", body))
    else:
        parts.append(_section(
            "Two-sentence takeaway",
            "_Not yet captured. Click "
            "“Mark as read” in SWP to record what the paper argued and why it matters to you._",
        ))

    # Roadmap V2 item 16-prime — follow-up flag (UI signal echoed into the vault)
    if getattr(paper, "follow_up", False):
        parts.append(_section(
            "Follow-up",
            "⭐ This paper is in your follow-up queue. Open it again when you have time.",
        ))

    parts.append(SWP_FOOTER_MARKER)
    return "\n".join(parts)


def _user_notes_default() -> str:
    return (
        f"\n{MY_NOTES_HEADING}\n\n"
        "<!-- Everything below this heading is yours. SWP will never overwrite it. -->\n\n"
        "- \n"
    )


# ----------------------------------------------------------------------
# Read / merge existing file
# ----------------------------------------------------------------------


def _split_existing(text: str) -> Tuple[str, str]:
    """Return (swp_owned, user_owned). ``user_owned`` starts at ``## My Notes``.

    Robust to small drifts: matches the *first* line that starts with
    ``## My Notes`` (case-sensitive). If absent, treats everything as
    SWP-owned and appends a default user-notes block.
    """
    lines = text.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.strip().startswith(MY_NOTES_HEADING):
            swp = "".join(lines[:idx]).rstrip() + "\n"
            user = "".join(lines[idx:])
            return swp, user
    return text.rstrip() + "\n", _user_notes_default()


# ----------------------------------------------------------------------
# Public: write one paper
# ----------------------------------------------------------------------


def write_paper_md(
    db_session,
    paper: Paper,
    analysis: Optional[AIAnalysis] = None,
    *,
    vault_path: Optional[Path] = None,
) -> Optional[Path]:
    """Write/update the `.md` file for *paper* in the user's vault.

    Returns the absolute path written to, or ``None`` if no vault is
    configured (silent no-op — the rest of SWP works without a vault).

    Raises ``VaultWriterError`` only for filesystem errors.
    """
    vault = vault_path or get_vault_path(db_session)
    if vault is None:
        return None

    if not vault.exists():
        try:
            vault.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultWriterError(f"Cannot create vault dir {vault}: {exc}") from exc

    if analysis is None:
        analysis = getattr(paper, "ai_analysis", None)

    filename = vault_filename_for_paper(paper)
    target = _resolve_collision(vault / filename, paper.id or "")

    frontmatter = _build_frontmatter(paper, analysis)
    swp_body = _build_swp_body(paper, analysis)

    # Merge: preserve any existing ``## My Notes`` section, replace the rest.
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        _, user_section = _split_existing(existing)
    else:
        user_section = _user_notes_default()

    content = frontmatter + swp_body + "\n" + user_section
    if not content.endswith("\n"):
        content += "\n"

    # Atomic write: temp file + rename
    tmp = target.with_suffix(target.suffix + ".swp-tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise VaultWriterError(f"Cannot write {target}: {exc}") from exc

    return target


def write_all_papers(db_session) -> Dict[str, Any]:
    """Backfill: write every paper in the library to the vault.

    Returns a small stats dict for logging / UI feedback.
    """
    vault = get_vault_path(db_session)
    if vault is None:
        return {"status": "skipped", "reason": "no_vault", "written": 0}

    written = 0
    errors: List[Dict[str, str]] = []
    for paper in db_session.query(Paper).all():
        try:
            path = write_paper_md(db_session, paper, vault_path=vault)
            if path is not None:
                written += 1
        except VaultWriterError as exc:
            errors.append({"paper_id": paper.id, "error": str(exc)})

    return {
        "status": "ok",
        "vault": str(vault),
        "written": written,
        "errors": errors,
    }
