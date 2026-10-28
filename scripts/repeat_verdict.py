"""Re-measure a run's verdict prompt on the held-out rows, several times, the way the Ultra reference is measured.

    python scripts/repeat_verdict.py demo_runs/45015b6df59e.json --repeats 3

A ladder run scores each cheaper rung once, while the cached Ultra reference is a 3-run mean. This script closes that
gap for a verdict: it runs the verdict's exact model, thinking flag, system prompt and few-shot examples on every
held-out row `--repeats` times, grades each pass in Nebius Sandboxes, and compares the per-row mean with Ultra's per-row
mean using the same paired bootstrap as the engine. The result is saved next to the run as <run id>.repeats.json.
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

from downshift.engine import max_tokens_for, rung_max_tokens, score_calls  # noqa: E402
from downshift.grading import Grader  # noqa: E402
from downshift.models import LLM, Meter, rung_by_key  # noqa: E402
from downshift.stats import bootstrap_ci, matches, paired_diff_ci  # noqa: E402
from downshift.tasks import build_messages, load_preset  # noqa: E402


async def main(run_path: Path, repeats: int, max_usd: float) -> None:
    run = json.loads(run_path.read_text(encoding="utf-8"))
    v = run["verdict"]
    slug = run["events"][0]["task"]["slug"]
    task = load_preset(slug)
    rows = [dict(r, id=str(r["id"])) for r in task["holdout"]]
    ref = json.loads((ROOT / "presets" / slug / "reference.json").read_text(encoding="utf-8"))
    ref_mode = ref["modes"][v["reference"]["rung"]]
    rung = rung_by_key(v["rung"])
    assert rung["model"] == v["model"] and rung["thinking"] == v["thinking"], "verdict rung changed"
    max_tokens = rung_max_tokens(max_tokens_for(task), rung)
    llm = LLM(run_id=f"repeat-{run['run_id']}", meter=Meter(cap_usd=max_usd), concurrency=16)
    grader = Grader()
    t0 = time.time()

    reps = []
    for k in range(1, repeats + 1):
        calls = await asyncio.gather(*(llm.chat(rung["model"], build_messages(v["system_prompt"], v["fewshots"], r["input"]),
                                                max_tokens=max_tokens, purpose=f"repeat:r{k}", thinking=rung["thinking"])
                                       for r in rows))
        e = await score_calls(grader, task["checker_code"], rung, rows, calls, "holdout", f"r{k}")
        reps.append(e)
        print(f"  repeat {k}: {e['score']:.4f} on {e['n']} rows, ${e['cost_per_1k_usd']}/1k calls, graded by {e['grader']}",
              flush=True)

    ids = [r["id"] for r in rows]
    per_row = [statistics.mean(e["row_scores"][i] for e in reps) for i in ids]
    ref_row = [statistics.mean(ref_mode["per_row"][i]) for i in ids]  # Ultra's per-row mean over its repeats
    diff = paired_diff_ci(per_row, ref_row)
    scores = [e["score"] for e in reps]
    out = {
        "run_id": run["run_id"], "slug": slug, "measured_on": str(date.today()), "model": rung["model"],
        "thinking": rung["thinking"], "repeats": repeats, "repeat_scores": scores,
        "mean": round(statistics.mean(scores), 4), "spread": round(max(scores) - min(scores), 4),
        "ci": bootstrap_ci(per_row), "reference": {"mode": ref_mode["rung"], "mean": ref_mode["mean"],
                                                   "repeat_scores": ref_mode["repeat_scores"]},
        "diff_vs_reference": diff, "tolerance": v["tolerance"], "matches": matches(diff, v["tolerance"]),
        "cost_per_1k_usd": round(statistics.mean(e["cost_per_1k_usd"] for e in reps), 4),
        "graded_by": sorted({e["grader"] for e in reps}), "spent_usd": round(llm.meter.spent_usd, 4),
        "wall_s": round(time.time() - t0, 1),
        "per_repeat": [{k: e[k] for k in ("score", "cost_usd", "p50_ms", "truncated", "errors", "grade_batch")} for e in reps],
    }
    dest = run_path.with_suffix(".repeats.json")
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"{rung['label']}: {repeats} runs {' / '.join(f'{s:.1%}' for s in scores)} (mean {out['mean']:.1%}); "
          f"Ultra {ref_mode['mean']:.1%}; paired diff {diff['diff'] * 100:+.1f} pts "
          f"(95% CI {diff['lo'] * 100:+.1f} to {diff['hi'] * 100:+.1f}) -> {'matches' if out['matches'] else 'does not match'} "
          f"within {v['tolerance'] * 100:.0f} pts. Spent ${out['spent_usd']}. Saved {dest.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run", type=Path)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--max-usd", type=float, default=0.10)
    a = ap.parse_args()
    asyncio.run(main(a.run, a.repeats, a.max_usd))
