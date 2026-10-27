import json

import pytest
from fastapi.testclient import TestClient

import app as server
from conftest import FakeLLM, inproc_grade
from downshift import models
from downshift.grading import Grader
from downshift.tasks import load_task_dir
from conftest import FIXTURE


@pytest.fixture
def client(monkeypatch):
    task = load_task_dir(FIXTURE)
    monkeypatch.setattr(server.app.state, "llm_factory", lambda run_id, cap: FakeLLM(task, cap=cap))
    monkeypatch.setattr(server, "backend_info", lambda: {"backend": "sandbox", "label": "Nebius Sandboxes"})
    monkeypatch.setattr(server.app.state, "grader_factory", lambda: Grader(sandbox_fn=inproc_grade, local_fn=inproc_grade))
    with TestClient(server.app) as c:
        yield c


def _events(client, run_id):
    out = []
    with client.stream("GET", f"/api/run/{run_id}/events") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: ") and line != "data: {}":
                out.append(json.loads(line[6:]))
    return out


def test_health_and_presets(client):
    h = client.get("/api/health").json()
    assert h["ok"] and "key_present" in h and "grader" in h
    assert "NEBIUS_API_KEY" not in json.dumps(h)
    ps = client.get("/api/presets").json()
    assert ps and all("slug" in p for p in ps)
    assert client.get("/").status_code == 200


def test_run_streams_and_is_reopenable(client, monkeypatch):
    monkeypatch.setattr(server, "load_preset", lambda slug: load_task_dir(FIXTURE))
    r = client.post("/api/run", json={"preset": "ticket_triage", "options": {"spend_cap_usd": 5}})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    events = _events(client, run_id)
    types = [e["type"] for e in events]
    assert types[0] == "started" and types[-1] == "done" and "verdict" in types
    assert events[0]["options"]["spend_cap_usd"] <= server.MAX_RUN_CAP_USD  # server clamps the cap
    # Reopen from disk after the in-memory record is gone.
    server.RUNS.pop(run_id)
    saved = client.get(f"/api/run/{run_id}").json()
    assert saved["verdict"]["kind"] == "downshift"
    assert [e["type"] for e in _events(client, run_id)] == types
    assert (models.RUNS_DIR / f"{run_id}.json").exists()


def test_bad_requests(client):
    assert client.post("/api/run", json={}).status_code == 400
    assert client.post("/api/run", json={"preset": "nope"}).status_code == 404
    assert client.post("/api/run", json={"custom": {"instruction": "x", "examples": []}}).status_code == 400
    assert client.get("/api/run/zzz").status_code == 404
    assert client.post("/api/run/abc123/finetune", json={"confirm": True}).status_code == 403
