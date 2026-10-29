"""The escalation chain (two cheap rungs, Ultra only when they disagree) and the answer-key audit."""
from conftest import FakeLLM, collect
from goodenough.engine import run_good_enough


def _by_type(events, t):
    return [e for e in events if e["type"] == t]


def test_chain_runs_when_no_cheap_rung_matches_and_adds_no_model_calls(task, grader):
    llm = FakeLLM(task, skill={"lightning": 0.4, "nano": 0.5, "super": 0.3, "ultra": 1.0}, rules_fix=())
    events = collect(run_good_enough(task, options={"thinking_rungs": False}, llm=llm, grader=grader))
    v = _by_type(events, "verdict")[0]["verdict"]
    assert v["rung"] not in ("lightning", "nano", "super")
    c = _by_type(events, "chain")[0]
    assert c["agreed"] + c["escalated"] == c["n"] == len(task["holdout"])
    assert set(c["rungs"]) <= {"lightning", "nano", "super"} and len(set(c["rungs"])) == 2
    # Cost = both cheap rungs on every row + the fallback on the escalated share.
    assert c["cost_per_1k_usd"] > 0 and abs(c["escalate_rate"] - c["escalated"] / c["n"]) < 1e-3
    # Every input to the chain was measured on held-out rows already: the chain stage itself calls no model.
    i = next(k for k, e in enumerate(events) if e["type"] == "stage" and e.get("stage") == "chain")
    j = next(k for k, e in enumerate(events) if e["type"] == "chain")
    assert not [e for e in events[i:j] if e["type"] == "eval"]
    assert "def run" in c["snippet"]


def test_chain_is_skipped_when_a_cheap_rung_matches(task, grader):
    events = collect(run_good_enough(task, llm=FakeLLM(task), grader=grader))
    assert _by_type(events, "verdict")[0]["verdict"]["rung"] in ("lightning", "nano", "super")
    assert not _by_type(events, "chain")


def _unanswered(task):
    t = dict(task)
    for s in ("train", "holdout"):
        t[s] = [dict(r, reference=None) for r in task[s]]
    return t


def test_audit_drops_rows_where_two_ultra_passes_disagree(task, grader):
    llm = FakeLLM(task, skill={"lightning": 0.5, "nano": 0.6, "super": 0.9, "ultra": 1.0, "ultra-think": 0.7})
    events = collect(run_good_enough(_unanswered(task), options={"reference": "live"}, llm=llm, grader=grader))
    a = _by_type(events, "audit")[0]
    assert a["n"] == len(task["train"]) + len(task["holdout"]) and 0 < a["flagged"] < a["n"]
    started_ho = _by_type(events, "started")[0]["n_holdout"]
    assert a["left"]["holdout"] < started_ho
    ho_eval = [e for e in _by_type(events, "eval") if e["split"] == "holdout"][0]
    assert ho_eval["n"] == a["left"]["holdout"]
    assert not set(ho_eval["row_scores"]) & set(a["ids"])


def test_audit_finds_nothing_when_both_passes_agree(task, grader):
    events = collect(run_good_enough(_unanswered(task), options={"reference": "live"}, llm=FakeLLM(task), grader=grader))
    a = _by_type(events, "audit")[0]
    assert a["flagged"] == 0 and a["left"]["holdout"] == len(task["holdout"])
