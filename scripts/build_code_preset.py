"""Build presets/code-mbpp from MBPP sanitized (Google Research, CC-BY-4.0).

    python scripts/build_code_preset.py            # downloads into presets/_cache/mbpp/ if missing

The answer key is unit tests, not reference answers: each row's `reference` is a JSON string
{"entry_point", "test_imports", "tests"} with the dataset's own asserts. The model sees the problem text plus the
FIRST assert (so it knows the function name and call shape); it is graded on ALL asserts, so at least two of the
three are hidden from it.

Splits follow the MBPP paper's task-id ranges, so the three files never share a problem:
  holdout    150 rows sampled from the test range (task_id 11-510)
  validation  30 rows sampled from the validation range (511-600)
  train       30 rows sampled from the train range (601-974)
Every canonical solution is checked against its own tests with the preset's checker (locally, here) before writing.
"""
from __future__ import annotations

import json
import random
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "presets" / "code-mbpp"
CACHE = ROOT / "presets" / "_cache" / "mbpp" / "sanitized-mbpp.json"
URL = "https://raw.githubusercontent.com/google-research/google-research/master/mbpp/sanitized-mbpp.json"
SEED = 20261029
# Canonical solution needs more than the 2.6 s per-item limit inside the sandbox (measured 2026-10-29), so a
# correct-but-slow answer would fail; dropped rather than loosening the limit for everyone.
TOO_SLOW = {123}

INSTRUCTION = ("Write a Python function that solves the task below. The example test shows the function's name and "
               "how it is called: use exactly that name and argument order. Standard library only. "
               "Put the code in one ```python block.")


def entry_point(code: str, tests: list[str]) -> str:
    names = re.findall(r"^def\s+([A-Za-z_]\w*)\s*\(", code, flags=re.M)
    joined = "\n".join(tests)
    for n in names:
        if re.search(r"\b%s\s*\(" % re.escape(n), joined):
            return n
    return names[-1] if names else ""


def row(p: dict) -> dict:
    tests = list(p["test_list"])
    ep = entry_point(p["code"], tests)
    return {
        "id": f"mbpp-{p['task_id']}",
        "input": f"Task: {p['prompt'].strip()}\nExample test:\n{tests[0]}",
        "reference": json.dumps({"entry_point": ep, "test_imports": list(p.get("test_imports") or []),
                                 "tests": tests}),
        "canonical_solution": p["code"],
    }


def main() -> None:
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(URL, CACHE)
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    ranges = {"holdout": (11, 510, 150), "validation": (511, 600, 30), "train": (601, 974, 30)}
    sys.path.insert(0, str(OUT))
    import checker  # noqa: E402

    rng = random.Random(SEED)
    splits = {}
    for name, (lo, hi, n) in ranges.items():
        pool = [p for p in data if lo <= p["task_id"] <= hi and p["task_id"] not in TOO_SLOW]
        rows = [row(p) for p in pool]
        good = []
        for r in rows:
            ok, why = checker.check("```python\n" + r["canonical_solution"] + "\n```", r["reference"], r["input"])
            if ok == 1.0 and json.loads(r["reference"])["entry_point"]:
                good.append(r)
            else:
                print(f"  drop {r['id']}: {why}")
        rng.shuffle(good)
        splits[name] = sorted(good[:n], key=lambda r: int(r["id"].split("-")[1]))
        rng.shuffle(splits[name])
        print(f"{name}: {len(pool)} in range, {len(good)} usable, kept {len(splits[name])}")

    task = {
        "title": "Python function from a one-line spec (graded by unit tests)",
        "business_story": ("Coding assistants, ticket-to-patch bots and spreadsheet-formula helpers turn a short "
                           "request into a small function, at high volume. There is no single right answer to "
                           "compare against, but there is a way to check one: run the code against tests. "
                           "Good Enough grades every reply by executing it in a Nebius Sandbox with networking off."),
        "instruction": INSTRUCTION,
        "output_format": "One ```python code block that defines the function named in the example test.",
        "max_answer_tokens": 1024,
        "grading": "unit tests run in Nebius Sandboxes (networking off); score 1 only if every assert passes",
        "source_url": "https://github.com/google-research/google-research/tree/master/mbpp "
                      "(also huggingface.co/datasets/google-research-datasets/mbpp, config 'sanitized')",
        "license": "CC-BY-4.0",
        "attribution": "Austin et al., 'Program Synthesis with Large Language Models', 2021. Google Research. "
                       "MBPP sanitized subset (hand-verified by the authors).",
        "split_note": ("Splits follow the MBPP task-id ranges: holdout from test (11-510), validation from "
                       "validation (511-600), tuning from train (601-974). The model sees the problem and the first "
                       "assert; it is graded on all asserts (usually 3). reference holds tests, not an answer, so "
                       "it must not be used as a few-shot assistant turn. Each row also carries the dataset's "
                       "canonical_solution, used only to prove the tests pass for correct code."),
        "slug": "code-mbpp",
        "n_train": len(splits["train"]),
        "n_validation": len(splits["validation"]),
        "n_holdout": len(splits["holdout"]),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")
    for name, rows in splits.items():
        with open(OUT / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
