"""Measure the Ultra reference for a preset and cache it in presets/<slug>/reference.json.

    python scripts/measure_reference.py support-triage                 # 3 repeats, thinking off and on
    python scripts/measure_reference.py receipt-extract --max-usd 1.5  # stop before spending more than this
    python scripts/measure_reference.py contract-clauses --estimate    # only print the cost estimate

Ultra runs the plain prompt (the same prompt and max_tokens the engine uses) on every held-out row, with thinking
off and with thinking on, `--repeats` times each (temperature 0 is not deterministic on Token Factory, so repeats
show how much Ultra moves on its own). It also runs once on the validation rows with thinking off; the engine uses
that score only as the point where prompt rewriting can stop early.

The saved file keeps per-repeat scores, the per-row scores and outputs for every repeat, cost, latency, the date
and model ids, so every reference number can be re-checked. Every call is logged to runs/usage.jsonl.

Before the expensive part the script measures one thinking-off pass and a 10-row thinking-on probe, extrapolates
the full cost and stops if it would pass --max-usd.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from downshift.engine import max_tokens_for, prompt_sha, rung_max_tokens, score_calls  # noqa: E402
from downshift.grading import Grader  # noqa: E402
from downshift.models import LLM, ULTRA_OFF, ULTRA_ON, Meter, get_pricing  # noqa: E402
from downshift.tasks import PRESETS_DIR, build_messages, load_preset, plain_system  # noqa: E402

PROBE_ROWS = 10


async def _calls(llm: LLM, rung: dict, system: str, rows: list[dict], max_tokens: int, purpose: str):
    return await asyncio.gather(*(llm.chat(rung["model"], build_messages(system, [], r["input"]),
                                           max_tokens=max_tokens, purpose=purpose, thinking=rung["thinking"])
                                  for r in rows))


async def measure(slug: str, repeats: int, max_usd: float, estimate_only: bool, concurrency: int) -> dict | None:
    task = load_preset(slug)
    for split in ("train", "validation", "holdout"):
        task[split] = [dict(r, id=str(r["id"])) for r in task[split]]
    plain = plain_system(task)
    answer_tokens = max_tokens_for(task)
    rows, ids = task["holdout"], [r["id"] for r in task["holdout"]]
    llm = LLM(run_id=f"reference-{slug}", meter=Meter(cap_usd=max_usd), concurrency=concurrency)
    grader = Grader()
    t0 = time.time()
    mt = {r["key"]: rung_max_tokens(answer_tokens, r) for r in (ULTRA_OFF, ULTRA_ON)}

    # Pass 1 of thinking off, and a probe of thinking on, to estimate the rest.
    off1, probe = await asyncio.gather(
        _calls(llm, ULTRA_OFF, plain, rows, mt["ultra"], "reference:off:r1"),
        _calls(llm, ULTRA_ON, plain, rows[:PROBE_ROWS], mt["ultra-think"], "reference:on:probe"))
    off_per = sum(c.cost_usd for c in off1) / len(off1)
    on_per = sum(c.cost_usd for c in probe) / len(probe)
    off_rest = off_per * len(rows) * (repeats - 1) + off_per * len(task["validation"])
    one_on = on_per * len(rows) * 1.15
    est_total = llm.meter.spent_usd + off_rest + one_on * repeats - on_per * PROBE_ROWS * 1.15
    est_min = llm.meter.spent_usd + off_rest + one_on - on_per * PROBE_ROWS * 1.15
    print(f"{slug}: {len(rows)} held-out rows; spent ${llm.meter.spent_usd:.4f} so far; "
          f"thinking off ${off_per * 1000:.3f}/1k calls, thinking on ${on_per * 1000:.3f}/1k calls (probe of "
          f"{PROBE_ROWS}); estimated total ${est_total:.3f} for {repeats} repeats of each", flush=True)
    if estimate_only:
        return None
    if est_min > max_usd:
        print(f"  stopping: even one thinking-on repeat would pass --max-usd {max_usd:.2f}")
        return None

    # The rest of thinking off, the validation pass and thinking-on repeat 1, concurrently.
    jobs = {("ultra", 1): off1}
    pending = [(("ultra", k), _calls(llm, ULTRA_OFF, plain, rows, mt["ultra"], f"reference:off:r{k}"))
               for k in range(2, repeats + 1)]
    pending.append((("ultra-think", 1), _calls(llm, ULTRA_ON, plain, rows[PROBE_ROWS:], mt["ultra-think"],
                                               "reference:on:r1")))
    val_job = _calls(llm, ULTRA_OFF, plain, task["validation"], mt["ultra"], "reference:off:validation")
    results = await asyncio.gather(*(c for _, c in pending), val_job)
    for (key, _), res in zip(pending, results[:-1]):
        jobs[key] = list(probe) + list(res) if key == ("ultra-think", 1) else res
    val_calls = results[-1]
    # Further thinking-on repeats one at a time, each only if its measured cost still fits --max-usd.
    n_on = 1
    while n_on < repeats:
        on_calls = [c for k in range(1, n_on + 1) for c in jobs[("ultra-think", k)]]
        need = sum(c.cost_usd for c in on_calls) / len(on_calls) * len(rows) * 1.1
        if llm.meter.spent_usd + need > max_usd:
            print(f"  thinking on: stopping after {n_on} repeats; another would cost about ${need:.3f} and pass "
                  f"--max-usd {max_usd:.2f}", flush=True)
            break
        n_on += 1
        jobs[("ultra-think", n_on)] = await _calls(llm, ULTRA_ON, plain, rows, mt["ultra-think"],
                                                   f"reference:on:r{n_on}")
    n_reps = {ULTRA_OFF["key"]: repeats, ULTRA_ON["key"]: n_on}

    modes = {}
    for rung in (ULTRA_OFF, ULTRA_ON):
        reps = []
        all_calls = []
        for k in range(1, n_reps[rung["key"]] + 1):
            calls = jobs[(rung["key"], k)]
            all_calls += calls
            reps.append(await score_calls(grader, task["checker_code"], rung, rows, calls, "holdout", f"r{k}"))
        ok = [c for c in all_calls if c.ok]
        cost = sum(c.cost_usd for c in all_calls)
        scores = [e["score"] for e in reps]
        modes[rung["key"]] = {
            "rung": rung["key"], "label": rung["label"], "model": rung["model"], "thinking": rung["thinking"],
            "max_tokens": mt[rung["key"]], "repeats": n_reps[rung["key"]], "repeat_scores": scores,
            "mean": round(statistics.mean(scores), 4), "spread": round(max(scores) - min(scores), 4),
            "sd": round(statistics.stdev(scores), 4) if len(scores) > 1 else 0.0,
            "cost_usd": round(cost, 6), "cost_per_1k_usd": round(1000 * cost / max(1, len(ok)), 4),
            "p50_ms": round(statistics.median(c.latency_ms for c in ok), 1) if ok else None,
            "mean_out_tokens": round(sum(c.completion_tokens for c in ok) / max(1, len(ok)), 1),
            "truncated": sum(1 for c in all_calls if c.truncated), "errors": len(all_calls) - len(ok),
            "graded_by": sorted({e["grader"] for e in reps}),
            "per_row": {i: [e["row_scores"][i] for e in reps] for i in ids},
            "per_row_outputs": {i: [e["outputs"][i] for e in reps] for i in ids},
        }
        # Rows where repeats disagree: the source of the run-to-run noise.
        modes[rung["key"]]["rows_unstable"] = sum(1 for i in ids if len(set(modes[rung["key"]]["per_row"][i])) > 1)
        print(f"  {rung['label']}: repeats {[round(s * 100, 1) for s in scores]}, mean {statistics.mean(scores) * 100:.1f}%, "
              f"{modes[rung['key']]['rows_unstable']} rows changed between repeats, ${cost:.4f}")
    val = await score_calls(grader, task["checker_code"], ULTRA_OFF, task["validation"], val_calls, "validation",
                            "plain")
    out = {
        "slug": slug, "measured_on": date.today().isoformat(), "measured_at": time.time(),
        "repeats": min(n_reps.values()), "repeats_by_mode": n_reps,
        "n_holdout": len(rows), "holdout_ids": ids, "plain_prompt_sha": prompt_sha(plain),
        "answer_max_tokens": answer_tokens, "temperature": 0.0, "pricing_source": get_pricing()["_source"],
        "modes": modes,
        "validation": {"rung": ULTRA_OFF["key"], "n": val["n"], "score": val["score"],
                       "cost_usd": val["cost_usd"]},
        "total_cost_usd": round(llm.meter.spent_usd, 6), "wall_s": round(time.time() - t0, 1),
    }
    path = PRESETS_DIR / slug / "reference.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}: total ${out['total_cost_usd']:.4f}, {out['wall_s']} s")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="+")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--max-usd", type=float, default=1.5, help="per preset; stops before passing it")
    ap.add_argument("--estimate", action="store_true", help="measure the probe and print the estimate only")
    ap.add_argument("--concurrency", type=int, default=16)
    a = ap.parse_args()
    for slug in a.slugs:
        asyncio.run(measure(slug, a.repeats, a.max_usd, a.estimate, a.concurrency))


if __name__ == "__main__":
    main()
