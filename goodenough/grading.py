"""Grader front door.

Model-written checker code runs in Nebius Sandboxes via goodenough.sandbox_grader (built separately).
If that module is missing or the sandbox call fails, we fall back to a local subprocess and say so
in the events, so the UI never claims Sandboxes when it did not use them.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass


@dataclass
class GradeResult:
    item_id: str
    score: float
    passed: bool
    detail: str = ""
    error: str | None = None


_HARNESS = r'''
import json, sys
src = open(sys.argv[1], encoding="utf-8").read()
items = json.load(open(sys.argv[2], encoding="utf-8"))
ns = {}
out = []
try:
    exec(compile(src, "checker.py", "exec"), ns)
    check = ns["check"]
except Exception as e:
    for it in items:
        out.append({"item_id": it["id"], "score": 0.0, "passed": False, "detail": "", "error": "checker load: %r" % (e,)})
    print(json.dumps(out)); sys.exit(0)
for it in items:
    try:
        r = check(it["output"], it["reference"], it["input"])
        score, detail = (r if isinstance(r, (tuple, list)) else (r, ""))[:2]
        score = max(0.0, min(1.0, float(score)))
        out.append({"item_id": it["id"], "score": score, "passed": score >= 0.999, "detail": str(detail)[:300], "error": None})
    except Exception as e:
        out.append({"item_id": it["id"], "score": 0.0, "passed": False, "detail": "", "error": "check: %r" % (e,)})
print(json.dumps(out))
'''


def grade_batch_local_stub(checker_code: str, items: list[dict], timeout_s: int = 120) -> list[GradeResult]:
    """Run the checker in a separate local Python process (fallback only)."""
    with tempfile.TemporaryDirectory() as d:
        src, data, harness = (os.path.join(d, n) for n in ("checker.py", "items.json", "harness.py"))
        open(src, "w", encoding="utf-8").write(checker_code)
        json.dump(items, open(data, "w", encoding="utf-8"))
        open(harness, "w", encoding="utf-8").write(_HARNESS)
        try:
            p = subprocess.run([sys.executable, "-I", harness, src, data], capture_output=True, text=True,
                               timeout=timeout_s, cwd=d)
            rows = json.loads(p.stdout.strip().splitlines()[-1])
            return [GradeResult(**r) for r in rows]
        except Exception as e:  # noqa: BLE001
            return [GradeResult(it["id"], 0.0, False, "", f"local grader: {e!r}"[:300]) for it in items]


def _sandbox_module():
    if os.environ.get("DOWNSHIFT_FORCE_LOCAL_GRADER") == "1":
        return None
    try:
        from goodenough import sandbox_grader  # type: ignore
        return sandbox_grader
    except Exception:  # noqa: BLE001
        return None


def backend_info() -> dict:
    mod = _sandbox_module()
    if mod is None:
        return {"backend": "local", "label": "Local subprocess (Sandboxes module not available)"}
    try:
        info = mod.sandbox_info()
    except Exception as e:  # noqa: BLE001
        info = {"error": str(e)[:200]}
    return {"backend": "sandbox", "label": "Nebius Sandboxes", "sandbox_info": info}


def _norm(r) -> GradeResult:
    if isinstance(r, GradeResult):
        return r
    if isinstance(r, dict):
        g = r
    else:
        g = {k: getattr(r, k, None) for k in ("item_id", "score", "passed", "detail", "error")}
    return GradeResult(str(g.get("item_id")), float(g.get("score") or 0.0), bool(g.get("passed")),
                       str(g.get("detail") or ""), g.get("error"))


class Grader:
    """Calls the sandbox grader, falls back to local, and records which backend graded each batch."""

    def __init__(self, sandbox_fn=None, local_fn=None):
        mod = _sandbox_module()
        self.with_meta = sandbox_fn is None and mod is not None and hasattr(mod, "grade_batch_with_meta")
        if sandbox_fn is not None:
            self.sandbox_fn = sandbox_fn
        elif mod is not None:
            # Prefer branching from a sealed answer-key checkpoint; it falls back to one-shot by itself.
            self.sandbox_fn = (getattr(mod, "grade_batch_sealed_with_meta", None) or mod.grade_batch_with_meta
                               if self.with_meta else mod.grade_batch)
        else:
            self.sandbox_fn = None
        self.local_fns = [local_fn] if local_fn is not None else (
            [mod.grade_batch_local, grade_batch_local_stub] if mod is not None and hasattr(mod, "grade_batch_local")
            else [grade_batch_local_stub])
        self.backends_used: list[str] = []
        self.batches: list[dict] = []
        self.last_error: str | None = None

    async def grade(self, checker_code: str, items: list[dict], timeout_s: int = 120) -> tuple[list[GradeResult], dict]:
        """Returns (results, batch) where batch = {backend: 'sandbox'|'local', label, operation_id?, wall_s?,
        checkpoint?, branched, seal_operation_id?, fallback_reason?}. branched=True means the batch ran in a
        disposable branch of the sealed answer-key image `checkpoint` (see sandbox_grader)."""
        if not items:
            return [], {"backend": "none", "label": "nothing to grade", "n": 0}
        items = [{"id": str(i["id"]), "input": i.get("input"), "output": i.get("output", ""),
                  "reference": i.get("reference")} for i in items]
        if self.sandbox_fn is not None:
            try:
                out = await asyncio.to_thread(self.sandbox_fn, checker_code, items, timeout_s)
                meta = None
                if self.with_meta:
                    out, meta = out
                res = [_norm(r) for r in out]
                if len(res) == len(items):
                    return res, self._record("sandbox", len(items), meta)
                self.last_error = f"sandbox returned {len(res)} results for {len(items)} items"
            except Exception as e:  # noqa: BLE001
                self.last_error = f"sandbox: {type(e).__name__}: {str(e)[:200]}"
        err = None
        for fn in self.local_fns:
            try:
                res = await asyncio.to_thread(fn, checker_code, items, timeout_s)
                if len(res) == len(items):
                    rec = self._record("local", len(items), None)
                    return [_norm(r) for r in res], rec
            except Exception as e:  # noqa: BLE001
                err = e
        raise RuntimeError(f"no grader could run this batch: {err!r}")

    def _record(self, backend: str, n: int, meta) -> dict:
        self.backends_used.append(backend)
        rec = {"backend": backend, "n": n, "branched": False, "checkpoint": None, "seal_operation_id": None}
        if meta is not None:
            rec.update({"label": getattr(meta, "label", None) or getattr(meta, "backend", None),
                        "operation_id": getattr(meta, "operation_id", None), "wall_s": getattr(meta, "wall_s", None)})
            for k in ("checkpoint", "branched", "seal_operation_id", "sealed_now", "seal_s", "key_sha256",
                      "fallback_reason"):
                if getattr(meta, k, None) is not None:
                    rec[k] = getattr(meta, k)
        else:
            rec["label"] = "Nebius Sandboxes" if backend == "sandbox" else "graded locally"
            if backend == "local" and self.last_error:
                rec["fallback_reason"] = self.last_error
        self.batches.append(rec)
        return rec
