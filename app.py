"""Downshift web server: static UI, presets, runs streamed over server-sent events."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downshift import finetune, models
from downshift.engine import DEFAULTS, run_downshift
from downshift.grading import Grader, backend_info
from downshift.models import RUNGS, TIERS, LLM, Meter, api_key, get_pricing
from downshift.tasks import custom_task, list_presets, load_preset

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEMO_RUNS = ROOT / "demo_runs"
MAX_ACTIVE_RUNS = int(os.environ.get("DOWNSHIFT_MAX_ACTIVE_RUNS", "2"))
SERVER_CAP_USD = float(os.environ.get("DOWNSHIFT_SERVER_CAP_USD", "5.0"))  # per process lifetime
MAX_RUN_CAP_USD = float(os.environ.get("DOWNSHIFT_MAX_RUN_CAP_USD", "0.50"))
ALLOW_FINETUNE = os.environ.get("DOWNSHIFT_ALLOW_FINETUNE") == "1"
RUN_ID_RE = re.compile(r"^[a-f0-9]{6,32}$")

app = FastAPI(title="Downshift")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Hooks so tests can inject fakes without touching the network.
app.state.llm_factory = lambda run_id, cap: LLM(run_id=run_id, meter=Meter(cap_usd=cap), concurrency=16)
app.state.grader_factory = Grader


class RunState:
    def __init__(self, run_id: str, meta: dict):
        self.run_id = run_id
        self.meta = meta
        self.events: list[dict] = []
        self.done = False
        self.cond = asyncio.Condition()
        self.task_rows: dict = {}

    def to_json(self) -> dict:
        verdict = next((e["verdict"] for e in reversed(self.events) if e["type"] == "verdict"), None)
        return {"run_id": self.run_id, "meta": self.meta, "done": self.done, "events": self.events,
                "verdict": verdict}


RUNS: dict[str, RunState] = {}
SERVER_SPEND = {"usd": 0.0}


def _run_path(run_id: str) -> Path:
    return models.RUNS_DIR / f"{run_id}.json"


def _save(state: RunState) -> None:
    models.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _run_path(state.run_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_json(), default=str), encoding="utf-8")
    os.replace(tmp, _run_path(state.run_id))


def _load_saved(run_id: str) -> dict | None:
    if not RUN_ID_RE.match(run_id):
        return None
    for p in (_run_path(run_id), DEMO_RUNS / f"{run_id}.json"):  # demo_runs/ ships in the image
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


async def _drive(state: RunState, task: dict, options: dict) -> None:
    cap = float(options["spend_cap_usd"])
    llm = app.state.llm_factory(state.run_id, cap)
    try:
        async for ev in run_downshift(task, options=options, llm=llm, grader=app.state.grader_factory(),
                                      run_id=state.run_id):
            async with state.cond:
                state.events.append(ev)
                if ev["type"] == "done":
                    state.done = True
                state.cond.notify_all()
            if ev["type"] in ("eval", "round", "verdict", "photo", "guess", "done", "grader", "aborted", "error"):
                _save(state)
    except Exception as e:  # noqa: BLE001 - never leave a run hanging
        async with state.cond:
            state.events.append({"type": "error", "t": time.time(), "message": f"{type(e).__name__}: {e}"[:300]})
            state.events.append({"type": "done", "t": time.time(), "ok": False})
            state.done = True
            state.cond.notify_all()
    finally:
        SERVER_SPEND["usd"] += llm.meter.spent_usd
        state.done = True
        _save(state)


class RunRequest(BaseModel):
    preset: str | None = None
    custom: dict | None = None
    options: dict | None = None


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    info = await asyncio.to_thread(backend_info)
    pricing = get_pricing()
    return {"ok": True, "backend": "Nebius Token Factory", "models": [t["model"] for t in TIERS],
            "key_present": bool(api_key()), "grader": info, "pricing_source": pricing["_source"],
            "active_runs": sum(1 for r in RUNS.values() if not r.done),
            "finetune_enabled": ALLOW_FINETUNE, "server_spend_usd": round(SERVER_SPEND["usd"], 4),
            "max_run_cap_usd": MAX_RUN_CAP_USD}


@app.get("/api/presets")
async def presets():
    return list_presets()


@app.get("/api/tiers")
async def tiers():
    p = get_pricing()
    return [r | {"price_in_per_m": round(p["models"].get(r["model"], {}).get("prompt", 0) * 1e6, 4),
                 "price_out_per_m": round(p["models"].get(r["model"], {}).get("completion", 0) * 1e6, 4)}
            for r in RUNGS]


@app.post("/api/run")
async def start_run(req: RunRequest):
    if sum(1 for r in RUNS.values() if not r.done) >= MAX_ACTIVE_RUNS:
        raise HTTPException(429, "Too many runs in progress. Try again in a minute.")
    if SERVER_SPEND["usd"] >= SERVER_CAP_USD:
        raise HTTPException(503, "This demo server has reached its spending limit.")
    try:
        if req.preset:
            task = load_preset(req.preset)
        elif req.custom:
            task = custom_task(req.custom)
        else:
            raise HTTPException(400, "Give a preset slug or a custom task.")
    except KeyError:
        raise HTTPException(404, f"Unknown preset {req.preset!r}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    opts = dict(DEFAULTS)
    for k, v in (req.options or {}).items():
        if k in ("tolerance", "rounds", "candidates", "spend_cap_usd") and v is not None:
            opts[k] = v
        if k in ("ultra_thinking", "thinking_rungs", "photo_check", "guess_check") and v is not None:
            opts[k] = bool(v)
        if k == "reference" and v in ("cached", "live"):
            opts[k] = v
    opts["spend_cap_usd"] = min(float(opts["spend_cap_usd"]), MAX_RUN_CAP_USD)
    opts["rounds"] = max(0, min(int(opts["rounds"]), 4))
    opts["candidates"] = max(1, min(int(opts["candidates"]), 3))
    opts["tolerance"] = max(0.0, min(float(opts["tolerance"]), 0.2))
    run_id = uuid.uuid4().hex[:12]
    state = RunState(run_id, {"created": time.time(), "task": {k: task.get(k) for k in
                                                                ("slug", "title", "business_story", "source")},
                              "options": opts})
    state.task_rows = {"train": task["train"], "validation": task.get("validation", []), "holdout": task["holdout"]}
    RUNS[run_id] = state
    asyncio.create_task(_drive(state, task, opts))
    return {"run_id": run_id, "events_url": f"/api/run/{run_id}/events", "page_url": f"/?run={run_id}"}


@app.get("/api/run/{run_id}")
async def get_run(run_id: str):
    if run_id in RUNS:
        return RUNS[run_id].to_json()
    saved = _load_saved(run_id)
    if not saved:
        raise HTTPException(404, "run not found")
    return saved


def _sse(ev: dict, idx: int) -> str:
    return f"id: {idx}\nevent: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"


@app.get("/api/run/{run_id}/events")
async def run_events(run_id: str, request: Request, start: int = 0, speed: float = 0):
    state = RUNS.get(run_id)
    if state is None:
        saved = _load_saved(run_id)
        if not saved:
            raise HTTPException(404, "run not found")
        speed = min(max(speed, 0), 64)  # 0 = all at once; otherwise the recorded pace divided by speed

        async def replay():
            prev = None
            for i, ev in enumerate(saved["events"][start:], start):
                if speed and prev is not None and ev.get("t") is not None:
                    await asyncio.sleep(min(30, max(0, ev["t"] - prev) / speed))
                prev = ev.get("t", prev)
                yield _sse(ev, i)
            yield "event: end\ndata: {}\n\n"
        return StreamingResponse(replay(), media_type="text/event-stream")

    last = request.headers.get("last-event-id")
    idx = int(last) + 1 if last and last.isdigit() else start

    async def live():
        nonlocal idx
        while True:
            async with state.cond:
                while idx >= len(state.events) and not state.done:
                    try:
                        await asyncio.wait_for(state.cond.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        break
                batch = state.events[idx:]
                finished = state.done and idx + len(batch) >= len(state.events)
            if not batch and not finished:
                yield ": keep-alive\n\n"
            for ev in batch:
                yield _sse(ev, idx)
                idx += 1
            if finished:
                yield "event: end\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return

    return StreamingResponse(live(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class FinetuneRequest(BaseModel):
    confirm: bool = False
    base_model: str = finetune.DEFAULT_BASE


@app.post("/api/run/{run_id}/finetune")
async def start_finetune(run_id: str, req: FinetuneRequest):
    if not ALLOW_FINETUNE:
        raise HTTPException(403, "Fine-tune launches are turned off on this server.")
    if not req.confirm:
        raise HTTPException(400, "Set confirm=true to launch a paid fine-tune job.")
    data = RUNS[run_id].to_json() if run_id in RUNS else _load_saved(run_id)
    if not data or not data.get("verdict"):
        raise HTTPException(404, "finished run not found")
    v = data["verdict"]
    if not v.get("finetune_offer"):
        raise HTTPException(400, "This run's verdict does not call for fine-tuning.")
    slug = (data["meta"].get("task") or {}).get("slug")
    if run_id in RUNS:
        train = RUNS[run_id].task_rows["train"]
    else:
        try:
            train = load_preset(slug)["train"]
        except KeyError:
            raise HTTPException(400, "Training rows for this run are no longer available.") from None
    try:
        info = await asyncio.to_thread(finetune.launch, run_id, v["system_prompt"], train,
                                       v.get("ultra_train_outputs") or {}, req.base_model)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return info


@app.get("/api/finetune/{job_id}")
async def finetune_status(job_id: str):
    if not re.match(r"^[A-Za-z0-9_\-:.]{4,80}$", job_id):
        raise HTTPException(400, "bad job id")
    return await asyncio.to_thread(finetune.status, job_id)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    detail = getattr(exc, "detail", "not found")
    return JSONResponse({"detail": detail}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
