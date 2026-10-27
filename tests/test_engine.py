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
    assert v["diff_vs_reference"]["lo"] > -v["tolerance"]
    assert "matches Ultra within 5 points (95% CI" in v["sentence"] and "better than" not in v["sentence"]
    assert v["holdout_ci"]["lo"] <= v["holdout_score"] <= v["holdout_ci"]["hi"]
    assert set(v["overfit"]) == {"tuning_score", "validation_score", "holdout_score"}
    assert v["reference"]["source"] == "live" and v["reference"]["repeats"] == 1  # the fixture has no cache
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
    n_train = _by_type(events, "started")[0]["n_train"]
    cand = [c for r in _by_type(events, "round") for c in r["candidates"] if c["fewshot_ids"]]
    assert cand
    for e in cand:
        assert e["n"] == n_train - len(e["fewshot_ids"])


def _three_splits(task):
    t = dict(task)
    t["validation"], t["train"] = task["train"][:4], task["train"][4:]
    return t


def test_validation_rows_never_reach_the_optimiser(task, grader):
    t = _three_splits(task)
    val_ids = [r["id"] for r in t["validation"]]
    llm = FakeLLM(t, fewshot_ids=(val_ids[0], "t7"), skill={"lightning": 0.0, "nano": 0.0, "super": 0.0, "ultra": 1.0})
    events = collect(run_downshift(t, llm=llm, grader=grader))
    assert llm.prompts
    for prompt in llm.prompts:
        assert not any(r["input"][:60] in prompt for r in t["validation"] + t["holdout"])
    shots = {i for r in _by_type(events, "round") for c in r["candidates"] for i in c["fewshot_ids"]}
    assert shots and not shots & set(val_ids)  # a validation id proposed as a few-shot example is dropped


def test_winner_is_picked_on_validation_not_tuning(task, grader):
    t = _three_splits(task)
    llm = FakeLLM(t, rules_fix=(), skill={"lightning": 0.0, "nano": 0.0, "super": 0.0, "ultra": 1.0},
                  proposals=[{"system": "MEMORISED answers", "fewshot_ids": [], "note": "overfits"},
                             {"system": "GENERAL rules", "fewshot_ids": [], "note": "general"}])
    llm.memorised = {r["input"] for r in t["train"]}
    events = collect(run_downshift(t, options={"rounds": 1, "thinking_rungs": False}, llm=llm, grader=grader))
    rnd = _by_type(events, "round")[0]
    by_label = {c["label"]: c for c in rnd["candidates"]}
    assert by_label["r1c1"]["score"] > by_label["r1c2"]["score"]  # MEMORISED wins on tuning rows...
    assert by_label["r1c2"]["val"] > by_label["r1c1"]["val"]      # ...GENERAL wins on validation rows
    assert rnd["best_label"] == "r1c2"
    done = _by_type(events, "tier_done")[0]
    assert done["prompt_label"] == "r1c2"
    assert done["tuning_score"] is not None and done["validation_score"] is not None
    # The held-out split is scored once per rung: one prompt only.
    ho = [e for e in _by_type(events, "eval") if e["split"] == "holdout" and e["tier_key"] == "lightning"]
    assert len(ho) == 1


def test_prompting_tops_out_gives_finetune(task, grader):
    llm = FakeLLM(task, rules_fix=(), skill={"lightning": 0.2, "nano": 0.3, "super": 0.5, "ultra": 0.6,
                                             "ultra-think": 1.0})
    events = collect(run_downshift(task, options={"rounds": 2}, llm=llm, grader=grader))
    v = _by_type(events, "verdict")[0]["verdict"]
    assert v["kind"] == "finetune" and v["finetune_offer"]
    assert v["model"] == "nvidia/Nemotron-3-Ultra-550b-a55b"
    assert "fine-tuning" in v["sentence"]
    assert [e["tier_key"] for e in _by_type(events, "tier_done")] == ["lightning", "nano", "super", "lightning-think"]
    assert "No cheaper rung could be shown to match Ultra" in v["sentence"]


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
    assert len(ultra_task_calls) == len(ct["train"]) + len(ct["validation"]) + len(ct["holdout"])


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
    assert v["thinking_off_only"] and v["sentence"].startswith("Turn thinking off:")
    assert "THINKING = False" in v["snippet"]


def test_default_tolerance_is_five_points():
    from downshift.engine import DEFAULTS
    from downshift.stats import matches
    assert DEFAULTS["tolerance"] == 0.05 and DEFAULTS["spend_cap_usd"] == 0.25
    assert matches({"lo": -0.047}, DEFAULTS["tolerance"]) and not matches({"lo": -0.056}, DEFAULTS["tolerance"])


def _append_many(path, tag, n):
    from downshift.models import append_line
    for i in range(n):
        append_line(path, '{"tag": "%s", "i": %d, "pad": "%s"}' % (tag, i, "x" * 300))


def test_usage_log_appends_do_not_interleave_across_processes(tmp_path):
    import multiprocessing as mp
    path = tmp_path / "usage.jsonl"
    procs = [mp.Process(target=_append_many, args=(path, f"p{k}", 150)) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 600
    assert all(json.loads(line)["pad"] for line in lines)


def test_wording_follows_the_ci():
    from downshift.engine import compare_sentence
    assert compare_sentence({"diff": 0.02, "lo": 0.005, "hi": 0.04}, 0.03).startswith("better than Ultra")
    s = compare_sentence({"diff": 0.02, "lo": -0.01, "hi": 0.05}, 0.03)
    assert s.startswith("matches Ultra within 3 points (95% CI") and "better" not in s
    assert compare_sentence({"diff": 0.0, "lo": -0.05, "hi": 0.05}, 0.03).startswith("not shown to match")


def test_bootstrap_is_seeded_and_paired():
    from downshift.stats import bootstrap_ci, matches, paired_diff_ci
    a = [1.0] * 70 + [0.0] * 30
    assert bootstrap_ci(a) == bootstrap_ci(a)
    ci = bootstrap_ci(a)
    assert ci["lo"] < 0.7 < ci["hi"] and ci["hi"] - ci["lo"] < 0.2
    # Identical scores: the paired difference is exactly zero, even though each side alone is uncertain.
    d = paired_diff_ci(a, a)
    assert d["diff"] == d["lo"] == d["hi"] == 0.0 and matches(d, 0.03)
    # Right on 5 more rows out of 100 and never worse: every discordant row favours b, so the CI is above 0.
    b = [1.0] * 75 + [0.0] * 25
    d2 = paired_diff_ci(b, a)
    assert abs(d2["diff"] - 0.05) < 1e-9 and d2["rows_better"] == 5 and d2["rows_worse"] == 0
    assert d2["lo"] > 0
    c = [1.0] * 65 + [0.0] * 5 + [1.0] * 10 + [0.0] * 20  # 5 rows worse, 10 rows better than a
    d3 = paired_diff_ci(c, a)
    assert d3["lo"] < 0 < d3["hi"]


def _fake_reference(task, scores_off, scores_on):
    from downshift.engine import prompt_sha
    from downshift.tasks import plain_system
    ids = [str(r["id"]) for r in task["holdout"]]

    def mode(key, scores, cost):
        return {"rung": key, "repeat_scores": [sum(s) / len(s) for s in zip(*[scores[i] for i in ids])],
                "per_row": {i: scores[i] for i in ids}, "cost_per_1k_usd": cost, "p50_ms": 900.0,
                "mean_out_tokens": 3.0, "truncated": 0}
    return {"measured_on": "2026-10-27", "repeats": 3, "plain_prompt_sha": prompt_sha(plain_system(task)),
            "modes": {"ultra": mode("ultra", scores_off, 0.5), "ultra-think": mode("ultra-think", scores_on, 1.5)},
            "validation": {"score": 1.0}, "total_cost_usd": 0.1}


def test_cached_reference_is_used_and_ultra_is_not_called_for_scoring(task, grader):
    t = dict(task)
    ids = [str(r["id"]) for r in t["holdout"]]
    t["cached_reference"] = _fake_reference(t, {i: [1.0, 1.0, 0.0 if k == 0 else 1.0] for k, i in enumerate(ids)},
                                            {i: [1.0, 1.0, 1.0] for i in ids})
    llm = FakeLLM(t)
    events = collect(run_downshift(t, llm=llm, grader=grader))
    bar = _by_type(events, "bar")[0]
    assert bar["reference_source"] == "cached" and bar["repeats"] == 3 and bar["measured_on"] == "2026-10-27"
    assert bar["reference"] == "ultra-think"
    assert not any("Ultra" in m and p.startswith("eval") for m, p, _t in llm.calls)
    v = _by_type(events, "verdict")[0]["verdict"]
    assert v["reference"]["source"] == "cached" and "3 runs" in v["reference"]["description"]
    assert v["ultra_cost_per_1k_usd"] == 1.5
    ultra_off_row = next(r for r in v["table"] if r["tier_key"] == "ultra")
    assert ultra_off_row["repeat_scores"] and "diff" in ultra_off_row


def test_stale_cached_reference_falls_back_to_live(task, grader):
    t = dict(task)
    ref = _fake_reference(t, {str(r["id"]): [1.0] * 3 for r in t["holdout"]},
                          {str(r["id"]): [1.0] * 3 for r in t["holdout"]})
    ref["plain_prompt_sha"] = "different"
    t["cached_reference"] = ref
    events = collect(run_downshift(t, llm=FakeLLM(t), grader=grader))
    assert _by_type(events, "bar")[0]["reference_source"] == "live"
    assert any("different prompt" in e.get("message", "") for e in _by_type(events, "stage"))


def test_live_option_remeasures_even_with_a_cache(task, grader):
    t = dict(task)
    t["cached_reference"] = _fake_reference(t, {str(r["id"]): [1.0] * 3 for r in t["holdout"]},
                                            {str(r["id"]): [1.0] * 3 for r in t["holdout"]})
    llm = FakeLLM(t)
    events = collect(run_downshift(t, options={"reference": "live"}, llm=llm, grader=grader))
    assert _by_type(events, "bar")[0]["reference_source"] == "live"
    assert any("Ultra" in m and p.startswith("eval:holdout") for m, p, _t in llm.calls)


def test_custom_task_has_three_disjoint_splits():
    spec = {"instruction": "x", "examples": [{"input": f"row {i}", "reference": "a"} for i in range(50)]}
    t = custom_task(spec)
    assert (len(t["train"]), len(t["validation"]), len(t["holdout"])) == (20, 10, 20)
    ids = [r["id"] for s in ("train", "validation", "holdout") for r in t[s]]
    assert len(ids) == len(set(ids)) == 50


def test_presets_have_three_disjoint_splits():
    from downshift.engine import prompt_sha
    from downshift.tasks import list_presets, load_preset, plain_system
    for p in list_presets():
        if p["source"] != "preset":
            continue
        t = load_preset(p["slug"])
        assert t["validation"], p["slug"]
        sets = [{r["input"] for r in t[s]} for s in ("train", "validation", "holdout")]
        assert not (sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])
        ref = t["cached_reference"]
        if ref:  # a cached reference must cover exactly this holdout, measured with the engine's prompt
            assert ref["holdout_ids"] == [str(r["id"]) for r in t["holdout"]]
            assert ref["plain_prompt_sha"] == prompt_sha(plain_system(t))
            assert all(len(m["repeat_scores"]) == m.get("repeats", ref["repeats"]) >= 2 for m in ref["modes"].values())


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
