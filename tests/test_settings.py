"""Settings / BYOK endpoint tests (2026-06-16).

GET reports masked key status (never the full key); POST upserts the key into
.env + os.environ. The write-path test monkeypatches main.BASE_DIR to a tmp dir
so it NEVER touches the developer's real .env.
"""
from __future__ import annotations

import os


def test_get_settings_reports_masked_gemini_key(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyABCDEFGHIJKLMNOP")
    monkeypatch.delenv("VERTEX_AI_API_KEY", raising=False)
    d = client.get("/api/settings").json()
    assert d["gemini_api_key_set"] is True
    assert d["key_source"] == "GEMINI_API_KEY"
    assert d["gemini_api_key_masked"].endswith("MNOP")
    assert "AIzaSyABCDEF" not in d["gemini_api_key_masked"]  # never the full key


def test_get_settings_falls_back_to_vertex(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("VERTEX_AI_API_KEY", "AQ.SomeVertexKey123")
    d = client.get("/api/settings").json()
    assert d["gemini_api_key_set"] is True
    assert d["key_source"] == "VERTEX_AI_API_KEY"


def test_get_settings_no_key(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("VERTEX_AI_API_KEY", raising=False)
    d = client.get("/api/settings").json()
    assert d["gemini_api_key_set"] is False
    assert d["gemini_api_key_masked"] is None


def test_post_settings_validation(client, monkeypatch):
    monkeypatch.delenv("VERTEX_AI_API_KEY", raising=False)
    assert client.post("/api/settings", json={"vertex_api_key": ""}).status_code == 400
    assert client.post("/api/settings", json={"vertex_api_key": "abc"}).status_code == 422
    # A var NOT in SETTINGS_FIELDS (dropped GEMINI) is ignored → 400 (nothing to do).
    assert client.post("/api/settings", json={"gemini_api_key": "AIzaSyABCDEFGHIJ"}).status_code == 400


def test_post_settings_upserts_env_and_sets_environ(client, monkeypatch, tmp_path):
    import main
    env = tmp_path / ".env"
    env.write_text("VERTEX_AI_API_KEY=existing\nFOO=bar\n", encoding="utf-8")
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)
    monkeypatch.delenv("GOOGLE_CSE_CX", raising=False)

    # POST by direct env name (a retained field).
    r = client.post("/api/settings", json={"GOOGLE_CSE_CX": "cse-cx-1234567890"})
    assert r.status_code == 200 and r.json()["ok"] is True

    txt = env.read_text(encoding="utf-8")
    assert "GOOGLE_CSE_CX=cse-cx-1234567890" in txt
    assert "VERTEX_AI_API_KEY=existing" in txt  # other lines preserved
    assert "FOO=bar" in txt
    assert os.environ.get("GOOGLE_CSE_CX") == "cse-cx-1234567890"  # live, no restart

    # Re-save replaces the line in place — no duplicate.
    client.post("/api/settings", json={"GOOGLE_CSE_CX": "cse-cx-0987654321"})
    txt2 = env.read_text(encoding="utf-8")
    assert txt2.count("GOOGLE_CSE_CX=") == 1
    assert "cse-cx-0987654321" in txt2


def test_post_settings_vertex_three_fields(client, monkeypatch, tmp_path):
    import main
    env = tmp_path / ".env"
    env.write_text("FOO=bar\n", encoding="utf-8")
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)
    for k in ("VERTEX_AI_API_KEY", "VERTEX_AI_PROJECT", "VERTEX_AI_LOCATION", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    r = client.post("/api/settings", json={
        "vertex_api_key": "AQ.VertexKey1234567890",
        "vertex_project": "my-gcp-project",
        "vertex_location": "us-central1",
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert set(r.json()["updated"]) == {"VERTEX_AI_API_KEY", "VERTEX_AI_PROJECT", "VERTEX_AI_LOCATION"}

    txt = env.read_text(encoding="utf-8")
    assert "VERTEX_AI_API_KEY=AQ.VertexKey1234567890" in txt
    assert "VERTEX_AI_PROJECT=my-gcp-project" in txt
    assert "VERTEX_AI_LOCATION=us-central1" in txt
    assert "FOO=bar" in txt  # untouched
    assert os.environ.get("VERTEX_AI_PROJECT") == "my-gcp-project"

    g = client.get("/api/settings").json()
    assert g["provider"] == "vertex"
    assert g["vertex"]["project"] == "my-gcp-project"
    assert g["vertex"]["location"] == "us-central1"
    assert g["vertex"]["api_key_masked"].endswith("7890")
    assert "VertexKey" not in (g["vertex"]["api_key_masked"] or "")  # never full key


def test_post_settings_partial_update_and_empty_ignored(client, monkeypatch, tmp_path):
    import main
    env = tmp_path / ".env"
    env.write_text("VERTEX_AI_API_KEY=oldkey123456\nVERTEX_AI_PROJECT=oldproj\n", encoding="utf-8")
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)
    monkeypatch.delenv("VERTEX_AI_PROJECT", raising=False)

    # Only update project — key/location lines must be left alone.
    r = client.post("/api/settings", json={"vertex_project": "newproj"})
    assert r.status_code == 200
    txt = env.read_text(encoding="utf-8")
    assert "VERTEX_AI_API_KEY=oldkey123456" in txt
    assert "VERTEX_AI_PROJECT=newproj" in txt
    assert "oldproj" not in txt

    # All-empty payload → 400 (nothing to update).
    assert client.post("/api/settings", json={"vertex_project": "", "vertex_location": ""}).status_code == 400
