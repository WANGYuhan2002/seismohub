"""Tests for the parallel eager-analysis path (2026-07-06).

Covers `analyze_pending_papers(workers>1)` — the bounded-thread-pool fan-out
added for the eager daemon — plus the drain-claim fix. `_run_analysis` is
monkeypatched throughout (no Gemini, no network): these tests assert the
ORCHESTRATION contract, which the 2026-07-06 audit found to be the risky part:

- paper IDs cross threads, ORM objects don't (each worker gets its own session)
- every pending paper is analysed exactly once (claim dedupe)
- one poison paper no longer aborts the batch (per-worker isolation)
- repeatedly-failing papers get blacklisted out of the selection
- a missing API key fails fast without faking progress
- workers=1 keeps the serial legacy semantics (the runtime rollback switch)
"""

from __future__ import annotations

import threading
import time

import pytest

import services.analyze_service as a
from models import AIAnalysis, Paper


@pytest.fixture(autouse=True)
def _clean_analysis_state():
    """The in-flight set, lazy queue and failure counter are process-global —
    reset them around every test so tests can't leak claims into each other."""
    def _clear():
        with a._INFLIGHT_LOCK:
            a._INFLIGHT_ANALYSIS.clear()
        with a._ANALYSIS_QUEUE_LOCK:
            a._ANALYSIS_QUEUE.clear()
        with a._ANALYSIS_FAILURES_LOCK:
            a._ANALYSIS_FAILURES.clear()

    _clear()
    yield
    _clear()


def _install_fake_analysis(monkeypatch, record, *, fail_ids=frozenset(), delay=0.02):
    """Replace `_run_analysis` with a stub that mimics its observable contract:
    stores an AIAnalysis row via the session it was HANDED and commits. Records
    (paper_id, thread name, session object) per call for the assertions."""
    record_lock = threading.Lock()

    def fake_run_analysis(paper, db_session, *, pdf_path, force_metadata_only,
                          progress_callback=None, position=None, total=None):
        time.sleep(delay)  # hold the slot long enough that the pool overlaps
        with record_lock:
            record.append({
                "paper_id": paper.id,
                "thread": threading.current_thread().name,
                "session": db_session,
            })
        if paper.id in fail_ids:
            raise RuntimeError(f"poison paper {paper.id}")
        db_session.add(AIAnalysis(paper_id=paper.id, payload={}))
        db_session.commit()
        return {}

    monkeypatch.setattr(a, "_run_analysis", fake_run_analysis)
    monkeypatch.setattr(a, "_get_vertex_client", lambda: object())


@pytest.fixture()
def analysis_db(isolated_db):
    """isolated_db swaps the engine/sessionmaker but creates no schema —
    run the app migration against the temp DB (same pattern as
    test_ingest_embedding_lazy)."""
    import main

    main.init_db()
    return isolated_db


def _make_papers(make_paper, n, prefix="par"):
    return [make_paper(f"{prefix}-{i:03d}") for i in range(n)]


def _analysis_count(session_factory):
    session = session_factory()
    try:
        return session.query(AIAnalysis).count()
    finally:
        session.close()


def test_parallel_analyzes_every_pending_paper_exactly_once(analysis_db, make_paper, monkeypatch):
    import db as db_module

    ids = _make_papers(make_paper, 10)
    record = []
    _install_fake_analysis(monkeypatch, record)

    dispatcher = db_module.SessionLocal()
    try:
        # session_factory deliberately omitted: the default must late-bind to
        # the monkeypatched db.SessionLocal (conftest isolated_db), NOT the
        # real database — this test would fail loudly if that regressed.
        result = a.analyze_pending_papers(dispatcher, workers=4)
    finally:
        dispatcher.close()

    assert result == {"analyzed": 10}
    assert sorted(r["paper_id"] for r in record) == sorted(ids)  # each exactly once
    assert _analysis_count(db_module.SessionLocal) == 10
    # The pool actually fanned out...
    assert len({r["thread"] for r in record}) > 1
    # ...and no worker ever received the dispatcher's session.
    assert all(r["session"] is not dispatcher for r in record)


def test_parallel_isolates_poison_papers(analysis_db, make_paper, monkeypatch):
    import db as db_module

    ids = _make_papers(make_paper, 6)
    poison = ids[2]
    record = []
    _install_fake_analysis(monkeypatch, record, fail_ids={poison})

    dispatcher = db_module.SessionLocal()
    try:
        result = a.analyze_pending_papers(dispatcher, workers=3)
    finally:
        dispatcher.close()

    # The poison paper failed but everything else was analysed — the serial
    # path would have aborted the batch at the poison instead.
    assert result == {"analyzed": 5}
    assert _analysis_count(db_module.SessionLocal) == 5
    with a._ANALYSIS_FAILURES_LOCK:
        assert a._ANALYSIS_FAILURES.get(poison) == 1
    # The poison's claim was released (it can be retried next pass).
    with a._INFLIGHT_LOCK:
        assert poison not in a._INFLIGHT_ANALYSIS


def test_parallel_skips_papers_claimed_elsewhere(analysis_db, make_paper, monkeypatch):
    import db as db_module

    ids = _make_papers(make_paper, 4)
    claimed = ids[0]
    record = []
    _install_fake_analysis(monkeypatch, record)

    assert a._claim_analysis(claimed)  # simulate the open-trigger holding it
    dispatcher = db_module.SessionLocal()
    try:
        result = a.analyze_pending_papers(dispatcher, workers=2)
    finally:
        dispatcher.close()
        a._release_analysis(claimed)

    assert result == {"analyzed": 3}
    assert claimed not in {r["paper_id"] for r in record}


def test_parallel_blacklist_excludes_repeat_failers_and_overfetches(analysis_db, make_paper, monkeypatch):
    import db as db_module

    ids = _make_papers(make_paper, 3)
    blacklisted = ids[1]
    with a._ANALYSIS_FAILURES_LOCK:
        a._ANALYSIS_FAILURES[blacklisted] = a._ANALYSIS_FAILURE_LIMIT
    record = []
    _install_fake_analysis(monkeypatch, record)

    dispatcher = db_module.SessionLocal()
    try:
        # limit=2 with 1 blacklisted of 3 pending: the over-fetch must still
        # fill the batch with the 2 healthy papers.
        result = a.analyze_pending_papers(dispatcher, limit=2, workers=2)
    finally:
        dispatcher.close()

    assert result == {"analyzed": 2}
    assert blacklisted not in {r["paper_id"] for r in record}


def test_parallel_missing_key_fails_fast_without_progress(analysis_db, make_paper, monkeypatch):
    import db as db_module

    _make_papers(make_paper, 3)
    record = []
    _install_fake_analysis(monkeypatch, record)

    def raise_missing_key():
        raise RuntimeError("Missing VERTEX_AI_API_KEY in your local .env configuration.")

    monkeypatch.setattr(a, "_get_vertex_client", raise_missing_key)

    dispatcher = db_module.SessionLocal()
    try:
        # Same observable contract as the serial path (first paper raises →
        # propagates): the eager loop catches it, sees zero progress, backs off.
        with pytest.raises(RuntimeError, match="VERTEX_AI_API_KEY"):
            a.analyze_pending_papers(dispatcher, workers=3)
    finally:
        dispatcher.close()

    assert record == []  # no worker ever ran
    assert _analysis_count(db_module.SessionLocal) == 0
    with a._INFLIGHT_LOCK:
        assert not a._INFLIGHT_ANALYSIS  # nothing left claimed


def test_workers_one_keeps_serial_semantics(analysis_db, make_paper, monkeypatch):
    import db as db_module

    ids = _make_papers(make_paper, 4)
    poison = ids[1]  # newest-first order: par-003, par-002, POISON par-001, par-000
    record = []
    _install_fake_analysis(monkeypatch, record, fail_ids={poison}, delay=0)

    dispatcher = db_module.SessionLocal()
    try:
        # Serial legacy contract (the SEISMO_EAGER_ANALYSIS_WORKERS=1 rollback
        # path): the first raiser propagates and aborts the rest of the batch.
        with pytest.raises(RuntimeError, match="poison"):
            a.analyze_pending_papers(dispatcher, workers=1)
    finally:
        dispatcher.rollback()
        dispatcher.close()

    assert len({r["thread"] for r in record}) == 1  # no pool
    seen = [r["paper_id"] for r in record]
    assert seen[-1] == poison  # aborted at the poison
    assert set(seen) < set(ids)  # papers behind it never ran


def test_drain_queue_respects_inflight_claim(analysis_db, make_paper, monkeypatch):
    import db as db_module

    first, second = _make_papers(make_paper, 2, prefix="drain")
    record = []
    _install_fake_analysis(monkeypatch, record, delay=0)

    a.enqueue_analysis(first)
    a.enqueue_analysis(second)
    assert a._claim_analysis(first)  # e.g. a parallel eager worker holds it

    session = db_module.SessionLocal()
    try:
        stats = a.drain_analysis_queue(session)
    finally:
        session.close()
        a._release_analysis(first)

    assert stats["analyzed"] == 1
    assert stats["skipped"] == 1
    assert {r["paper_id"] for r in record} == {second}
    with a._INFLIGHT_LOCK:
        assert second not in a._INFLIGHT_ANALYSIS  # drain released its claim
