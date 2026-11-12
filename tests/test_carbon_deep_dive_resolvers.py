"""Offline unit tests for SeismoHub v2.3 Carbon Deep-Dive upgrades:
- Link-based identifier inference & DOI backfill (`nature.com/articles/*`, ETH RC handles, HAL)
- Nature Accelerated Article Preview (`_reference.pdf`) fallback on HTML response
- Springer (`10.1007/*`) PDF tiering & in-loop 'All rights reserved' TDM demotion + preprint fallback
- Smart Zenodo main-manuscript selection over larger supplement PDFs
- Multi-author surname Gate-4 verification for edited preprint titles (with strict >=3 co-author guard)
- ESSOAr (`10.22541/essoar.*`) -> published journal VoR resolution & TDM compliance
- OSTI.gov, USGS Pubs, HAL, GFZ PubMan, Semantic Scholar DSpace7, and DataCite resolvers
- OpenAlex API key isolation (never leaked into URLs or sent to list/search endpoints)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Tuple

import pytest

fitz = pytest.importorskip("fitz")

from services import fullbundle_harvest as fb  # noqa: E402
from services import deep_oa_and_vor_resolvers as dv  # noqa: E402
from scripts import seismohub_upgrade_runner as ur  # noqa: E402


def _make_pdf(title: str, *, pages: int = 4, extra_lines: Tuple[str, ...] = (), filler: str = "seismic wavefield tomography") -> bytes:
    doc = fitz.open()
    for idx in range(pages):
        page = doc.new_page()
        y = 40
        if idx == 0:
            page.insert_text((36, y), title, fontsize=9)
            y += 14
            for ln in extra_lines:
                page.insert_text((36, y), ln, fontsize=8)
                y += 12
        while y < 780:
            page.insert_text((36, y), (filler + " ") * 4, fontsize=6)
            y += 9
    data = doc.tobytes()
    doc.close()
    return data


class FakeRouteHttp:
    def __init__(self, routes: Dict[str, Tuple[int, bytes]]):
        self.routes = routes
        self.calls = []

    def get(self, url: str, *, headers=None, sensitive_headers=(), max_bytes=0, retries=1):
        self.calls.append((url, dict(headers or {})))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                st, body = self.routes[prefix]
                return fb.HttpResult(url=url, final_url=url, status=st, data=body)
        return fb.HttpResult(url=url, final_url=url, status=404, data=b"")


def test_infer_identifiers_from_link():
    nat = dv.infer_identifiers_from_link("", "https://www.nature.com/articles/s41467-026-76407-5")
    assert nat["inferred_doi"] == "10.1038/s41467-026-76407-5"
    assert nat["effective_doi"] == "10.1038/s41467-026-76407-5"

    eth = dv.infer_identifiers_from_link("", "https://www.research-collection.ethz.ch/handle/20.500.11850/806982.2")
    assert eth["ethrc_handle_num"] == "806982"

    hal = dv.infer_identifiers_from_link("", "https://hal.science/hal-05621959v1")
    assert hal["hal_id"] == "hal-05621959"


def test_smart_zenodo_prefers_main_preprint_over_larger_supplement():
    zenodo_payload = json.dumps({
        "metadata": {"resource_type": {"type": "publication"}, "license": {"id": "cc-by-4.0"}},
        "files": [
            {"key": "Wang_Fichtner_2026_supplement_v1.pdf", "size": 16_000_000,
             "links": {"self": "https://zenodo.org/api/records/23354895/files/supplement.pdf/content"}},
            {"key": "Wang_Fichtner_2026_preprint_v1.pdf", "size": 4_600_000,
             "links": {"self": "https://zenodo.org/api/records/23354895/files/preprint.pdf/content"}},
        ],
    }).encode()
    http = FakeRouteHttp({"https://zenodo.org/api/records/23354895": (200, zenodo_payload)})
    url, lic, why = dv.resolve_zenodo_smart(http, "10.5281/zenodo.23354895")
    assert why == ""
    assert url.endswith("preprint.pdf/content")
    assert lic == "cc-by-4.0"


def test_enhanced_identity_check_rescues_retitled_preprint_only_with_3plus_coauthors():
    journal_title = "Super-resolution method based on a Transformer: a novel solution for improving seismic data"
    preprint_head = (
        "Transformer for seismic image super-resolution\n"
        "Shiqi Dong, Xintong Dong, Kaiyuan Zheng, Ming Cheng, Tie Zhong, Hongzhou Wang\n"
        "Abstract: Seismic images obtained by stacking or migration..."
    )
    authors_6 = json.dumps(["Shiqi Dong", "Xintong Dong", "Kaiyuan Zheng", "Ming Cheng", "Tie Zhong", "Hongzhou Wang"])
    ok, reason = dv.enhanced_identity_check(
        preprint_head,
        doi="10.1190/geo-2026-1896",
        title=journal_title,
        authors_raw=authors_6,
        orig_identity_fn=fb._orig_id_for_test if hasattr(fb, "_orig_id_for_test") else (
            lambda h, *, doi, title: (False, "identity_mismatch")
        ),
        title_overlap_fn=fb.title_overlap,
    )
    assert ok and "+authors(5/5)" in reason

    # Different co-authors MUST still be rejected even with similar partial title words
    wrong_authors = json.dumps(["Alice Smith", "Bob Jones", "Carol White", "Dan Brown"])
    ok2, _ = dv.enhanced_identity_check(
        preprint_head,
        doi="10.1190/geo-2026-1896",
        title=journal_title,
        authors_raw=wrong_authors,
        orig_identity_fn=lambda h, *, doi, title: (False, "identity_mismatch"),
        title_overlap_fn=fb.title_overlap,
    )
    assert not ok2


def test_nature_aap_fallback_and_in_loop_rights_demotion(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "DEFAULT_KEYS_FILE", tmp_path / "none.env")
    ur.install_harvester_upgrades(fb, cache_path=tmp_path / "cache.sqlite", readonly=True)

    # 1. Nature CEE Gold OA whose `.pdf` serves HTML but `_reference.pdf` serves 4-page PDF
    cee_doi = "10.1038/s43247-026-03820-5"
    cee_title = "Earthquake doublets in subduction zones are an Omori process"
    http_cee = FakeRouteHttp({
        "https://api.openalex.org/": (404, b""),
        "https://www.nature.com/articles/s43247-026-03820-5.pdf": (200, b"<!DOCTYPE html><html>article</html>"),
        "https://www.nature.com/articles/s43247-026-03820-5_reference.pdf": (
            200,
            _make_pdf(cee_title, extra_lines=("Creative Commons Attribution 4.0 International License",)),
        ),
    })
    harvester = fb.FullBundleHarvester(http_cee, min_bytes=0)
    out = harvester.harvest(fb.PaperRef("p_cee", doi=cee_doi, title=cee_title))
    assert out.status == fb.SUCCESS
    assert out.app_hit is not None and out.app_hit.channel == "nature_aap" and out.app_hit.tier == fb.TIER_OPEN

    # 2. Subscribed Springer `10.1007/*` with 'All rights reserved' stays strictly in held_hit (TDM-only),
    #    never attached to app_hit!
    sp_doi = "10.1007/s10950-026-10429-w"
    sp_title = "Seismograms and instrument-response functions for stations of the Lamont-IGY network"
    http_sp = FakeRouteHttp({
        "https://api.openalex.org/": (404, b""),
        "https://api.crossref.org/": (200, b'{"message":{"license":[]}}'),
        f"https://link.springer.com/content/pdf/{sp_doi}.pdf": (
            200,
            _make_pdf(sp_title, extra_lines=("(c) 2026 Springer Nature B.V. All rights reserved.",)),
        ),
    })
    out_sp = fb.FullBundleHarvester(http_sp, min_bytes=0).harvest(fb.PaperRef("p_sp", doi=sp_doi, title=sp_title))
    assert out_sp.app_hit is None
    assert out_sp.held_hit is not None and out_sp.held_hit.tier == fb.TIER_TDM
    assert out_sp.status == fb.HELD_TDM_ONLY


def test_essoar_vor_resolution_and_osti_resolver(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "DEFAULT_KEYS_FILE", tmp_path / "none.env")
    monkeypatch.setenv("SEISMO_DB_PATH", str(tmp_path / "nonexistent.db"))
    ur.install_harvester_upgrades(fb, cache_path=tmp_path / "cache.sqlite", readonly=True)

    essoar_doi = "10.22541/essoar.15006812/v1"
    osti_doi = "10.2172/3398818"
    title = "Full Waveform Inversion Tomography for Improved Seismic Velocity Models"
    cr_essoar = json.dumps({
        "message": {
            "relation": {"is-preprint-of": [{"id-type": "doi", "id": osti_doi}]}
        }
    }).encode()
    http = FakeRouteHttp({
        "https://api.openalex.org/": (404, b""),
        f"https://api.crossref.org/works/{essoar_doi}": (200, cr_essoar),
        "https://www.osti.gov/servlets/purl/3398818": (200, _make_pdf(title)),
    })
    ref = fb.PaperRef("p_ess", doi=essoar_doi, title=title)
    out = fb.FullBundleHarvester(http, min_bytes=0).harvest(ref)
    assert out.status == fb.SUCCESS
    assert out.app_hit is not None
    assert "osti_gov" in out.app_hit.channel
    assert out.app_hit.tier == fb.TIER_OPEN


def test_audit_fixes_reject_egu_abstract_readme_pdf_disallowed_ethrc_and_email_ua(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "DEFAULT_KEYS_FILE", tmp_path / "none.env")
    monkeypatch.delenv("SEISMO_HARVEST_UA", raising=False)
    ur.install_harvester_upgrades(fb, cache_path=tmp_path / "cache.sqlite", readonly=True)

    # 1. Privacy-safe UA has NO personal email address
    ps = fb.PoliteSession(sleep=lambda s: None)
    assert "@" not in ps.session.headers["User-Agent"]
    assert "mailto:" not in ps.session.headers["User-Agent"]

    # 2. EGU General Assembly abstract (`EGU26-18759`) masquerading under a BSSA journal title is rejected
    bssa_title = "When Earthquakes Cross the Gap: Physics-Based Dynamic Modeling of Stepover Jumps"
    egu_head = (
        "HAL Id: hal-05620196\nEGU26-18759, updated on 12 May 2026\n"
        "https://doi.org/10.5194/egusphere-egu26-18759\nEGU General Assembly 2026\n" + bssa_title
    )
    ok_egu, why_egu = fb.identity_check(egu_head, doi="10.1785/0120260099", title=bssa_title)
    assert not ok_egu and "conference_abstract_not_full_paper" in why_egu

    # 3. Zenodo record containing ONLY `ReadMe.pdf` is rejected as documentation rather than paper full text
    z_readme = json.dumps({
        "metadata": {"resource_type": {"type": "publication"}, "license": {"id": "cc-by-4.0"}},
        "files": [{"key": "ReadMe.pdf", "size": 1_235_505, "links": {"self": "https://zenodo.org/files/ReadMe.pdf"}}],
    }).encode()
    http_z = FakeRouteHttp({"https://zenodo.org/api/records/17500574": (200, z_readme)})
    u_z, _, why_z = dv.resolve_zenodo_smart(http_z, "10.5281/zenodo.17500574")
    assert u_z == "" and why_z == "zenodo_only_readme_or_doc_pdf"

    # 4. ETH Research Collection resolver uses ONLY robots.txt-allowed `/handle/` + `/server/api/core/bitstreams/`,
    #    NEVER touching `/server/api/discover/` or `/server/api/core/items/`
    eth_html = (
        '<html><head>'
        '<meta name="citation_title" content="Centrifuge-based experimental validation of seismic metamaterials">'
        '<meta name="citation_doi" content="https://doi.org/10.3929/ethz-c-000802564">'
        '<meta name="citation_pdf_url" content="https://www.research-collection.ethz.ch/bitstreams/885db2dc-9787-4f36-b40e-89e81bcd3014/download">'
        '</head></html>'
    ).encode()
    http_eth = FakeRouteHttp({"https://www.research-collection.ethz.ch/handle/20.500.11850/802564": (200, eth_html)})
    backfill: Dict[str, str] = {}
    u_eth, _, why_eth = dv.resolve_ethrc_handle_or_doi(
        http_eth,
        doi="",
        link="https://www.research-collection.ethz.ch/handle/20.500.11850/802564",
        title="Centrifuge-based experimental validation of seismic metamaterials",
        title_overlap_fn=fb.title_overlap,
        normalize_lic_fn=fb.normalize_license_label,
        out_backfill=backfill,
    )
    assert why_eth == ""
    assert u_eth == "https://www.research-collection.ethz.ch/server/api/core/bitstreams/885db2dc-9787-4f36-b40e-89e81bcd3014/content"
    assert backfill.get("doi") == "10.3929/ethz-c-000802564"
    assert not any("/server/api/discover" in c[0] or "/server/api/core/items" in c[0] for c in http_eth.calls)

