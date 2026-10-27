"""The Downshift pipeline (PLAN.md "How it works", steps 1-5).

run_downshift() is an async generator of progress events (plain dicts) so the server can stream them.
Every number in the verdict comes from calls measured in this run.

The ladder is made of rungs: (model, thinking on/off) pairs, cheapest first. Ultra is measured first to set the
bar; then rungs are tried one at a time (plain prompt, then Ultra-written rewrites scored on the train split, then
one held-out score) until one reaches the bar. Rungs above the winner are never called.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
import uuid
from pprint import pformat

from .grading import Grader
from .models import (CHEAP_RUNGS, RUNGS, THINKING_BUDGET, ULTRA, ULTRA_OFF, ULTRA_ON, BudgetExceeded, LLM, Meter,
                     get_pricing)
from .tasks import as_text, build_messages, ensure_labels, plain_system

DEFAULTS = {
    "tolerance": 0.02,       # bar = reference (Ultra) held-out score minus this (2 points)
    "rounds": 3,             # prompt-rewrite rounds per rung
    "candidates": 2,         # candidates Ultra proposes per round
    "spend_cap_usd": 0.10,   # hard per-run cap
    "fewshot_max": 4,
    "concurrency": 16,
    "ultra_thinking": True,  # also measure Ultra with thinking on, if it fits the cap
    "thinking_rungs": True,  # include Lightning with thinking on as a rung
    "train_limit": None,
    "holdout_limit": None,
}

# Ultra with thinking on costs several times Ultra with thinking off (reasoning tokens); used only to decide
# whether that measurement fits the remaining budget.
ULTRA_THINK_COST_FACTOR = 6.0

BUILTIN_EXACT_CHECKER = '''
import json, re
def _norm(x):
    if not isinstance(x, str):
        x = json.dumps(x, sort_keys=True)
    x = x.strip()
    x = re.sub(r"^```[a-zA-Z]*\\s*|\\s*```$", "", x)
    return re.sub(r"\\s+", " ", x).strip().strip(".").lower()
def check(output, reference, input):
    ok = _norm(output) == _norm(reference)
    return (1.0 if ok else 0.0), ("exact match" if ok else "differs from reference")
'''


def _now() -> float:
    return round(time.time(), 3)


def _pct(x: float) -> str:
    v = round(x * 100, 1)
    return f"{v:.0f}%" if v == int(v) else f"{v:.1f}%"


def _p50(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _max_tokens_for(task: dict) -> int:
    refs = [as_text(r["reference"]) for r in task["train"] + task["holdout"] if r.get("reference") is not None]
    longest = max((len(r) for r in refs), default=400)
    return int(min(1024, max(64, longest // 2 + 48)))


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def make_snippet(model: str, system: str, fewshots: list[dict], max_tokens: int, thinking: bool) -> str:
    shots = []
    for ex in fewshots:
        shots.append({"role": "user", "content": as_text(ex["input"])})
        shots.append({"role": "assistant", "content": as_text(ex["reference"])})
    return (
        "import os\n"
        "from openai import OpenAI\n\n"
        "client = OpenAI(base_url=\"https://api.tokenfactory.nebius.com/v1\", api_key=os.environ[\"NEBIUS_API_KEY\"])\n\n"
        f"MODEL = {model!r}\n"
        f"THINKING = {thinking!r}  # Nemotron reasoning; {'measured with it on' if thinking else 'off is faster and cheaper'}\n"
        f"SYSTEM = {system!r}\n"
        f"FEW_SHOT = {pformat(shots, width=100)}\n\n\n"
        "def run(text: str) -> str:\n"
        "    messages = [{\"role\": \"system\", \"content\": SYSTEM}, *FEW_SHOT, {\"role\": \"user\", \"content\": text}]\n"
        "    r = client.chat.completions.create(\n"
        f"        model=MODEL, messages=messages, temperature=0, max_tokens={max_tokens},\n"
        "        extra_body={\"chat_template_kwargs\": {\"enable_thinking\": THINKING}},\n"
        "    )\n"
        "    if r.choices[0].finish_reason == \"length\":\n"
        "        raise RuntimeError(\"ran out of tokens before answering\")\n"
        "    return r.choices[0].message.content.strip()\n"
    )


class Run:
    """Holds per-run state: LLM client, grader, call cache, measured evaluations."""

    def __init__(self, task: dict, opts: dict, llm: LLM, grader: Grader, run_id: str):
        self.task, self.opts, self.llm, self.grader, self.run_id = task, opts, llm, grader, run_id
        self.cache: dict = {}
        self.answer_tokens = _max_tokens_for(task)
        self.checker_code: str | None = None
        self.evals: list[dict] = []
        self.grade_backends: set[str] = set()
        self.last_propose_problem: str | None = None

    def max_tokens(self, rung: dict) -> int:
        return self.answer_tokens + (THINKING_BUDGET if rung["thinking"] else 0)

    def ev(self, type_: str, **kw) -> dict:
        return {"type": type_, "t": _now(), "spend_usd": round(self.llm.meter.spent_usd, 6), **kw}

    def remaining(self) -> float:
        return self.llm.meter.cap_usd - self.llm.meter.spent_usd

    async def outputs(self, rung: dict, system: str, fewshots: list[dict], rows: list[dict], purpose: str):
        shot_key = tuple(str(f["id"]) for f in fewshots)

        async def one(row):
            key = (rung["model"], rung["thinking"], system, shot_key, str(row["id"]))
            if key not in self.cache:
                self.cache[key] = await self.llm.chat(rung["model"], build_messages(system, fewshots, row["input"]),
                                                      max_tokens=self.max_tokens(rung), purpose=purpose,
                                                      thinking=rung["thinking"])
            return self.cache[key]

        results = await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)
        for r in results:
            if isinstance(r, BudgetExceeded):
                raise r
        for r in results:
            if isinstance(r, Exception):
                raise r
        return results

    async def evaluate(self, rung: dict, system: str, fewshots: list[dict], rows: list[dict], split: str,
                       label: str) -> dict:
        calls = await self.outputs(rung, system, fewshots, rows, purpose=f"eval:{split}:{label}")
        items = [{"id": r["id"], "input": r["input"], "output": c.text, "reference": r["reference"]}
                 for r, c in zip(rows, calls)]
        grades, batch = await self.grader.grade(self.checker_code, items)
        backend = batch["backend"]
        self.grade_backends.add(backend)
        by_id = {g.item_id: g for g in grades}
        scores, failures = [], []
        for it, c in zip(items, calls):
            g = by_id.get(str(it["id"]))
            s = g.score if g else 0.0
            scores.append(s)
            if s < 0.999:
                detail = "ran out of tokens before answering" if c.truncated and not c.text else (
                    g.detail if g else "not graded")
                failures.append({"id": it["id"], "input": as_text(it["input"])[:400],
                                 "expected": as_text(it["reference"])[:200], "got": c.text[:200],
                                 "detail": detail[:160]})
        ok_calls = [c for c in calls if c.ok]
        cost = sum(c.cost_usd for c in calls)
        e = {
            "model": rung["model"], "tier_key": rung["key"], "thinking": rung["thinking"], "split": split,
            "label": label, "n": len(rows),
            "score": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "cost_usd": round(cost, 6),
            "cost_per_1k_usd": round(1000 * cost / len(ok_calls), 4) if ok_calls else None,
            "p50_ms": _p50([c.latency_ms for c in ok_calls]),
            "mean_out_tokens": round(sum(c.completion_tokens for c in ok_calls) / len(ok_calls), 1) if ok_calls else None,
            "errors": len(calls) - len(ok_calls), "truncated": sum(1 for c in calls if c.truncated),
            "grader": backend, "grade_batch": batch,
            "system": system, "fewshot_ids": [str(f["id"]) for f in fewshots],
            "failures": failures[:6],
        }
        self.evals.append(e)
        return e

    def ultra_outputs(self, rung: dict, system: str, rows: list[dict]) -> dict:
        out = {}
        for r in rows:
            c = self.cache.get((rung["model"], rung["thinking"], system, (), str(r["id"])))
            if c is not None and c.text:
                out[str(r["id"])] = c.text
        return out


def _public_eval(e: dict) -> dict:
    return {k: v for k, v in e.items() if k not in ("system", "failures")} | {
        "system_preview": e["system"][:240], "failures_shown": e["failures"][:3]}


async def _write_checker(run: Run, feedback: str | None) -> str:
    t = run.task
    examples = "\n".join(f"- input: {as_text(r['input'])[:300]}\n  reference: {as_text(r['reference'])[:200]}"
                         for r in t["train"][:4])
    prompt = (
        "Write a Python grader for this task.\n"
        f"Task: {t['instruction']}\nOutput format: {t.get('output_format') or 'free text'}\n"
        + (f"Allowed labels: {t['label_set']}\n" if t.get("label_set") else "")
        + (f"JSON schema: {as_text(t['json_schema'])}\n" if t.get("json_schema") else "")
        + f"Examples:\n{examples}\n\n"
        "Define exactly: def check(output, reference, input) -> tuple[float, str]\n"
        "- output is the model's raw text; reference may be a string or parsed JSON.\n"
        "- Return a score in [0, 1] and a short reason. Use only the Python standard library.\n"
        "- Be lenient on whitespace, case, trailing punctuation and ```code fences```; strict on the facts "
        "(labels, field values, numbers within a small tolerance).\n"
        "- Never raise: catch parse errors and return 0.0.\n"
        "Reply with one ```python code block and nothing else."
        + (f"\n\nYour previous checker failed validation: {feedback}. Fix it." if feedback else "")
    )
    res = await run.llm.chat(ULTRA, [{"role": "user", "content": prompt}], max_tokens=1200, purpose="write_checker")
    if not res.ok:
        raise RuntimeError(f"Ultra could not write a checker: {res.error}")
    return _extract_code(res.text)


async def _validate_checker(run: Run, code: str) -> dict:
    rows = [r for r in run.task["train"] if r.get("reference") is not None][:12]
    items = []
    for i, r in enumerate(rows):
        items.append({"id": f"{r['id']}::ref", "input": r["input"], "output": as_text(r["reference"]),
                      "reference": r["reference"]})
        wrong = next((o for o in rows[i + 1:] + rows[:i] if as_text(o["reference"]) != as_text(r["reference"])), None)
        items.append({"id": f"{r['id']}::wrong", "input": r["input"],
                      "output": as_text(wrong["reference"]) if wrong else "", "reference": r["reference"]})
    grades, batch = await run.grader.grade(code, items)
    backend = batch["backend"]
    run.grade_backends.add(backend)
    ref = [g.score for g in grades if g.item_id.endswith("::ref")]
    wrong = [g.score for g in grades if g.item_id.endswith("::wrong")]
    errors = [g.error for g in grades if g.error]
    ref_mean = sum(ref) / len(ref) if ref else 0.0
    wrong_mean = sum(wrong) / len(wrong) if wrong else 1.0
    ok = ref_mean >= 0.95 and wrong_mean <= 0.5
    problem = None
    if not ok:
        problem = (f"references scored {ref_mean:.2f} (need >= 0.95), wrong outputs scored {wrong_mean:.2f} "
                   f"(need <= 0.5)" + (f"; errors: {errors[:2]}" if errors else ""))
    return {"ok": ok, "ref_mean": round(ref_mean, 3), "wrong_mean": round(wrong_mean, 3), "backend": backend,
            "grade_batch": batch, "problem": problem}


async def _propose(run: Run, rung: dict, best: dict, history: list[dict], k: int) -> list[dict]:
    t = run.task
    fails = "\n".join(f"- input: {f['input'][:300]}\n  expected: {f['expected']}\n  got: {f['got'] or '(no answer)'}"
                      + (f"\n  note: {f['detail']}" if f.get("detail") else "")
                      for f in best["failures"][:5]) or "(none)"
    pool = "\n".join(f"- {r['id']}: {as_text(r['input'])[:90]}" for r in t["train"][:40])
    tried = "\n".join(f"- score {h['score']:.2f}, shots {h['fewshot_ids']}: {h['system'][:160]!r}"
                      for h in history[-6:]) or "(none)"
    labels_note = "; do NOT repeat the allowed label list, it is appended automatically" if t.get("label_set") else ""
    prompt = (
        f"A small model ({rung['label']}) must do this task as well as a much larger model.\n"
        f"Task: {t['instruction'][:1500]}\nOutput format: {t.get('output_format') or 'free text'}\n"
        f"\nCurrent best system prompt (train score {best['score']:.2f}):\n{best['system']}\n\n"
        f"Its mistakes:\n{fails}\n\nAlready tried:\n{tried}\n\n"
        f"Training examples you may use as few-shot demonstrations (by id):\n{pool}\n\n"
        f"Propose {k} different improved candidates. Each has a complete system prompt (at most 1200 characters; "
        f"keep the output format rules{labels_note}) and 0-{run.opts['fewshot_max']} few-shot example ids from the "
        "list. Make them differ in approach (for example, one clearer zero-shot rule set, one with targeted examples).\n"
        'Reply with JSON only: {"candidates": [{"system": "...", "fewshot_ids": ["..."], "note": "what changed"}]}'
    )
    res = await run.llm.chat(ULTRA, [{"role": "user", "content": prompt}], max_tokens=2500, purpose="propose_prompts")
    if not res.ok:
        run.last_propose_problem = f"Ultra call failed: {res.error}"
        return []
    try:
        data = _extract_json(res.text)
    except Exception:  # noqa: BLE001
        run.last_propose_problem = (f"Ultra's reply was not valid JSON ({res.completion_tokens} tokens, "
                                    "probably cut off); this round is skipped")
        return []
    ids = {str(r["id"]): r for r in t["train"]}
    out = []
    for c in (data.get("candidates") or [])[:k]:
        system = str(c.get("system") or "").strip()[:4000]
        if not system:
            continue
        system = ensure_labels(t, system)
        shots = [ids[str(i)] for i in (c.get("fewshot_ids") or []) if str(i) in ids][: run.opts["fewshot_max"]]
        out.append({"system": system, "fewshots": shots, "note": str(c.get("note") or "")[:200]})
    return out


def _train_rows_excluding(train: list[dict], shots: list[dict]) -> list[dict]:
    used = {str(s["id"]) for s in shots}
    rows = [r for r in train if str(r["id"]) not in used]
    return rows if len(rows) >= 3 else train


def _compare(score: float, ref: float, n: int) -> str:
    """Plain wording that does not claim a difference smaller than one held-out row."""
    row = 1.0 / max(1, n)
    gap = score - ref
    if abs(gap) < row - 1e-9:
        return f"matching Ultra's {_pct(ref)} (within one row)"
    if gap > 0:
        return f"against Ultra's {_pct(ref)}"
    return f"against Ultra's {_pct(ref)}, within the {_pct(-gap)} allowance"


def _verdict(run: Run, ref: dict, ultra_evals: dict, chosen: dict | None, best_cheap: dict | None, tol: float,
             bar: float, budget_stop: str | None) -> dict:
    t = run.task
    n = ref["n"]
    ref_cost = ref["cost_per_1k_usd"]
    ref_rung = next(r for r in RUNGS if r["key"] == ref["tier_key"])
    if chosen:
        ho = chosen["holdout"]
        rung = next(r for r in RUNGS if r["key"] == ho["tier_key"])
        system, shots = chosen["system"], chosen["fewshots"]
        cost = ho["cost_per_1k_usd"]
        savings = round(ref_cost / cost, 1) if cost and ref_cost else None
        kind = "downshift"
        how = "this prompt" if (shots or system != plain_system(t)) else "the plain prompt"
        sentence = (f"Use {rung['label']} with {how}: it scored {_pct(ho['score'])} on {n} held-out examples, "
                    f"{_compare(ho['score'], ref['score'], n)}, at ${cost:.4f} per 1k calls instead of "
                    f"${ref_cost:.4f}" + (f" ({savings}x cheaper)." if savings and savings > 1 else "."))
        score, p50 = ho["score"], ho["p50_ms"]
    else:
        rung = ref_rung
        system, shots = plain_system(t), []
        cost, savings, kind = ref_cost, 1.0, "finetune"
        score, p50 = ref["score"], ref["p50_ms"]
        if best_cheap:
            br = next(r for r in RUNGS if r["key"] == best_cheap["tier_key"])
            sentence = (f"Prompting tops out at {_pct(best_cheap['score'])} on held-out rows with {br['label']}, below the "
                        f"bar of {_pct(bar)} (Ultra {_pct(ref['score'])} minus {tol * 100:.0f} points). "
                        "Keep Ultra for now; fine-tuning a small model on Ultra's outputs is the next step.")
        else:
            sentence = "No cheaper rung was measured on held-out data in this run, so keep Ultra."
        if budget_stop:
            sentence += f" Tuning {budget_stop}, so a larger cap may do better."
    table = []
    for r in RUNGS:
        rows = [e for e in run.evals if e["tier_key"] == r["key"]]
        plain_train = next((e for e in rows if e["split"] == "train" and e["label"] == "plain"), None)
        ho_rows = [e for e in rows if e["split"] == "holdout"]
        ho_best = max(ho_rows, key=lambda e: e["score"]) if ho_rows else None
        basis = ho_best or plain_train or {}
        table.append({
            "tier_key": r["key"], "name": r["name"], "label": r["label"], "model": r["model"],
            "thinking": r["thinking"],
            "plain_train_score": plain_train["score"] if plain_train else None,
            "holdout_score": ho_best["score"] if ho_best else None,
            "holdout_label": ho_best["label"] if ho_best else None,
            "cost_per_1k_usd": basis.get("cost_per_1k_usd"), "p50_ms": basis.get("p50_ms"),
            "truncated": basis.get("truncated"),
            "cost_basis": "holdout" if ho_best else ("train" if plain_train else None),
        })
    ultra_cmp = {k: {"score": e["score"], "cost_per_1k_usd": e["cost_per_1k_usd"], "p50_ms": e["p50_ms"]}
                 for k, e in ultra_evals.items() if e}
    return {
        "kind": kind, "model": rung["model"], "rung": rung["key"], "tier_name": rung["name"],
        "rung_label": rung["label"], "thinking": rung["thinking"], "system_prompt": system,
        "fewshots": [{"id": s["id"], "input": s["input"], "reference": s["reference"]} for s in shots],
        "holdout_score": score, "holdout_n": n, "row_points": round(100 / max(1, n), 2),
        "reference": {"rung": ref_rung["key"], "label": ref_rung["label"], "thinking": ref_rung["thinking"]},
        "ultra_holdout_score": ref["score"], "ultra_modes": ultra_cmp,
        "bar": round(bar, 4), "tolerance": tol,
        "cost_per_1k_usd": cost, "ultra_cost_per_1k_usd": ref_cost, "savings_multiple": savings,
        "p50_latency_ms": p50, "ultra_p50_latency_ms": ref["p50_ms"],
        "sentence": sentence,
        "snippet": make_snippet(rung["model"], system, shots, run.max_tokens(rung), rung["thinking"]),
        "finetune_offer": kind == "finetune",
        "best_cheap": ({"rung": best_cheap["tier_key"], "model": best_cheap["model"],
                        "holdout_score": best_cheap["score"]} if best_cheap else None),
        "budget_stop": budget_stop,
        "run_spend_usd": round(run.llm.meter.spent_usd, 6),
        "graded_by": sorted(run.grade_backends), "table": table,
    }


async def run_downshift(task: dict, options: dict | None = None, llm: LLM | None = None,
                        grader: Grader | None = None, run_id: str | None = None):
    opts = {**DEFAULTS, **{k: v for k, v in (options or {}).items() if v is not None}}
    run_id = run_id or uuid.uuid4().hex[:10]
    llm = llm or LLM(run_id=run_id, meter=Meter(cap_usd=float(opts["spend_cap_usd"])),
                     concurrency=int(opts["concurrency"]))
    grader = grader or Grader()
    task = dict(task)
    task["train"] = [dict(r, id=str(r["id"])) for r in task["train"]][: opts["train_limit"] or None]
    task["holdout"] = [dict(r, id=str(r["id"])) for r in task["holdout"]][: opts["holdout_limit"] or None]
    run = Run(task, opts, llm, grader, run_id)
    tol = float(opts["tolerance"])
    plain = plain_system(task)
    cheap_rungs = [r for r in CHEAP_RUNGS if opts["thinking_rungs"] or not r["thinking"]]

    pricing = get_pricing()

    def price(r, k):
        return round(pricing["models"].get(r["model"], {}).get(k, 0) * 1e6, 4)

    yield run.ev("started", run_id=run_id, task={k: task.get(k) for k in ("slug", "title", "business_story")},
                 n_train=len(task["train"]), n_holdout=len(task["holdout"]), options=opts,
                 row_points=round(100 / max(1, len(task["holdout"])), 2),
                 tiers=[r | {"price_in_per_m": price(r, "prompt"), "price_out_per_m": price(r, "completion")}
                        for r in cheap_rungs + [ULTRA_OFF, ULTRA_ON]],
                 pricing_source=pricing["_source"])
    try:
        # 1. Reference outputs from Ultra where none were given.
        missing = [r for r in task["train"] + task["holdout"] if r.get("reference") is None]
        if missing:
            yield run.ev("stage", stage="reference", message=f"Ultra is writing reference outputs for {len(missing)} examples")
            calls = await run.outputs(ULTRA_OFF, plain, [], missing, purpose="reference")
            for r, c in zip(missing, calls):
                r["reference"] = c.text
            yield run.ev("references", n=len(missing))

        # 2. Grader.
        if task.get("checker_code"):
            yield run.ev("stage", stage="grader", message="Checking the task's checker against its references")
            run.checker_code = task["checker_code"]
            v = await _validate_checker(run, run.checker_code)
            yield run.ev("grader", source="preset", validation=v)
        else:
            yield run.ev("stage", stage="grader", message="Ultra is writing a checker for this task")
            code, feedback, v = None, None, None
            for attempt in range(2):
                code = await _write_checker(run, feedback)
                v = await _validate_checker(run, code)
                yield run.ev("checker_attempt", attempt=attempt + 1, validation=v, code=code)
                if v["ok"]:
                    break
                feedback = v["problem"]
            if v and v["ok"]:
                run.checker_code = code
                yield run.ev("grader", source="ultra", validation=v, code=code)
            else:
                run.checker_code = BUILTIN_EXACT_CHECKER
                v2 = await _validate_checker(run, run.checker_code)
                yield run.ev("grader", source="builtin_exact", validation=v2,
                             message="Ultra's checker failed validation twice; using exact match instead")
        if run.grader.last_error:
            yield run.ev("notice", message=f"Sandbox grading fell back to local: {run.grader.last_error}")

        # 3. The bar: Ultra (thinking off) on both splits, then Ultra (thinking on) on held-out rows if it fits.
        yield run.ev("stage", stage="bar", message="Measuring Ultra to set the bar")
        ultra_train, ultra_off_ho = await asyncio.gather(
            run.evaluate(ULTRA_OFF, plain, [], task["train"], "train", "plain"),
            run.evaluate(ULTRA_OFF, plain, [], task["holdout"], "holdout", "plain"))
        yield run.ev("eval", **_public_eval(ultra_train))
        yield run.ev("eval", **_public_eval(ultra_off_ho))
        ultra_on_ho = None
        reserve = min(0.3 * llm.meter.cap_usd, 0.03)  # keep enough for at least the cheapest rungs
        on_est = ultra_off_ho["cost_usd"] * ULTRA_THINK_COST_FACTOR
        if opts["ultra_thinking"] and ultra_off_ho["cost_usd"] * 1.5 + reserve <= run.remaining():
            # Probe a few rows first, then extrapolate from what they actually cost.
            yield run.ev("stage", stage="bar", tier_key=ULTRA_ON["key"],
                         message="Measuring Ultra with thinking on (its default mode)")
            probe_rows = task["holdout"][:5]
            probe = await run.outputs(ULTRA_ON, plain, [], probe_rows, purpose="eval:holdout:probe")
            per_call = sum(c.cost_usd for c in probe) / max(1, len(probe))
            on_est = per_call * (len(task["holdout"]) - len(probe_rows)) * 1.2
            if on_est + reserve <= run.remaining():
                ultra_on_ho = await run.evaluate(ULTRA_ON, plain, [], task["holdout"], "holdout", "plain")
                yield run.ev("eval", **_public_eval(ultra_on_ho))
        if opts["ultra_thinking"] and ultra_on_ho is None:
            yield run.ev("skipped", tier_key=ULTRA_ON["key"],
                         message=f"Skipped Ultra with thinking on: the full held-out pass would cost about ${on_est:.3f}, which does not "
                                 f"fit this run's ${llm.meter.cap_usd:.2f} cap. Raise the cap to include it.")
        # Reference is Ultra with thinking on, unless thinking off scores higher on held-out rows.
        if ultra_on_ho and ultra_on_ho["score"] >= ultra_off_ho["score"]:
            ref = ultra_on_ho
        else:
            ref = ultra_off_ho
        bar = ref["score"] - tol
        # While tuning on the train split, aim to match Ultra's train score outright; the tolerance is only
        # applied to the final held-out comparison. Train scores are noisy, so this avoids stopping early.
        train_bar = ultra_train["score"]
        yield run.ev("bar", bar=round(bar, 4), train_bar=round(train_bar, 4), ultra_holdout=ref["score"],
                     reference=ref["tier_key"], ultra_train=ultra_train["score"], tolerance=tol,
                     row_points=round(100 / max(1, ref["n"]), 2))

        # 4. Downshift loop, cheapest rung first. Each rung is measured only when we reach it.
        chosen, best_cheap, carry, budget_stop = None, None, None, None
        meter = llm.meter
        ultra_per_call = ultra_train["cost_usd"] / max(1, ultra_train["n"])
        propose_cost = [0.009]  # replaced by the measured cost of Ultra's proposal calls

        def est(rung: dict, n_rows: int, base: dict | None = None, factor: float = 2.5) -> float:
            if base:
                per = base["cost_usd"] / max(1, base["n"])
            else:
                p_r = pricing["models"].get(rung["model"], {}).get("prompt", 1e-6)
                p_u = pricing["models"].get(ULTRA, {}).get("prompt", 1e-6)
                per = ultra_per_call * (p_r / p_u) * (8 if rung["thinking"] else 1)
            return per * n_rows * factor  # rewritten prompts with examples run ~2x longer

        for rung in cheap_rungs:
            if est(rung, len(task["train"]) + len(task["holdout"]), factor=1.2) > run.remaining():
                budget_stop = f"stopped before {rung['label']} to stay under the ${meter.cap_usd:.2f} cap"
                yield run.ev("notice", message=budget_stop[0].upper() + budget_stop[1:])
                break
            yield run.ev("stage", stage="downshift", tier_key=rung["key"], message=f"Trying {rung['label']}")
            base = await run.evaluate(rung, plain, [], task["train"], "train", "plain")
            yield run.ev("eval", **_public_eval(base))
            holdout_est = est(rung, len(task["holdout"]), base)
            train_est = est(rung, len(task["train"]), base)
            best = {"system": plain, "fewshots": [], "score": base["score"], "failures": base["failures"],
                    "label": "plain", "fewshot_ids": []}
            history = [best]
            if carry and best["score"] < train_bar and train_est + holdout_est <= run.remaining():
                rows = _train_rows_excluding(task["train"], carry["fewshots"])
                e = await run.evaluate(rung, carry["system"], carry["fewshots"], rows, "train", "carried")
                yield run.ev("eval", **_public_eval(e))
                cand = {"system": carry["system"], "fewshots": carry["fewshots"], "score": e["score"],
                        "failures": e["failures"], "label": "carried", "fewshot_ids": e["fewshot_ids"]}
                history.append(cand)
                if cand["score"] > best["score"]:
                    best = cand
            rnd = 0
            while best["score"] < train_bar and rnd < int(opts["rounds"]):
                round_est = propose_cost[0] + int(opts["candidates"]) * train_est
                if round_est + holdout_est > run.remaining():
                    budget_stop = f"stopped rewriting prompts for {rung['label']} to stay under the ${meter.cap_usd:.2f} cap"
                    yield run.ev("notice", message=budget_stop[0].upper() + budget_stop[1:])
                    break
                rnd += 1
                before = meter.by_model.get(ULTRA, 0.0)
                cands = await _propose(run, rung, best, history, int(opts["candidates"]))
                propose_cost[0] = max(propose_cost[0] * 0.5, meter.by_model.get(ULTRA, 0.0) - before)
                if not cands:
                    yield run.ev("round", tier_key=rung["key"], round=rnd, candidates=[], best_score=best["score"],
                                 message=run.last_propose_problem or "Ultra returned no usable candidates")
                    continue
                results = await asyncio.gather(*(
                    run.evaluate(rung, c["system"], c["fewshots"],
                                 _train_rows_excluding(task["train"], c["fewshots"]), "train", f"r{rnd}c{i + 1}")
                    for i, c in enumerate(cands)))
                shown = []
                for c, e in zip(cands, results):
                    entry = {"system": c["system"], "fewshots": c["fewshots"], "score": e["score"],
                             "failures": e["failures"], "label": e["label"], "fewshot_ids": e["fewshot_ids"]}
                    history.append(entry)
                    shown.append({"label": e["label"], "score": e["score"], "n": e["n"], "note": c["note"],
                                  "fewshot_ids": e["fewshot_ids"], "system_preview": c["system"][:240],
                                  "cost_per_1k_usd": e["cost_per_1k_usd"], "truncated": e["truncated"]})
                    if e["score"] > best["score"]:
                        best = entry
                yield run.ev("round", tier_key=rung["key"], round=rnd, candidates=shown, best_score=best["score"],
                             best_label=best["label"], train_bar=round(train_bar, 4))
            if holdout_est > run.remaining():
                budget_stop = f"stopped before scoring {rung['label']} on held-out rows (${meter.cap_usd:.2f} cap)"
                yield run.ev("notice", message=budget_stop[0].upper() + budget_stop[1:])
                break
            ho = await run.evaluate(rung, best["system"], best["fewshots"], task["holdout"], "holdout", best["label"])
            yield run.ev("eval", **_public_eval(ho))
            passed = ho["score"] >= bar - 1e-9
            yield run.ev("tier_done", tier_key=rung["key"], holdout_score=ho["score"], bar=round(bar, 4),
                         passed=passed, prompt_label=best["label"])
            if best_cheap is None or ho["score"] > best_cheap["score"]:
                best_cheap = ho
            carry = best if best["label"] != "plain" else carry
            if passed:
                chosen = {"holdout": ho, "system": best["system"], "fewshots": best["fewshots"]}
                break

        # Ultra with thinking off is itself a cheaper mode than the reference; it is already measured.
        if not chosen and ref is ultra_on_ho and ultra_off_ho["score"] >= bar - 1e-9:
            chosen = {"holdout": ultra_off_ho, "system": plain, "fewshots": []}
            yield run.ev("tier_done", tier_key=ULTRA_OFF["key"], holdout_score=ultra_off_ho["score"],
                         bar=round(bar, 4), passed=True, prompt_label="plain")

        # 5. Verdict.
        verdict = _verdict(run, ref, {"ultra": ultra_off_ho, "ultra-think": ultra_on_ho}, chosen, best_cheap, tol,
                           bar, budget_stop)
        verdict["ultra_train_outputs"] = run.ultra_outputs(ULTRA_OFF, plain, task["train"])
        yield run.ev("verdict", verdict=verdict)
        yield run.ev("done", ok=True, spend_usd=round(llm.meter.spent_usd, 6), calls=llm.meter.calls)
    except BudgetExceeded as e:
        yield run.ev("aborted", reason=str(e))
        yield run.ev("done", ok=False, spend_usd=round(llm.meter.spent_usd, 6), calls=llm.meter.calls)
    except Exception as e:  # noqa: BLE001
        yield run.ev("error", message=f"{type(e).__name__}: {str(e)[:300]}")
        yield run.ev("done", ok=False, spend_usd=round(llm.meter.spent_usd, 6), calls=llm.meter.calls)
