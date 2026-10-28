"""The Downshift pipeline (PLAN.md "How it works").

run_downshift() is an async generator of progress events (plain dicts) so the server can stream them.

Three splits, each with one job:
  tuning (train.jsonl)   the optimiser sees these: plain-prompt failures to fix, few-shot examples to pick
  validation             used only to choose between prompt candidates; never shown to the optimiser
  holdout                each rung's single winning prompt is scored on it once

The reference is Ultra, measured on the full holdout with thinking on and off, 3 repeats each, by
scripts/measure_reference.py and cached in presets/<slug>/reference.json. A run uses the cache by default and can
re-measure Ultra live instead (option reference="live", one repeat).

Verdict rule: a cheaper rung "matches Ultra" only if the lower bound of the 95% paired-bootstrap CI of
(rung score - Ultra score) on the same held-out rows is above -tolerance. It "beats" Ultra only if that lower
bound is above 0. Rungs are tried cheapest first; rungs above the first match are never called.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import statistics
import time
import uuid
from datetime import date
from pprint import pformat

from .grading import Grader
from pathlib import Path

from .models import (CHEAP_RUNGS, PHOTO_RUNG, RUNGS, THINKING_BUDGET, ULTRA, ULTRA_OFF, ULTRA_ON, BudgetExceeded, LLM,
                     Meter, get_pricing, rung_by_key)
from .stats import MIN_ROWS, beats, bootstrap_ci, enough_rows, matches, paired_diff_ci
from .tasks import as_text, build_messages, ensure_labels, photo_messages, plain_system

DEFAULTS = {
    "tolerance": 0.05,       # a rung matches Ultra if the 95% CI of (rung - Ultra) stays above -5 points
    "rounds": 2,             # prompt-rewrite rounds per rung
    "candidates": 2,         # candidates Ultra proposes per round
    "spend_cap_usd": 0.25,   # hard per-run cap
    "fewshot_max": 4,
    "concurrency": 16,
    "reference": "cached",   # "cached": presets/<slug>/reference.json; "live": re-measure Ultra in this run
    "ultra_thinking": True,  # live reference only: also measure Ultra with thinking on, if it fits the cap
    "thinking_rungs": True,  # include Lightning with thinking on as a rung
    "photo_check": True,     # tasks that ship photos: after the verdict, try Super VL reading the photo (no OCR)
    "train_limit": None,
    "validation_limit": None,
    "holdout_limit": None,
}

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


def _pts(x: float) -> str:
    """A difference in points, with sign: +2.7 / -1.3."""
    return f"{x * 100:+.1f}"


def _p50(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def prompt_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def max_tokens_for(task: dict) -> int:
    rows = task["train"] + task.get("validation", []) + task["holdout"]
    refs = [as_text(r["reference"]) for r in rows if r.get("reference") is not None]
    longest = max((len(r) for r in refs), default=400)
    return int(min(1024, max(64, longest // 2 + 48)))


def rung_max_tokens(answer_tokens: int, rung: dict) -> int:
    return answer_tokens + (THINKING_BUDGET if rung["thinking"] else 0)


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

    def __init__(self, task: dict, opts: dict, llm: LLM, grader: Grader, run_id: str, answer_tokens: int):
        self.task, self.opts, self.llm, self.grader, self.run_id = task, opts, llm, grader, run_id
        self.cache: dict = {}
        self.answer_tokens = answer_tokens
        self.checker_code: str | None = None
        self.evals: list[dict] = []
        self.grade_backends: set[str] = set()
        self.last_propose_problem: str | None = None

    def max_tokens(self, rung: dict) -> int:
        return rung_max_tokens(self.answer_tokens, rung)

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
        e = await score_calls(self.grader, self.checker_code, rung, rows, calls, split, label)
        self.grade_backends.add(e["grader"])
        e.update({"system": system, "fewshot_ids": [str(f["id"]) for f in fewshots]})
        self.evals.append(e)
        return e


async def score_calls(grader: Grader, checker_code: str, rung: dict, rows: list[dict], calls: list, split: str,
                      label: str) -> dict:
    """Grade one batch of model outputs; returns an eval record with per-row scores."""
    items = [{"id": r["id"], "input": r["input"], "output": c.text, "reference": r["reference"]}
             for r, c in zip(rows, calls)]
    grades, batch = await grader.grade(checker_code, items)
    by_id = {g.item_id: g for g in grades}
    scores, failures, row_scores = [], [], {}
    for it, c in zip(items, calls):
        g = by_id.get(str(it["id"]))
        s = g.score if g else 0.0
        scores.append(s)
        row_scores[str(it["id"])] = round(s, 4)
        if s < 0.999:
            detail = "ran out of tokens before answering" if c.truncated and not c.text else (
                g.detail if g else "not graded")
            failures.append({"id": it["id"], "input": as_text(it["input"])[:400],
                             "expected": as_text(it["reference"])[:200], "got": c.text[:200],
                             "detail": detail[:160]})
    ok_calls = [c for c in calls if c.ok]
    cost = sum(c.cost_usd for c in calls)
    return {
        "model": rung["model"], "tier_key": rung["key"], "thinking": rung["thinking"], "split": split,
        "label": label, "n": len(rows),
        "score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "cost_usd": round(cost, 6),
        "cost_per_1k_usd": round(1000 * cost / len(ok_calls), 4) if ok_calls else None,
        "p50_ms": _p50([c.latency_ms for c in ok_calls]),
        "mean_out_tokens": round(sum(c.completion_tokens for c in ok_calls) / len(ok_calls), 1) if ok_calls else None,
        "errors": len(calls) - len(ok_calls), "truncated": sum(1 for c in calls if c.truncated),
        "grader": batch["backend"], "grade_batch": batch,
        "row_scores": row_scores, "outputs": {str(r["id"]): c.text for r, c in zip(rows, calls)},
        "failures": failures[:6],
    }


def _public_eval(e: dict) -> dict:
    out = {k: v for k, v in e.items() if k not in ("system", "failures", "outputs", "row_scores")}
    out |= {"system_preview": e["system"][:240], "failures_shown": e["failures"][:3]}
    if e["split"] == "holdout":
        out["row_scores"] = e["row_scores"]  # kept so every held-out number can be re-checked from the saved run
    return out


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


async def validate_checker(grader: Grader, rows_all: list[dict], code: str) -> dict:
    rows = [r for r in rows_all if r.get("reference") is not None][:12]
    items = []
    for i, r in enumerate(rows):
        items.append({"id": f"{r['id']}::ref", "input": r["input"], "output": as_text(r["reference"]),
                      "reference": r["reference"]})
        wrong = next((o for o in rows[i + 1:] + rows[:i] if as_text(o["reference"]) != as_text(r["reference"])), None)
        items.append({"id": f"{r['id']}::wrong", "input": r["input"],
                      "output": as_text(wrong["reference"]) if wrong else "", "reference": r["reference"]})
    grades, batch = await grader.grade(code, items)
    backend = batch["backend"]
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


async def _validate_checker(run: Run, code: str) -> dict:
    v = await validate_checker(run.grader, run.task["train"], code)
    run.grade_backends.add(v["backend"])
    return v


async def _propose(run: Run, rung: dict, best: dict, history: list[dict], k: int) -> list[dict]:
    """Ultra proposes prompts. It sees tuning rows and tuning scores only, never validation or held-out rows."""
    t = run.task
    fails = "\n".join(f"- input: {f['input'][:300]}\n  expected: {f['expected']}\n  got: {f['got'] or '(no answer)'}"
                      + (f"\n  note: {f['detail']}" if f.get("detail") else "")
                      for f in best["failures"][:5]) or "(none)"
    pool = "\n".join(f"- {r['id']}: {as_text(r['input'])[:90]}" for r in t["train"][:60])
    tried = "\n".join(f"- score {h['score']:.2f}, shots {h['fewshot_ids']}: {h['system'][:160]!r}"
                      for h in history[-6:]) or "(none)"
    labels_note = "; do NOT repeat the allowed label list, it is appended automatically" if t.get("label_set") else ""
    prompt = (
        f"A small model ({rung['label']}) must do this task as well as a much larger model.\n"
        f"Task: {t['instruction'][:1500]}\nOutput format: {t.get('output_format') or 'free text'}\n"
        f"\nCurrent best system prompt (score {best['score']:.2f} on training rows):\n{best['system']}\n\n"
        f"Its mistakes:\n{fails}\n\nAlready tried:\n{tried}\n\n"
        f"Training examples you may use as few-shot demonstrations (by id):\n{pool}\n\n"
        f"Propose {k} different improved candidates. Each has a complete system prompt (at most 1200 characters; "
        f"keep the output format rules{labels_note}) and 0-{run.opts['fewshot_max']} few-shot example ids from the "
        "list. Prefer general rules over rules that only fix the listed mistakes: the prompt will be judged on "
        "different examples. Make them differ in approach (for example, one clearer zero-shot rule set, one with "
        "targeted examples).\n"
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


# --------------------------------------------------------------------------------------------- reference

def mode_summary(key: str, ids: list[str], per_row: dict, repeat_scores: list[float], extra: dict) -> dict:
    """Summary of one Ultra mode: per-row mean over repeats, bootstrap CI over rows, repeat-to-repeat spread."""
    rung = rung_by_key(key)
    rows = [sum(per_row[i]) / len(per_row[i]) if isinstance(per_row[i], list) else per_row[i] for i in ids]
    ci = bootstrap_ci(rows)
    return {"rung": key, "label": rung["label"], "model": rung["model"], "thinking": rung["thinking"],
            "mean": ci["mean"], "ci": ci, "repeat_scores": [round(x, 4) for x in repeat_scores],
            "spread": round(max(repeat_scores) - min(repeat_scores), 4) if repeat_scores else 0.0,
            "rows": rows, **{k: extra.get(k) for k in ("cost_per_1k_usd", "p50_ms", "mean_out_tokens", "truncated")}}


def cached_reference(task: dict, plain: str, ids: list[str]) -> tuple[dict | None, str | None]:
    c = task.get("cached_reference")
    if not c:
        return None, "this task has no cached Ultra reference"
    if c.get("plain_prompt_sha") != prompt_sha(plain):
        return None, "the cached Ultra reference was measured with a different prompt"
    modes = {}
    for key, m in c.get("modes", {}).items():
        if not all(i in m["per_row"] for i in ids):
            return None, "the cached Ultra reference does not cover these held-out rows"
        modes[key] = mode_summary(key, ids, m["per_row"], m["repeat_scores"], m)
    if "ultra" not in modes:
        return None, "the cached Ultra reference has no thinking-off measurement"
    return {"source": "cached", "measured_on": c.get("measured_on"), "repeats": c.get("repeats"),
            "measure_cost_usd": c.get("total_cost_usd"), "modes": modes,
            "validation_target": (c.get("validation") or {}).get("score")}, None


def pick_reference(modes: dict) -> str:
    """Ultra with thinking on (its default) is the reference, unless thinking off scores higher on average."""
    on, off = modes.get(ULTRA_ON["key"]), modes[ULTRA_OFF["key"]]
    return ULTRA_ON["key"] if on and on["mean"] >= off["mean"] else ULTRA_OFF["key"]


def _ci_text(ci: dict) -> str:
    return f"95% CI {_pct(ci['lo'])} to {_pct(ci['hi'])}"


def _diff_text(d: dict) -> str:
    return f"{_pts(d['diff'])} points (95% CI {_pts(d['lo'])} to {_pts(d['hi'])})"


def compare_sentence(d: dict, tol: float) -> str:
    """Wording tied to the verdict rule. Never says 'beats' unless the CI lower bound is above zero."""
    if beats(d):
        return f"better than Ultra by {_diff_text(d)}"
    if matches(d, tol):
        return f"matches Ultra within {tol * 100:.0f} points (95% CI of the difference {_pts(d['lo'])} to {_pts(d['hi'])})"
    if not enough_rows(d):
        return (f"not judged: only {d['n']} held-out rows (at least {MIN_ROWS} are needed); difference {_diff_text(d)}")
    return (f"not shown to match Ultra within {tol * 100:.0f} points: difference {_diff_text(d)}")


# --------------------------------------------------------------------------------------------- verdict

def _verdict(run: Run, ref_info: dict, ref_key: str, rungs: dict, chosen_key: str | None, tol: float,
             budget_stop: str | None, started: float) -> dict:
    t = run.task
    ref = ref_info["modes"][ref_key]
    n = len(ref["rows"])
    ref_cost = ref["cost_per_1k_usd"]
    ref_desc = (f"Ultra ({'thinking on' if ref['thinking'] else 'thinking off'}) "
                + (f"measured on {ref_info['measured_on']}, {len(ref['repeat_scores'])} runs, mean {_pct(ref['mean'])}"
                   + (f" (runs ranged {_pct(min(ref['repeat_scores']))} to {_pct(max(ref['repeat_scores']))})"
                      if len(ref["repeat_scores"]) > 1 else "")))
    if chosen_key:
        c = rungs[chosen_key]
        rung = rung_by_key(chosen_key)
        system, shots = c["system"], c["fewshots"]
        cost = c["cost_per_1k_usd"]
        savings = round(ref_cost / cost, 1) if cost and ref_cost else None
        kind = "downshift"
        how = "this prompt" if (shots or system != plain_system(t)) else "the plain prompt"
        if chosen_key == ULTRA_OFF["key"]:
            sentence = (f"Turn thinking off: Ultra with thinking off scored {_pct(c['holdout_score'])} on {n} held-out "
                        f"rows (3-run mean), which {compare_sentence(c['diff'], tol)} with thinking on, at "
                        f"${cost:.4f} per 1k calls instead of ${ref_cost:.4f}"
                        + (f" ({savings}x cheaper)." if savings and savings > 1 else ".")
                        + " No smaller model was shown to match.")
        else:
            sentence = (f"Use {rung['label']} with {how}: {_pct(c['holdout_score'])} on {n} held-out rows "
                        f"({_ci_text(c['ci'])}, one run), which {compare_sentence(c['diff'], tol)}, at ${cost:.4f} "
                        f"per 1k calls instead of ${ref_cost:.4f}"
                        + (f" ({savings}x cheaper)." if savings and savings > 1 else "."))
        score, p50 = c["holdout_score"], c["p50_ms"]
    else:
        rung = rung_by_key(ref_key)
        system, shots = plain_system(t), []
        cost, savings, kind = ref_cost, 1.0, "finetune"
        score, p50 = ref["mean"], ref["p50_ms"]
        tried = [rungs[k] for k in rungs if rungs[k].get("diff")]
        if tried:
            best = max(tried, key=lambda x: x["diff"]["lo"])
            sentence = (f"No cheaper rung could be shown to match Ultra within {tol * 100:.0f} points (95% CI). "
                        f"Closest: {rung_by_key(best['rung'])['label']} at {_pct(best['holdout_score'])}, difference "
                        f"{_diff_text(best['diff'])}. Keep Ultra for now; fine-tuning a small model is the next step.")
        else:
            sentence = "No cheaper rung was scored on held-out rows in this run, so keep Ultra."
        if budget_stop:
            sentence += f" Tuning {budget_stop}, so a larger cap may do better."
    table = []
    for r in [x for x in CHEAP_RUNGS] + [ULTRA_OFF, ULTRA_ON]:
        k = r["key"]
        row = {"tier_key": k, "name": r["name"], "label": r["label"], "model": r["model"], "thinking": r["thinking"]}
        if k in ref_info["modes"]:
            m = ref_info["modes"][k]
            row.update({"holdout_score": m["mean"], "ci": m["ci"], "repeat_scores": m["repeat_scores"],
                        "spread": m["spread"], "cost_per_1k_usd": m["cost_per_1k_usd"], "p50_ms": m["p50_ms"],
                        "is_reference": k == ref_key, "source": ref_info["source"]})
            if k != ref_key:
                row["diff"] = paired_diff_ci(m["rows"], ref["rows"])
        elif k in rungs:
            row.update({x: rungs[k].get(x) for x in ("plain_tuning", "tuning_score", "validation_score",
                                                       "holdout_score", "ci", "diff", "cost_per_1k_usd", "p50_ms",
                                                       "prompt_label", "passed", "truncated")})
        table.append(row)
    return {
        "kind": kind, "thinking_off_only": chosen_key == ULTRA_OFF["key"], "model": rung["model"], "rung": rung["key"], "tier_name": rung["name"],
        "rung_label": rung["label"], "thinking": rung["thinking"], "system_prompt": system,
        "fewshots": [{"id": s["id"], "input": s["input"], "reference": s["reference"]} for s in shots],
        "holdout_score": score, "holdout_ci": rungs[chosen_key]["ci"] if chosen_key else ref["ci"],
        "diff_vs_reference": rungs[chosen_key]["diff"] if chosen_key else None,
        "overfit": ({x: rungs[chosen_key].get(x) for x in ("tuning_score", "validation_score", "holdout_score")}
                    if chosen_key else None),
        "holdout_n": n, "tolerance": tol,
        "reference": {"rung": ref_key, "label": ref["label"], "thinking": ref["thinking"], "mean": ref["mean"],
                      "ci": ref["ci"], "repeat_scores": ref["repeat_scores"], "spread": ref["spread"],
                      "source": ref_info["source"], "measured_on": ref_info["measured_on"],
                      "repeats": len(ref["repeat_scores"]), "description": ref_desc},
        "ultra_holdout_score": ref["mean"],
        "cost_per_1k_usd": cost, "ultra_cost_per_1k_usd": ref_cost, "savings_multiple": savings,
        "p50_latency_ms": p50, "ultra_p50_latency_ms": ref["p50_ms"],
        "sentence": sentence,
        "snippet": make_snippet(rung["model"], system, shots, run.max_tokens(rung), rung["thinking"]),
        "finetune_offer": kind == "finetune",
        "budget_stop": budget_stop,
        "run_spend_usd": round(run.llm.meter.spent_usd, 6),
        "run_seconds": round(time.time() - started, 1),
        "graded_by": sorted(run.grade_backends), "table": table,
    }


def photo_system(task: dict, verdict: dict) -> tuple[str, list[dict], str]:
    """The prompt for the photo check: the verdict's tuned prompt and few-shots if a cheaper rung won with one,
    else the plain prompt. Its wording about OCR text is swapped for the photo."""
    p = task["photos"]
    tuned = (verdict["kind"] == "downshift" and not verdict["thinking_off_only"]
             and (verdict["fewshots"] or verdict["system_prompt"] != plain_system(task)))
    system = verdict["system_prompt"] if tuned else plain_system(task)
    shots = verdict["fewshots"] if tuned else []
    if p.get("text_says") and p["text_says"] in system:
        system = system.replace(p["text_says"], p["photo_says"])
    else:
        system = p["lead"] + system
    return system, shots, "the verdict's prompt" if tuned else "the plain prompt"


def _photo_rows(task: dict) -> list[tuple[dict, Path]] | None:
    d = Path(task["photos"]["dir"])
    rows = [(r, d / f"{r['id']}.jpg") for r in task["holdout"]]
    return rows if rows and all(p.exists() for _, p in rows) else None


async def _photo_check(run: Run, verdict: dict, ref: dict, ho_ids: list[str], pricing: dict):
    """Does a vision model reading the photo match Ultra reading the text? Scored once on the held-out rows."""
    task, rung, tol = run.task, PHOTO_RUNG, verdict["tolerance"]
    rows = _photo_rows(task)
    if not rows:
        yield run.ev("skipped", tier_key=rung["key"], message="Skipped the photo check: this server does not have the task's photos.")
        return
    system, shots, how = photo_system(task, verdict)
    image_tokens = 1100  # a 1280 px receipt photo, measured
    p = pricing["models"].get(rung["model"], {})
    est = len(rows) * ((image_tokens + len(system) // 3) * p.get("prompt", 3e-7) + run.max_tokens(rung) * p.get("completion", 9e-7))
    if est > run.remaining():
        yield run.ev("skipped", tier_key=rung["key"],
                     message=f"Skipped the photo check: it would cost about ${est:.3f}, more than is left of this run's cap.")
        return
    yield run.ev("stage", stage="photo", tier_key=rung["key"],
                 message=f"Photo check: {rung['name']} reads the {len(rows)} held-out photos, no OCR, with {how}")
    calls = await asyncio.gather(*(run.llm.chat(rung["model"], photo_messages(system, shots, path.read_bytes()),
                                                max_tokens=run.max_tokens(rung), purpose="eval:holdout:photo",
                                                thinking=False) for _, path in rows))
    e = await score_calls(run.grader, run.checker_code, rung, [r for r, _ in rows], calls, "holdout", "photo")
    run.grade_backends.add(e["grader"])
    e.update({"system": system, "fewshot_ids": [str(f["id"]) for f in shots]})
    run.evals.append(e)
    yield run.ev("eval", **_public_eval(e))
    scores = [e["row_scores"][i] for i in ho_ids]
    diff = paired_diff_ci(scores, ref["rows"])
    yield run.ev("photo", tier_key=rung["key"], model=rung["model"], label=rung["label"], prompt=how,
                 holdout_score=e["score"], ci=bootstrap_ci(scores), diff=diff, passed=matches(diff, tol),
                 beats=beats(diff), tolerance=tol, cost_per_1k_usd=e["cost_per_1k_usd"], p50_ms=e["p50_ms"],
                 reference_label=ref["label"], reference_cost_per_1k_usd=ref["cost_per_1k_usd"],
                 verdict_cost_per_1k_usd=verdict["cost_per_1k_usd"],
                 message=f"Photo check, {rung['label']}: {_pct(e['score'])} with no OCR step, "
                         f"{compare_sentence(diff, tol)} (Ultra reads the OCR text)")


# --------------------------------------------------------------------------------------------- pipeline

async def run_downshift(task: dict, options: dict | None = None, llm: LLM | None = None,
                        grader: Grader | None = None, run_id: str | None = None):
    started = time.time()
    opts = {**DEFAULTS, **{k: v for k, v in (options or {}).items() if v is not None}}
    run_id = run_id or uuid.uuid4().hex[:10]
    llm = llm or LLM(run_id=run_id, meter=Meter(cap_usd=float(opts["spend_cap_usd"])),
                     concurrency=int(opts["concurrency"]))
    grader = grader or Grader()
    task = dict(task)
    task.setdefault("validation", [])
    answer_tokens = max_tokens_for(task)  # from the full task, so it matches the cached reference
    for split in ("train", "validation", "holdout"):
        task[split] = [dict(r, id=str(r["id"])) for r in task[split]]
    if not task["validation"]:  # e.g. an old two-split task: carve validation rows out of the tuning rows
        cut = max(2, len(task["train"]) // 3)
        task["validation"], task["train"] = task["train"][:cut], task["train"][cut:]
    for split in ("train", "validation", "holdout"):
        task[split] = task[split][: opts[f"{split}_limit"] or None]
    run = Run(task, opts, llm, grader, run_id, answer_tokens)
    tol = float(opts["tolerance"])
    plain = plain_system(task)
    cheap_rungs = [r for r in CHEAP_RUNGS if opts["thinking_rungs"] or not r["thinking"]]
    ho_ids = [r["id"] for r in task["holdout"]]

    pricing = get_pricing()

    def price(r, k):
        return round(pricing["models"].get(r["model"], {}).get(k, 0) * 1e6, 4)

    photo = bool(task.get("photos") and opts["photo_check"])
    yield run.ev("started", run_id=run_id, task={k: task.get(k) for k in ("slug", "title", "business_story")},
                 n_train=len(task["train"]), n_validation=len(task["validation"]), n_holdout=len(task["holdout"]),
                 options=opts, row_points=round(100 / max(1, len(task["holdout"])), 2),
                 tiers=[r | {"price_in_per_m": price(r, "prompt"), "price_out_per_m": price(r, "completion")}
                        for r in cheap_rungs + [ULTRA_OFF, ULTRA_ON] + ([PHOTO_RUNG] if photo else [])],
                 pricing_source=pricing["_source"])
    if len(task["holdout"]) < MIN_ROWS:
        yield run.ev("notice", message=f"Only {len(task['holdout'])} held-out rows: Downshift needs at least {MIN_ROWS} "
                                       f"to call a match, so this run can only say keep Ultra. Add more examples "
                                       f"(held out = 40% of them).")
    try:
        # 1. Reference outputs from Ultra where none were given.
        missing = [r for r in task["train"] + task["validation"] + task["holdout"] if r.get("reference") is None]
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

        # 3. The reference: cached (3 repeats per thinking mode) unless asked to re-measure, or none is cached.
        ref_info, why_live = None, None
        if opts["reference"] != "live":
            ref_info, why_live = cached_reference(task, plain, ho_ids)
        if ref_info is None:
            yield run.ev("stage", stage="bar", tier_key=ULTRA_OFF["key"],
                         message="Measuring Ultra live on the held-out rows" + (f" ({why_live})" if why_live else ""))
            off_ho, off_val = await asyncio.gather(
                run.evaluate(ULTRA_OFF, plain, [], task["holdout"], "holdout", "plain"),
                run.evaluate(ULTRA_OFF, plain, [], task["validation"], "validation", "plain"))
            yield run.ev("eval", **_public_eval(off_ho))
            yield run.ev("eval", **_public_eval(off_val))
            modes = {ULTRA_OFF["key"]: mode_summary(ULTRA_OFF["key"], ho_ids, off_ho["row_scores"],
                                                     [off_ho["score"]], off_ho)}
            on_est = off_ho["cost_usd"] * 6
            if opts["ultra_thinking"] and off_ho["cost_usd"] * 1.5 + 0.03 <= run.remaining():
                yield run.ev("stage", stage="bar", tier_key=ULTRA_ON["key"],
                             message="Measuring Ultra with thinking on (its default mode)")
                probe = await run.outputs(ULTRA_ON, plain, [], task["holdout"][:5], purpose="eval:holdout:probe")
                on_est = sum(c.cost_usd for c in probe) / max(1, len(probe)) * (len(ho_ids) - 5) * 1.2
                if on_est + 0.03 <= run.remaining():
                    on_ho = await run.evaluate(ULTRA_ON, plain, [], task["holdout"], "holdout", "plain")
                    yield run.ev("eval", **_public_eval(on_ho))
                    modes[ULTRA_ON["key"]] = mode_summary(ULTRA_ON["key"], ho_ids, on_ho["row_scores"],
                                                          [on_ho["score"]], on_ho)
            if opts["ultra_thinking"] and ULTRA_ON["key"] not in modes:
                yield run.ev("skipped", tier_key=ULTRA_ON["key"],
                             message=f"Skipped Ultra with thinking on: the held-out pass would cost about ${on_est:.3f}, "
                                     f"which does not fit this run's ${llm.meter.cap_usd:.2f} cap.")
            ref_info = {"source": "live", "measured_on": date.today().isoformat(), "repeats": 1, "modes": modes,
                        "validation_target": off_val["score"], "measure_cost_usd": None}
        ref_key = pick_reference(ref_info["modes"])
        ref = ref_info["modes"][ref_key]
        target = ref_info.get("validation_target")
        target = ref["mean"] if target is None else target
        yield run.ev("bar", reference=ref_key, reference_source=ref_info["source"],
                     measured_on=ref_info["measured_on"], repeats=len(ref["repeat_scores"]),
                     ultra_holdout=ref["mean"], ultra_ci=ref["ci"], repeat_scores=ref["repeat_scores"],
                     spread=ref["spread"], tolerance=tol, validation_target=round(target, 4),
                     modes={k: {x: m[x] for x in ("mean", "ci", "repeat_scores", "spread", "cost_per_1k_usd",
                                                  "p50_ms", "label", "thinking")}
                            for k, m in ref_info["modes"].items()},
                     row_points=round(100 / max(1, len(ho_ids)), 2))

        # 4. Downshift loop, cheapest rung first. Each rung is measured only when we reach it.
        rungs: dict = {}
        chosen_key, carry, budget_stop = None, None, None
        meter = llm.meter
        ultra_per_call = (ref_info["modes"][ULTRA_OFF["key"]]["cost_per_1k_usd"] or 1.0) / 1000
        propose_cost = [0.009]  # replaced by the measured cost of Ultra's proposal calls
        n_tv = len(task["train"]) + len(task["validation"])

        def est(rung: dict, n_rows: int, base: dict | None = None, factor: float = 2.5) -> float:
            if base:
                per = base["cost_usd"] / max(1, base["n"])
            else:
                p_r = pricing["models"].get(rung["model"], {}).get("prompt", 1e-6)
                p_u = pricing["models"].get(ULTRA, {}).get("prompt", 1e-6)
                per = ultra_per_call * (p_r / p_u) * (8 if rung["thinking"] else 1)
            return per * n_rows * factor  # rewritten prompts with examples run ~2x longer

        async def score_candidate(rung, system, shots, label):
            tr, va = await asyncio.gather(
                run.evaluate(rung, system, shots, _train_rows_excluding(task["train"], shots), "train", label),
                run.evaluate(rung, system, shots, task["validation"], "validation", label))
            return {"system": system, "fewshots": shots, "score": tr["score"], "val": va["score"],
                    "failures": tr["failures"], "label": label, "fewshot_ids": tr["fewshot_ids"],
                    "tr": tr, "va": va}

        def better(a, b):  # chosen on validation; tuning score breaks ties; otherwise keep the earlier one
            return (a["val"], a["score"]) > (b["val"], b["score"])

        for rung in cheap_rungs:
            if est(rung, n_tv + len(ho_ids), factor=1.2) > run.remaining():
                budget_stop = f"skipped {rung['label']} to stay under the ${meter.cap_usd:.2f} cap"
                yield run.ev("skipped", tier_key=rung["key"], message=budget_stop[0].upper() + budget_stop[1:])
                continue  # a later rung may still fit
            yield run.ev("stage", stage="downshift", tier_key=rung["key"], message=f"Trying {rung['label']}")
            best = await score_candidate(rung, plain, [], "plain")
            yield run.ev("eval", **_public_eval(best["tr"]))
            yield run.ev("eval", **_public_eval(best["va"]))
            plain_tuning = best["score"]
            holdout_est = est(rung, len(ho_ids), best["tr"])
            cand_est = est(rung, n_tv, best["tr"])
            history = [best]
            if carry and best["val"] < target and cand_est + holdout_est <= run.remaining():
                cand = await score_candidate(rung, carry["system"], carry["fewshots"], "carried")
                yield run.ev("eval", **_public_eval(cand["tr"]))
                yield run.ev("eval", **_public_eval(cand["va"]))
                history.append(cand)
                if better(cand, best):
                    best = cand
            rnd = 0
            while best["val"] < target and rnd < int(opts["rounds"]):
                round_est = propose_cost[0] + int(opts["candidates"]) * cand_est
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
                                 best_val=best["val"], message=run.last_propose_problem or "Ultra returned no usable candidates")
                    continue
                results = await asyncio.gather(*(score_candidate(rung, c["system"], c["fewshots"], f"r{rnd}c{i + 1}")
                                                 for i, c in enumerate(cands)))
                shown = []
                for c, e in zip(cands, results):
                    history.append(e)
                    shown.append({"label": e["label"], "score": e["score"], "val": e["val"], "n": e["tr"]["n"],
                                  "n_val": e["va"]["n"], "note": c["note"], "fewshot_ids": e["fewshot_ids"],
                                  "system_preview": c["system"][:240], "cost_per_1k_usd": e["tr"]["cost_per_1k_usd"],
                                  "truncated": e["tr"]["truncated"] + e["va"]["truncated"]})
                    if better(e, best):
                        best = e
                yield run.ev("round", tier_key=rung["key"], round=rnd, candidates=shown, best_score=best["score"],
                             best_val=best["val"], best_label=best["label"], validation_target=round(target, 4))
            if holdout_est > run.remaining():
                budget_stop = f"stopped before scoring {rung['label']} on held-out rows (${meter.cap_usd:.2f} cap)"
                yield run.ev("notice", message=budget_stop[0].upper() + budget_stop[1:])
                continue
            ho = await run.evaluate(rung, best["system"], best["fewshots"], task["holdout"], "holdout", best["label"])
            yield run.ev("eval", **_public_eval(ho))
            rows = [ho["row_scores"][i] for i in ho_ids]
            diff = paired_diff_ci(rows, ref["rows"])
            passed = matches(diff, tol)
            rungs[rung["key"]] = {
                "rung": rung["key"], "system": best["system"], "fewshots": best["fewshots"],
                "prompt_label": best["label"], "plain_tuning": plain_tuning, "tuning_score": best["score"],
                "validation_score": best["val"], "holdout_score": ho["score"], "ci": bootstrap_ci(rows),
                "diff": diff, "passed": passed, "cost_per_1k_usd": ho["cost_per_1k_usd"], "p50_ms": ho["p50_ms"],
                "truncated": ho["truncated"]}
            yield run.ev("tier_done", tier_key=rung["key"], holdout_score=ho["score"], ci=rungs[rung["key"]]["ci"],
                         diff=diff, passed=passed, beats=beats(diff), tolerance=tol, prompt_label=best["label"],
                         tuning_score=best["score"], validation_score=best["val"],
                         message=f"{rung['label']}: {compare_sentence(diff, tol)}")
            carry = best if best["label"] != "plain" else carry
            if passed:
                chosen_key = rung["key"]
                break

        # Ultra with thinking off is itself a cheaper mode than the reference; it is already measured.
        if not chosen_key and ref_key == ULTRA_ON["key"]:
            off = ref_info["modes"][ULTRA_OFF["key"]]
            diff = paired_diff_ci(off["rows"], ref["rows"])
            if matches(diff, tol):
                chosen_key = ULTRA_OFF["key"]
                rungs[chosen_key] = {"rung": chosen_key, "system": plain, "fewshots": [], "prompt_label": "plain",
                                     "tuning_score": None, "validation_score": None, "holdout_score": off["mean"],
                                     "ci": off["ci"], "diff": diff, "passed": True,
                                     "cost_per_1k_usd": off["cost_per_1k_usd"], "p50_ms": off["p50_ms"]}
                yield run.ev("tier_done", tier_key=chosen_key, holdout_score=off["mean"], ci=off["ci"], diff=diff,
                             passed=True, beats=beats(diff), tolerance=tol, prompt_label="plain",
                             message=f"{ULTRA_OFF['label']}: {compare_sentence(diff, tol)}")

        # 5. Verdict.
        verdict = _verdict(run, ref_info, ref_key, rungs, chosen_key, tol, budget_stop, started)
        yield run.ev("verdict", verdict=verdict)

        # 6. Photo check, for tasks whose text came from photos.
        if photo:
            try:
                async for ev in _photo_check(run, verdict, ref, ho_ids, pricing):
                    yield ev
            except BudgetExceeded as e:
                yield run.ev("skipped", tier_key=PHOTO_RUNG["key"], message=f"Photo check stopped at the spend cap: {e}")
        yield run.ev("done", ok=True, spend_usd=round(llm.meter.spent_usd, 6), calls=llm.meter.calls,
                     seconds=round(time.time() - started, 1))
    except BudgetExceeded as e:
        yield run.ev("aborted", reason=str(e))
        yield run.ev("done", ok=False, spend_usd=round(llm.meter.spent_usd, 6), calls=llm.meter.calls,
                     seconds=round(time.time() - started, 1))
    except Exception as e:  # noqa: BLE001
        yield run.ev("error", message=f"{type(e).__name__}: {str(e)[:300]}")
        yield run.ev("done", ok=False, spend_usd=round(llm.meter.spent_usd, 6), calls=llm.meter.calls,
                     seconds=round(time.time() - started, 1))
