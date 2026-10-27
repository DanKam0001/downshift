"""Plain-prompt baseline: run each preset's holdout once per model, score with the preset's checker.

    python presets/run_baseline.py [--models a,b] [--tasks slug,...] [--no-think-lightning]

Writes raw outputs to presets/_baseline/<slug>__<model>.jsonl and a summary to presets/_baseline/summary.json.
Key from NEBIUS_API_KEY (loaded from .env); never printed.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import statistics
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

HERE = Path(__file__).resolve().parent
BASE_URL = "https://api.tokenfactory.nebius.com/v1"
BUDGET_USD = 1.30
DEFAULT_MODELS = ["nvidia/Nemotron-3_5-Lightning", "nvidia/Nemotron-3-Ultra-550b-a55b"]
TASKS = ["support-triage", "receipt-extract", "contract-clauses"]

spent = 0.0


def pricing(key: str) -> dict[str, tuple[float, float]]:
    r = requests.get(BASE_URL + "/models?verbose=true", headers={"Authorization": "Bearer " + key}, timeout=30)
    r.raise_for_status()
    return {m["id"]: (float(m["pricing"]["prompt"]), float(m["pricing"]["completion"])) for m in r.json()["data"]}


def load_checker(slug: str):
    ns: dict = {}
    src = (HERE / slug / "checker.py").read_text(encoding="utf-8")
    exec(compile(src, str(HERE / slug / "checker.py"), "exec"), ns)
    return ns["check"]


def run(client, model, variant, slug, price, extra):
    global spent
    task = json.loads((HERE / slug / "task.json").read_text(encoding="utf-8"))
    rows = [json.loads(l) for l in (HERE / slug / "holdout.jsonl").read_text(encoding="utf-8").splitlines() if l]
    check = load_checker(slug)

    def one(row):
        prompt = task["instruction"] + "\n\nInput:\n" + row["input"]
        t = time.time()
        for attempt in range(3):
            try:
                r = client.chat.completions.create(model=model, temperature=0, max_tokens=8000,
                                                   messages=[{"role": "user", "content": prompt}], **extra)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    return {**row, "output": None, "error": str(e)[:300], "score": 0.0, "reason": "api error",
                            "latency": time.time() - t, "pt": 0, "ct": 0, "rt": 0}
                time.sleep(2 + 3 * attempt)
        lat = time.time() - t
        out = r.choices[0].message.content or ""
        u = r.usage
        rt = (u.completion_tokens_details.reasoning_tokens or 0) if u.completion_tokens_details else 0
        s, why = check(out, row["reference"], row["input"])
        return {"id": row["id"], "output": out, "reference": row["reference"], "score": s, "reason": why,
                "latency": lat, "pt": u.prompt_tokens, "ct": u.completion_tokens, "rt": rt,
                "finish": r.choices[0].finish_reason}

    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, rows))
    pt, ct = sum(r["pt"] for r in res), sum(r["ct"] for r in res)
    cost = pt * price[0] + ct * price[1]
    spent += cost
    outdir = HERE / "_baseline"
    outdir.mkdir(exist_ok=True)
    tag = model.split("/")[-1] + ("" if variant == "default" else "__" + variant)
    with (outdir / f"{slug}__{tag}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for r in res:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    lats = sorted(r["latency"] for r in res)
    summ = {"task": slug, "model": model, "variant": variant, "n": len(res),
            "mean_score": round(statistics.mean(r["score"] for r in res), 4),
            "errors": sum(1 for r in res if r.get("error")),
            "truncated": sum(1 for r in res if r.get("finish") == "length"),
            "prompt_tokens": pt, "completion_tokens": ct, "reasoning_tokens": sum(r["rt"] for r in res),
            "p50_latency_s": round(statistics.median(lats), 2), "p90_latency_s": round(lats[int(0.9 * (len(lats) - 1))], 2),
            "cost_usd": round(cost, 5), "usd_per_1k_calls": round(cost / len(res) * 1000, 4)}
    print(json.dumps(summ), f"| spent so far ${spent:.4f}", flush=True)
    return summ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--no-think", action="store_true", help="also run each model with thinking disabled")
    ap.add_argument("--only-no-think", action="store_true")
    a = ap.parse_args()
    load_dotenv(HERE.parent / ".env")
    key = os.environ["NEBIUS_API_KEY"]
    client = OpenAI(base_url=BASE_URL, api_key=key, timeout=180)
    prices = pricing(key)
    summary_path = HERE / "_baseline" / "summary.json"
    all_s = json.loads(summary_path.read_text()) if summary_path.exists() else []
    for slug in a.tasks.split(","):
        for model in a.models.split(","):
            variants = [] if a.only_no_think else ["default"]
            if a.no_think or a.only_no_think:
                variants.append("no_think")
            for v in variants:
                if spent > BUDGET_USD:
                    print("budget reached, stopping")
                    break
                extra = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}} if v == "no_think" else {}
                s = run(client, model, v, slug, prices[model], extra)
                all_s = [x for x in all_s if not (x["task"] == slug and x["model"] == model and x["variant"] == v)]
                all_s.append(s)
                summary_path.write_text(json.dumps(all_s, indent=2))
    print(f"total spent this run ${spent:.4f}")


if __name__ == "__main__":
    main()
