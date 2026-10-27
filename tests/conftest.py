import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from downshift import models  # noqa: E402
from downshift.grading import GradeResult, Grader  # noqa: E402
from downshift.models import FALLBACK_PRICING, CallResult, Meter, call_cost  # noqa: E402
from downshift.tasks import load_task_dir  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ticket_triage"

GOOD_CHECKER = '''
def check(output, reference, input):
    ok = str(output).strip().lower() == str(reference).strip().lower()
    return (1.0 if ok else 0.0), "match" if ok else "mismatch"
'''
BAD_CHECKER = '''
def check(output, reference, input):
    return 1.0, "always yes"
'''


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """No network, no writes to the real runs/ folder, fixed pricing."""
    monkeypatch.setattr(models, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(models, "USAGE_LOG", tmp_path / "usage.jsonl")
    monkeypatch.setattr(models, "_pricing_mem", {"models": dict(FALLBACK_PRICING), "_source": "test",
                                                 "_fetched": 9e18})
    yield


def inproc_grade(checker_code, items, timeout_s=120):
    ns = {}
    exec(checker_code, ns)
    out = []
    for it in items:
        s, d = ns["check"](it["output"], it["reference"], it["input"])
        out.append(GradeResult(it["id"], float(s), s >= 0.999, d, None))
    return out


@pytest.fixture
def grader():
    return Grader(sandbox_fn=inproc_grade, local_fn=inproc_grade)


@pytest.fixture
def task():
    return load_task_dir(FIXTURE)


class FakeLLM:
    """Deterministic stand-in for models.LLM.

    skill[tier] -> fraction of answers that are right with the plain prompt; any system prompt containing
    'QUEUE RULES' makes tiers listed in `rules_fix` fully correct.
    """

    def __init__(self, task, skill=None, rules_fix=("lightning", "nano", "super"), checkers=None, cap=10.0,
                 proposal_system="QUEUE RULES: billing=charges, shipping=delivery, technical=bugs, "
                                 "account=profile, refund=money back."):
        self.meter = Meter(cap_usd=cap)
        self.answers = {}
        for r in task["train"] + task["holdout"]:
            self.answers[r["input"]] = r["reference"]
        self.skill = skill or {"lightning": 0.5, "nano": 0.6, "super": 0.9, "ultra": 1.0}
        self.rules_fix = set(rules_fix)
        self.checkers = list(checkers or [GOOD_CHECKER])
        self.proposal_system = proposal_system
        self.calls = []

    async def chat(self, model, messages, max_tokens=512, temperature=0.0, purpose="", thinking=False):
        self.meter.check()
        await asyncio.sleep(0)
        self.calls.append((model, purpose, thinking))
        tier = models.tier_by_model(model)["key"]
        rung_key = tier + ("-think" if thinking else "")
        if purpose == "write_checker":
            code = self.checkers.pop(0) if len(self.checkers) > 1 else self.checkers[0]
            text = f"```python\n{code}\n```"
        elif purpose == "propose_prompts":
            text = json.dumps({"candidates": [
                {"system": self.proposal_system, "fewshot_ids": ["t1", "t2"], "note": "rules + examples"},
                {"system": "Route it. Be careful.", "fewshot_ids": [], "note": "shorter"},
            ]})
        else:
            system = messages[0]["content"]
            user = messages[-1]["content"]
            right = self.answers.get(user, "billing")
            ok = tier in self.rules_fix and "QUEUE RULES" in system
            if not ok:
                # deterministic: item is right if its hash bucket falls under the skill level
                bucket = (sum(map(ord, user)) % 100) / 100
                ok = bucket < self.skill.get(rung_key, self.skill[tier])
            text = right if ok else ("shipping" if right != "shipping" else "billing")
        pt = sum(len(m["content"]) for m in messages) // 4
        ct = max(1, len(text) // 4)
        cost = call_cost(model, pt, ct)
        self.meter.add(model, cost)
        lat = {"lightning": 300, "nano": 350, "super": 600, "ultra": 900}.get(tier, 500)
        return CallResult(model, text, pt, ct, cost, float(lat))


def collect(agen):
    async def go():
        return [e async for e in agen]
    return asyncio.run(go())
