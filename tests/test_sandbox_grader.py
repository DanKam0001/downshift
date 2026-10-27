"""Tests for downshift.sandbox_grader.

Offline tests use the local-subprocess fallback (same harness). The live Nebius Sandboxes test runs
only when DOWNSHIFT_LIVE=1 is set:  DOWNSHIFT_LIVE=1 python -m pytest tests/test_sandbox_grader.py
"""
import json
import os

import pytest

from downshift import sandbox_grader as sg

CHECKER = '''
import json
def check(output, reference, input):
    if output == "RAISE":
        raise ValueError("deliberate")
    if output == "LOOP":
        while True:
            pass
    if output == "EXIT":
        import os; os._exit(3)
    if output == "BADTYPE":
        return "nope", "x"
    if output == "BIG":
        return 7, "r" * 1000
    return (1.0 if output.strip() == reference.strip() else 0.0), ("match" if output.strip() == reference.strip() else "mismatch")
'''


def _items():
    items = [{"id": f"i{i}", "input": "q", "output": str(i % 2), "reference": "1"} for i in range(6)]
    items += [{"id": x.lower(), "input": "", "output": x, "reference": ""} for x in ("RAISE", "LOOP", "EXIT", "BADTYPE", "BIG")]
    return items


def _assert_behaviour(res, items):
    assert [r.item_id for r in res] == [it["id"] for it in items]
    by = {r.item_id: r for r in res}
    for i in range(6):
        r = by[f"i{i}"]
        assert r.error is None
        assert r.score == (1.0 if i % 2 else 0.0)
        assert r.passed == bool(i % 2)
    assert "ValueError" in by["raise"].error and not by["raise"].passed
    assert by["loop"].error.startswith("timeout")
    assert "died" in by["exit"].error
    assert by["badtype"].error and "ValueError" in by["badtype"].error
    assert by["big"].score == 1.0 and by["big"].error is None  # clamped to 1.0
    for r in res:
        assert 0.0 <= r.score <= 1.0
        assert len(r.detail) <= sg.MAX_DETAIL


def test_local_behaviour():
    items = _items()
    res = sg.grade_batch_local(CHECKER, items, timeout_s=30)
    _assert_behaviour(res, items)
    assert sg.LAST_BATCH_META.backend.startswith("local")


def test_local_load_error_marks_every_item():
    res = sg.grade_batch_local("def check(:\n", _items()[:3], timeout_s=20)
    assert len(res) == 3 and all(r.error and "failed to load" in r.error for r in res)
    res = sg.grade_batch_local("x = 1\n", _items()[:2], timeout_s=20)
    assert all("no callable check" in r.error for r in res)


def test_empty_batch():
    assert sg.grade_batch_local(CHECKER, []) == []
    assert sg.grade_batch(CHECKER, []) == []  # no operation submitted


def test_unicode_and_missing_fields():
    items = [{"id": 1, "output": "héllo ✓", "reference": "héllo ✓"}, {"id": 2}]
    res = sg.grade_batch_local(CHECKER, items, timeout_s=20)
    assert res[0].item_id == "1" and res[0].score == 1.0
    assert res[1].score == 1.0  # None fields become "" on both sides


def test_parse_rejects_missing_marker():
    with pytest.raises(sg.SandboxError):
        sg._parse_harness_stdout("nothing here", [{"id": "a"}])


def test_payload_has_no_credentials(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "SECRET-SENTINEL-123")
    script, _ = sg._build_script(CHECKER, _items(), 60)
    assert "SECRET-SENTINEL-123" not in script
    payload = script.split('b64decode("', 1)[1].split('"', 1)[0]
    import base64
    assert "SECRET-SENTINEL-123" not in base64.b64decode(payload).decode()


live = pytest.mark.skipif(os.environ.get("DOWNSHIFT_LIVE") != "1", reason="set DOWNSHIFT_LIVE=1 for live Sandboxes")


@live
def test_live_sandbox_behaviour():
    items = _items()
    res, meta = sg.grade_batch_with_meta(CHECKER, items, timeout_s=60)
    _assert_behaviour(res, items)
    assert meta.backend == "Nebius Sandboxes" and meta.operation_id and meta.mode == "fork"


@live
def test_live_networking_disabled():
    net = '''
import os, socket
def check(output, reference, input):
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3)
        return 0.0, "CONNECTED"
    except OSError as e:
        return 1.0, "ifaces=%s %s" % (sorted(os.listdir("/sys/class/net")), e)
'''
    res = sg.grade_batch(net, [{"id": "n", "input": "", "output": "", "reference": ""}], timeout_s=30)
    assert res[0].passed, res[0].detail
    assert "['lo']" in res[0].detail


@live
def test_live_info():
    info = sg.sandbox_info()
    assert info["backend"] == "Nebius Sandboxes" and info["spawn"] is True
    assert "error" not in info
    assert json.dumps(info)  # serialisable for the UI
