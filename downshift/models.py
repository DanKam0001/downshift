"""Nemotron tiers on Nebius Token Factory: pricing, calls, and per-call accounting.

Every call is measured (tokens, latency, US$) and appended to runs/usage.jsonl.
The API key is read from the environment (or .env) and never logged.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BASE_URL = "https://api.tokenfactory.nebius.com/v1"
ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = Path(os.environ.get("DOWNSHIFT_RUNS_DIR", ROOT / "runs"))
USAGE_LOG = RUNS_DIR / "usage.jsonl"
PRICING_CACHE = RUNS_DIR / "pricing_cache.json"
PRICING_TTL_S = 6 * 3600

# Cheapest first. Ultra is the quality reference.
TIERS = [
    {"key": "lightning", "name": "Nemotron 3.5 Lightning", "model": "nvidia/Nemotron-3_5-Lightning"},
    {"key": "nano", "name": "Nemotron 3 Nano 30B", "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"},
    {"key": "super", "name": "Nemotron 3 Super 120B", "model": "nvidia/nemotron-3-super-120b-a12b"},
    {"key": "ultra", "name": "Nemotron 3 Ultra 550B", "model": "nvidia/Nemotron-3-Ultra-550b-a55b"},
]
ULTRA = TIERS[-1]["model"]

# A rung is a (model, thinking on/off) pair. All Nemotron tiers are reasoning models; thinking moves cost and
# latency more than model size does, so it is its own step on the ladder. Ordered by measured cost per call on the
# presets (Lightning with thinking on costs more per call than Super with thinking off, because it writes
# 1,000-4,000 reasoning tokens); the two Ultra rungs are the quality reference.
def _rung(tier_key: str, thinking: bool) -> dict:
    t = next(x for x in TIERS if x["key"] == tier_key)
    return {"key": t["key"] + ("-think" if thinking else ""), "tier": t["key"], "name": t["name"],
            "model": t["model"], "thinking": thinking,
            "label": t["name"] + (" (thinking on)" if thinking else " (thinking off)")}


RUNGS = [_rung("lightning", False), _rung("nano", False), _rung("super", False), _rung("lightning", True),
         _rung("ultra", False), _rung("ultra", True)]
CHEAP_RUNGS = RUNGS[:4]
ULTRA_OFF, ULTRA_ON = RUNGS[4], RUNGS[5]
THINKING_BUDGET = 4096  # extra max_tokens allowed for reasoning when thinking is on


def rung_by_key(key: str) -> dict:
    return next(r for r in RUNGS if r["key"] == key)

# Used only if the live /models call fails. US$ per token, checked 2026-10-27.
FALLBACK_PRICING = {
    "nvidia/Nemotron-3_5-Lightning": {"prompt": 6e-8, "completion": 2.4e-7, "cache_read": None},
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": {"prompt": 6e-8, "completion": 2.4e-7, "cache_read": None},
    "nvidia/nemotron-3-super-120b-a12b": {"prompt": 3e-7, "completion": 9e-7, "cache_read": 6e-8},
    "nvidia/Nemotron-3-Ultra-550b-a55b": {"prompt": 1e-6, "completion": 3e-6, "cache_read": 1.2e-7},
}

_log_lock = threading.Lock()


def append_line(path: Path, line: str) -> None:
    """Append one line so that concurrent writers (threads or processes) never interleave.

    The line goes out in a single write() under an OS file lock (msvcrt on Windows, fcntl elsewhere); several
    processes (the server and scripts/measure_reference.py) share runs/usage.jsonl.
    """
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    with _log_lock, open(path, "ab", buffering=0) as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # retries for ~10 s, then raises OSError
            try:
                f.seek(0, 2)
                f.write(data)
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(data)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
_pricing_mem: dict | None = None


def load_env(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from .env into os.environ without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_key() -> str | None:
    load_env()
    return os.environ.get("NEBIUS_API_KEY") or None


def tier_by_model(model: str) -> dict:
    for t in TIERS:
        if t["model"] == model:
            return t
    return {"key": model, "name": model, "model": model}


def _parse_pricing(data: dict) -> dict:
    out = {}
    for m in data.get("data", []):
        p = m.get("pricing") or {}
        try:
            out[m["id"]] = {
                "prompt": float(p.get("prompt") or 0),
                "completion": float(p.get("completion") or 0),
                "cache_read": float(p["input_cache_read"]) if p.get("input_cache_read") else None,
            }
        except (TypeError, ValueError):
            continue
    return out


def get_pricing(force: bool = False) -> dict:
    """{model_id: {prompt, completion, cache_read}} in US$ per token, plus '_source'."""
    global _pricing_mem
    if _pricing_mem and not force and time.time() - _pricing_mem["_fetched"] < PRICING_TTL_S:
        return _pricing_mem
    if not force and PRICING_CACHE.exists():
        try:
            cached = json.loads(PRICING_CACHE.read_text())
            if time.time() - cached["_fetched"] < PRICING_TTL_S:
                _pricing_mem = cached
                return cached
        except Exception:
            pass
    key = api_key()
    try:
        if not key:
            raise RuntimeError("no key")
        r = httpx.get(f"{BASE_URL}/models", params={"verbose": "true"},
                      headers={"Authorization": f"Bearer {key}"}, timeout=20)
        r.raise_for_status()
        prices = _parse_pricing(r.json())
        if not all(t["model"] in prices for t in TIERS):
            raise RuntimeError("tier missing from /models")
        result = {"models": prices, "_source": "live /v1/models", "_fetched": time.time()}
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        PRICING_CACHE.write_text(json.dumps(result))
    except Exception:
        result = {"models": dict(FALLBACK_PRICING), "_source": "fallback table (2026-10-27)",
                  "_fetched": time.time()}
    _pricing_mem = result
    return result


def call_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0,
              pricing: dict | None = None) -> float:
    p = (pricing or get_pricing())["models"].get(model) or FALLBACK_PRICING.get(model)
    if not p:
        return 0.0
    cached = min(cached_tokens or 0, prompt_tokens)
    cache_price = p["cache_read"] if p.get("cache_read") is not None else p["prompt"]
    return (prompt_tokens - cached) * p["prompt"] + cached * cache_price + completion_tokens * p["completion"]


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def clean_answer(text: str | None) -> str:
    """Remove reasoning blocks so only the final answer is graded."""
    if not text:
        return ""
    text = _THINK_RE.sub("", text)
    if "</think>" in text.lower():  # unopened block: keep what follows the last close tag
        text = re.split(r"</think>", text, flags=re.I)[-1]
    return text.strip()


class BudgetExceeded(Exception):
    pass


@dataclass
class Meter:
    """Per-run spend tracker with a hard cap."""
    cap_usd: float = 0.10
    spent_usd: float = 0.0
    calls: int = 0
    by_model: dict = field(default_factory=dict)

    def check(self) -> None:
        if self.spent_usd >= self.cap_usd:
            raise BudgetExceeded(f"spend cap ${self.cap_usd:.2f} reached (${self.spent_usd:.4f} spent)")

    def add(self, model: str, cost: float) -> None:
        self.spent_usd += cost
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0.0) + cost


@dataclass
class CallResult:
    model: str
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    ok: bool = True
    error: str | None = None
    truncated: bool = False   # hit max_tokens; with thinking on this means no answer was written


def _retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status == 429 or status >= 500
    name = type(exc).__name__
    return name in ("APITimeoutError", "APIConnectionError", "TimeoutError", "ReadTimeout")


class LLM:
    """Async Token Factory client with bounded concurrency, retries and accounting."""

    def __init__(self, run_id: str = "adhoc", meter: Meter | None = None, concurrency: int = 8,
                 max_attempts: int = 4, client=None):
        self.run_id = run_id
        self.meter = meter or Meter(cap_usd=float("inf"))
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self._client = client
        self._sems: dict[str, asyncio.Semaphore] = {}
        self.pricing = get_pricing()

    @property
    def client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            key = api_key()
            if not key:
                raise RuntimeError("NEBIUS_API_KEY is not set")
            self._client = AsyncOpenAI(base_url=BASE_URL, api_key=key, max_retries=0, timeout=90)
        return self._client

    def _sem(self, model: str) -> asyncio.Semaphore:
        if model not in self._sems:
            self._sems[model] = asyncio.Semaphore(self.concurrency)
        return self._sems[model]

    async def chat(self, model: str, messages: list[dict], max_tokens: int = 512, temperature: float = 0.0,
                   purpose: str = "", thinking: bool = False) -> CallResult:
        self.meter.check()
        async with self._sem(model):
            self.meter.check()
            last_err = None
            for attempt in range(self.max_attempts):
                t0 = time.perf_counter()
                try:
                    resp = await self.client.chat.completions.create(
                        model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
                        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
                    )
                    latency_ms = (time.perf_counter() - t0) * 1000
                    u = resp.usage
                    pt = getattr(u, "prompt_tokens", 0) or 0
                    ct = getattr(u, "completion_tokens", 0) or 0
                    cached = getattr(u, "prompt_cache_hit_tokens", 0) or 0
                    if not cached and getattr(u, "prompt_tokens_details", None) is not None:
                        cached = getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0
                    cost = call_cost(model, pt, ct, cached, self.pricing)
                    self.meter.add(model, cost)
                    choice = resp.choices[0] if resp.choices else None
                    text = clean_answer(choice.message.content if choice else "")
                    truncated = bool(choice and choice.finish_reason == "length")
                    if truncated and thinking:
                        text = ""  # ran out of tokens while reasoning: count as a failed answer
                    res = CallResult(model, text, pt, ct, cost, latency_ms, truncated=truncated)
                    self._log(res, purpose, attempt)
                    return res
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if not _retryable(exc) or attempt == self.max_attempts - 1:
                        break
                    await asyncio.sleep(min(2 ** attempt, 8) + 0.1 * attempt)
            res = CallResult(model, "", 0, 0, 0.0, 0.0, ok=False, error=f"{type(last_err).__name__}: {str(last_err)[:200]}")
            self._log(res, purpose, self.max_attempts)
            return res

    def _log(self, res: CallResult, purpose: str, attempt: int) -> None:
        rec = {"ts": round(time.time(), 3), "run_id": self.run_id, "model": res.model, "purpose": purpose,
               "truncated": res.truncated,
               "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens,
               "cost_usd": round(res.cost_usd, 8), "latency_ms": round(res.latency_ms, 1),
               "ok": res.ok, "attempts": attempt + 1, "error": res.error}
        try:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            append_line(USAGE_LOG, json.dumps(rec))
        except OSError:
            pass
