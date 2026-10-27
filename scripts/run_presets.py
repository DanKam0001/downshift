"""Run the full pipeline once per preset through a running server, the same way the UI does.

    python -m uvicorn app:app --port 8000          # in another terminal
    python scripts/run_presets.py                   # all presets, default options
    python scripts/run_presets.py support-triage --base http://localhost:8000

Prints each run's verdict, spend and wall time. Runs are saved by the server in runs/<id>.json.
"""
from __future__ import annotations

import argparse
import json
import time

import httpx


def run_one(base: str, slug: str, options: dict) -> dict:
    t0 = time.time()
    r = httpx.post(f"{base}/api/run", json={"preset": slug, "options": options}, timeout=30)
    r.raise_for_status()
    run_id = r.json()["run_id"]
    done = None
    with httpx.stream("GET", f"{base}/api/run/{run_id}/events", timeout=None) as s:
        for line in s.iter_lines():
            if line.startswith("data: ") and line != "data: {}":
                ev = json.loads(line[6:])
                if ev["type"] in ("stage", "notice", "tier_done", "aborted", "error"):
                    print(f"  [{time.time() - t0:5.0f}s] {ev.get('message') or ev.get('reason') or ev['type']}")
                if ev["type"] == "done":
                    done = ev
    wall = round(time.time() - t0, 1)
    saved = httpx.get(f"{base}/api/run/{run_id}", timeout=30).json()
    v = saved.get("verdict") or {}
    print(f"{slug}: run {run_id}, {wall} s wall, ${(done or {}).get('spend_usd', 0):.4f}, "
          f"{(done or {}).get('calls')} calls\n  {v.get('sentence')}")
    return {"slug": slug, "run_id": run_id, "wall_s": wall, "spend_usd": (done or {}).get("spend_usd")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*", default=["support-triage", "receipt-extract", "contract-clauses"])
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--cap", type=float, default=0.10)
    a = ap.parse_args()
    out = [run_one(a.base, s, {"spend_cap_usd": a.cap}) for s in a.slugs]
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
