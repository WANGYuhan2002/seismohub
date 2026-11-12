"""Tests for the QA-history delete endpoint (PR-10).

Cohabits with the broader QA-persistence suite from PR-08/PR-09. These
cases are scoped to ``DELETE /api/papers/{paper_id}/qa/history`` only.
"""

from __future__ import annotations

import db as db_module
from models import QAMessage


def _seed_turns(paper_id: str, n: int, *, archived: bool = False) -> None:
    """Insert ``n`` user/assistant pairs straight into qa_messages."""
    session = db_module.SessionLocal()
    try:
        # Allocate positions after any already-seeded rows for this paper.
        from sqlalchemy import func
        base = (
            session.query(func.coalesce(func.max(QAMessage.position), 0))
            .filter(QAMessage.paper_id == paper_id)
            .scalar()
            or 0
        )
        for i in range(n):
            session.add(QAMessage(
                paper_id=paper_id, role="user", text=f"q{i}",
                position=base + 2 * i + 1, archived=archived,
                citations=[],
            ))
            session.add(QAMessage(
                paper_id=paper_id, role="assistant", text=f"a{i}",
                model_id="gemini-3.1-pro-preview",
                model_label="Gemini 3.1 Pro",
                citations=[{"page": i + 1}],
                position=base + 2 * i + 2, archived=archived,
            ))
        session.commit()
    finally:
        session.close()


def test_delete_soft_archives_visible_rows(client, make_paper):
    paper_id = make_paper("p1")
    _seed_turns(paper_id, 3)               # 6 visible rows

    resp = client.delete(f"/api/papers/{paper_id}/qa/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 6
    assert body["mode"] == "archive"
    assert body["paper_id"] == paper_id

    # Rows survive in the DB, just flipped to archived=True.
    session = db_module.SessionLocal()
    try:
        total = session.query(QAMessage).filter_by(paper_id=paper_id).count()
        visible = session.query(QAMessage).filter_by(
            paper_id=paper_id, archived=False
        ).count()
        assert total == 6
        assert visible == 0
    finally:
        session.close()


def test_delete_hard_truly_deletes_rows(client, make_paper):
    paper_id = make_paper("p2")
    _seed_turns(paper_id, 2)               # 4 visible rows
    _seed_turns(paper_id, 1, archived=True)  # 2 archived; hard must reap them too

    resp = client.delete(f"/api/papers/{paper_id}/qa/history?hard=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 6            # archive + visible, all gone
    assert body["mode"] == "hard"

    session = db_module.SessionLocal()
    try:
        remaining = session.query(QAMessage).filter_by(paper_id=paper_id).count()
        assert remaining == 0
    finally:
        session.close()


def test_delete_is_idempotent_on_empty_history(client, make_paper):
    paper_id = make_paper("p3")
    # No turns seeded.
    resp = client.delete(f"/api/papers/{paper_id}/qa/history")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0

    # Soft-archiving twice in a row must still report 0 the second time
    # (already-archived rows are skipped).
    _seed_turns(paper_id, 1)
    assert client.delete(f"/api/papers/{paper_id}/qa/history").json()["deleted"] == 2
    assert client.delete(f"/api/papers/{paper_id}/qa/history").json()["deleted"] == 0


def test_delete_unknown_paper_is_404(client):
    resp = client.delete("/api/papers/does-not-exist/qa/history")
    assert resp.status_code == 404
