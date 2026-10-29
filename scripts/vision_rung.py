"""Do you even need OCR? Nemotron Super VL reads the receipt photo directly, compared with the text pipeline.

    python scripts/fetch_cord_images.py            # once: the CORD v2 photos behind receipt-extract
    python scripts/vision_rung.py --repeats 3 --max-usd 0.5

receipt-extract's inputs are CORD v2's annotated OCR text, i.e. *perfect* OCR. Here Nemotron 3.5 Super VL gets the
photo instead (no OCR step at all) and must produce the same JSON. Two prompts, thinking off, `--repeats` passes
each over all 86 held-out receipts, graded by the preset's checker in Nebius Sandboxes:

- plain:  the preset's plain prompt, reworded for an image;
- rules:  the rewritten prompt and the 4 text few-shot pairs from the receipts verdict (demo_runs/45015b6df59e.json);
          the photo replaces the OCR text only in the final user turn. (Without the few-shots the rules prompt never
          names the output keys, and Super VL nests the totals under "totals": 55.7%. That is a setup error, not a
          finding, so the few-shots are kept.)

Each is compared with the cached Ultra reference (Ultra, thinking on, reading the perfect OCR text) using the same
paired bootstrap and 5-point rule as the engine. Results go to demo_runs/vision-receipt-extract.json.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goodenough.engine import max_tokens_for, score_calls  # noqa: E402
from goodenough.grading import Grader  # noqa: E402
from goodenough.models import LLM, Meter  # noqa: E402
from goodenough.stats import bootstrap_ci, matches, paired_diff_ci  # noqa: E402
from goodenough.tasks import as_text, load_preset, plain_system  # noqa: E402

MODEL = "nvidia/Nemotron-3.5-Super-VL"
IMAGES = ROOT / "presets" / "_cache" / "cord-v2" / "images"
IMAGE_LEAD = "The image is a photo of a shop or restaurant receipt from Indonesia (amounts in rupiah). "
TOLERANCE = 0.05


def image_messages(system: str, fewshots: list[dict], row_id: str) -> list[dict]:
    b64 = base64.b64encode((IMAGES / f"{row_id}.jpg").read_bytes()).decode("ascii")
    msgs = [{"role": "system", "content": system}]
    for ex in fewshots:
        msgs += [{"role": "user", "content": as_text(ex["input"])}, {"role": "assistant", "content": as_text(ex["reference"])}]
    return msgs + [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]


async def main(repeats: int, max_usd: float, only: list[str]) -> None:
    task = load_preset("receipt-extract")
    rows = [dict(r, id=str(r["id"])) for r in task["holdout"]]
    missing = [r["id"] for r in rows if not (IMAGES / f"{r['id']}.jpg").exists()]
    if missing:
        raise SystemExit(f"{len(missing)} images missing; run scripts/fetch_cord_images.py first")
    verdict = json.loads((ROOT / "demo_runs" / "45015b6df59e.json").read_text(encoding="utf-8"))["verdict"]
    ref = json.loads((ROOT / "presets" / "receipt-extract" / "reference.json").read_text(encoding="utf-8"))
    ultra = ref["modes"]["ultra-think"]
    nano = json.loads((ROOT / "demo_runs" / "45015b6df59e.repeats.json").read_text(encoding="utf-8"))
    prompts = {
        "plain": (IMAGE_LEAD + plain_system(task).split("\n", 1)[1], []),
        "rules": (IMAGE_LEAD + verdict["system_prompt"], verdict["fewshots"]),
    }
    prompts = {k: v for k, v in prompts.items() if k in only}
    rung = {"key": "super-vl", "model": MODEL, "thinking": False, "label": "Nemotron 3.5 Super VL (thinking off)"}
    llm = LLM(run_id="vision-receipt-extract", meter=Meter(cap_usd=max_usd), concurrency=12)
    grader = Grader()
    max_tokens = max_tokens_for(task)
    ids = [r["id"] for r in rows]
    ultra_row = [statistics.mean(ultra["per_row"][i]) for i in ids]
    t0 = time.time()
    out = {"task": "receipt-extract", "measured_on": str(date.today()), "model": MODEL, "thinking": False,
           "input": "receipt photo (CORD v2), long side <= 1280 px; no OCR", "n": len(rows), "tolerance": TOLERANCE,
           "reference": {"label": ultra["label"], "input": "CORD annotated OCR text", "mean": ultra["mean"],
                         "repeat_scores": ultra["repeat_scores"], "cost_per_1k_usd": ultra["cost_per_1k_usd"]},
           "text_verdict": {"label": "Nemotron 3 Nano (thinking off), rules prompt + 4 text few-shots",
                            "mean": nano["mean"], "repeat_scores": nano["repeat_scores"],
                            "cost_per_1k_usd": nano["cost_per_1k_usd"]},
           "prompts": {}}
    for name, (system, fewshots) in prompts.items():
        reps = []
        for k in range(1, repeats + 1):
            calls = await asyncio.gather(*(llm.chat(MODEL, image_messages(system, fewshots, r["id"]), max_tokens=max_tokens,
                                                    purpose=f"vision:{name}:r{k}", thinking=False) for r in rows))
            e = await score_calls(grader, task["checker_code"], rung, rows, calls, "holdout", f"{name}-r{k}")
            reps.append(e)
            print(f"  {name} r{k}: {e['score']:.4f}  ${e['cost_per_1k_usd']}/1k  p50 {e['p50_ms']} ms  "
                  f"errors {e['errors']}  graded by {e['grader']}  (spent ${llm.meter.spent_usd:.3f})", flush=True)
        per_row = [statistics.mean(e["row_scores"][i] for e in reps) for i in ids]
        diff = paired_diff_ci(per_row, ultra_row)
        scores = [e["score"] for e in reps]
        out["prompts"][name] = {
            "system": system, "fewshot_ids": [str(f["id"]) for f in fewshots], "repeat_scores": scores, "mean": round(statistics.mean(scores), 4),
            "spread": round(max(scores) - min(scores), 4), "ci": bootstrap_ci(per_row),
            "diff_vs_ultra": diff, "matches": matches(diff, TOLERANCE),
            "cost_per_1k_usd": round(statistics.mean(e["cost_per_1k_usd"] for e in reps), 4),
            "p50_ms": round(statistics.median(e["p50_ms"] for e in reps), 1),
            "graded_by": sorted({e["grader"] for e in reps}),
            "per_row": {i: [e["row_scores"][i] for e in reps] for i in ids},
            "outputs_r1": reps[0]["outputs"], "failures_r1": reps[0]["failures"],
        }
        print(f"{name}: {' / '.join(f'{s:.1%}' for s in scores)} (mean {statistics.mean(scores):.1%}) vs Ultra "
              f"{ultra['mean']:.1%}: {diff['diff'] * 100:+.1f} pts (95% CI {diff['lo'] * 100:+.1f} to "
              f"{diff['hi'] * 100:+.1f}) -> {'matches' if out['prompts'][name]['matches'] else 'does not match'}",
              flush=True)
    out["spent_usd"] = round(llm.meter.spent_usd, 4)
    out["wall_s"] = round(time.time() - t0, 1)
    dest = ROOT / "demo_runs" / "vision-receipt-extract.json"
    if dest.exists():  # keep conditions measured in an earlier call
        prev = json.loads(dest.read_text(encoding="utf-8"))
        out["prompts"] = {**prev.get("prompts", {}), **out["prompts"]}
        out["spent_usd"] = round(out["spent_usd"] + prev.get("spent_usd", 0), 4)
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Spent ${out['spent_usd']} in {out['wall_s']} s. Saved {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--max-usd", type=float, default=0.5)
    ap.add_argument("--only", nargs="+", default=["plain", "rules"], choices=["plain", "rules"])
    a = ap.parse_args()
    asyncio.run(main(a.repeats, a.max_usd, a.only))
