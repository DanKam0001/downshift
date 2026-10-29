"""Code generation, graded by unit tests in Nebius Sandboxes: which Nemotron setup is the cheapest that matches Ultra?

    python scripts/code_levers.py calibrate                  # canonical solutions + fixed guesses, sandbox only, $0
    python scripts/code_levers.py run ultra-think ultra-off  # model calls on the 150 held-out rows, then grading
    python scripts/code_levers.py report                     # table vs Ultra (thinking on), paired 95% CIs

Self-contained client (does not touch downshift/models.py) so the lever experiments can vary the prompt and
max_tokens freely. Every call's usage and cost goes to demo_runs/code-mbpp/<config>.jsonl and the running total to
demo_runs/code-mbpp/spend.json; a run refuses to start, and stops issuing calls, once the cap would be passed.
Grading uses only the public downshift.sandbox_grader.grade_batch_with_meta (one sandbox operation per batch).
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from downshift import sandbox_grader  # noqa: E402
from downshift.stats import matches, beats, paired_diff_ci  # noqa: E402
from downshift.tasks import load_preset  # noqa: E402

OUT = ROOT / "demo_runs" / "code-mbpp"
SPEND = OUT / "spend.json"
CAP_USD = 2.50
BASE_URL = "https://api.tokenfactory.nebius.com/v1"
CONCURRENCY = 8
TOL = 0.05
THINK_BUDGET = 4096

MODELS = {  # id, $/M prompt, $/M cached prompt, $/M completion
    "lightning": ("nvidia/Nemotron-3_5-Lightning", 0.06, 0.03, 0.24),
    "nano": ("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", 0.06, 0.012, 0.24),
    "super": ("nvidia/nemotron-3-super-120b-a12b", 0.30, 0.06, 0.90),
    "ultra": ("nvidia/Nemotron-3-Ultra-550b-a55b", 1.00, 0.12, 3.00),
}

TASK = load_preset("code-mbpp")
BASE = TASK["instruction"]
PROMPTS = {
    "default": BASE,
    "answer-only": BASE + " Reply with the code block only: no explanation, no example usage, no tests.",
    "short": "Write this Python function. Reply with a ```python block.",
    "long": BASE + "\n\nGuidelines:\n"
                   "- Read the task and the example test carefully; the hidden tests call the function the same way "
                   "with other inputs.\n"
                   "- Match the return type the example expects exactly (tuple vs list, int vs float, bool, string "
                   "formatting).\n"
                   "- Handle edge cases: empty inputs, zero, negatives, single elements, duplicates.\n"
                   "- Return the result; do not print it, read input, or call exit().\n"
                   "- Put every import the code needs inside the block.\n"
                   "- Do not rename the function or reorder its arguments, even if the task wording suggests "
                   "other names.\n"
                   "- Keep it simple and correct; performance only matters if the inputs are large.",
}

# name -> (model key, thinking, prompt key, answer max_tokens)
CONFIGS = {
    "ultra-think": ("ultra", True, "default", 1024),
    "ultra-off": ("ultra", False, "default", 1024),
    "super-off": ("super", False, "default", 1024),
    "nano-off": ("nano", False, "default", 1024),
    "nano-think": ("nano", True, "default", 1024),
    "lightning-off": ("lightning", False, "default", 1024),
    "lightning-think": ("lightning", True, "default", 1024),
}


def lever_configs(base: str) -> dict:
    m, th = CONFIGS[base][:2]
    return {f"{base}+answer-only": (m, th, "answer-only", 1024),
            f"{base}+short": (m, th, "short", 1024),
            f"{base}+long": (m, th, "long", 1024),
            f"{base}+cap256": (m, th, "default", 256),
            f"{base}+answer-only+cap256": (m, th, "answer-only", 256),
            # thinking rungs: cap the WHOLE reply (reasoning + answer), not just the answer part
            f"{base}+total1024": (m, th, "default", 1024, 1024),
            f"{base}+total2048": (m, th, "default", 1024, 2048),
            f"{base}+answer-only+total2048": (m, th, "answer-only", 1024, 2048)}


def all_configs() -> dict:
    c = dict(CONFIGS)
    for base in ("nano-off", "lightning-off", "super-off", "nano-think", "lightning-think"):
        c.update(lever_configs(base))
    return c


def api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("NEBIUS_API_KEY") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("NEBIUS_API_KEY missing")


def spent() -> float:
    return json.loads(SPEND.read_text())["total_usd"] if SPEND.exists() else 0.0


def add_spend(label: str, usd: float) -> None:
    d = json.loads(SPEND.read_text()) if SPEND.exists() else {"total_usd": 0.0, "by_config": {}}
    d["total_usd"] = round(d["total_usd"] + usd, 6)
    d["by_config"][label] = round(d["by_config"].get(label, 0.0) + usd, 6)
    SPEND.write_text(json.dumps(d, indent=1))


def cost(model_key: str, pt: int, cached: int, ct: int) -> float:
    _, p, pc, c = MODELS[model_key]
    cached = min(cached or 0, pt)
    return ((pt - cached) * p + cached * pc + ct * c) / 1e6


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.I)[-1]
    return text.strip()


class Budget:
    def __init__(self, cap: float):
        self.cap, self.used, self.stopped = cap, spent(), False

    def ok(self) -> bool:
        if self.used >= self.cap:
            self.stopped = True
        return not self.stopped


async def call(client, sem, budget: Budget, mk: str, thinking: bool, system: str, user: str, max_tokens: int) -> dict:
    model = MODELS[mk][0]
    async with sem:
        if not budget.ok():
            return {"ok": False, "error": "budget cap reached", "output": "", "pt": 0, "cached": 0, "ct": 0,
                    "cost": 0.0}
        last = None
        for attempt in range(6):
            t0 = time.perf_counter()
            try:
                r = await client.chat.completions.create(
                    model=model, temperature=0.0, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
                u = r.usage
                pt, ct = u.prompt_tokens or 0, u.completion_tokens or 0
                det = getattr(u, "prompt_tokens_details", None)
                cached = (getattr(det, "cached_tokens", 0) or 0) if det is not None else 0
                c = cost(mk, pt, cached, ct)
                budget.used += c
                ch = r.choices[0]
                raw = ch.message.content or ""
                reasoning = getattr(ch.message, "reasoning_content", None) or ""
                out = strip_think(raw)
                truncated = ch.finish_reason == "length"
                if truncated and thinking:
                    out = ""  # ran out of tokens while reasoning: no answer (same rule as downshift/models.py)
                return {"ok": True, "output": out, "pt": pt, "cached": cached, "ct": ct, "cost": c,
                        "finish": ch.finish_reason, "reasoning_chars": len(reasoning) + (len(raw) - len(out)),
                        "latency_ms": round((time.perf_counter() - t0) * 1000), "attempts": attempt + 1}
            except Exception as e:  # noqa: BLE001
                last = e
                status = getattr(e, "status_code", None)
                if status is not None and status != 429 and status < 500:
                    break
                await asyncio.sleep(min(2 ** attempt, 20))
        return {"ok": False, "error": f"{type(last).__name__}: {str(last)[:200]}", "output": "", "pt": 0,
                "cached": 0, "ct": 0, "cost": 0.0}


def grade(items: list[dict], label: str) -> tuple[list, dict]:
    last = None
    for attempt in range(4):
        try:
            res, meta = sandbox_grader.grade_batch_with_meta(TASK["checker_code"], items, timeout_s=180)
            return res, {"backend": meta.backend, "operation_id": meta.operation_id, "image": meta.image,
                         "networking": meta.networking, "wall_s": meta.wall_s, "guest_elapsed_s": meta.guest_elapsed_s,
                         "harness_s": meta.harness_s, "workers": meta.workers, "mode": meta.mode,
                         "attempts": attempt + 1}
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  [{label}] sandbox attempt {attempt + 1} failed: {str(e)[:200]}")
            time.sleep(3 + 3 * attempt)
    raise RuntimeError(f"sandbox grading failed for {label}: {last}")


async def run_config(name: str, cfg: tuple, rows: list[dict], cap: float) -> None:
    from openai import AsyncOpenAI
    mk, thinking, pkey, ans_tokens = cfg[:4]
    max_tokens = cfg[4] if len(cfg) > 4 else ans_tokens + (THINK_BUDGET if thinking else 0)
    budget = Budget(cap)
    if not budget.ok():
        print(f"{name}: skipped, spend {budget.used:.4f} >= cap {cap}")
        return
    client = AsyncOpenAI(base_url=BASE_URL, api_key=api_key(), max_retries=0, timeout=180)
    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.time()
    calls = await asyncio.gather(*(call(client, sem, budget, mk, thinking, PROMPTS[pkey], r["input"], max_tokens)
                                   for r in rows))
    usd = sum(c["cost"] for c in calls)
    add_spend(name, usd)
    if budget.stopped:
        print(f"{name}: BUDGET CAP hit mid-run; partial results not graded")
        return
    items = [{"id": r["id"], "input": r["input"], "output": c["output"], "reference": r["reference"]}
             for r, c in zip(rows, calls)]
    res, meta = grade(items, name)
    with open(OUT / f"{name}.jsonl", "w", encoding="utf-8") as f:
        for r, c, g in zip(rows, calls, res):
            f.write(json.dumps({"id": r["id"], "score": g.score, "detail": g.detail, "grader_error": g.error,
                                **{k: v for k, v in c.items()}}) + "\n")
    summ = {"config": name, "model": MODELS[mk][0], "thinking": thinking, "prompt": pkey, "system": PROMPTS[pkey],
            "max_tokens": max_tokens, "n": len(rows), "pass_rate": statistics.mean(g.score for g in res),
            "call_errors": sum(not c["ok"] for c in calls),
            "truncated": sum(c.get("finish") == "length" for c in calls),
            "mean_in": statistics.mean(c["pt"] for c in calls), "mean_out": statistics.mean(c["ct"] for c in calls),
            "usd_total": usd, "usd_per_1k": usd / len(rows) * 1000, "wall_s": round(time.time() - t0, 1),
            "sandbox": meta}
    (OUT / f"{name}.summary.json").write_text(json.dumps(summ, indent=1))
    print(f"{name}: pass {summ['pass_rate']:.1%}  in {summ['mean_in']:.0f} out {summ['mean_out']:.0f}  "
          f"${summ['usd_per_1k']:.4f}/1k  errors {summ['call_errors']} trunc {summ['truncated']}  "
          f"op {meta['operation_id']}  total spend ${spent():.4f}")


def calibrate() -> None:
    rows = TASK["holdout"]
    out = {}
    canon = [{"id": r["id"], "input": r["input"], "reference": r["reference"],
              "output": "```python\n" + r["canonical_solution"] + "\n```"} for r in rows]
    res, meta = grade(canon, "canonical")
    out["canonical"] = {"pass_rate": statistics.mean(g.score for g in res), "sandbox": meta,
                        "fails": [(g.item_id, g.detail, g.error) for g in res if g.score < 1]}
    print(f"canonical solutions: {out['canonical']['pass_rate']:.1%}  op {meta['operation_id']}")
    guesses = {
        "def f(*a): return None": lambda ep: "```python\ndef f(*a): return None\n```",
        "right name, return None": lambda ep: f"```python\ndef {ep}(*a, **k):\n    return None\n```",
        "right name, return True": lambda ep: f"```python\ndef {ep}(*a, **k):\n    return True\n```",
        "right name, return False": lambda ep: f"```python\ndef {ep}(*a, **k):\n    return False\n```",
        "right name, return 0": lambda ep: f"```python\ndef {ep}(*a, **k):\n    return 0\n```",
        "right name, echo first arg": lambda ep: f"```python\ndef {ep}(*a, **k):\n    return a[0] if a else None\n```",
        "infinite loop": lambda ep: f"```python\ndef {ep}(*a, **k):\n    while True:\n        pass\n```",
        "empty reply": lambda ep: "",
    }
    for label, mk in guesses.items():
        items = [{"id": r["id"], "input": r["input"], "reference": r["reference"],
                  "output": mk(json.loads(r["reference"])["entry_point"])} for r in rows]
        res, meta = grade(items, label)
        pr = statistics.mean(g.score for g in res)
        out[label] = {"pass_rate": pr, "sandbox": meta, "example_detail": res[0].detail,
                      "passing_ids": [g.item_id for g in res if g.score >= 1]}
        print(f"guess {label!r}: {pr:.1%}  op {meta['operation_id']}  e.g. {res[0].detail}")
    (OUT / "calibration.json").write_text(json.dumps(out, indent=1))


def load_scores(name: str) -> dict:
    p = OUT / f"{name}.jsonl"
    return {json.loads(l)["id"]: json.loads(l)["score"] for l in p.read_text(encoding="utf-8").splitlines()} \
        if p.exists() else {}


def report(ref: str = "ultra-think") -> None:
    ids = [r["id"] for r in TASK["holdout"]]
    ref_s = load_scores(ref)
    rows = []
    for name in all_configs():
        sp = OUT / f"{name}.summary.json"
        if not sp.exists():
            continue
        s = json.loads(sp.read_text())
        sc = load_scores(name)
        d = paired_diff_ci([sc[i] for i in ids], [ref_s[i] for i in ids]) if ref_s and name != ref else None
        rows.append((name, s, d))
    print(f"| setup | pass | mean in | mean out | $/1k calls | diff vs {ref} [95% CI] | within 5 pts | beats |")
    print("|---|---|---|---|---|---|---|---|")
    for name, s, d in rows:
        ci = f"{d['diff'] * 100:+.1f} [{d['lo'] * 100:+.1f}, {d['hi'] * 100:+.1f}]" if d else "reference"
        print(f"| {name} | {s['pass_rate']:.1%} | {s['mean_in']:.0f} | {s['mean_out']:.0f} | ${s['usd_per_1k']:.3f} | "
              f"{ci} | {'yes' if d and matches(d, TOL) else ('-' if not d else 'no')} | "
              f"{'yes' if d and beats(d) else ('-' if not d else 'no')} |")
    ok = [(s["usd_per_1k"], n) for n, s, d in rows if d and matches(d, TOL)]
    refsum = next((s for n, s, d in rows if n == ref), None)
    if ok and refsum:
        c, n = min(ok)
        print(f"\ncheapest within 5 points: {n} at ${c:.4f}/1k vs ${refsum['usd_per_1k']:.4f}/1k "
              f"= {refsum['usd_per_1k'] / c:.1f}x cheaper")
    print(f"total spend: ${spent():.4f}")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "calibrate":
        calibrate()
    elif cmd == "run":
        rows = TASK["holdout"]
        limit = None
        if args and args[0].startswith("--rows="):
            limit = int(args.pop(0).split("=")[1])
        tag = ""
        if args and args[0].startswith("--tag="):
            tag = "@" + args.pop(0).split("=")[1]   # repeat run of the same config, saved separately
        allc = all_configs()
        for name in args:
            use = rows[:limit] if limit else rows
            label = (name if not limit else f"probe{limit}-{name}") + tag
            loop = asyncio.new_event_loop()
            loop.run_until_complete(run_config(label, allc[name], use, CAP_USD))
            loop.run_until_complete(asyncio.sleep(0.25))  # let the HTTP client close before the loop does
    elif cmd == "report":
        report(*(args[:1]))
