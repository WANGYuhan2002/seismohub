"""PDF Q&A TDM check: services/tdm_qa_guard.py + the hook in main.chat_with_paper (owner decision 2026-11-09).

Offline. Bundle manifests live in tmp dirs; the chat endpoint runs against the isolated test DB with the LLM call
replaced, so nothing leaves the machine.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services import tdm_qa_guard as g

OPEN_SHA, TDM_SHA, PREPRINT_SHA, DETACHED_SHA, REPORTED_SHA = "a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64
RR = "(c) 2026 American Geophysical Union. All rights reserved."
OA = "This is an open access article under the terms of the Creative Commons Attribution License."
ETH = "Downloaded from https://academic.oup.com/gji/article/1/1/1/123 by ETH Zurich user on 09 November 2026"
PLAIN = "Seismic velocity changes after a large earthquake. 1 Introduction. Ambient noise ..."


def _paper(pid="p1", sha=OPEN_SHA, status="success", source="openalex"):
    return SimpleNamespace(id=pid, pdf_sha256=sha, retrieval_status=status, retrieval_source=source)


def _pages(*texts):
    return [{"page": i + 1, "text": t, "chars": len(t)} for i, t in enumerate(texts)]


@pytest.fixture()
def bundles(tmp_path):
    root = tmp_path / "paper_bundles"

    def bundle(name, manifest):
        (root / name).mkdir(parents=True)
        (root / name / "bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    bundle("PaperBundle_10.1029_tdm", {
        "paper_id": "pT", "app_attached": {"attached": False},
        "main_manuscript": {"license_tier": g.TIER_TDM, "hosted_ai_upload_allowed": False, "sha256": TDM_SHA}})
    copy = {"license_tier": "OPEN_CC_BY_OR_PREPRINT", "hosted_ai_upload_allowed": True, "sha256": PREPRINT_SHA,
            "kind": "open_copy"}
    bundle("PaperBundle_arXiv_2611.00001", {"paper_id": "pP", "main_manuscript": dict(copy),
                                            "app_attached": {"attached": True, "copy": copy}})
    (root / "_app_detached_tdm_2026-11-09").mkdir()
    (root / "_app_detached_tdm_2026-11-09" / "DETACHED_FROM_APP.json").write_text(
        json.dumps({"papers": [], "additions": [{"sha256": DETACHED_SHA}]}), encoding="utf-8")
    (root / "_reports").mkdir()
    reported = {"license_tier": "OPEN_CC_BY_OR_PREPRINT", "hosted_ai_upload_allowed": True, "sha256": REPORTED_SHA,
                "kind": "open_copy"}
    (root / "_reports" / "harvest_2026-11-09_000000.jsonl").write_text(
        json.dumps({"paper_id": "pT", "attached": True, "app_copy": reported}) + "\n", encoding="utf-8")
    g.clear_cache()
    yield root
    g.clear_cache()


def test_no_pages_never_blocks(bundles):
    assert g.qa_block_reason(_paper(status="held_tdm_only"), [], bundles) == ""


@pytest.mark.parametrize("paper", [_paper(status="held_tdm_only"), _paper(source="elsevier_tdm_xml"),
                                   _paper(sha=TDM_SHA), _paper(sha=DETACHED_SHA)])
def test_tdm_papers_and_copies_are_blocked(bundles, paper):
    assert g.qa_block_reason(paper, _pages(PLAIN), bundles)


def test_tdm_banner_is_blocked(bundles):
    assert g.qa_block_reason(_paper(), _pages("[LICENSED TDM COPY] " + PLAIN), bundles)


def test_rights_reserved_without_open_statement_is_blocked(bundles):
    assert "All rights reserved" in g.qa_block_reason(_paper(), _pages(PLAIN, RR), bundles)


def test_eth_download_stamp_without_open_statement_is_blocked(bundles):
    assert "institution download stamp" in g.qa_block_reason(_paper(), _pages(PLAIN + " " + ETH), bundles)


@pytest.mark.parametrize("texts", [(PLAIN,), (OA, RR), (PLAIN + " " + ETH, OA)])
def test_open_papers_are_allowed(bundles, texts):
    assert g.qa_block_reason(_paper(), _pages(*texts), bundles) == ""


def test_preprint_copy_with_a_copyright_line_is_allowed(bundles):
    assert g.qa_block_reason(_paper(pid="pP", sha=PREPRINT_SHA, source="arxiv"), _pages(RR), bundles) == ""


def test_other_copy_of_a_paper_with_a_tdm_copy_on_file_is_blocked(bundles):
    # e.g. the subscription article saved from a browser and uploaded by hand: other bytes, no licence text
    reason = g.qa_block_reason(_paper(pid="pT", sha=OPEN_SHA, status=None, source=None), _pages(PLAIN), bundles)
    assert "TDM-licensed copy of this paper is on file" in reason


def test_known_open_copy_of_a_paper_with_a_tdm_copy_on_file_is_allowed(bundles):
    # the harvester kept the TDM version in the bundle and attached an open repository copy (harvest report)
    assert g.qa_block_reason(_paper(pid="pT", sha=REPORTED_SHA, source="arxiv"), _pages(RR), bundles) == ""


def test_errors_fail_closed(bundles, monkeypatch):
    def boom(_root):
        raise OSError("disk gone")

    monkeypatch.setattr(g, "_get_index", boom)
    assert g.qa_block_reason(_paper(), _pages(PLAIN), bundles).startswith("licence check failed")


# --- the hook in main.chat_with_paper ----------------------------------------------------------------------

def _add_page_cache(paper_id, pages):
    import db as db_module
    from models import PaperPageCache

    session = db_module.SessionLocal()
    try:
        session.add(PaperPageCache(paper_id=paper_id, pages=pages, page_count=len(pages)))
        session.commit()
    finally:
        session.close()


def _qa_rows(paper_id):
    import db as db_module
    from models import QAMessage

    session = db_module.SessionLocal()
    try:
        return session.query(QAMessage).filter(QAMessage.paper_id == paper_id).count()
    finally:
        session.close()


@pytest.fixture()
def chat_env(client, monkeypatch, tmp_path):
    import services.qa_service as qa

    monkeypatch.setattr(g, "DEFAULT_BUNDLE_ROOT", tmp_path / "no_bundles")
    g.clear_cache()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    calls = []

    def fake_answer_question(**kwargs):
        calls.append(kwargs)
        return {"reply": "fake answer", "citations": [], "model": "test-model", "model_label": "Test model"}

    monkeypatch.setattr(qa, "answer_question", fake_answer_question)
    yield client, calls
    g.clear_cache()


def test_chat_refuses_a_tdm_paper_before_any_llm_call(chat_env, make_paper):
    client, calls = chat_env
    make_paper("chat-tdm", title="A TDM-licensed paper", retrieval_status="held_tdm_only")
    _add_page_cache("chat-tdm", _pages(PLAIN))
    r = client.post("/api/papers/chat-tdm/chat", json={"message": "Which method is used?"})
    assert r.status_code == 403
    assert "institution-licensed" in r.json()["detail"]
    assert calls == []  # the LLM was never asked
    assert _qa_rows("chat-tdm") == 0  # nothing persisted


def test_chat_refuses_a_subscription_pdf_uploaded_by_hand(chat_env, make_paper):
    client, calls = chat_env
    make_paper("chat-sub", title="A subscription paper", pdf_sha256=OPEN_SHA, retrieval_status=None)
    _add_page_cache("chat-sub", _pages(PLAIN + " " + ETH, RR))
    r = client.post("/api/papers/chat-sub/chat", json={"message": "Summarise it"})
    assert r.status_code == 403 and calls == []


def test_chat_still_answers_for_an_open_paper(chat_env, make_paper):
    client, calls = chat_env
    make_paper("chat-open", title="An open paper", retrieval_status="success", retrieval_source="arxiv")
    _add_page_cache("chat-open", _pages(PLAIN, OA))
    r = client.post("/api/papers/chat-open/chat", json={"message": "Which method is used?"})
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "fake answer" and r.json()["has_pages"] is True
    assert len(calls) == 1 and calls[0]["pages"][0]["text"] == PLAIN
    assert _qa_rows("chat-open") == 2
