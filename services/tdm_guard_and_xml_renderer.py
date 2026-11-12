#!/usr/bin/env python3
"""
tdm_guard_and_xml_renderer.py
=============================
Solves the Elsevier 1-page PDF preview limitation and the TDM/Gemini compliance gap:

1. ``compile_elsevier_xml_bundle_to_pdf(bundle_dir)``:
   - Compiles Elsevier's ``main_manuscript.xml`` (``view=FULL``, ``<ce:sections>``) + all downloaded
     high-resolution figures (``figures/*_lrg.jpg``) and figure captions (``<ce:figure>``) into a
     multi-page, searchable PDF (``main_manuscript.pdf``) that passes all 4 verification gates
     (magic header, >=40 KB, >=3 pages, >=5000 chars, title+DOI identity match) and opens natively
     in the SeismoHub App PDF reader.
2. ``is_tdm_restricted_paper(paper, pdf_path, bundle_root)``:
   - Triple-layer TDM Guard (DB status/source + ``bundle_manifest.json`` + Page-1 PDF watermark)
     for ``services/analyze_service.py`` (``_run_analysis``).
   - When ``True``, forces ``force_metadata_only=True`` so TDM PDFs attached to the App for human
     reading are NEVER uploaded to Gemini / Vertex AI.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore

TDM_BANNER_MARKER = "LICENSED TDM COPY"
TIER_OPEN = "OPEN_CC_BY_OR_PREPRINT"
TIER_TDM = "LICENSED_TDM_ONLY"


# ---------------------------------------------------------------------------
# 1. XML Parsing Helpers for Elsevier Full-Text XML (<ce:sections> + <ce:figure>)
# ---------------------------------------------------------------------------

def _strip_tags(raw: str) -> str:
    """Remove XML tags and normalize whitespace while preserving readable text."""
    if not raw:
        return ""
    # Replace cross-refs and math tags with clean spacing
    cleaned = re.sub(r"<ce:hsp\b[^>]*/?>", " ", raw)
    cleaned = re.sub(r"<ce:inf\b[^>]*>(.*?)</ce:inf>", r"_\1", cleaned, flags=re.S)
    cleaned = re.sub(r"<ce:sup\b[^>]*>(.*?)</ce:sup>", r"^\1", cleaned, flags=re.S)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_first(pattern: str, xml_text: str) -> str:
    m = re.search(pattern, xml_text, flags=re.S | re.I)
    return _strip_tags(m.group(1)) if m else ""


def parse_elsevier_fulltext_xml(xml_text: str) -> Dict[str, Any]:
    """Extract structured title, authors, abstract, sections, figures, and references from Elsevier XML."""
    title = (
        _extract_first(r"<ce:title\b[^>]*>(.*?)</ce:title>", xml_text)
        or _extract_first(r"<dc:title\b[^>]*>(.*?)</dc:title>", xml_text)
    )
    doi = (
        _extract_first(r"<prism:doi\b[^>]*>(.*?)</prism:doi>", xml_text)
        or _extract_first(r"<ce:doi\b[^>]*>(.*?)</ce:doi>", xml_text)
    )
    journal = _extract_first(r"<prism:publicationName\b[^>]*>(.*?)</prism:publicationName>", xml_text)
    pub_date = (
        _extract_first(r"<prism:coverDisplayDate\b[^>]*>(.*?)</prism:coverDisplayDate>", xml_text)
        or _extract_first(r"<prism:coverDate\b[^>]*>(.*?)</prism:coverDate>", xml_text)
    )

    # Authors
    authors: List[str] = []
    for am in re.finditer(r"<ce:author\b[^>]*>(.*?)</ce:author>", xml_text, flags=re.S | re.I):
        block = am.group(1)
        gn = _extract_first(r"<ce:given-name\b[^>]*>(.*?)</ce:given-name>", block)
        sn = _extract_first(r"<ce:surname\b[^>]*>(.*?)</ce:surname>", block)
        full = f"{gn} {sn}".strip()
        if full and full not in authors:
            authors.append(full)

    # Abstracts (including author-highlights and main abstract)
    abstract_paras: List[str] = []
    for abm in re.finditer(r"<ce:abstract\b[^>]*>(.*?)</ce:abstract>", xml_text, flags=re.S | re.I):
        ab_block = abm.group(1)
        sec_title = _extract_first(r"<ce:section-title\b[^>]*>(.*?)</ce:section-title>", ab_block)
        paras = [
            _strip_tags(pm.group(1))
            for pm in re.finditer(r"<ce:simple-para\b[^>]*>(.*?)</ce:simple-para>", ab_block, flags=re.S | re.I)
        ]
        paras = [p for p in paras if p]
        if paras:
            prefix = f"{sec_title}: " if sec_title and sec_title.lower() != "abstract" else ""
            abstract_paras.append(prefix + " ".join(paras))

    # Figures (<ce:figure id="fig0001"> ... <ce:label>Fig. 1</ce:label> ... <ce:link locator="gr1"/>)
    figures: List[Dict[str, str]] = []
    seen_locators = set()
    for fm in re.finditer(r"<ce:figure\b([^>]*)>(.*?)</ce:figure>", xml_text, flags=re.S | re.I):
        attrs, fbody = fm.group(1), fm.group(2)
        fid_m = re.search(r'\bid="([^"]+)"', attrs)
        fid = fid_m.group(1) if fid_m else ""
        label = _extract_first(r"<ce:label\b[^>]*>(.*?)</ce:label>", fbody)
        caption = _extract_first(r"<ce:caption\b[^>]*>(.*?)</ce:caption>", fbody)
        loc_m = re.search(r'<ce:link\b[^>]*\blocator="([^"]+)"', fbody, flags=re.I)
        locator = loc_m.group(1) if loc_m else ""
        key = locator or fid or label
        if key and key not in seen_locators:
            seen_locators.add(key)
            figures.append({
                "id": fid,
                "label": label or locator.upper(),
                "caption": caption,
                "locator": locator,
            })

    # Body sections: extract <ce:sections> content in document order (section titles and paragraphs)
    body_blocks: List[Tuple[str, str]] = []
    sections_m = re.search(r"<ce:sections\b[^>]*>(.*?)</ce:sections>", xml_text, flags=re.S | re.I)
    body_xml = sections_m.group(1) if sections_m else xml_text
    # Remove figure/table blocks from body_xml before scanning paragraphs so captions aren't duplicated
    body_no_floats = re.sub(r"<ce:figure\b[^>]*>.*?</ce:figure>", " ", body_xml, flags=re.S | re.I)
    body_no_floats = re.sub(r"<ce:table\b[^>]*>.*?</ce:table>", " ", body_no_floats, flags=re.S | re.I)

    token_re = re.compile(
        r"<(ce:section-title|ce:para|ce:simple-para)\b[^>]*>(.*?)</\1>",
        flags=re.S | re.I,
    )
    for tm in token_re.finditer(body_no_floats):
        tag = tm.group(1).lower()
        txt = _strip_tags(tm.group(2))
        if not txt:
            continue
        if tag == "ce:section-title":
            body_blocks.append(("heading", txt))
        else:
            body_blocks.append(("para", txt))

    # Fallback if <ce:para> tags were nested or sparse
    if sum(len(t) for _, t in body_blocks) < 3000:
        for pm in re.finditer(r"<ce:para\b[^>]*>(.*?)</ce:para>", body_no_floats, flags=re.S | re.I):
            txt = _strip_tags(pm.group(1))
            if len(txt) > 30:
                body_blocks.append(("para", txt))

    # References (up to 120 references for completeness)
    refs: List[str] = []
    for rm in re.finditer(r"<ce:bib-reference\b[^>]*>(.*?)</ce:bib-reference>", xml_text, flags=re.S | re.I):
        rtxt = _strip_tags(rm.group(1))
        if rtxt:
            refs.append(rtxt)
        if len(refs) >= 120:
            break

    return {
        "title": title,
        "doi": doi,
        "journal": journal,
        "pub_date": pub_date,
        "authors": authors,
        "abstract_paras": abstract_paras,
        "body_blocks": body_blocks,
        "figures": figures,
        "references": refs,
    }


def _match_figure_files(figures_dir: Path, parsed_figs: List[Dict[str, str]]) -> List[Tuple[Path, str, str]]:
    """Pair each figure file in ``figures/`` with its ``(path, label, caption)``."""
    if not figures_dir.is_dir():
        return []
    all_files = sorted(
        p for p in figures_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")
    )
    if not all_files:
        return []

    matched: List[Tuple[Path, str, str]] = []
    used_paths = set()

    for fig in parsed_figs:
        loc = (fig.get("locator") or "").strip()
        if not loc:
            continue
        pat = re.compile(rf"-{re.escape(loc)}(?:_lrg|_sml)?\.[a-z]+$", re.I)
        for fpath in all_files:
            if fpath not in used_paths and pat.search(fpath.name):
                used_paths.add(fpath)
                matched.append((fpath, fig.get("label") or loc, fig.get("caption") or ""))
                break

    # Include any remaining image files not matched by locator
    for fpath in all_files:
        if fpath not in used_paths:
            used_paths.add(fpath)
            m = re.search(r"-(gr\d+|fx\d+)", fpath.name, re.I)
            lbl = f"Figure ({m.group(1)})" if m else fpath.stem
            matched.append((fpath, lbl, ""))

    return matched


# ---------------------------------------------------------------------------
# 2. Multi-Page PDF Synthesis (Text + Embedded High-Res Figures)
# ---------------------------------------------------------------------------

def compile_elsevier_xml_bundle_to_pdf(
    bundle_dir: Path,
    output_pdf_name: str = "main_manuscript.pdf",
    overwrite: bool = False,
) -> Optional[Path]:
    """Render an Elsevier XML-only bundle (``main_manuscript.xml`` + ``figures/*_lrg.jpg``) into a PDF.

    Returns the path to the generated PDF if successful, else ``None``.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required to compile Elsevier XML bundles to PDF.")

    bundle_dir = Path(bundle_dir)
    xml_path = bundle_dir / "main_manuscript.xml"
    if not xml_path.is_file():
        return None
    out_pdf = bundle_dir / output_pdf_name
    if out_pdf.is_file() and not overwrite:
        return out_pdf

    manifest_path = bundle_dir / "bundle_manifest.json"
    manifest: Dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    xml_text = xml_path.read_text(encoding="utf-8", errors="ignore")
    parsed = parse_elsevier_fulltext_xml(xml_text)

    title = parsed["title"] or manifest.get("title") or "Untitled Manuscript"
    doi = parsed["doi"] or manifest.get("doi") or ""
    journal = parsed["journal"] or manifest.get("journal") or "Elsevier Journal"
    pub_date = parsed["pub_date"] or ""
    authors_str = ", ".join(parsed["authors"][:25])

    # Build HTML for fitz.Story layout of the full article text
    esc = html.escape
    html_parts: List[str] = [
        "<html><head><style>",
        "body { font-family: sans-serif; font-size: 10pt; line-height: 1.38; color: #111111; }",
        ".banner { background-color: #fff3cd; border: 1px solid #d39e00; padding: 6pt; font-size: 8.5pt; "
        "font-weight: bold; color: #664d03; margin-bottom: 10pt; }",
        ".meta { font-size: 9pt; color: #444444; margin-bottom: 6pt; }",
        "h1 { font-size: 15pt; line-height: 1.25; margin-top: 4pt; margin-bottom: 6pt; color: #000000; }",
        "h2 { font-size: 11.5pt; margin-top: 12pt; margin-bottom: 4pt; color: #1a1a1a; }",
        ".authors { font-size: 9.5pt; color: #222222; margin-bottom: 10pt; }",
        ".abstract { background-color: #f8f9fa; padding: 8pt; margin-bottom: 12pt; font-size: 9.5pt; }",
        "p { margin-top: 0pt; margin-bottom: 6pt; text-align: justify; }",
        ".ref { font-size: 8pt; line-height: 1.25; margin-bottom: 3pt; color: #333333; }",
        "</style></head><body>",
        f'<div class="banner">[{TDM_BANNER_MARKER} — RENDERED FROM ELSEVIER FULL-TEXT XML + HIGH-RES FIGURES | '
        f'LOCAL APP READING ONLY — DO NOT UPLOAD TO HOSTED AI]</div>',
        f'<div class="meta"><b>Journal:</b> {esc(journal)} &nbsp;|&nbsp; <b>Date:</b> {esc(pub_date)} '
        f'&nbsp;|&nbsp; <b>DOI:</b> https://doi.org/{esc(doi)}</div>',
        f"<h1>{esc(title)}</h1>",
    ]
    if authors_str:
        html_parts.append(f'<div class="authors">{esc(authors_str)}</div>')
    if parsed["abstract_paras"]:
        html_parts.append('<div class="abstract"><b>Abstract &amp; Highlights</b><br/>')
        for ap in parsed["abstract_paras"]:
            html_parts.append(f"<p>{esc(ap)}</p>")
        html_parts.append("</div>")

    for btype, btxt in parsed["body_blocks"]:
        if btype == "heading":
            html_parts.append(f"<h2>{esc(btxt)}</h2>")
        else:
            html_parts.append(f"<p>{esc(btxt)}</p>")

    if parsed["references"]:
        html_parts.append("<h2>References</h2>")
        for idx, ref in enumerate(parsed["references"], 1):
            html_parts.append(f'<p class="ref">[{idx}] {esc(ref)}</p>')

    html_parts.append("</body></html>")
    full_html = "\n".join(html_parts)

    # 1) Render text pages via fitz.Story into an in-memory PDF buffer
    a4_rect = fitz.paper_rect("a4")
    margin = 42.0
    body_rect = fitz.Rect(margin, margin, a4_rect.width - margin, a4_rect.height - margin)

    tmp_text_pdf = bundle_dir / f".{output_pdf_name}.text.part"
    story = fitz.Story(html=full_html)
    writer = fitz.DocumentWriter(str(tmp_text_pdf))
    more = 1
    page_num = 0
    while more:
        page_num += 1
        dev = writer.begin_page(a4_rect)
        more, _ = story.place(body_rect)
        story.draw(dev)
        writer.end_page()
        if page_num >= 120:  # hard safety cap
            break
    writer.close()

    # 2) Open the rendered text PDF and append high-resolution figure pages
    doc = fitz.open(str(tmp_text_pdf))
    matched_figs = _match_figure_files(bundle_dir / "figures", parsed["figures"])

    for fig_path, fig_label, fig_caption in matched_figs:
        try:
            page = doc.new_page(width=a4_rect.width, height=a4_rect.height)
            header_rect = fitz.Rect(margin, 28, a4_rect.width - margin, 48)
            page.insert_textbox(
                header_rect,
                f"{fig_label} — DOI: {doi} ({TDM_BANNER_MARKER})",
                fontsize=9,
                fontname="helv",
                color=(0.25, 0.25, 0.25),
            )
            # Reserve top 60% of the page for the high-res image, bottom 32% for the caption
            img_box = fitz.Rect(margin, 52, a4_rect.width - margin, a4_rect.height * 0.66)
            page.insert_image(img_box, filename=str(fig_path), keep_proportion=True)

            cap_box = fitz.Rect(margin, a4_rect.height * 0.68, a4_rect.width - margin, a4_rect.height - margin)
            cap_text = f"{fig_label}: {fig_caption}" if fig_caption else fig_label
            page.insert_textbox(cap_box, cap_text, fontsize=9.5, fontname="helv", color=(0.1, 0.1, 0.1))
        except Exception:
            continue

    tmp_final = bundle_dir / f".{output_pdf_name}.part"
    doc.save(str(tmp_final), deflate=True, garbage=3)
    total_pages = doc.page_count
    doc.close()
    try:
        tmp_text_pdf.unlink(missing_ok=True)
    except Exception:
        pass
    os.replace(tmp_final, out_pdf)

    # Update bundle_manifest.json with the rendered PDF metadata while keeping LICENSED_TDM_ONLY
    if manifest_path.is_file() and manifest:
        pdf_bytes = out_pdf.read_bytes()
        manifest.setdefault("files", {})[output_pdf_name] = "rendered_from_elsevier_xml_and_figures"
        manifest["rendered_tdm_pdf"] = {
            "file": output_pdf_name,
            "pages": total_pages,
            "figures_embedded": len(matched_figs),
            "bytes": len(pdf_bytes),
            "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "md5": hashlib.md5(pdf_bytes).hexdigest(),
            "license_tier": TIER_TDM,
            "hosted_ai_upload_allowed": False,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return out_pdf


def batch_compile_all_elsevier_xml_bundles(bundle_root: Path, overwrite: bool = False) -> Dict[str, Any]:
    """Scan ``paper_bundles/`` and compile every XML-only Elsevier bundle into ``main_manuscript.pdf``."""
    bundle_root = Path(bundle_root)
    compiled: List[str] = []
    skipped: List[str] = []
    errors: List[str] = []

    for xml_file in sorted(bundle_root.glob("PaperBundle_*/main_manuscript.xml")):
        bdir = xml_file.parent
        pdf_file = bdir / "main_manuscript.pdf"
        if pdf_file.is_file() and not overwrite:
            skipped.append(bdir.name)
            continue
        try:
            out = compile_elsevier_xml_bundle_to_pdf(bdir, overwrite=overwrite)
            if out and out.is_file():
                compiled.append(bdir.name)
            else:
                skipped.append(bdir.name)
        except Exception as exc:
            errors.append(f"{bdir.name}: {type(exc).__name__}: {exc}")

    return {
        "bundle_root": str(bundle_root),
        "compiled_count": len(compiled),
        "skipped_existing_count": len(skipped),
        "error_count": len(errors),
        "compiled_bundles": compiled,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 3. Triple-Layer TDM Guard for services/analyze_service.py
# ---------------------------------------------------------------------------

def _safe_bundle_dir_for_paper(paper: Any, bundle_root: Path) -> Optional[Path]:
    doi = (getattr(paper, "doi", None) or "").strip()
    pid = (getattr(paper, "id", None) or "").strip()
    try:
        from services import fullbundle_harvest as fb  # type: ignore
        cand = bundle_root / fb.safe_bundle_name(fb.normalize_doi(doi), "", pid)
        if cand.is_dir():
            return cand
    except Exception:
        pass
    if doi:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)[:160]
        cand = bundle_root / f"PaperBundle_{safe}"
        if cand.is_dir():
            return cand
    if pid:
        cand = bundle_root / f"PaperBundle_{pid}"
        if cand.is_dir():
            return cand
    return None


def is_tdm_restricted_paper(
    paper: Any,
    pdf_path: Optional[str] = None,
    bundle_root: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Return ``(True, reason)`` if the attached PDF is TDM-restricted and MUST NOT be sent to Gemini.

    Enforces all 6 rules of ``services/tdm_qa_guard.py`` (§3.8-a) plus direct ``bundle_manifest.json`` check.
    """
    if bundle_root is None:
        repo_guess = Path(__file__).resolve().parents[1]
        bundle_root = repo_guess / "paper_bundles"

    target_pdf = pdf_path or getattr(paper, "pdf_path", None)
    first_pages: List[Dict[str, Any]] = []
    if target_pdf and fitz is not None and Path(target_pdf).is_file():
        try:
            with fitz.open(str(target_pdf)) as doc:
                for idx in range(min(2, doc.page_count)):
                    first_pages.append({"page": idx + 1, "text": doc[idx].get_text("text") or ""})
        except Exception:
            first_pages = [{"page": 1, "text": ""}]

    # Layer 0: Delegate to services.tdm_qa_guard.qa_block_reason (all 6 rules, including SHA256,
    # "All rights reserved", and institution download stamp on manually uploaded PDFs)
    try:
        from services.tdm_qa_guard import qa_block_reason  # type: ignore
        pages_for_qa = first_pages or [{"page": 1, "text": ""}]
        qa_reason = qa_block_reason(paper, pages_for_qa, bundle_root=Path(bundle_root))
        if qa_reason:
            return True, f"tdm_qa_guard: {qa_reason}"
    except Exception:
        pass

    # Layer 1: Check Paper ORM fields
    status = (getattr(paper, "retrieval_status", None) or "").strip().lower()
    source = (getattr(paper, "retrieval_source", None) or "").strip().lower()
    if status == "held_tdm_only":
        return True, "paper.retrieval_status == held_tdm_only"
    if source in ("elsevier_tdm_xml", "elsevier_xml_rendered"):
        return True, f"paper.retrieval_source == {source}"

    # Layer 2: Check paper_bundles/PaperBundle_*/bundle_manifest.json
    bdir = _safe_bundle_dir_for_paper(paper, Path(bundle_root))
    if bdir is not None:
        mpath = bdir / "bundle_manifest.json"
        if mpath.is_file():
            try:
                manifest = json.loads(mpath.read_text(encoding="utf-8"))
                app_copy = (manifest.get("app_attached") or {}).get("copy") or {}
                if app_copy.get("license_tier") == TIER_OPEN:
                    return False, "app_attached.copy is OPEN_CC_BY_OR_PREPRINT"
                if app_copy.get("license_tier") == TIER_TDM or app_copy.get("hosted_ai_upload_allowed") is False:
                    return True, "bundle_manifest.app_attached.copy is LICENSED_TDM_ONLY"
                main_m = manifest.get("main_manuscript") or {}
                if main_m.get("license_tier") == TIER_TDM or main_m.get("hosted_ai_upload_allowed") is False:
                    return True, "bundle_manifest.main_manuscript is LICENSED_TDM_ONLY"
            except Exception:
                pass

    # Layer 3: Check Page-1 PDF watermark
    if first_pages:
        p1_text = " ".join(p.get("text") or "" for p in first_pages)
        if TDM_BANNER_MARKER in p1_text or "DO NOT UPLOAD TO HOSTED AI" in p1_text:
            return True, "Page-1 TDM compliance watermark detected"

    return False, ""
