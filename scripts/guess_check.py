"""Can a preset's checker tell skill from guessing? Scores fixed guesses (no model) against the cached Ultra reference.

    python scripts/guess_check.py receipt-extract support-triage contract-clauses

The same check runs inside every Good Enough run before the ladder (engine.guess_check); this script runs it alone,
with no model calls, so a checker can be tested before any money is spent.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goodenough.engine import DEFAULTS, Run, cached_reference, guess_check, max_tokens_for, pick_reference  # noqa: E402
from goodenough.grading import Grader  # noqa: E402
from goodenough.models import LLM, Meter  # noqa: E402
from goodenough.tasks import load_preset, plain_system  # noqa: E402


async def one(slug: str) -> None:
    task = load_preset(slug)
    for split in ("train", "validation", "holdout"):
        task[split] = [dict(r, id=str(r["id"])) for r in task[split]]
    ids = [r["id"] for r in task["holdout"]]
    ref_info, why = cached_reference(task, plain_system(task), ids)
    if not ref_info:
        print(f"{slug}: {why}")
        return
    run = Run(task, dict(DEFAULTS), LLM(run_id=f"guess-{slug}", meter=Meter(cap_usd=0.0)), Grader(), f"guess-{slug}",
              max_tokens_for(task))
    run.checker_code = task["checker_code"]
    g = await guess_check(run, ref_info["modes"][pick_reference(ref_info["modes"])], ids)
    print(f"{slug}: " + ("TRIPPED  " if g["tripped"] else "ok       ") + g.get("message", "no guesses"))
    for x in g["guesses"]:
        print(f"    {x['score']:.1%}  {x['label']}: {x['answer'][:90]}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        asyncio.run(one(s))
