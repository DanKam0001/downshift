"""The squeeze: after a cheaper verdict, cut tokens while validation holds, then re-prove on held-out rows."""
from conftest import FakeLLM, collect
from goodenough.engine import _minify, run_good_enough


def _by_type(events, t):
    return [e for e in events if e["type"] == t]


def test_squeeze_keeps_only_cuts_that_hold_and_reproves_on_holdout(task, grader):
    events = collect(run_good_enough(task, llm=FakeLLM(task), grader=grader))
    v = _by_type(events, "verdict")[0]["verdict"]
    s = _by_type(events, "squeeze")[0]
    kept = {x["lever"] for x in s["steps"] if x["kept"]}
    assert "shots0" in kept            # the rules fix the small model; the examples were dead weight
    assert "compress" not in kept      # a rewrite that loses the rules loses validation score
    assert s["applied"] and s["cost_per_1k_usd"] < v["cost_per_1k_usd"]
    assert s["diff"]["n"] == len(task["holdout"])
    # Cuts are chosen on validation; only the final squeezed setup touches held-out rows.
    ho_labels = {e["label"] for e in _by_type(events, "eval") if e["split"] == "holdout"}
    assert not [l for l in ho_labels if l.startswith("squeeze:")]
    assert "squeezed" in ho_labels
    assert events[-1]["type"] == "done" and events[-1]["ok"]


def test_squeeze_can_be_turned_off(task, grader):
    events = collect(run_good_enough(task, options={"squeeze": False}, llm=FakeLLM(task), grader=grader))
    assert not _by_type(events, "squeeze")


def test_minify_json_example():
    assert _minify({"id": 1, "reference": '{"a": 1, "b": [1, 2]}'})["reference"] == '{"a":1,"b":[1,2]}'
    assert _minify({"id": 1, "reference": "billing"})["reference"] == "billing"


def test_checker_code_that_handles_fences_is_extracted_whole():
    from goodenough.engine import _extract_code
    reply = "```python\nimport re\ndef check(output, reference, input):\n    out = re.sub(r'^```\w*|```$', '', output)\n    return 1.0, 'ok'\n```"
    code = _extract_code(reply)
    compile(code, "checker", "exec")
    assert "return 1.0" in code
