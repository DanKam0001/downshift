"""Print the RESULTS.md tables from saved runs and the cached references, so every number is traceable.

    python scripts/results_tables.py demo_runs/73ad158d7950.json demo_runs/7b83f078f3bd.json ...
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goodenough.stats import paired_diff_ci  # noqa: E402


def pct(x):
    return "–" if x is None else f"{x * 100:.1f}%"


def pts(x):
    return f"{x * 100:+.1f}"


def ci(c):
    return f"{c['lo'] * 100:.1f}–{c['hi'] * 100:.1f}" if c else "–"


def run_table(path: str) -> str:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    v = d["verdict"]
    done = next(e for e in d["events"] if e["type"] == "done")
    skipped = {e["tier_key"] for e in d["events"] if e["type"] == "skipped" and e.get("tier_key")}
    out = [f"### {d['meta']['task']['title']} (`{d['meta']['task']['slug']}`, run `{d['run_id']}`)", "",
           "| Rung | Thinking | Prompt | Tuning → validation → held out | Held out, 95% CI | vs reference, paired 95% CI | US$ per 1k calls | p50 latency | Status |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in v["table"]:
        think = "on" if r["thinking"] else "off"
        if r.get("repeat_scores"):
            runs = " / ".join(pct(x) for x in r["repeat_scores"])
            diff = "reference" if r.get("is_reference") else (
                f"{pts(r['diff']['diff'])} ({pts(r['diff']['lo'])} to {pts(r['diff']['hi'])})")
            out.append(f"| {r['name']} | {think} | plain | 3 runs: {runs} | **{pct(r['holdout_score'])}** ({ci(r['ci'])}) "
                       f"| {diff} | {r['cost_per_1k_usd']:.4f} | {r['p50_ms'] / 1000:.2f} s | "
                       f"{'reference' if r.get('is_reference') else 'cached'} |")
        elif r.get("holdout_score") is not None:
            tvh = f"{pct(r['tuning_score'])} → {pct(r['validation_score'])} → {pct(r['holdout_score'])}"
            label = "plain" if r["prompt_label"] == "plain" else ("carried" if r["prompt_label"] == "carried" else "rewritten")
            status = "matches" if r["passed"] else "not shown to match"
            out.append(f"| {r['name']} | {think} | {label} | {tvh} | **{pct(r['holdout_score'])}** ({ci(r['ci'])}) | "
                       f"{pts(r['diff']['diff'])} ({pts(r['diff']['lo'])} to {pts(r['diff']['hi'])}) | "
                       f"{r['cost_per_1k_usd']:.4f} | {r['p50_ms'] / 1000:.2f} s | {status} |")
        else:
            if v["kind"] == "downshift" and v["rung"] != "ultra":
                why = "not needed: a cheaper rung matched"
            else:
                why = "skipped: spend cap" if r["tier_key"] in skipped else "not scored on held out (spend cap)"
            out.append(f"| {r['name']} | {think} | – | – | – | – | – | – | {why} |")
    out += ["", f"Verdict: {v['sentence']}", "",
            f"Run: US${done['spend_usd']:.4f}, {done.get('seconds')} s, {done['calls']} model calls, graded by "
            f"{', '.join(v['graded_by'])}.", ""]
    return "\n".join(out)


def thinking_table(slugs: list[str]) -> str:
    out = ["| Task | Held-out rows | Ultra thinking off: 3 runs (mean) | Ultra thinking on: 3 runs (mean) | On minus off, paired 95% CI | Cost per 1k calls, off → on | p50, off → on | Rows that changed between repeats, off / on |",
           "|---|---|---|---|---|---|---|---|"]
    for slug in slugs:
        ref = json.loads((ROOT / "presets" / slug / "reference.json").read_text(encoding="utf-8"))
        ids = ref["holdout_ids"]
        m = ref["modes"]
        off = [sum(m["ultra"]["per_row"][i]) / len(m["ultra"]["per_row"][i]) for i in ids]
        on = [sum(m["ultra-think"]["per_row"][i]) / len(m["ultra-think"]["per_row"][i]) for i in ids]
        d = paired_diff_ci(on, off)
        mo, mn = m["ultra"], m["ultra-think"]
        out.append(
            f"| {slug} | {len(ids)} | {' / '.join(pct(x) for x in mo['repeat_scores'])} ({pct(sum(off) / len(off))}) | "
            f"{' / '.join(pct(x) for x in mn['repeat_scores'])} ({pct(sum(on) / len(on))}) | "
            f"{pts(d['diff'])} ({pts(d['lo'])} to {pts(d['hi'])}) | {mo['cost_per_1k_usd']:.3f} → {mn['cost_per_1k_usd']:.3f} "
            f"(**{mn['cost_per_1k_usd'] / mo['cost_per_1k_usd']:.1f}x**) | {mo['p50_ms'] / 1000:.2f} → {mn['p50_ms'] / 1000:.2f} s | "
            f"{mo['rows_unstable']} / {mn['rows_unstable']} |")
    return "\n".join(out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(thinking_table(["support-triage", "receipt-extract", "contract-clauses"]))
    print()
    for p in sys.argv[1:]:
        print(run_table(p))
