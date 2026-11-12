"""PDF analysis is a display layer (owner decision 2026-11-13).

Papers are kept and classified from title + abstract (+ public metadata) only. An analysis that reads the PDF is
stored under ``payload["pdf_analysis"]``: the detail view shows it, but topics, facets, relevance, keywords, the
summary columns the keyword predicate reads, and tracker assignments stay with the title/abstract analysis.

Offline: the Gemini client, web context and licence guard are replaced; the PDF is the synthetic test fixture.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "sample_paper.pdf"
ABSTRACT_TOPIC = "Ambient-noise seismology & interferometry"
PDF_TOPIC = "Machine learning in seismology"

ABSTRACT_RESULT = {
    "cos_topic_match": [{"topic": ABSTRACT_TOPIC, "confidence": 0.9, "why": "abstract"}],
    "relevance_score": 6,
    "keywords": ["ambient noise"],
    "summary_150w_en": "Abstract-based summary.",
    "one_sentence_takeaway_en": "Abstract takeaway.",
    "evidence": {"based_on": "abstract+web", "notes": ""},
}
PDF_RESULT = {
    "cos_topic_match": [{"topic": PDF_TOPIC, "confidence": 0.95, "why": "full text"}],
    "relevance_score": 10,
    "keywords": ["graph wavelet transform"],
    "summary_150w_en": "PDF-based summary.",
    "one_sentence_takeaway_en": "PDF takeaway.",
    "evidence": {"based_on": "pdf", "notes": "read the full text"},
}


@pytest.fixture()
def fake_ai(monkeypatch):
    """Replace every network-facing piece of the analysis; record which calls carried the PDF."""
    import services.analyze_service as A
    import services.tdm_guard_and_xml_renderer as guard

    calls = {"gemini": [], "assign": 0}

    def fake_call_gemini(client, prompt, parts=None, model_name=None, fallback_model=None):
        calls["gemini"].append("pdf" if parts else "abstract")
        return json.dumps(PDF_RESULT if parts else ABSTRACT_RESULT), model_name

    real_assign = A._auto_assign_to_topic_monitors

    def spy_assign(*args, **kwargs):
        calls["assign"] += 1
        return real_assign(*args, **kwargs)

    monkeypatch.setattr(A, "_get_vertex_client", lambda: object())
    monkeypatch.setattr(A, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(A, "gather_web_context", lambda item: {"used_sources": ["openalex"]})
    monkeypatch.setattr(A, "_validate_paragraph_anchors", lambda *a, **k: [])
    monkeypatch.setattr(A, "_cache_pages", lambda *a, **k: None)
    monkeypatch.setattr(A, "extract_figure_insights_from_pdf", lambda path: [])
    monkeypatch.setattr(A, "_auto_assign_to_topic_monitors", spy_assign)
    monkeypatch.setattr(guard, "is_tdm_restricted_paper", lambda paper, pdf_path=None: (False, ""))
    return calls


@pytest.fixture()
def paper_with_pdf(isolated_db, make_paper, tmp_path):
    import db as db_module

    db_module.init_db()  # schema + preset trackers in the isolated test DB
    pdf = tmp_path / "sample_paper.pdf"
    shutil.copy(FIXTURE_PDF, pdf)
    make_paper("p-layer", title="Ambient noise monitoring with a neural operator",
               abstract="We monitor velocity changes with ambient noise interferometry.",
               abstract_flag="present", pdf_path=str(pdf))
    return "p-layer", str(pdf)


def _session():
    import db as db_module

    return db_module.SessionLocal()


def _analysis(paper_id):
    from models import Paper

    s = _session()
    try:
        paper = s.get(Paper, paper_id)
        a = paper.ai_analysis
        return paper, a, dict(a.payload or {})
    finally:
        s.close()


def test_pdf_analysis_classifies_from_abstract_first(fake_ai, paper_with_pdf):
    import services.analyze_service as A

    pid, pdf = paper_with_pdf
    s = _session()
    try:
        A.analyze_paper_from_pdf(pid, pdf, s)
    finally:
        s.close()
    assert fake_ai["gemini"] == ["abstract", "pdf"]  # classification before the PDF is read
    assert fake_ai["assign"] == 1  # only the title/abstract analysis assigns trackers

    paper, a, payload = _analysis(pid)
    assert a.pdf_used is False and a.evidence_based_on == "abstract+web"
    assert [m["topic"] for m in a.topic_matches] == [ABSTRACT_TOPIC]
    assert a.relevance_score == 6 and a.keywords == ["ambient noise"]
    assert a.summary == "Abstract-based summary."
    assert ABSTRACT_TOPIC in paper.topics and PDF_TOPIC not in paper.topics
    layer = payload["pdf_analysis"]
    assert layer["pdf_used"] is True and layer["summary_150w_en"] == "PDF-based summary."


def test_existing_abstract_analysis_is_untouched_by_a_pdf_run(fake_ai, paper_with_pdf):
    import services.analyze_service as A

    pid, pdf = paper_with_pdf
    s = _session()
    try:
        A.analyze_paper_from_metadata(pid, s)
        before = _analysis(pid)[1]
        A.analyze_paper_from_pdf(pid, pdf, s)
    finally:
        s.close()
    assert fake_ai["gemini"] == ["abstract", "pdf"]  # no second classification call
    paper, a, payload = _analysis(pid)
    assert (a.topic_matches, a.relevance_score, a.keywords, a.summary, a.facets) == (
        before.topic_matches, before.relevance_score, before.keywords, before.summary, before.facets)
    assert "pdf_analysis" in payload


def test_rerunning_the_abstract_analysis_keeps_the_pdf_layer(fake_ai, paper_with_pdf):
    import services.analyze_service as A

    pid, pdf = paper_with_pdf
    s = _session()
    try:
        A.analyze_paper_from_pdf(pid, pdf, s)
        A.analyze_paper_from_metadata(pid, s)
    finally:
        s.close()
    _, a, payload = _analysis(pid)
    assert a.pdf_used is False
    assert payload["pdf_analysis"]["summary_150w_en"] == "PDF-based summary."


def test_legacy_pdf_row_is_split_by_an_abstract_analysis(fake_ai, paper_with_pdf):
    import services.analyze_service as A
    from models import AIAnalysis

    pid, _pdf = paper_with_pdf
    s = _session()
    try:
        legacy = {"summary_150w_en": "Old PDF summary.", "pdf_used": True,
                  "cos_topic_match": [{"topic": PDF_TOPIC, "confidence": 1.0, "why": ""}]}
        s.add(AIAnalysis(paper_id=pid, payload=legacy, pdf_used=True, summary="Old PDF summary.",
                         topic_matches=legacy["cos_topic_match"], relevance_score=10))
        s.commit()
        A.analyze_paper_from_metadata(pid, s)
    finally:
        s.close()
    paper, a, payload = _analysis(pid)
    assert a.pdf_used is False and a.summary == "Abstract-based summary."
    assert [m["topic"] for m in a.topic_matches] == [ABSTRACT_TOPIC]
    assert payload["pdf_analysis"]["summary_150w_en"] == "Old PDF summary."
    assert A.has_abstract_layer(a) and A.pdf_layer_of(a) is not None


def test_keyword_matching_reads_only_the_abstract_layer(fake_ai, paper_with_pdf):
    import services.analyze_service as A
    from models import Paper
    from services.predicates import _keyword_haystack

    pid, pdf = paper_with_pdf
    s = _session()
    try:
        A.analyze_paper_from_pdf(pid, pdf, s)
        hay = _keyword_haystack(s.get(Paper, pid))
    finally:
        s.close()
    assert "abstract-based summary" in hay and "ambient noise" in hay
    assert "pdf-based summary" not in hay and "graph wavelet transform" not in hay


def test_detail_view_shows_the_pdf_layer_and_lists_do_not(fake_ai, paper_with_pdf, client):
    import services.analyze_service as A

    pid, pdf = paper_with_pdf
    s = _session()
    try:
        A.analyze_paper_from_pdf(pid, pdf, s)
    finally:
        s.close()
    detail = client.get(f"/api/papers/{pid}").json()
    detail = detail.get("paper", detail)
    assert detail["analysis_summary"] == "PDF-based summary."
    assert detail["pdf_used"] is True and detail["pdf_analysis_state"] == "used"
    assert detail["evidence"]["based_on"] == "pdf"
    assert [m["topic"] for m in detail["cos_topic_match"]] == [ABSTRACT_TOPIC]
    assert detail["analysis_payload"]["relevance_score"] == 6  # classification keys stay from title/abstract

    import main
    from models import Paper

    s = _session()
    try:
        row = main._paper_to_dict(s.get(Paper, pid), include_detail=False)  # what list views serialise
    finally:
        s.close()
    assert row["analysis_summary"] == "Abstract-based summary." and row["pdf_used"] is False
    assert "analysis_payload" not in row


def test_reanalyze_stale_pdf_skips_papers_with_a_pdf_layer(fake_ai, paper_with_pdf, make_paper, tmp_path):
    import services.analyze_service as A
    from scripts import reanalyze_stale_pdf as R

    pid, pdf = paper_with_pdf
    other_pdf = tmp_path / "other.pdf"
    shutil.copy(FIXTURE_PDF, other_pdf)
    make_paper("p-stale", title="Another paper", abstract="Seismic noise.", abstract_flag="present",
               pdf_path=str(other_pdf))
    s = _session()
    try:
        A.analyze_paper_from_pdf(pid, pdf, s)          # has a PDF layer -> skipped
        A.analyze_paper_from_metadata("p-stale", s)    # abstract only, PDF on disk -> candidate
        picked, skipped = R.select_candidates(s)
    finally:
        s.close()
    assert [p["id"] for p in picked] == ["p-stale"]
    assert skipped["has_pdf_layer"] == 1
