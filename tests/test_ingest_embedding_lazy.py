"""Ingest-embedding + lazy-analysis contract tests (BRIEF v2 §3 / §6).

These tests pin the steady-state fetch pipeline's two load-bearing promises that
were previously wired but UNVERIFIED by the suite:

§3  Ingest computes a CHEAP title+abstract embedding (via
    services/embedding_service.py — no PDF, no Gemini Pro) and stores it on the
    Paper. Covered for both the direct ``embed_papers_batch`` helper and the
    live ``fetch_service.fetch_and_store_new_papers`` intake path.

§6  Full Gemini Pro analysis is LAZY: ingest never force-analyses the whole
    intake. A paper is analysed ONLY when it matches a tracker
    (``match_new_papers_to_trackers`` -> ``enqueue_analysis``) and the queue is
    later drained (``drain_analysis_queue``), or when opened. We verify:
      - matching writes ProjectPaper.match_score and enqueues the matched paper;
      - a NON-matching paper is never enqueued and gets no analysis;
      - draining runs the real analysis ONLY for the enqueued (matched) paper;
      - the ``process_ingested_papers`` orchestrator ties embed->match->drain.

All embeddings are mocked at the ``services.embedding_service`` boundary (the
helpers import ``embed_text`` / ``embed_texts`` lazily from there) so NO network
is touched, exactly like tests/test_embedding_service.py. The full analysis is
mocked at ``analyze_service._run_analysis`` so NO Gemini Pro call happens; we
assert *which* paper ids it was called for, which is the whole lazy contract.
Honors DISABLE_TRACKER_SCHEDULER=1 via the conftest autouse fixture.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Embedding mock — deterministic unit-ish vectors keyed off the text length so
# two different texts get two different (but valid, same-dim) vectors.
# ---------------------------------------------------------------------------

_DIM = 8


def _vec_for(text: str):
    """A length-_DIM float vector that varies with the text (never all-zero)."""
    h = abs(hash(text)) % 997 + 1
    base = [(h % (i + 7)) + 1 for i in range(_DIM)]
    norm = sum(v * v for v in base) ** 0.5
    return [v / norm for v in base]


@pytest.fixture()
def mock_embeddings(monkeypatch):
    """Patch embedding_service so embed_text / embed_texts return fake vectors.

    Patched on the module object so the lazy ``from services.embedding_service
    import embed_text, embed_texts`` inside the analyze_service helpers resolves
    to these fakes. A client is faked too so get_embedding_client() is truthy.
    """
    import services.embedding_service as es

    monkeypatch.setattr(es, "get_embedding_client", lambda: object())

    def _embed_text(text, *, is_query=False):
        if not text or not text.strip():
            return None
        return _vec_for(text)

    def _embed_texts(texts, *, is_query=False):
        return [(_vec_for(t) if (t and t.strip()) else None) for t in texts]

    monkeypatch.setattr(es, "embed_text", _embed_text)
    monkeypatch.setattr(es, "embed_texts", _embed_texts)
    return es


@pytest.fixture(autouse=True)
def _clear_analysis_queue():
    """The lazy queue is a process-global set — clear it around every test."""
    from services import analyze_service as a

    a._pop_analysis_batch()  # drain to empty
    yield
    a._pop_analysis_batch()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_tracker(db_session, *, name, keywords=None, topics=None) -> str:
    from models import Project

    cfg: dict = {"query": {}}
    if keywords:
        cfg["query"]["keywords"] = keywords
        cfg["keywords"] = keywords
    if topics:
        cfg["query"]["topics"] = topics
        cfg["topics"] = topics
    proj = Project(
        id=str(uuid.uuid4()),
        name=name,
        type="topic_monitor",
        kind="tracker",
        scope="personal",
        cadence="weekly",
        config=cfg,
    )
    db_session.add(proj)
    db_session.commit()
    return proj.id


# ===========================================================================
# §3 — ingest computes + stores the cheap embedding
# ===========================================================================


@pytest.mark.usefixtures("client")
def test_embed_papers_batch_writes_embedding_columns(make_paper, mock_embeddings):
    """embed_papers_batch stores embedding + embedding_model and returns the
    count of vectors written — the cheap §3 ingest hook."""
    import db as db_module
    from models import Paper
    from services.analyze_service import embed_papers_batch
    from services.embedding_service import EMBED_MODEL, decode_vector

    make_paper("emb-1", title="Slow slip on the megathrust", abstract="DAS arrays.")
    make_paper("emb-2", title="Ambient noise tomography of the crust", abstract="")

    session = db_module.SessionLocal()
    try:
        papers = session.query(Paper).all()
        stored = embed_papers_batch(papers, session, commit=True)
        assert stored == 2

        for pid in ("emb-1", "emb-2"):
            p = session.get(Paper, pid)
            assert p.embedding, f"{pid} has no embedding cell"
            assert p.embedding_model == EMBED_MODEL
            assert p.embedded_at is not None
            vec = decode_vector(p.embedding)
            assert vec is not None and vec.size == _DIM
    finally:
        session.close()


@pytest.mark.usefixtures("client")
def test_embed_papers_batch_is_idempotent(make_paper, mock_embeddings):
    """A second pass under the same model re-embeds nothing (returns 0)."""
    import db as db_module
    from models import Paper
    from services.analyze_service import embed_papers_batch

    make_paper("emb-idem", title="Repeatable embedding", abstract="x")
    session = db_module.SessionLocal()
    try:
        papers = session.query(Paper).all()
        assert embed_papers_batch(papers, session, commit=True) == 1
        # Re-run: already embedded under the active model -> skipped.
        assert embed_papers_batch(session.query(Paper).all(), session, commit=True) == 0
    finally:
        session.close()


def test_fetch_and_store_new_papers_embeds_at_intake(
    isolated_db, mock_embeddings, monkeypatch
):
    """The LIVE intake path computes the embedding for every fetched paper.

    All four network fetchers are stubbed to return one fixed paper-dict so no
    HTTP happens; we then assert fetch_and_store_new_papers persisted the row
    WITH an embedding and reported a non-zero ``embedded`` count. This is the
    gap the audit flagged: the steady-state fetch path must embed at ingest.
    """
    import main  # ensures init_db ran against the isolated DB
    import db as db_module
    from models import Paper
    from services import fetch_service as fs
    from services.embedding_service import EMBED_MODEL, decode_vector

    main.init_db()

    paper_dict = {
        "id": "fetch-emb-1",
        "source": "test",
        "title": "Fibre-optic sensing of microseismicity",
        "journal": "Journal of Test Seismology",
        "categories": ["DAS"],
        "institutions": [],
        "authors": ["A. Tester"],
        "date": "2026-05-20",
        "datetime_utc": "2026-05-20T00:00:00+00:00",
        "link": "https://example.test/1",
        "doi": "10.1234/test.1",
        "abstract": "We image microseismicity with distributed acoustic sensing.",
        "abstract_flag": "ok",
    }

    # Stub all source fetchers: RSS returns our paper, the rest return [].
    monkeypatch.setattr(
        fs, "fetch_from_rss", lambda *a, **k: [dict(paper_dict)], raising=True
    )
    monkeypatch.setattr(fs, "fetch_from_openalex", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "fetch_from_crossref", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "fetch_from_eth_rc", lambda *a, **k: [], raising=True)
    # Preprint Crossref-prefix (EarthArXiv/ESSOAr, added 2026-06-14) + tracker
    # author/arXiv intake are also wired into fetch_and_store_new_papers; stub
    # them so the test stays deterministic and offline.
    monkeypatch.setattr(fs, "fetch_from_crossref_prefix", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "_fetch_tracker_sources", lambda *a, **k: [], raising=True)

    session = db_module.SessionLocal()
    try:
        result = fs.fetch_and_store_new_papers(
            session,
            date_from=dt.date(2026, 5, 1),
            date_to=dt.date(2026, 5, 31),
        )
        assert result["created"] == 1
        assert result["embedded"] == 1, "intake did not embed the fetched paper"

        p = session.get(Paper, "fetch-emb-1")
        assert p is not None
        assert p.embedding, "fetched paper persisted WITHOUT an embedding (§3 unmet)"
        assert p.embedding_model == EMBED_MODEL
        assert decode_vector(p.embedding).size == _DIM
    finally:
        session.close()


def test_fetch_and_store_survives_embedding_outage(
    isolated_db, monkeypatch
):
    """If the embedding backend is down (no key -> embed returns None), intake
    still inserts the paper; it is simply left embedding-less (graceful §3)."""
    import main
    import db as db_module
    from models import Paper
    from services import fetch_service as fs
    import services.embedding_service as es

    main.init_db()
    # Simulate "no API key": client is None, so embed_texts returns all-None.
    monkeypatch.setattr(es, "get_embedding_client", lambda: None)

    paper_dict = {
        "id": "fetch-noemb-1",
        "source": "test",
        "title": "Paper with no embedding backend",
        "journal": "Journal of Test Seismology",
        "categories": [],
        "institutions": [],
        "authors": [],
        "date": "2026-05-20",
        "datetime_utc": "2026-05-20T00:00:00+00:00",
        "link": "https://example.test/2",
        "doi": "",
        "abstract": "Body.",
        "abstract_flag": "ok",
    }
    monkeypatch.setattr(
        fs, "fetch_from_rss", lambda *a, **k: [dict(paper_dict)], raising=True
    )
    monkeypatch.setattr(fs, "fetch_from_openalex", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "fetch_from_crossref", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "fetch_from_eth_rc", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "fetch_from_crossref_prefix", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "_fetch_tracker_sources", lambda *a, **k: [], raising=True)

    session = db_module.SessionLocal()
    try:
        result = fs.fetch_and_store_new_papers(
            session, date_from=dt.date(2026, 5, 1), date_to=dt.date(2026, 5, 31)
        )
        assert result["created"] == 1
        assert result["embedded"] == 0
        p = session.get(Paper, "fetch-noemb-1")
        assert p is not None and not p.embedding  # persisted, just unembedded
    finally:
        session.close()


# ===========================================================================
# §6 — lazy analysis: match -> enqueue -> drain (NOT eager over all intake)
# ===========================================================================


@pytest.mark.usefixtures("client")
def test_match_writes_score_and_enqueues_only_matches(make_paper, mock_embeddings):
    """match_new_papers_to_trackers writes ProjectPaper.match_score for a
    matching paper and enqueues ONLY it; a non-matching paper is left alone and
    NO analysis is created (the queue is a hint, no Gemini Pro is called)."""
    import db as db_module
    from models import Paper, ProjectPaper, AIAnalysis
    from services.analyze_service import (
        match_new_papers_to_trackers,
        analysis_queue_snapshot,
    )

    session = db_module.SessionLocal()
    try:
        _seed_tracker(session, name="DAS tracker", keywords=["distributed acoustic"])

        make_paper(
            "match-hit",
            title="Distributed acoustic sensing of aftershocks",
            abstract="We use distributed acoustic sensing on dark fibre.",
        )
        make_paper(
            "match-miss",
            title="Mantle convection modelling",
            abstract="Geodynamic simulations of whole-mantle flow.",
        )

        papers = session.query(Paper).all()
        stats = match_new_papers_to_trackers(papers, session)

        assert stats["papers_matched"] == 1
        assert stats["links_created"] == 1

        # The matched paper got a ProjectPaper row with a numeric match_score.
        link = (
            session.query(ProjectPaper)
            .filter(ProjectPaper.paper_id == "match-hit")
            .one()
        )
        assert link.match_score is not None and link.match_score > 0

        # The non-matching paper has no link.
        assert (
            session.query(ProjectPaper)
            .filter(ProjectPaper.paper_id == "match-miss")
            .count()
            == 0
        )

        # ONLY the matched paper is enqueued for lazy analysis.
        assert analysis_queue_snapshot() == ["match-hit"]

        # Matching alone NEVER creates an AIAnalysis row (analysis is lazy).
        assert session.query(AIAnalysis).count() == 0
    finally:
        session.close()


@pytest.mark.usefixtures("client")
def test_drain_runs_analysis_only_for_enqueued_paper(
    make_paper, mock_embeddings, monkeypatch
):
    """drain_analysis_queue runs the REAL analysis only for ids the match step
    enqueued — never for the whole intake. _run_analysis is mocked so no Gemini
    Pro call happens; we assert exactly which paper id it ran for."""
    import db as db_module
    from models import Paper
    from services import analyze_service as a

    analyzed_ids: list[str] = []

    def _fake_run_analysis(paper, db_session, **kwargs):
        analyzed_ids.append(paper.id)
        return {"ok": True}

    monkeypatch.setattr(a, "_run_analysis", _fake_run_analysis)

    session = db_module.SessionLocal()
    try:
        _seed_tracker(session, name="Slip tracker", keywords=["slow slip"])
        make_paper(
            "drain-hit",
            title="Slow slip events on the subduction zone",
            abstract="We characterise slow slip with cGPS.",
        )
        make_paper(
            "drain-miss",
            title="Lunar regolith mechanics",
            abstract="Granular flow on the Moon.",
        )

        papers = session.query(Paper).all()
        a.match_new_papers_to_trackers(papers, session)

        stats = a.drain_analysis_queue(session)

        assert stats["analyzed"] == 1
        assert analyzed_ids == ["drain-hit"], (
            "lazy drain analysed the wrong / too many papers — analysis is not "
            "scoped to tracker matches"
        )
        # Queue is now empty.
        assert a.analysis_queue_snapshot() == []
    finally:
        session.close()


@pytest.mark.usefixtures("client")
def test_process_ingested_papers_embeds_matches_and_drains(
    make_paper, mock_embeddings, monkeypatch
):
    """The orchestrator runs embed -> match -> drain in one call: every paper is
    embedded, only the matching one is analysed."""
    import db as db_module
    from models import Paper, ProjectPaper
    from services import analyze_service as a

    analyzed_ids: list[str] = []
    monkeypatch.setattr(
        a, "_run_analysis", lambda paper, s, **k: analyzed_ids.append(paper.id)
    )

    session = db_module.SessionLocal()
    try:
        _seed_tracker(session, name="Tomo tracker", keywords=["ambient noise"])
        make_paper(
            "orch-hit",
            title="Ambient noise cross-correlation imaging",
            abstract="Ambient noise tomography of the lithosphere.",
        )
        make_paper(
            "orch-miss",
            title="Cometary outgassing dynamics",
            abstract="Sublimation jets on comet 67P.",
        )

        papers = session.query(Paper).all()
        result = a.process_ingested_papers(papers, session)

        # Both papers embedded (cheap path).
        assert result["embedded"] == 2
        for pid in ("orch-hit", "orch-miss"):
            assert session.get(Paper, pid).embedding

        # Exactly one tracker link, exactly one analysis.
        assert result["match"]["papers_matched"] == 1
        assert (
            session.query(ProjectPaper)
            .filter(ProjectPaper.paper_id == "orch-hit")
            .count()
            == 1
        )
        assert result["analysis"]["analyzed"] == 1
        assert analyzed_ids == ["orch-hit"]
    finally:
        session.close()


@pytest.mark.usefixtures("client")
def test_process_ingested_papers_drain_false_defers_analysis(
    make_paper, mock_embeddings, monkeypatch
):
    """With drain=False the orchestrator embeds + matches + enqueues but runs NO
    analysis — leaving the heavy work to a later worker/route (e.g. the tracker
    scheduler enqueues but never auto-drains)."""
    import db as db_module
    from models import Paper
    from services import analyze_service as a

    monkeypatch.setattr(
        a, "_run_analysis", lambda *args, **k: pytest.fail("analysis ran with drain=False")
    )

    session = db_module.SessionLocal()
    try:
        _seed_tracker(session, name="Defer tracker", keywords=["earthquake early warning"])
        make_paper(
            "defer-hit",
            title="Earthquake early warning latency",
            abstract="Reducing earthquake early warning alert latency.",
        )

        papers = session.query(Paper).all()
        result = a.process_ingested_papers(papers, session, drain=False)

        assert result["embedded"] == 1
        assert result["match"]["papers_matched"] == 1
        assert result["analysis"] == {"analyzed": 0, "skipped": 0, "failed": 0}
        # The match still enqueued the paper for a future drain.
        assert a.analysis_queue_snapshot() == ["defer-hit"]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# P1-1 / P1-2 regression (2026-06-11): source-independent DOI dedup + field-level
# source-of-truth merge. See _audit_2026-06-11/05-CLEANUP-EXECUTED.md.
# ---------------------------------------------------------------------------

def _payload(*, pid, source, doi, authors, institutions, abstract, flag):
    return {
        "id": pid, "source": source, "title": "Crustal imaging",
        "journal": "JGR", "categories": [], "institutions": institutions,
        "keywords": [], "authors": authors, "date": "2026-05-20",
        "datetime_utc": "2026-05-20T00:00:00+00:00", "link": "https://x/1",
        "doi": doi, "abstract": abstract, "abstract_flag": flag,
    }


def test_dedup_collapses_same_doi_across_sources_case_insensitive(
    isolated_db, mock_embeddings, monkeypatch
):
    """P1-1: the same DOI from two sources, differing only in CASE, must map to a
    single row (dedup_key lowercases the DOI). The stronger source (crossref,
    which now runs first) owns the row's bibliographic metadata."""
    import main  # noqa: F401 — ensures init_db ran against the isolated DB
    import db as db_module
    from models import Paper
    from services import fetch_service as fs

    main.init_db()

    cx_id = fs.dedup_key(doi="10.1029/2024JB099999")   # Crossref casing
    rs_id = fs.dedup_key(doi="10.1029/2024jb099999")   # OpenAlex/RSS casing
    assert cx_id == rs_id, "dedup_key must be DOI-case-insensitive (P1-1)"

    cx = _payload(pid=cx_id, source="crossref", doi="10.1029/2024JB099999",
                  authors=["A. One", "B. Two"], institutions=["ETH Zurich"],
                  abstract="", flag="missing")
    rs = _payload(pid=rs_id, source="rss", doi="10.1029/2024jb099999",
                  authors=[], institutions=[], abstract="Full text.", flag="ok")

    monkeypatch.setattr(fs, "fetch_from_crossref", lambda *a, **k: [dict(cx)], raising=True)
    monkeypatch.setattr(fs, "fetch_from_openalex", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "fetch_from_eth_rc", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "fetch_from_rss", lambda *a, **k: [dict(rs)], raising=True)
    monkeypatch.setattr(fs, "fetch_from_crossref_prefix", lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "_fetch_tracker_sources", lambda *a, **k: [], raising=True)

    session = db_module.SessionLocal()
    try:
        result = fs.fetch_and_store_new_papers(
            session, date_from=dt.date(2026, 5, 1), date_to=dt.date(2026, 5, 31)
        )
        assert result["created"] == 1, "same DOI across sources must create ONE row"
        rows = session.query(Paper).all()
        assert len(rows) == 1
        p = rows[0]
        assert p.source == "crossref"               # strong source ran first, won
        assert p.authors == ["A. One", "B. Two"]    # crossref's rich authors kept
        assert p.institutions == ["ETH Zurich"]
        assert p.abstract == "Full text."           # weak rss filled the empty abstract (within-run merge)
    finally:
        session.close()


def test_strong_source_upgrades_but_empty_does_not_clobber(
    isolated_db, mock_embeddings, monkeypatch
):
    """P1-2 / P1-2-b: across runs, a stronger source fills empty bibliographic
    fields, but its EMPTY abstract must NOT wipe a full abstract a weaker source
    already stored."""
    import main  # noqa: F401
    import db as db_module
    from models import Paper
    from services import fetch_service as fs

    main.init_db()
    doi = "10.1234/sot.1"
    pid = fs.dedup_key(doi=doi)

    # Run 1: only RSS has it — full abstract, no authors/institutions.
    rs = _payload(pid=pid, source="rss", doi=doi, authors=[], institutions=[],
                  abstract="Full RSS abstract.", flag="ok")
    for fn in ("fetch_from_crossref", "fetch_from_openalex", "fetch_from_eth_rc"):
        monkeypatch.setattr(fs, fn, lambda *a, **k: [], raising=True)
    monkeypatch.setattr(fs, "fetch_from_rss", lambda *a, **k: [dict(rs)], raising=True)
    s = db_module.SessionLocal()
    try:
        fs.fetch_and_store_new_papers(s, date_from=dt.date(2026, 5, 1), date_to=dt.date(2026, 5, 31))
    finally:
        s.close()

    # Run 2: now OpenAlex (strong) has it — rich authors+institutions, NO abstract.
    oa = _payload(pid=pid, source="openalex", doi=doi, authors=["Z. Author"],
                  institutions=["Caltech"], abstract="", flag="missing")
    monkeypatch.setattr(fs, "fetch_from_openalex", lambda *a, **k: [dict(oa)], raising=True)
    monkeypatch.setattr(fs, "fetch_from_rss", lambda *a, **k: [], raising=True)
    s = db_module.SessionLocal()
    try:
        fs.fetch_and_store_new_papers(s, date_from=dt.date(2026, 5, 1), date_to=dt.date(2026, 5, 31))
        p = s.get(Paper, pid)
        assert p.authors == ["Z. Author"]          # strong filled empty authors
        assert p.institutions == ["Caltech"]        # strong filled empty institutions
        assert p.abstract == "Full RSS abstract."   # P1-2-b: empty abstract did NOT clobber
        assert p.source == "openalex"               # source upgraded
    finally:
        s.close()
