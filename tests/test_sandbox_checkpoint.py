"""Sealed answer-key checkpoints + branching (goodenough.sandbox_grader.grade_batch_sealed_with_meta).

Offline tests replace the HTTP client with FakeSandboxes, which really runs the seal script and the sealed
harness in a local subprocess and snapshots "images" as file dicts, so the code under test is the code that
ships. The live test runs only with DOWNSHIFT_LIVE=1 (a handful of tiny Sandboxes operations):
    DOWNSHIFT_LIVE=1 python -m pytest -q -s tests/test_sandbox_checkpoint.py
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from goodenough import sandbox_grader as sg
from goodenough.grading import Grader

CHECKER = '''
def check(output, reference, input):
    ok = output.strip() == reference.strip()
    return (1.0 if ok else 0.0), ("match" if ok else "mismatch for " + input)
'''
BASE = "00000000-0000-4000-8000-00000000base"


def _items(outputs=("SECRET-REF-a", "wrong", "SECRET-REF-c")):
    return [{"id": f"r{i}", "input": f"q{i}", "output": o, "reference": f"SECRET-REF-{'abc'[i]}"}
            for i, o in enumerate(outputs)]


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._data, self.text = code, data, json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sg.httpx.HTTPStatusError("err", request=None, response=None)


class FakeSandboxes:
    """Minimal stand-in for the Sandboxes REST API: POST instances runs the command synchronously."""

    def __init__(self, seal_dir: Path):
        self.seal_dir = seal_dir
        self.images: dict[str, dict[str, bytes]] = {BASE: {}}
        self.ops: dict[str, dict] = {}
        self.bodies: list[dict] = []
        self.fail_retained = False
        self.lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, path, params=None):
        if path == "images":
            return _Resp(200, {"images": [{"tag": sg.IMAGE_TAG, "uuid": BASE}]})
        return _Resp(200, self.ops[path.split("/", 1)[1]])

    def delete(self, path):
        return _Resp(200, {})

    def post(self, path, json=None):
        assert path == "instances"
        with self.lock:  # one shared seal dir stands in for each VM's filesystem
            return self._run(json)

    def _run(self, body):
        self.bodies.append(body)
        op_id = str(uuid.uuid4())
        op = {"uuid": op_id, "status": "SUCCESS", "error": None, "image_uuid": body["image"],
              "result_image_uuid": None, "metadata": {"result": {}}}
        self.ops[op_id] = op
        files = self.images.get(body["image"])
        if files is None or (self.fail_retained and not body["disposable"]):
            op.update(status="FAILED", error="image not found" if files is None else "injected failure")
            return _Resp(201, {"uuid": op_id})
        shutil.rmtree(self.seal_dir, ignore_errors=True)
        self.seal_dir.mkdir(parents=True)
        for name, data in files.items():
            (self.seal_dir / name).write_bytes(data)
        stdin = base64.b64decode(body["stdin"]["value"])
        cmd = body["command"].removeprefix("exec python3 -I ").strip()
        argv = [sys.executable, "-I", "-"] if cmd == "-" else [sys.executable, "-I", cmd]
        p = subprocess.run(argv, input=stdin, capture_output=True, timeout=120)
        op["metadata"]["result"] = {
            "stdout": {"value": base64.b64encode(p.stdout).decode(), "encoding": "base64"},
            "stderr": {"value": base64.b64encode(p.stderr).decode(), "encoding": "base64"},
            "state": {"exit_code": p.returncode}, "resources": {"elapsed_time": 0.1}}
        if not body["disposable"]:
            new = str(uuid.uuid4())
            self.images[new] = {f.name: f.read_bytes() for f in self.seal_dir.iterdir()}
            op["result_image_uuid"] = new
        return _Resp(201, {"uuid": op_id})

    def retained(self):
        return [b for b in self.bodies if not b["disposable"]]

    def branches(self):
        return [b for b in self.bodies if b["disposable"] and b["image"] != BASE]

    def one_shots(self):
        return [b for b in self.bodies if b["disposable"] and b["image"] == BASE]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fs = FakeSandboxes(tmp_path / "vm" / "downshift")
    monkeypatch.setattr(sg, "SEAL_DIR", (tmp_path / "vm" / "downshift").as_posix() + "/")
    monkeypatch.setattr(sg, "_http", lambda timeout=20.0: fs)
    monkeypatch.setattr(sg, "_IMAGE_UUID", None)
    monkeypatch.setattr(sg, "_SEALS", {})
    monkeypatch.setattr(sg, "_SEAL_FAILED", {})
    monkeypatch.setattr(sg, "_SEAL_LOCKS", {})
    monkeypatch.delenv("DOWNSHIFT_NO_CHECKPOINT", raising=False)
    monkeypatch.delenv("DOWNSHIFT_FORCE_LOCAL_GRADER", raising=False)
    return fs


def _stdin(body) -> str:
    return base64.b64decode(body["stdin"]["value"]).decode("utf-8")


def _scores(res):
    return [(r.item_id, r.score, r.passed) for r in res]


EXPECTED = [("r0", 1.0, True), ("r1", 0.0, False), ("r2", 1.0, True)]


def test_seal_once_branch_many(fake):
    res1, m1 = sg.grade_batch_sealed_with_meta(CHECKER, _items())
    res2, m2 = sg.grade_batch_sealed_with_meta(CHECKER, _items(("x", "SECRET-REF-b", "y")))
    assert _scores(res1) == EXPECTED
    assert _scores(res2) == [("r0", 0.0, False), ("r1", 1.0, True), ("r2", 0.0, False)]
    assert "mismatch for q0" in res2[0].detail  # input came from the sealed refs file
    assert len(fake.retained()) == 1 and len(fake.branches()) == 2 and not fake.one_shots()
    assert m1.branched and m2.branched and m1.sealed_now and not m2.sealed_now
    assert m1.checkpoint == m2.checkpoint == fake.ops[m1.seal_operation_id]["result_image_uuid"]
    assert m1.seal_operation_id == m2.seal_operation_id and m1.operation_id != m2.operation_id
    assert m1.label == f"Nebius Sandboxes · branch of sealed answer key {m1.checkpoint[:8]}"
    assert m1.key_sha256 == m2.key_sha256 and len(m1.key_sha256) == 64
    assert m1.fallback_reason is None
    # Hidden references travel once (in the seal); branches carry only ids + outputs.
    assert "SECRET-REF-b" not in _stdin(fake.branches()[0])
    assert all(b["networking"] == {"enabled": False} for b in fake.bodies)


def test_subset_reuses_seal_changed_reference_reseals(fake):
    sg.grade_batch_sealed_with_meta(CHECKER, _items())
    _, sub = sg.grade_batch_sealed_with_meta(CHECKER, _items()[:2])
    assert sub.branched and not sub.sealed_now and len(fake.retained()) == 1
    changed = _items()
    changed[0]["reference"] = "other"
    res, m = sg.grade_batch_sealed_with_meta(CHECKER, changed)
    assert m.sealed_now and m.checkpoint != sub.checkpoint and len(fake.retained()) == 2
    assert res[0].score == 0.0
    _, other_code = sg.grade_batch_sealed_with_meta(CHECKER + "\n# v2\n", _items())
    assert other_code.sealed_now and len(fake.retained()) == 3


def test_seal_failure_falls_back_to_one_shot_and_gives_up(fake):
    fake.fail_retained = True
    for _ in range(3):
        res, m = sg.grade_batch_sealed_with_meta(CHECKER, _items())
        assert _scores(res) == EXPECTED
        assert not m.branched and m.checkpoint is None and m.fallback_reason.startswith("seal failed")
        assert m.label == "Nebius Sandboxes · one-shot"
    assert len(fake.retained()) == 2 and len(fake.one_shots()) == 3  # no third seal attempt


def test_expired_checkpoint_falls_back_then_reseals(fake):
    _, m1 = sg.grade_batch_sealed_with_meta(CHECKER, _items())
    del fake.images[m1.checkpoint]          # e.g. retention expiry
    res, m2 = sg.grade_batch_sealed_with_meta(CHECKER, _items())
    assert _scores(res) == EXPECTED and not m2.branched
    assert m2.fallback_reason.startswith(f"branch of checkpoint {m1.checkpoint[:8]} failed")
    _, m3 = sg.grade_batch_sealed_with_meta(CHECKER, _items())
    assert m3.branched and m3.sealed_now and m3.checkpoint != m1.checkpoint


def test_tampered_key_is_detected(fake):
    _, m1 = sg.grade_batch_sealed_with_meta(CHECKER, _items())
    fake.images[m1.checkpoint]["refs.json"] = json.dumps(
        {f"r{i}": {"reference": "x", "input": "q"} for i in range(3)}).encode()
    res, m2 = sg.grade_batch_sealed_with_meta(CHECKER, _items())
    assert not m2.branched and "hash mismatch" in m2.fallback_reason
    assert _scores(res) == EXPECTED  # graded by the one-shot path against the real references


def test_duplicate_ids_and_opt_out_use_one_shot(fake, monkeypatch):
    dup = _items() + [_items()[0]]
    _, m = sg.grade_batch_sealed_with_meta(CHECKER, dup)
    assert not m.branched and "duplicate" in m.fallback_reason
    monkeypatch.setenv("DOWNSHIFT_NO_CHECKPOINT", "1")
    _, m = sg.grade_batch_sealed_with_meta(CHECKER, _items())
    assert not m.branched and "disabled" in m.fallback_reason
    assert not fake.retained()


def test_concurrent_callers_seal_once(fake):
    out = []
    ts = [threading.Thread(target=lambda: out.append(sg.grade_batch_sealed_with_meta(CHECKER, _items())))
          for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(fake.retained()) == 1 and len({m.checkpoint for _, m in out}) == 1
    assert sum(m.sealed_now for _, m in out) == 1


def test_grader_records_checkpoint_fields(fake):
    g = Grader()
    res, batch = asyncio.run(g.grade(CHECKER, _items()))
    assert [r.score for r in res] == [1.0, 0.0, 1.0]
    assert batch["backend"] == "sandbox" and batch["branched"] is True
    assert batch["checkpoint"] and batch["seal_operation_id"] and batch["operation_id"]
    assert batch["label"].startswith("Nebius Sandboxes · branch of sealed answer key ")
    fake.fail_retained = True
    _, b2 = asyncio.run(g.grade(CHECKER, [dict(_items()[0], reference="new")]))
    assert b2["branched"] is False and b2["checkpoint"] is None and b2["fallback_reason"].startswith("seal failed")


live = pytest.mark.skipif(os.environ.get("DOWNSHIFT_LIVE") != "1", reason="set DOWNSHIFT_LIVE=1 for live Sandboxes")


@live
def test_live_seal_and_branch(monkeypatch):
    monkeypatch.setattr(sg, "_SEALS", {})
    monkeypatch.setattr(sg, "_SEAL_FAILED", {})
    t = time.monotonic()
    res1, m1 = sg.grade_batch_sealed_with_meta(CHECKER, _items(), timeout_s=30)
    first = time.monotonic() - t
    t = time.monotonic()
    res2, m2 = sg.grade_batch_sealed_with_meta(CHECKER, _items(("x", "SECRET-REF-b", "y")), timeout_s=30)
    second = time.monotonic() - t
    t = time.monotonic()
    res3, m3 = sg.grade_batch_with_meta(CHECKER, _items(), timeout_s=30)
    one_shot = time.monotonic() - t
    assert _scores(res1) == _scores(res3) == EXPECTED
    assert [r.passed for r in res2] == [False, True, False]
    assert m1.branched and m2.branched and m1.sealed_now and not m2.sealed_now and m1.checkpoint == m2.checkpoint
    assert m1.key_sha256 == m2.key_sha256
    print(json.dumps({"checkpoint": m1.checkpoint, "seal_operation_id": m1.seal_operation_id,
                      "seal_s": m1.seal_s, "branch1_operation_id": m1.operation_id,
                      "branch1_s": m1.extra["branch_wall_s"], "first_call_s": round(first, 3),
                      "branch2_operation_id": m2.operation_id, "branch2_s": round(second, 3),
                      "one_shot_operation_id": m3.operation_id, "one_shot_s": round(one_shot, 3),
                      "key_sha256": m1.key_sha256, "label": m1.label}, indent=1))
