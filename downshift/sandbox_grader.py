"""Run model-written checker code against model outputs inside Nebius Token Factory Sandboxes.

The checker is untrusted, model-written Python. It never runs on our server in the normal path:
`grade_batch` ships the checker plus every item to ONE disposable Sandboxes operation with the guest
network interface disabled, where a small harness runs `check()` per item in a forked child with a
per-item time limit, and prints the results as one JSON line.

`grade_batch_local` runs the same harness in a local subprocess. It is an explicit fallback / test
path only; callers must label which backend graded (see `GradeBatchMeta.backend`).

Credentials (NEBIUS_API_KEY, NEBIUS_PROJECT_ID) are read from the environment or the repo `.env`.
They are only ever sent as HTTP headers to the Sandboxes API, never into the sandbox payload.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

API_BASE = os.environ.get("CONTREE_BASE_URL", "https://api.tokenfactory.nebius.com/sandboxes").rstrip("/") + "/v1"
IMAGE_TAG = "python:3.12-slim"          # public image already present in Sandboxes; no import needed
PER_ITEM_TIMEOUT_S = 3.0                # wall-clock limit for one check() call
PASS_THRESHOLD = 0.999                  # passed = no error and score ~= 1.0 (fully correct)
MAX_DETAIL = 200
RESULT_MARKER = "__DOWNSHIFT_RESULTS__"
_TERMINAL = {"SUCCESS", "FAILED", "CANCELLED"}
_REPO_ROOT = Path(__file__).resolve().parent.parent


class SandboxError(RuntimeError):
    """The sandbox platform could not produce results (submission, platform failure, no output).

    Per-item checker crashes/timeouts are NOT raised; they come back as GradeResult.error.
    """


@dataclass
class GradeResult:
    item_id: str
    score: float        # 0.0..1.0
    passed: bool
    detail: str         # short reason, <=200 chars
    error: str | None   # set if checker crashed/timed out for this item


@dataclass
class GradeBatchMeta:
    backend: str                         # "Nebius Sandboxes" or "local subprocess (fallback)"
    operation_id: str | None = None
    image: str | None = None
    networking: bool | None = None
    wall_s: float = 0.0                  # client-side total, submit -> parsed results
    guest_elapsed_s: float | None = None  # elapsed_time reported by the platform
    harness_s: float | None = None       # time the harness itself measured inside the guest
    workers: int | None = None
    mode: str | None = None              # "fork" or "subprocess"
    extra: dict = field(default_factory=dict)


LAST_BATCH_META: GradeBatchMeta | None = None


# --------------------------------------------------------------------------------------------
# Harness: runs INSIDE the sandbox (or the local subprocess). Pure stdlib, Python 3.8+.
# The payload placeholder is replaced with base64(JSON) before submission.
# --------------------------------------------------------------------------------------------
_HARNESS = r'''
import base64, json, math, os, sys, time
P = json.loads(base64.b64decode("__PAYLOAD__").decode("utf-8"))
CODE, ITEMS = P["code"], P["items"]
PER_ITEM, BUDGET, MARK, MAXD = float(P["per_item"]), float(P["budget"]), P["marker"], int(P["max_detail"])
MEM = 1024 * 1024 * 1024
T0 = time.monotonic()

def short(s, n=MAXD):
    s = str(s).replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."

def norm(out):
    if isinstance(out, (tuple, list)):
        if len(out) == 0:
            raise TypeError("check() returned an empty tuple")
        score, reason = out[0], (out[1] if len(out) > 1 else "")
    else:
        score, reason = out, ""
    if isinstance(score, bool):
        score = 1.0 if score else 0.0
    score = float(score)
    if math.isnan(score):
        score = 0.0
    return min(1.0, max(0.0, score)), short(reason)

def run_one(fn, it):
    try:
        score, reason = norm(fn(str(it.get("output") or ""), str(it.get("reference") or ""), str(it.get("input") or "")))
        return {"score": score, "detail": reason, "error": None}
    except BaseException as e:
        return {"score": 0.0, "detail": "", "error": short("%s: %s" % (type(e).__name__, e))}

def load():
    ns = {"__name__": "checker"}
    exec(compile(CODE, "checker.py", "exec"), ns)
    fn = ns.get("check")
    if not callable(fn):
        raise TypeError("checker code defines no callable check()")
    return fn

def err(msg):
    return {"score": 0.0, "detail": "", "error": short(msg)}

N = len(ITEMS)
RES = [None] * N
MODE = "fork" if hasattr(os, "fork") else "subprocess"
WORKERS = max(2, min(8, (os.cpu_count() or 1) * 2))

def run_fork(fn):
    import select, signal
    pending = list(range(N))
    running = {}  # read fd -> [idx, pid, started, bytes]
    def reap(fd, killed_msg=None):
        idx, pid, _, buf = running.pop(fd)
        if killed_msg:
            try: os.kill(pid, signal.SIGKILL)
            except Exception: pass
        try: os.close(fd)
        except Exception: pass
        try: _, status = os.waitpid(pid, 0)
        except Exception: status = -1
        if killed_msg:
            RES[idx] = err(killed_msg); return
        try:
            RES[idx] = json.loads(buf.decode("utf-8"))
        except Exception:
            RES[idx] = err("checker process died without a result (status %s)" % status)
    while pending or running:
        while pending and len(running) < WORKERS and time.monotonic() - T0 < BUDGET:
            i = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(r)
                    dn = os.open(os.devnull, os.O_RDWR)
                    os.dup2(dn, 0); os.dup2(dn, 1); os.dup2(dn, 2)
                    try:
                        import resource
                        resource.setrlimit(resource.RLIMIT_AS, (MEM, MEM))
                    except Exception:
                        pass
                    msg = run_one(fn, ITEMS[i])
                    try: data = json.dumps(msg).encode("utf-8")
                    except Exception: data = json.dumps(err("result not serialisable")).encode("utf-8")
                    view = memoryview(data)
                    while view:
                        n = os.write(w, view); view = view[n:]
                finally:
                    os._exit(0)
            os.close(w)
            running[r] = [i, pid, time.monotonic(), b""]
        if pending and time.monotonic() - T0 >= BUDGET:
            for i in pending: RES[i] = err("batch time budget exhausted before this item ran")
            pending = []
        if not running:
            continue
        now = time.monotonic()
        wait = min([PER_ITEM - (now - v[2]) for v in running.values()] + [0.5])
        ready, _, _ = select.select(list(running), [], [], max(0.0, wait))
        for fd in ready:
            chunk = os.read(fd, 65536)
            if chunk: running[fd][3] += chunk
            else: reap(fd)
        now = time.monotonic()
        for fd in [fd for fd, v in running.items() if now - v[2] >= PER_ITEM]:
            reap(fd, "timeout: check() exceeded %.1fs" % PER_ITEM)
        if now - T0 >= BUDGET + PER_ITEM:
            for fd in list(running): reap(fd, "batch time budget exhausted")

WORKER = r"""
import json, math, sys
P = json.loads(sys.stdin.read())
CODE = P["code"]; MAXD = P["max_detail"]
exec(P["harness_fns"], globals())
fn = load()
r = run_one(fn, P["item"])
sys.stdout.write("\n" + P["marker"] + json.dumps(r) + "\n")
"""

def run_subprocess():
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    fns = HARNESS_FNS
    def one(i):
        if time.monotonic() - T0 >= BUDGET:
            return err("batch time budget exhausted before this item ran")
        req = json.dumps({"code": CODE, "item": ITEMS[i], "marker": MARK, "max_detail": MAXD, "harness_fns": fns})
        try:
            p = subprocess.run([sys.executable, "-I", "-c", WORKER], input=req, capture_output=True,
                               text=True, timeout=PER_ITEM, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return err("timeout: check() exceeded %.1fs" % PER_ITEM)
        for line in reversed(p.stdout.splitlines()):
            if line.startswith(MARK):
                try: return json.loads(line[len(MARK):])
                except Exception: break
        tail = (p.stderr or "").strip().splitlines()
        return err("checker process died: " + (tail[-1] if tail else "exit %s" % p.returncode))
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, r in enumerate(ex.map(one, range(N))):
            RES[i] = r

load_error = None
try:
    if MODE == "fork":
        FN = load()
    else:
        load()  # validate once up front so a syntax error is reported for every item cheaply
except BaseException as e:
    load_error = "checker failed to load: %s: %s" % (type(e).__name__, e)

if load_error:
    RES = [err(load_error) for _ in range(N)]
elif MODE == "fork":
    run_fork(FN)
else:
    run_subprocess()

RES = [r if r is not None else err("no result") for r in RES]
sys.stdout.write("\n" + MARK + json.dumps({"results": RES, "harness_s": round(time.monotonic() - T0, 3),
                                         "mode": MODE, "workers": WORKERS, "load_error": load_error}) + "\n")
sys.stdout.flush()
'''

# The subprocess (Windows/local) worker re-uses the helper functions by exec'ing their source.
_HARNESS_FNS_SRC = _HARNESS[_HARNESS.index("def short("):_HARNESS.index("N = len(ITEMS)")]


def _build_script(checker_code: str, items: list[dict], timeout_s: int) -> tuple[str, float]:
    per_item = float(min(PER_ITEM_TIMEOUT_S, max(1.0, timeout_s / 2)))
    budget = float(max(2.0, timeout_s - per_item - 5))
    payload = {
        "code": checker_code,
        "items": [{"output": it.get("output"), "reference": it.get("reference"), "input": it.get("input")}
                  for it in items],
        "per_item": per_item,
        "budget": budget,
        "marker": RESULT_MARKER,
        "max_detail": MAX_DETAIL,
    }
    b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    script = _HARNESS.replace("__PAYLOAD__", b64, 1)
    script = script.replace("fns = HARNESS_FNS", "fns = " + repr(_HARNESS_FNS_SRC), 1)
    return script, per_item


def _parse_harness_stdout(stdout: str, items: list[dict]) -> tuple[list[GradeResult], dict]:
    data = None
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            data = json.loads(line[len(RESULT_MARKER):])
            break
    if data is None or len(data.get("results", [])) != len(items):
        raise SandboxError("harness produced no result line" if data is None else "harness result count mismatch")
    out = []
    for it, r in zip(items, data["results"]):
        score = float(r.get("score") or 0.0)
        error = r.get("error")
        detail = str(r.get("detail") or (error or ""))[:MAX_DETAIL]
        out.append(GradeResult(item_id=str(it.get("id")), score=score,
                               passed=error is None and score >= PASS_THRESHOLD,
                               detail=detail, error=error))
    return out, data


# --------------------------------------------------------------------------------------------
# Credentials + HTTP
# --------------------------------------------------------------------------------------------
def _creds() -> tuple[str, str]:
    key, project = os.environ.get("NEBIUS_API_KEY"), os.environ.get("NEBIUS_PROJECT_ID")
    if not (key and project):
        env_file = _REPO_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
                if k == "NEBIUS_API_KEY" and not key:
                    key = v
                elif k == "NEBIUS_PROJECT_ID" and not project:
                    project = v
    if not (key and project):
        raise SandboxError("NEBIUS_API_KEY / NEBIUS_PROJECT_ID not set")
    return key, project


def _http(timeout: float = 20.0) -> httpx.Client:
    key, project = _creds()
    return httpx.Client(base_url=API_BASE + "/", timeout=timeout,
                        headers={"Authorization": f"Bearer {key}", "Project": project})


_IMAGE_UUID: str | None = None


def _image_ref(http: httpx.Client) -> str:
    """Resolve IMAGE_TAG to its immutable UUID once per process (cheap GET, no import)."""
    global _IMAGE_UUID
    if _IMAGE_UUID:
        return _IMAGE_UUID
    try:
        r = http.get("images", params={"tag": IMAGE_TAG, "limit": 50})
        r.raise_for_status()
        for img in r.json().get("images", []):
            if img.get("tag") == IMAGE_TAG:
                _IMAGE_UUID = img["uuid"]
                return _IMAGE_UUID
    except httpx.HTTPError:
        pass
    return f"tag:{IMAGE_TAG}"


def _decode(stream: dict | None) -> str:
    if not stream:
        return ""
    value = stream.get("value", "")
    if stream.get("encoding") == "base64":
        return base64.b64decode(value).decode("utf-8", errors="replace")
    return value


def _safe(text: str) -> str:
    """Never let the API key leak into an exception message."""
    try:
        key, _ = _creds()
        return text.replace(key, "***") if key else text
    except SandboxError:
        return text


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------
def grade_batch_with_meta(checker_code: str, items: list[dict],
                          timeout_s: int = 120) -> tuple[list[GradeResult], GradeBatchMeta]:
    """Same as grade_batch, also returning operation id / timings for the UI and run logs."""
    global LAST_BATCH_META
    meta = GradeBatchMeta(backend="Nebius Sandboxes", networking=False)
    if not items:
        LAST_BATCH_META = meta
        return [], meta
    t0 = time.monotonic()
    script, _ = _build_script(checker_code, items, timeout_s)
    with _http() as http:
        image = _image_ref(http)
        meta.image = f"{IMAGE_TAG} ({image})"
        body = {
            "image": image,
            "command": "exec python3 -I -",
            "shell": True,
            "stdin": {"value": base64.b64encode(script.encode("utf-8")).decode("ascii"), "encoding": "base64"},
            "networking": {"enabled": False},
            "disposable": True,
            "timeout": int(timeout_s),
            "truncate_output_at": 4 * 1024 * 1024,
        }
        try:
            r = http.post("instances", json=body)
        except httpx.HTTPError as e:
            raise SandboxError(_safe(f"submit failed: {type(e).__name__}: {e}")) from None
        if r.status_code not in (200, 201, 202):
            raise SandboxError(_safe(f"submit rejected: HTTP {r.status_code}: {r.text[:300]}"))
        op_id = r.json()["uuid"]
        meta.operation_id = op_id
        deadline = time.monotonic() + timeout_s + 60   # queueing + VM start on top of guest time
        op = None
        try:
            while True:
                g = http.get(f"operations/{op_id}")
                g.raise_for_status()
                op = g.json()
                if op.get("status") in _TERMINAL:
                    break
                if time.monotonic() > deadline:
                    raise SandboxError(f"operation {op_id} did not finish within {timeout_s + 60}s")
                time.sleep(0.25)
        except BaseException as e:
            # We own this operation: cancel it rather than leave it running.
            try:
                http.delete(f"operations/{op_id}")
            except Exception:
                pass
            if isinstance(e, SandboxError):
                raise
            raise SandboxError(_safe(f"polling operation {op_id} failed: {type(e).__name__}: {e}")) from None

    status = op.get("status")
    if status != "SUCCESS":
        raise SandboxError(_safe(f"operation {op_id} ended {status}: {str(op.get('error'))[:300]}"))
    result = (op.get("metadata") or {}).get("result") or {}
    state = result.get("state") or {}
    meta.guest_elapsed_s = (result.get("resources") or {}).get("elapsed_time")
    stdout = _decode(result.get("stdout"))
    try:
        results, data = _parse_harness_stdout(stdout, items)
    except (SandboxError, ValueError) as e:
        stderr = _decode(result.get("stderr")).strip().splitlines()
        why = "guest timed out" if state.get("timed_out") else f"exit_code={state.get('exit_code')}"
        raise SandboxError(_safe(f"operation {op_id}: {e} ({why}; stderr: {(stderr[-1] if stderr else '')[:200]})")) from None
    meta.harness_s, meta.mode, meta.workers = data.get("harness_s"), data.get("mode"), data.get("workers")
    meta.wall_s = round(time.monotonic() - t0, 3)
    LAST_BATCH_META = meta
    return results, meta


def grade_batch(checker_code: str, items: list[dict], timeout_s: int = 120) -> list[GradeResult]:
    """checker_code defines `def check(output: str, reference: str, input: str) -> tuple[float, str]` (score, reason).
    items: [{"id","input","output","reference"}]. Runs ALL items in ONE sandbox operation (write items as JSON file,
    run a harness script that imports checker, catches per-item exceptions, enforces per-item time limit, prints JSON
    results). Returns one GradeResult per item, same order.

    Runs in Nebius Sandboxes with guest networking disabled. Per-item crashes/timeouts come back as
    GradeResult.error; raises SandboxError only if the platform itself fails (caller may then choose
    grade_batch_local explicitly and must label it)."""
    return grade_batch_with_meta(checker_code, items, timeout_s)[0]


def grade_batch_local(checker_code: str, items: list[dict], timeout_s: int = 120) -> list[GradeResult]:
    """Same behaviour as grade_batch, but runs the harness in a LOCAL subprocess.

    Fallback/testing only: the checker code runs on this machine with no isolation. Label the
    result as locally graded (LAST_BATCH_META.backend == "local subprocess (fallback)")."""
    global LAST_BATCH_META
    meta = GradeBatchMeta(backend="local subprocess (fallback)", networking=None)
    if not items:
        LAST_BATCH_META = meta
        return []
    t0 = time.monotonic()
    script, _ = _build_script(checker_code, items, timeout_s)
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEBIUS_")}  # no credentials to the child
    try:
        p = subprocess.run([sys.executable, "-I", "-"], input=script, capture_output=True, text=True,
                           timeout=timeout_s + 10, env=env, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise SandboxError(f"local harness exceeded {timeout_s + 10}s") from None
    try:
        results, data = _parse_harness_stdout(p.stdout, items)
    except (SandboxError, ValueError) as e:
        tail = (p.stderr or "").strip().splitlines()
        raise SandboxError(f"local harness: {e} (stderr: {(tail[-1] if tail else '')[:200]})") from None
    meta.harness_s, meta.mode, meta.workers = data.get("harness_s"), data.get("mode"), data.get("workers")
    meta.wall_s = round(time.monotonic() - t0, 3)
    LAST_BATCH_META = meta
    return results


def sandbox_info() -> dict:
    """whoami summary for the UI label. Never raises; never includes the key."""
    info = {"backend": "Nebius Sandboxes", "image": IMAGE_TAG, "networking": "disabled", "spawn": False}
    try:
        with _http(timeout=10) as http:
            r = http.get("whoami")
            r.raise_for_status()
            w = r.json()
        perms, stat, lim = w.get("permissions", {}), w.get("operations_stat", {}), w.get("limits", {})
        info.update({
            "spawn": bool(perms.get("spawn")),
            "spawn_disposable": bool(perms.get("spawn_disposable")),
            "running_instances": stat.get("running_instances"),
            "max_concurrency": lim.get("instance_max_concurrency"),
            "max_timeout_s": lim.get("instance_max_timeout"),
        })
    except Exception as e:  # noqa: BLE001 - UI label must not crash the app
        info["error"] = _safe(f"{type(e).__name__}: {e}")[:200]
    return info


if __name__ == "__main__":  # quick manual check: python -m downshift.sandbox_grader
    print(json.dumps(sandbox_info(), indent=1))
