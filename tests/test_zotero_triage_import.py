"""Offline tests for `scripts/zotero_triage_import.py`:
Verifies that Zotero / browser downloads are cleanly split:
- CC-BY / Open Access / Preprint PDFs -> attached to `downloads/` (`papers.pdf_path` set, `status='success'`)
- Subscribed / 'All rights reserved' / institution-stamped / unverified closed PDFs -> stored ONLY in
  `paper_bundles/PaperBundle_<DOI>/main_manuscript.pdf` (`papers.pdf_path` stays NULL, `status='held_tdm_only'`,
  `hosted_ai_upload_allowed=False`), and blocked by `services/tdm_qa_guard.py`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

import pytest

fitz = pytest.importorskip("fitz")

from scripts import zotero_triage_import as zt  # noqa: E402
from services import fullbundle_harvest as fb  # noqa: E402
from services import tdm_qa_guard  # noqa: E402


def _make_pdf(title: str, doi: str, lines: Tuple[str, ...], *, pages: int = 4) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page()
        y = 40
        if i == 0:
            p.insert_text((36, y), title, fontsize=10)
            y += 14
            p.insert_text((36, y), f"DOI: {doi}", fontsize=9)
            y += 14
            for ln in lines:
                p.insert_text((36, y), ln, fontsize=8)
                y += 12
        while y < 780:
            p.insert_text((36, y), "seismic rupture tomography attenuation wavefield " * 3, fontsize=6)
            y += 9
    # Make sure byte size >= 40 KB (inspect_pdf default floor) using uncompressed stream padding
    pad = doc.new_page()
    for k in range(220):
        pad.insert_text((36, 20 + (k % 70) * 10), f"supplementary waveform catalog line {k} " + "x" * 70, fontsize=6)
    data = doc.tobytes(deflate=False)
    doc.close()
    assert len(data) >= 40_000
    return data


@pytest.fixture()
def triage_db(isolated_db):
    import db as db_mod
    db_mod.init_db()
    return db_mod


def test_zotero_triage_splits_open_to_app_and_subscribed_to_bundle(triage_db, make_paper, tmp_path):
    from models import Paper

    open_doi = "10.1785/0220260099"
    open_title = "Open Access Crustal Tomography of the Alpine Foreland Basin"
    closed_doi = "10.1190/geo-2026-0999"
    closed_title = "Subscribed Elastic Full Waveform Inversion in Anisotropic Media"
    stamped_doi = "10.1785/0120260888"
    stamped_title = "Source Scaling of Moderate Earthquakes in Southern California"

    make_paper("p_open", doi=open_doi, title=open_title, journal="SRL")
    make_paper("p_closed", doi=closed_doi, title=closed_title, journal="Geophysics")
    make_paper("p_stamped", doi=stamped_doi, title=stamped_title, journal="BSSA")

    zotero_dir = tmp_path / "zotero_export"
    (zotero_dir / "files" / "1").mkdir(parents=True)
    (zotero_dir / "files" / "2").mkdir(parents=True)
    (zotero_dir / "files" / "3").mkdir(parents=True)

    pdf_open = _make_pdf(
        open_title,
        open_doi,
        ("This is an open access article distributed under the Creative Commons Attribution 4.0 License.",),
    )
    pdf_closed = _make_pdf(
        closed_title,
        closed_doi,
        ("(c) 2026 Society of Exploration Geophysicists. All rights reserved.",),
    )
    pdf_stamped = _make_pdf(
        stamped_title,
        stamped_doi,
        ("Downloaded from http://pubs.geoscienceworld.org/ssa/bssa by ETH Zurich user on 11 November 2026",),
    )

    (zotero_dir / "files" / "1" / "open.pdf").write_bytes(pdf_open)
    (zotero_dir / "files" / "2" / "closed.pdf").write_bytes(pdf_closed)
    (zotero_dir / "files" / "3" / "stamped.pdf").write_bytes(pdf_stamped)
    (zotero_dir / "export.bib").write_text(
        f"@article{{k1, title={{{open_title}}}, doi={{{open_doi}}}, file={{open.pdf:files/1/open.pdf:application/pdf}}}}\n"
        f"@article{{k2, title={{{closed_title}}}, doi={{{closed_doi}}}, file={{closed.pdf:files/2/closed.pdf:application/pdf}}}}\n"
        f"@article{{k3, title={{{stamped_title}}}, doi={{{stamped_doi}}}, file={{stamped.pdf:files/3/stamped.pdf:application/pdf}}}}\n",
        encoding="utf-8",
    )

    bundles = tmp_path / "bundles"
    downloads = tmp_path / "downloads"
    csv_out = tmp_path / "checklist.csv"

    # 1. Checklist export works cleanly
    rc_csv = zt.main(["--export-batch", str(csv_out), "--bundle-root", str(bundles)])
    assert rc_csv == 0 and csv_out.is_file()
    assert open_doi in csv_out.read_text(encoding="utf-8-sig")

    # 2. Dry-run writes NOTHING to DB, downloads/, or bundles/
    rc_dry = zt.main([str(zotero_dir), "--bundle-root", str(bundles), "--downloads-dir", str(downloads)])
    assert rc_dry == 0
    assert not downloads.exists() and not bundles.exists()

    # 3. Commit routes OPEN -> downloads/ (`pdf_path` set) and TDM -> bundles/ (`pdf_path` NULL)
    rc_commit = zt.main([
        str(zotero_dir), "--commit", "--no-backup",
        "--bundle-root", str(bundles), "--downloads-dir", str(downloads),
    ])
    assert rc_commit == 0

    session = triage_db.SessionLocal()
    try:
        p_o = session.get(Paper, "p_open")
        p_c = session.get(Paper, "p_closed")
        p_s = session.get(Paper, "p_stamped")

        assert p_o.pdf_path and Path(p_o.pdf_path).is_file()
        assert p_o.retrieval_status == fb.SUCCESS

        assert p_c.pdf_path is None and p_c.retrieval_status == fb.HELD_TDM_ONLY
        assert p_s.pdf_path is None and p_s.retrieval_status == fb.HELD_TDM_ONLY

        # Verify bundle manifests & TDM Q&A guard enforcement
        tdm_qa_guard.clear_cache()
        assert tdm_qa_guard.qa_block_reason(p_o, [{"page": 1, "text": open_title}], bundle_root=bundles) == ""
        assert tdm_qa_guard.qa_block_reason(p_c, [{"page": 1, "text": closed_title}], bundle_root=bundles) != ""
        assert tdm_qa_guard.qa_block_reason(p_s, [{"page": 1, "text": stamped_title}], bundle_root=bundles) != ""
    finally:
        session.close()

    closed_mf = json.loads((bundles / fb.safe_bundle_name(closed_doi) / "bundle_manifest.json").read_text())
    assert closed_mf["main_manuscript"]["license_tier"] == fb.TIER_TDM
    assert closed_mf["main_manuscript"]["hosted_ai_upload_allowed"] is False
    assert (bundles / fb.safe_bundle_name(closed_doi) / "main_manuscript.pdf").is_file()
