"""Shared bits for the input-side lever measurements on Nemotron 3.5 Super VL (photos and video frames).

Own thin client (not downshift.models.LLM) because these runs need the cached-token split and the raw reasoning
length per call. Costs are computed from `usage` with the published Super VL prices; spend is capped across a whole
script run and appended to runs/levers_spend.json so separate runs share one budget.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import statistics
import sys
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from downshift.models import api_key, clean_answer  # noqa: E402
from downshift.stats import bootstrap_ci, beats, matches, paired_diff_ci  # noqa: E402

MODEL = "nvidia/Nemotron-3.5-Super-VL"
BASE_URL = "https://api.tokenfactory.nebius.com/v1"
PRICE = {"prompt": 0.30e-6, "cached": 0.06e-6, "completion": 0.90e-6}  # US$ per token
DATA = ROOT / "data_media"
SPEND_LOG = ROOT / "runs" / "levers_spend.json"
TOTAL_CAP_USD = 3.00
TOLERANCE = 0.05


def total_spent() -> float:
    try:
        return float(json.loads(SPEND_LOG.read_text())["spent_usd"])
    except Exception:
        return 0.0


def add_spent(usd: float) -> None:
    SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    SPEND_LOG.write_text(json.dumps({"spent_usd": round(total_spent() + usd, 6), "updated": time.time()}))


def cost_of(prompt_tokens: int, cached: int, completion: int) -> float:
    cached = min(cached or 0, prompt_tokens)
    return (prompt_tokens - cached) * PRICE["prompt"] + cached * PRICE["cached"] + completion * PRICE["completion"]


def jpeg_bytes(img: Image.Image, long_side: int | None = None, quality: int = 90) -> bytes:
    """Resize so the long side is exactly `long_side` (down OR up; None keeps the original), then JPEG-encode."""
    img = img.convert("RGB")
    if long_side:
        w, h = img.size
        s = long_side / max(w, h)
        if abs(s - 1) > 1e-6:
            img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def image_part(jpeg: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}}


class Client:
    def __init__(self, cap_usd: float, concurrency: int = 8):
        from openai import AsyncOpenAI
        key = api_key()
        if not key:
            raise SystemExit("NEBIUS_API_KEY missing")
        self.client = AsyncOpenAI(base_url=BASE_URL, api_key=key, max_retries=0, timeout=180)
        self.sem = asyncio.Semaphore(concurrency)
        self.cap = cap_usd
        self.spent = 0.0
        self.stopped = False

    async def chat(self, messages: list[dict], thinking: bool, max_tokens: int) -> dict:
        if self.spent >= self.cap:
            self.stopped = True
            return {"ok": False, "error": "budget cap", "text": "", "prompt_tokens": 0, "cached_tokens": 0,
                    "completion_tokens": 0, "cost_usd": 0.0, "latency_ms": 0.0, "finish": None}
        async with self.sem:
            err = None
            for attempt in range(6):
                t0 = time.perf_counter()
                try:
                    r = await self.client.chat.completions.create(
                        model=MODEL, messages=messages, max_tokens=max_tokens, temperature=0,
                        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
                    u = r.usage
                    pt = u.prompt_tokens or 0
                    ct = u.completion_tokens or 0
                    det = getattr(u, "prompt_tokens_details", None)
                    cached = (getattr(det, "cached_tokens", 0) or 0) if det is not None else 0
                    c = cost_of(pt, cached, ct)
                    self.spent += c
                    ch = r.choices[0]
                    raw = ch.message.content or ""
                    reasoning = getattr(ch.message, "reasoning_content", None) or ""
                    text = clean_answer(raw)
                    if ch.finish_reason == "length" and thinking and not text:
                        text = ""
                    return {"ok": True, "text": text, "reasoning_chars": len(reasoning) + (len(raw) - len(text)),
                            "prompt_tokens": pt, "cached_tokens": cached, "completion_tokens": ct,
                            "cost_usd": c, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                            "finish": ch.finish_reason}
                except Exception as exc:  # noqa: BLE001
                    err = exc
                    status = getattr(exc, "status_code", None)
                    retry = status is None or status == 429 or status >= 500
                    if not retry:
                        break
                    await asyncio.sleep(min(2 ** attempt, 30) + 0.3 * attempt)
            return {"ok": False, "error": f"{type(err).__name__}: {str(err)[:200]}", "text": "", "prompt_tokens": 0,
                    "cached_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "latency_ms": 0.0, "finish": None}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def extract_answer(text: str) -> str:
    """The text after the last 'Answer:' if present, else the last non-empty line."""
    m = re.findall(r"answer\s*[:\-]\s*(.+)", text or "", flags=re.I)
    if m:
        return m[-1].strip()
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return lines[-1].strip() if lines else ""


def summarise(name: str, setup: dict, rows: list[dict], calls: list[dict], scores: list[float]) -> dict:
    ok = [c for c in calls if c["ok"]]
    n = len(calls)
    cost = sum(c["cost_usd"] for c in calls)
    return {
        "name": name, "setup": setup, "n": n, "errors": n - len(ok),
        "accuracy": round(sum(scores) / n, 4) if n else 0.0, "ci": bootstrap_ci(scores),
        "mean_prompt_tokens": round(statistics.mean(c["prompt_tokens"] for c in ok), 1) if ok else 0,
        "mean_cached_tokens": round(statistics.mean(c["cached_tokens"] for c in ok), 1) if ok else 0,
        "mean_output_tokens": round(statistics.mean(c["completion_tokens"] for c in ok), 1) if ok else 0,
        "usd_per_1k_calls": round(cost / n * 1000, 4) if n else 0.0,
        "p50_latency_ms": round(statistics.median(c["latency_ms"] for c in ok), 1) if ok else 0,
        "truncated": sum(1 for c in ok if c["finish"] == "length"),
        "spent_usd": round(cost, 5),
        "rows": [{"id": r["id"], "label": r["label"], "answer": c["text"][:600], "score": s,
                  "prompt_tokens": c["prompt_tokens"], "cached_tokens": c["cached_tokens"],
                  "completion_tokens": c["completion_tokens"], "finish": c["finish"],
                  "error": c.get("error")} for r, c, s in zip(rows, calls, scores)],
    }


def compare_all(results: dict, top: str) -> None:
    ids_top = [r["id"] for r in results[top]["rows"]]
    top_scores = [r["score"] for r in results[top]["rows"]]
    top_cost = results[top]["usd_per_1k_calls"]
    for name, res in results.items():
        assert [r["id"] for r in res["rows"]] == ids_top
        d = paired_diff_ci([r["score"] for r in res["rows"]], top_scores)
        res["diff_vs_top"] = d
        res["within_5_points"] = matches(d, TOLERANCE)
        res["beats_top"] = beats(d)
        res["saving_multiple_vs_top"] = round(top_cost / res["usd_per_1k_calls"], 2) if res["usd_per_1k_calls"] else None


def table(results: dict, top: str) -> str:
    hdr = ("| setup | acc | 95% CI | prompt tok | output tok | $/1k calls | diff vs top (95% CI) | within 5 | x cheaper |\n"
           "|---|---|---|---|---|---|---|---|---|")
    lines = [hdr]
    for name, r in sorted(results.items(), key=lambda kv: -kv[1]["usd_per_1k_calls"]):
        d = r["diff_vs_top"]
        lines.append(f"| {name}{' (top)' if name == top else ''} | {r['accuracy']:.1%} | {r['ci']['lo']:.1%}-{r['ci']['hi']:.1%} | "
                     f"{r['mean_prompt_tokens']:.0f} | {r['mean_output_tokens']:.0f} | ${r['usd_per_1k_calls']:.3f} | "
                     f"{d['diff'] * 100:+.1f} ({d['lo'] * 100:+.1f} to {d['hi'] * 100:+.1f}) | "
                     f"{'yes' if r['within_5_points'] else 'no'} | {r['saving_multiple_vs_top']}x |")
    return "\n".join(lines)


def cheapest_within(results: dict) -> str | None:
    ok = [(r["usd_per_1k_calls"], n) for n, r in results.items() if r["within_5_points"] and r["errors"] == 0]
    return min(ok)[1] if ok else None
