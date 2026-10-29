"""Fine-tune fallback: distil Ultra's outputs into a small open model on Token Factory.

Only offered when the verdict says prompting tops out. Token Factory fine-tunes Qwen / Llama / Gemma etc.,
not Nemotron, and serving the result needs the custom-weights beta.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import models
from .models import BASE_URL, api_key
from .tasks import as_text

SUPPORTED_BASES = ["Qwen/Qwen3-4B", "meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen3-0.6B"]
DEFAULT_BASE = "Qwen/Qwen3-4B"
DEFAULT_HPARAMS = {"n_epochs": 2, "lora": True, "lora_r": 16, "lora_alpha": 32, "batch_size": 8,
                   "learning_rate": 1e-4}


def build_dataset(system_prompt: str, train_rows: list[dict], ultra_outputs: dict[str, str]) -> list[dict]:
    """Chat rows: system + train input -> Ultra's output (falls back to the reference if Ultra's is missing)."""
    rows = []
    for r in train_rows:
        answer = ultra_outputs.get(str(r["id"])) or (as_text(r["reference"]) if r.get("reference") is not None else "")
        if not answer.strip():
            continue
        rows.append({"messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": as_text(r["input"])},
            {"role": "assistant", "content": answer},
        ]})
    return rows


def write_dataset(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


def _client(client=None):
    if client is not None:
        return client
    from openai import OpenAI
    key = api_key()
    if not key:
        raise RuntimeError("NEBIUS_API_KEY is not set")
    return OpenAI(base_url=BASE_URL, api_key=key)


def launch(run_id: str, system_prompt: str, train_rows: list[dict], ultra_outputs: dict[str, str],
           base_model: str = DEFAULT_BASE, hyperparameters: dict | None = None, client=None) -> dict:
    if base_model not in SUPPORTED_BASES:
        raise ValueError(f"base model must be one of {SUPPORTED_BASES}")
    rows = build_dataset(system_prompt, train_rows, ultra_outputs)
    if len(rows) < 10:
        raise ValueError(f"need at least 10 training rows, have {len(rows)}")
    path = write_dataset(rows, models.RUNS_DIR / f"{run_id}_ft_train.jsonl")
    c = _client(client)
    with open(path, "rb") as fh:
        f = c.files.create(file=fh, purpose="fine-tune")
    job = c.fine_tuning.jobs.create(model=base_model, training_file=f.id, suffix=f"downshift-{run_id}"[:40],
                                    hyperparameters=hyperparameters or DEFAULT_HPARAMS)
    info = {"job_id": job.id, "status": job.status, "base_model": base_model, "n_rows": len(rows),
            "training_file": f.id, "launched_at": time.time(), "run_id": run_id}
    (models.RUNS_DIR / f"{run_id}_finetune.json").write_text(json.dumps(info, indent=2))
    return info


def status(job_id: str, client=None) -> dict:
    c = _client(client)
    job = c.fine_tuning.jobs.retrieve(job_id)
    d = job.model_dump() if hasattr(job, "model_dump") else dict(job)
    out = {k: d.get(k) for k in ("id", "status", "model", "fine_tuned_model", "created_at", "finished_at",
                                 "trained_tokens", "error")}
    try:
        events = c.fine_tuning.jobs.list_events(job_id).data[:5]
        out["events"] = [{"created_at": e.created_at, "level": e.level, "message": e.message} for e in events]
    except Exception:  # noqa: BLE001
        out["events"] = []
    return out


def poll(job_id: str, client=None, interval_s: float = 30, timeout_s: float = 3600, sleep=time.sleep) -> dict:
    t0 = time.time()
    while True:
        s = status(job_id, client)
        if s["status"] in ("succeeded", "failed", "cancelled") or time.time() - t0 > timeout_s:
            return s
        sleep(interval_s)
