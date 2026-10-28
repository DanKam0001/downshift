"""The guess check: no model verdict on a checker that a fixed answer passes as well as Ultra."""
import json

from conftest import FakeLLM, collect
from downshift.engine import guess_answers, run_downshift


def _by_type(events, t):
    return [e for e in events if e["type"] == t]


def test_a_checker_that_measures_skill_passes(task, grader):
    events = collect(run_downshift(task, llm=FakeLLM(task), grader=grader))
    g = _by_type(events, "guess")[0]
    assert not g["tripped"] and g["best"]["score"] < g["ultra_score"]
    assert _by_type(events, "verdict")[0]["verdict"]["kind"] == "downshift"


def test_a_checker_a_guess_can_pass_trips_before_the_ladder(task, grader):
    t = dict(task)
    for split in ("train", "validation", "holdout"):
        t[split] = [dict(r, reference="billing") for r in task[split]]
    llm = FakeLLM(t)
    events = collect(run_downshift(t, llm=llm, grader=grader))
    g = _by_type(events, "guess")[0]
    assert g["tripped"] and g["best"]["answer"] == "billing" and g["best"]["score"] == 1.0
    v = _by_type(events, "verdict")[0]["verdict"]
    assert v["kind"] == "no_verdict" and not v["finetune_offer"] and "No verdict" in v["sentence"]
    assert not _by_type(events, "tier_done")  # nothing spent on the ladder
    assert not [c for c in llm.calls if c[1].startswith("eval:train")]
    assert events[-1]["type"] == "done" and events[-1]["ok"]


def test_guess_check_can_be_turned_off(task, grader):
    events = collect(run_downshift(task, options={"guess_check": False}, llm=FakeLLM(task), grader=grader))
    assert not _by_type(events, "guess")


def test_typical_value_of_every_json_field():
    refs = [{"a": 1, "b": "x"}, {"a": 3, "b": "x"}, {"a": 5, "b": "y"}]
    t = {"train": [{"reference": json.dumps(r)} for r in refs], "validation": []}
    gs = guess_answers(t)
    typical = [json.loads(g["answer"]) for g in gs if g["label"] == "the typical value of every field"][0]
    assert typical == {"a": 3, "b": "x"}
