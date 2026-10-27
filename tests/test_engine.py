import json
from types import SimpleNamespace

from conftest import BAD_CHECKER, GOOD_CHECKER, FakeLLM, collect, inproc_grade
from downshift import finetune
from downshift.engine import run_downshift
from downshift.grading import Grader, grade_batch_local_stub
from downshift.models import call_cost, clean_answer
from downshift.tasks import build_messages, custom_task


def _by_type(events, t):
    return [e for e in events if e["type"] == t]


def test_clean_answer_strips_reasoning():
    assert clean_answer("<think>hmm\nlong</think>\n billing ") == "billing"
    assert clean_answer("partial reasoning</think>refund") == "refund"
    assert clean_answer(None) == ""


def test_call_cost_uses_cache_price():
    full = call_cost("nvidia/Nemotron-3-Ultra-550b-a55b", 1000, 100)
    cached = call_cost("nvidia/Nemotron-3-Ultra-550b-a55b", 1000, 100, cached_tokens=1000)
    assert abs(full - (1000 * 1e-6 + 100 * 3e-6)) < 1e-12
    assert cached < full


def test_downshift_to_lightning(task, grader):
    llm = FakeLLM(task)
    events = collect(run_downshift(task, llm=llm, grader=grader, run_id="t1"))
    assert events[-1] == {**events[-1], "type": "done", "ok": True}
    v = _by_type(events, "verdict")[0]["verdict"]
    assert v["kind"] == "downshift"
    assert v["model"] == "nvidia/Nemotron-3_5-Lightning"
    assert "QUEUE RULES" in v["system_prompt"]
    assert v["holdout_score"] >= v["bar"]
    assert v["savings_multiple"] and v["savings_multiple"] > 1
    assert "MODEL = 'nvidia/Nemotron-3_5-Lightning'" in v["snippet"]
    compile(v["snippet"], "snippet", "exec")
    # Every verdict number comes from a measured holdout eval in the same run.
    ho = [e for e in _by_type(events, "eval") if e["split"] == "holdout"]
    lightning_ho = [e for e in ho if e["tier_key"] == "lightning"][-1]
    ultra_ho = [e for e in ho if e["tier_key"] == v["reference"]["rung"]][-1]
    assert v["reference"]["rung"] == "ultra-think"  # thinking on is the reference unless off scores higher
    assert "THINKING = False" in v["snippet"] and v["thinking"] is False
    assert v["holdout_score"] == lightning_ho["score"]
    assert v["cost_per_1k_usd"] == lightning_ho["cost_per_1k_usd"]
    assert v["ultra_cost_per_1k_usd"] == ultra_ho["cost_per_1k_usd"]
    assert v["p50_latency_ms"] == 300.0
    rounds = _by_type(events, "round")
    assert rounds and rounds[0]["tier_key"] == "lightning"


def test_fewshot_rows_not_scored_on_train(task, grader):
    llm = FakeLLM(task)
    events = collect(run_downshift(task, llm=llm, grader=grader))
    cand = [c for r in _by_type(events, "round") for c in r["candidates"] if c["fewshot_ids"]]
    assert cand
    for e in cand:
        assert e["n"] == len(task["train"]) - len(e["fewshot_ids"])


def test_prompting_tops_out_gives_finetune(task, grader):
    llm = FakeLLM(task, rules_fix=(), skill={"lightning": 0.2, "nano": 0.3, "super": 0.5, "ultra": 0.6,
                                             "ultra-think": 1.0})
    events = collect(run_downshift(task, options={"rounds": 2}, llm=llm, grader=grader))
    v = _by_type(events, "verdict")[0]["verdict"]
    assert v["kind"] == "finetune" and v["finetune_offer"]
    assert v["model"] == "nvidia/Nemotron-3-Ultra-550b-a55b"
    assert "tops out" in v["sentence"]
    assert [e["tier_key"] for e in _by_type(events, "tier_done")] == ["lightning", "lightning-think", "nano", "super"]
    assert v["ultra_train_outputs"]  # dataset material for the fine-tune fallback


def test_plain_prompt_good_enough_skips_rewrites(task, grader):
    llm = FakeLLM(task, skill={"lightning": 1.0, "nano": 1.0, "super": 1.0, "ultra": 1.0})
    events = collect(run_downshift(task, llm=llm, grader=grader))
    assert not _by_type(events, "round")
    v = _by_type(events, "verdict")[0]["verdict"]
    assert v["model"] == "nvidia/Nemotron-3_5-Lightning" and "plain prompt" in v["sentence"]
    assert not any(p == "propose_prompts" for _, p, _t in llm.calls)
    # Rungs above the winner are never called.
    assert not any("Nano" in m or "super" in m for m, _p, _t in llm.calls)


def test_spend_cap_aborts_cleanly(task, grader):
    llm = FakeLLM(task, cap=0.0005)
    events = collect(run_downshift(task, llm=llm, grader=grader))
    assert events[-1]["type"] == "done"
    verdicts = _by_type(events, "verdict")
    # Either the hard cap aborted the run, or the budget guard stopped tuning and still gave a verdict.
    assert _by_type(events, "aborted") or (verdicts and verdicts[0]["verdict"]["budget_stop"])
    assert not any(e["type"] == "eval" and e["tier_key"] in ("nano", "super") for e in events)


def test_hard_cap_aborts_cleanly(task, grader):
    llm = FakeLLM(task, cap=0.0)
    events = collect(run_downshift(task, llm=llm, grader=grader))
    assert _by_type(events, "aborted")
    assert events[-1]["type"] == "done" and events[-1]["ok"] is False


def test_custom_task_regenerates_bad_checker(task, grader):
    spec = {"instruction": task["instruction"], "output_format": task["output_format"],
            "label_set": task["label_set"],
            "examples": [{"input": r["input"]} for r in task["train"] + task["holdout"]]}
    ct = custom_task(spec)
    assert all(r["reference"] is None for r in ct["train"])
    llm = FakeLLM(task, checkers=[BAD_CHECKER, GOOD_CHECKER])
    events = collect(run_downshift(ct, llm=llm, grader=grader))
    assert _by_type(events, "references")
    attempts = _by_type(events, "checker_attempt")
    assert [a["validation"]["ok"] for a in attempts] == [False, True]
    assert _by_type(events, "grader")[0]["source"] == "ultra"
    assert _by_type(events, "verdict")
    # Ultra reference calls are reused for Ultra's own scoring, not repeated.
    ultra_task_calls = [p for m, p, th in llm.calls if "Ultra" in m and not th and p.startswith(("reference", "eval"))]
    assert len(ultra_task_calls) == len(ct["train"]) + len(ct["holdout"])


def test_checker_falls_back_to_exact_match(task, grader):
    spec = {"instruction": "x", "examples": [{"input": r["input"], "reference": r["reference"]}
                                             for r in task["train"]]}
    llm = FakeLLM(task, checkers=[BAD_CHECKER])
    events = collect(run_downshift(custom_task(spec), llm=llm, grader=grader))
    g = _by_type(events, "grader")[0]
    assert g["source"] == "builtin_exact" and g["validation"]["ok"]


def test_sandbox_failure_falls_back_to_local(task):
    def broken(*a, **k):
        raise RuntimeError("sandbox down")
    g = Grader(sandbox_fn=broken, local_fn=inproc_grade)
    events = collect(run_downshift(task, llm=FakeLLM(task), grader=g))
    assert any("fell back" in e.get("message", "") for e in _by_type(events, "notice"))
    assert _by_type(events, "verdict")[0]["verdict"]["graded_by"] == ["local"]


def test_local_subprocess_grader():
    items = [{"id": "a", "input": "x", "output": "billing", "reference": "billing"},
             {"id": "b", "input": "x", "output": "refund", "reference": "billing"}]
    res = grade_batch_local_stub(GOOD_CHECKER, items)
    assert [r.score for r in res] == [1.0, 0.0]
    crash = grade_batch_local_stub("def check(o, r, i):\n    raise ValueError('x')\n", items)
    assert all(r.error for r in crash)


def test_build_messages_with_fewshots():
    msgs = build_messages("SYS", [{"id": "1", "input": "a", "reference": {"k": 1}}], "b")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert json.loads(msgs[2]["content"]) == {"k": 1}


def test_finetune_launch_with_mock_client(task):
    created = {}

    class Jobs:
        def create(self, **kw):
            created.update(kw)
            return SimpleNamespace(id="ftjob-1", status="queued")

        def retrieve(self, job_id):
            return SimpleNamespace(model_dump=lambda: {"id": job_id, "status": "succeeded",
                                                       "fine_tuned_model": "qwen-ft"})

        def list_events(self, job_id):
            return SimpleNamespace(data=[SimpleNamespace(created_at=1, level="info", message="done")])

    client = SimpleNamespace(files=SimpleNamespace(create=lambda file, purpose: SimpleNamespace(id="file-1")),
                             fine_tuning=SimpleNamespace(jobs=Jobs()))
    outputs = {r["id"]: r["reference"] for r in task["train"]}
    info = finetune.launch("run1", "SYS", task["train"], outputs, client=client)
    assert info["job_id"] == "ftjob-1" and info["n_rows"] == len(task["train"])
    assert created["model"] == "Qwen/Qwen3-4B" and created["training_file"] == "file-1"
    s = finetune.poll("ftjob-1", client=client, sleep=lambda s: None)
    assert s["status"] == "succeeded" and s["events"]
    rows = finetune.build_dataset("SYS", task["train"], outputs)
    assert rows[0]["messages"][2]["content"] == task["train"][0]["reference"]


def test_budget_guard_stops_tuning_but_still_gives_verdict(task, grader):
    first = collect(run_downshift(task, llm=FakeLLM(task), grader=grader))
    after_bar = next(e for e in first if e["type"] == "bar")["spend_usd"]
    llm = FakeLLM(task, cap=after_bar * 1.02)
    events = collect(run_downshift(task, options={"spend_cap_usd": after_bar * 1.02}, llm=llm, grader=grader))
    assert not _by_type(events, "aborted")
    v = _by_type(events, "verdict")[0]["verdict"]
    assert v["budget_stop"] and v["kind"] == "finetune"
    assert llm.meter.spent_usd <= llm.meter.cap_usd


def test_ultra_off_wins_reference_when_it_scores_higher(task, grader):
    llm = FakeLLM(task, skill={"lightning": 1.0, "nano": 1.0, "super": 1.0, "ultra": 1.0, "ultra-think": 0.5})
    events = collect(run_downshift(task, llm=llm, grader=grader))
    assert _by_type(events, "bar")[0]["reference"] == "ultra"


def test_ultra_thinking_off_can_be_the_downshift(task, grader):
    llm = FakeLLM(task, rules_fix=(), skill={"lightning": 0.1, "nano": 0.1, "super": 0.1, "ultra": 1.0,
                                             "ultra-think": 1.0})
    v = _by_type(collect(run_downshift(task, options={"rounds": 0}, llm=llm, grader=grader)), "verdict")[0]["verdict"]
    assert v["kind"] == "downshift" and v["rung"] == "ultra" and v["reference"]["rung"] == "ultra-think"


def test_within_one_row_is_not_claimed_as_a_win():
    from downshift.engine import _compare
    assert "within one row" in _compare(0.90, 0.89, 40)
    assert "against" in _compare(0.95, 0.89, 40)


def test_truncated_thinking_counts_as_failure(task, grader):
    from downshift.models import CallResult

    class Trunc(FakeLLM):
        async def chat(self, model, messages, max_tokens=512, temperature=0.0, purpose="", thinking=False):
            r = await super().chat(model, messages, max_tokens, temperature, purpose, thinking)
            if thinking and purpose.startswith("eval"):
                return CallResult(model, "", r.prompt_tokens, max_tokens, r.cost_usd, r.latency_ms, truncated=True)
            return r

    events = collect(run_downshift(task, options={"rounds": 0}, llm=Trunc(task), grader=grader))
    lt = [e for e in _by_type(events, "eval") if e["tier_key"] == "lightning-think"]
    if lt:
        assert lt[0]["truncated"] == lt[0]["n"] and lt[0]["score"] == 0.0
