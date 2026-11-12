"""D1 regression (计划_业务合约统一_2026-06-15.md §1-D1).

Before the 2026-06-15 fix, ``create_project`` ignored ``body['kind']`` and
derived the taxonomy solely from ``LEGACY_PROJECT_TYPE_MAP[type]``. The
New-Collection wizard sends ``type="monthly"`` + ``kind="collection"``, and
``monthly -> ("tracker", ...)``, so an *empty* collection silently landed as a
tracker. The fix honours an explicit ``kind="collection"`` (when type is
monthly/static) — see main.py create_project.

These go through the REAL ``/api/projects`` POST handler (not an ORM insert) so
they actually exercise the handler contract.
"""


def test_create_project_empty_collection_yields_collection(client):
    """type=monthly + kind=collection must produce a COLLECTION, not a tracker."""
    resp = client.post(
        "/api/projects",
        json={"name": "D1 empty collection", "type": "monthly", "kind": "collection"},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body.get("kind") == "collection", (
        f"empty collection landed as kind={body.get('kind')!r} (expected 'collection'): {body}"
    )


def test_create_project_static_collection_still_collection(client):
    """The import path (type=static + kind=collection) stays a collection."""
    resp = client.post(
        "/api/projects",
        json={"name": "D1 static collection", "type": "static", "kind": "collection"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json().get("kind") == "collection", resp.text


def test_create_project_monthly_without_kind_stays_tracker(client):
    """Converse guard: a plain monthly project (no kind) must remain a tracker.

    Ensures the D1 fix only fires on an explicit kind='collection' and does not
    regress the default monthly->tracker reclassification (db.py LEGACY_PROJECT_TYPE_MAP).
    """
    resp = client.post(
        "/api/projects",
        json={"name": "D1 monthly tracker", "type": "monthly"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json().get("kind") == "tracker", resp.text
