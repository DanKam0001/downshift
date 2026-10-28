"""Run Downshift on your own logged traffic.

    python -m downshift.cli logs.jsonl --instruction "Classify the email into one of the labels." \\
        --labels billing,shipping,refund --cap 0.25 --json verdict.json

Each line of the JSONL file is either a plain example, {"input": ..., "reference"?: ...}, or an OpenAI-style chat log,
{"messages": [...], "response" | "output" | "completion"?: ...}. For a chat log the last user message is the input and
the logged assistant answer (the explicit field, or a trailing assistant message) is the reference. Rows without a
reference get one from Ultra, as in the web app's "Your own" tab. Progress goes to stderr; the verdict goes to stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from downshift.engine import DEFAULTS, run_downshift
from downshift.tasks import as_text, custom_task

ANSWER_KEYS = ("reference", "response", "output", "completion", "answer")


def _content(msg) -> str | None:
    """Text of a chat message whose content is a string or a list of content parts."""
    if not isinstance(msg, dict):
        return None
    c = msg.get("content")
    if isinstance(c, list):
        c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) and c.strip() else None


def _answer(x) -> str | None:
    if x is None:
        return None
    if isinstance(x, dict):  # e.g. a logged {"role": "assistant", "content": ...} or a chat completion choice
        return _content(x.get("message", x))
    return as_text(x) if as_text(x).strip() else None


def parse_record(rec: dict) -> dict:
    """One log line -> {"input", "reference"?}. Raises ValueError if it has no usable input."""
    if not isinstance(rec, dict):
        raise ValueError("each line must be a JSON object")
    if "messages" in rec:
        msgs = rec["messages"] or []
        users = [i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "user" and _content(m)]
        if not users:
            raise ValueError("chat log has no user message")
        last = users[-1]
        out = {"input": _content(msgs[last])}
        ref = next((_answer(rec[k]) for k in ANSWER_KEYS if k in rec and _answer(rec[k])), None)
        if ref is None:
            ref = next((_content(m) for m in msgs[last + 1:] if isinstance(m, dict) and m.get("role") == "assistant"
                        and _content(m)), None)
    elif "input" in rec:
        out = {"input": rec["input"]}
        ref = next((_answer(rec[k]) for k in ANSWER_KEYS if k in rec and _answer(rec[k])), None)
    else:
        raise ValueError("expected an 'input' field or a 'messages' chat log")
    if ref is not None:
        out["reference"] = ref
    if "id" in rec:
        out["id"] = str(rec["id"])
    return out


def load_examples(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(parse_record(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as e:
            raise SystemExit(f"{path}:{n}: {e}") from None
    return rows


def monthly(cost_per_1k: float | None, calls: float) -> float | None:
    return None if cost_per_1k is None else cost_per_1k * calls / 1000


def describe(ev: dict) -> str | None:
    """A one-line progress message for an engine event, or None to stay quiet."""
    t = ev["type"]
    if t == "stage":
        return ev["message"]
    if t == "eval":
        return (f"  {ev['tier_key']} · {ev['split']} · {ev['label']}: {ev['score']:.1%} on {ev['n']} rows, "
                f"${ev['cost_per_1k_usd']}/1k calls, graded by {ev['grader']}")
    if t == "tier_done":
        return "  " + ev.get("message", "")
    if t in ("notice", "skipped"):
        return "  " + ev["message"]
    if t in ("aborted", "error"):
        return f"{t}: {ev.get('reason') or ev.get('message')}"
    return None


def verdict_text(v: dict, calls: float) -> str:
    pct = lambda x: f"{x * 100:.1f}%"
    pts = lambda x: f"{x * 100:+.1f}"
    lines = []
    if v["kind"] != "downshift":
        lines.append("Verdict: keep Ultra. No cheaper option was shown to match it.")
    elif v.get("thinking_off_only"):
        lines.append(f"Verdict: turn thinking off on Ultra ({v['model']}), {v['savings_multiple']}x cheaper.")
    else:
        lines.append(f"Verdict: use {v['model']} with thinking {'on' if v['thinking'] else 'off'}, "
                     f"{v['savings_multiple']}x cheaper than Ultra.")
    ci = v.get("holdout_ci") or {}
    lines.append(f"Held out: {pct(v['holdout_score'])} (95% CI {pct(ci.get('lo', 0))}-{pct(ci.get('hi', 0))}, "
                 f"n={v['holdout_n']}); Ultra {pct(v['reference']['mean'])}")
    d = v.get("diff_vs_reference")
    if d:
        lines.append(f"Difference vs Ultra: {pts(d['diff'])} pts (95% CI {pts(d['lo'])} to {pts(d['hi'])}), "
                     f"tolerance {v['tolerance'] * 100:.0f} pts")
    now, new = monthly(v.get("ultra_cost_per_1k_usd"), calls), monthly(v.get("cost_per_1k_usd"), calls)
    lines.append(f"Cost per 1k calls: ${v.get('cost_per_1k_usd')} vs Ultra ${v.get('ultra_cost_per_1k_usd')}")
    if v["kind"] == "downshift" and now is not None and new is not None:
        lines.append(f"At {calls:,.0f} calls a month: ${now:,.2f} -> ${new:,.2f}, saving ${now - new:,.2f} a month "
                     f"(${(now - new) * 12:,.2f} a year)")
    lines.append(f"This run: ${v.get('run_spend_usd')} in {round(v.get('run_seconds') or 0)} s")
    return "\n".join(lines)


async def run(args) -> int:
    examples = load_examples(Path(args.logs))
    spec = {"title": args.title, "instruction": args.instruction, "output_format": args.output_format,
            "label_set": [s.strip() for s in args.labels.split(",") if s.strip()] if args.labels else None,
            "examples": examples}
    try:
        task = custom_task(spec)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from None
    with_ref = sum(1 for e in examples if e.get("reference") is not None)
    print(f"{len(examples)} examples ({with_ref} with a logged answer): {len(task['train'])} tuning, "
          f"{len(task['validation'])} validation, {len(task['holdout'])} held out. Spend cap ${args.cap}.",
          file=sys.stderr)
    opts = dict(DEFAULTS, spend_cap_usd=args.cap, tolerance=args.tolerance / 100, reference="live")
    verdict, spent = None, 0.0
    async for ev in run_downshift(task, options=opts):
        spent = ev.get("spend_usd", spent)
        msg = describe(ev)
        if msg:
            print(msg, file=sys.stderr, flush=True)
        if ev["type"] == "verdict":
            verdict = ev["verdict"]
    if verdict is None:
        print(f"No verdict (spent ${spent:.4f}). See the messages above.", file=sys.stderr)
        return 1
    print(verdict_text(verdict, args.calls_per_month))
    if args.json:
        Path(args.json).write_text(json.dumps(verdict, indent=1, default=str), encoding="utf-8")
        print(f"Verdict and code snippet written to {args.json}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m downshift.cli", description=__doc__.split("\n")[0])
    ap.add_argument("logs", help="JSONL file of examples or chat logs (8 to 200 lines)")
    ap.add_argument("--instruction", required=True, help="what the model is asked to do")
    ap.add_argument("--title", default="Logged traffic")
    ap.add_argument("--labels", help="comma-separated allowed labels, for classification")
    ap.add_argument("--output-format", default="", help="e.g. 'One label, lowercase' or 'JSON object'")
    ap.add_argument("--cap", type=float, default=0.25, help="hard spend cap for the run in US$ (default 0.25)")
    ap.add_argument("--tolerance", type=float, default=5, help="points a cheaper model may trail Ultra (default 5)")
    ap.add_argument("--calls-per-month", type=float, default=1e6, help="volume for the monthly saving (default 1e6)")
    ap.add_argument("--json", help="write the full verdict (incl. prompt and snippet) to this file")
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
