"""Opt-in live smoke test against Nebius Token Factory. Costs well under US$0.02.

    python -m pytest -m live
"""
import pytest

from conftest import FIXTURE, collect
from downshift import models
from downshift.engine import run_downshift
from downshift.models import LLM, Meter, TIERS, api_key
from downshift.tasks import load_task_dir

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _real_pricing(monkeypatch):
    monkeypatch.setattr(models, "_pricing_mem", None)
    if not api_key():
        pytest.skip("NEBIUS_API_KEY not set")


def test_live_pricing_and_one_call():
    p = models.get_pricing(force=True)
    assert p["_source"].startswith("live")
    assert all(t["model"] in p["models"] for t in TIERS)
    import asyncio
    llm = LLM(run_id="live-smoke", meter=Meter(cap_usd=0.01))
    r = asyncio.run(llm.chat(TIERS[0]["model"], [{"role": "user", "content": "Reply with the word ok."}], max_tokens=8))
    assert r.ok and "ok" in r.text.lower() and r.cost_usd > 0 and "<think>" not in r.text


def test_live_engine_tiny():
    task = load_task_dir(FIXTURE)
    events = collect(run_downshift(task, options={"rounds": 1, "train_limit": 6, "holdout_limit": 6,
                                                  "spend_cap_usd": 0.03}))
    assert events[-1]["type"] == "done"
    assert any(e["type"] in ("verdict", "aborted") for e in events)
