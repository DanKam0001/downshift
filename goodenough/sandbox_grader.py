"""Run model-written checker code against model outputs inside Nebius Token Factory Sandboxes.

The checker is untrusted, model-written Python. It never runs on our server in the normal path:
`grade_batch` ships the checker plus every item to ONE disposable Sandboxes operation with the guest
network interface disabled, where a small harness runs `check()` per item in a forked child with a
per-item time limit, and prints the results as one JSON line.

`grade_batch_sealed_with_meta` adds Sandboxes checkpoints + branching: the checker, the harness and the hidden
answer key (id -> reference, input) are written ONCE by a retained (`disposable: false`) operation whose
`result_image_uuid` is the checkpoint. Every later batch for that key is a disposable branch started from the
checkpoint that receives only (id, output) on stdin; the sealed harness reads the references from disk and
reports the refs file's sha256, so each rung is provably graded against the bit-identical key. Any
seal/branch failure falls back to the one-shot path (the reason is recorded in the batch meta).

`grade_batch_local` runs the same harness in a local subprocess. It is an explicit fallback / test
path only; callers must label which backend graded (see `GradeBatchMeta.backend`).

Credentials (NEBIUS_API_KEY, NEBIUS_PROJECT_ID) are read from the environment or the repo `.env`.
They are only ever sent as HTTP headers to the Sandboxes API, never into the sandbox payload.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
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
SEAL_DIR = "/downshift/"                # where the sealed answer key lives inside the checkpoint image
SEAL_TIMEOUT_S = 60
SEAL_MARKER = "__DOWNSHIFT_SEALED__"
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
    label: str | None = None             # short human label for the UI
    checkpoint: str | None = None        # sealed answer-key image UUID this batch branched from
    branched: bool = False               # True = graded in a disposable branch of `checkpoint`
    seal_operation_id: str | None = None  # the retained operation that produced `checkpoint`
    sealed_now: bool = False             # True = this call paid for the seal
    seal_s: float | None = None          # client-side wall time of the seal operation
    key_sha256: str | None = None        # sha256 of the sealed refs file, as reported by the guest
    fallback_reason: str | None = None   # why a checkpoint branch was wanted but not used
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
                                         "mode": MODE, "workers": WORKERS, "load_error": load_error,
                                         "key_sha256": globals().get("KEY_SHA")}) + "\n")
sys.stdout.flush()
'''

# The subprocess (Windows/local) worker re-uses the helper functions by exec'ing their source.
_HARNESS_FNS_SRC = _HARNESS[_HARNESS.index("def short("):_HARNESS.index("N = len(ITEMS)")]

_ONE_SHOT_PREAMBLE = '''P = json.loads(base64.b64decode("__PAYLOAD__").decode("utf-8"))
CODE, ITEMS = P["code"], P["items"]
'''
# Sealed variant, written into the checkpoint. stdin carries only {"items": [{"id", "output"}], limits};
# checker + references come from the sealed files, and the refs file's sha256 is reported back.
_SEALED_PREAMBLE = '''import hashlib
P = json.loads(sys.stdin.buffer.read().decode("utf-8"))
CODE = open("__DIR__checker.py", encoding="utf-8").read()
_RB = open("__DIR__refs.json", "rb").read()
KEY_SHA = hashlib.sha256(_RB).hexdigest()
REFS = json.loads(_RB.decode("utf-8"))
ITEMS = [{"output": it.get("output"), "reference": REFS[it["id"]]["reference"], "input": REFS[it["id"]]["input"]}
         for it in P["items"]]
'''
assert _ONE_SHOT_PREAMBLE in _HARNESS


def _sealed_harness() -> str:
    return (_HARNESS.replace(_ONE_SHOT_PREAMBLE, _SEALED_PREAMBLE.replace("__DIR__", SEAL_DIR), 1)
            .replace("fns = HARNESS_FNS", "fns = " + repr(_HARNESS_FNS_SRC), 1))


# Seal script: runs once in the retained operation. Writes the files, then self-checks WITHOUT running the
# (untrusted) checker, so nothing model-written executes before the checkpoint is taken.
_SEAL_SCRIPT = r'''
import base64, hashlib, json, os, sys
F = json.loads(base64.b64decode("__FILES__").decode("utf-8"))
os.makedirs("__DIR__", exist_ok=True)
for name, text in F.items():
    with open("__DIR__" + name, "w", encoding="utf-8", newline="") as f:
        f.write(text)
compile(open("__DIR__harness.py", encoding="utf-8").read(), "harness.py", "exec")
rb = open("__DIR__refs.json", "rb").read()
n = len(json.loads(rb.decode("utf-8")))
try:
    compile(open("__DIR__checker.py", encoding="utf-8").read(), "checker.py", "exec")
    syntax = None
except SyntaxError as e:
    syntax = "%s: %s" % (type(e).__name__, e)
code_sha = hashlib.sha256(open("__DIR__checker.py", "rb").read()).hexdigest()
sys.stdout.write("\n__MARK__" + json.dumps({"refs": n, "key_sha256": hashlib.sha256(rb).hexdigest(),
                                            "code_sha256": code_sha, "checker_syntax_error": syntax}) + "\n")
'''


def _build_script(checker_code: str, items: list[dict], timeout_s: int) -> tuple[str, float]:
    per_item, budget = _limits(timeout_s)
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
# Operations
# --------------------------------------------------------------------------------------------
def _limits(timeout_s: int) -> tuple[float, float]:
    per_item = float(min(PER_ITEM_TIMEOUT_S, max(1.0, timeout_s / 2)))
    return per_item, float(max(2.0, timeout_s - per_item - 5))


def _body(image: str, command: str, stdin_text: str, timeout_s: int, disposable: bool) -> dict:
    return {
        "image": image,
        "command": command,
        "shell": True,
        "stdin": {"value": base64.b64encode(stdin_text.encode("utf-8")).decode("ascii"), "encoding": "base64"},
        "networking": {"enabled": False},
        "disposable": disposable,
        "timeout": int(timeout_s),
        "truncate_output_at": 4 * 1024 * 1024,
    }


def _run_operation(http: httpx.Client, body: dict, timeout_s: int) -> dict:
    """POST one instance, poll it to a terminal state and return the final operation JSON (status SUCCESS).

    We own the operation: if polling fails or overruns it is cancelled. Raises SandboxError otherwise."""
    try:
        r = http.post("instances", json=body)
    except httpx.HTTPError as e:
        raise SandboxError(_safe(f"submit failed: {type(e).__name__}: {e}")) from None
    if r.status_code not in (200, 201, 202):
        raise SandboxError(_safe(f"submit rejected: HTTP {r.status_code}: {r.text[:300]}"))
    op_id = r.json()["uuid"]
    deadline = time.monotonic() + timeout_s + 60   # queueing + VM start on top of guest time
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
        try:
            http.delete(f"operations/{op_id}")
        except Exception:
            pass
        if isinstance(e, SandboxError):
            raise
        raise SandboxError(_safe(f"polling operation {op_id} failed: {type(e).__name__}: {e}")) from None
    op.setdefault("uuid", op_id)
    if op.get("status") != "SUCCESS":
        raise SandboxError(_safe(f"operation {op_id} ended {op.get('status')}: {str(op.get('error'))[:300]}"))
    return op


def _op_stdout(op: dict, marker: str, what: str) -> dict:
    """Find the marker JSON line in an operation's stdout, or raise with a short diagnosis."""
    result = (op.get("metadata") or {}).get("result") or {}
    for line in reversed(_decode(result.get("stdout")).splitlines()):
        if line.startswith(marker):
            try:
                return json.loads(line[len(marker):])
            except ValueError:
                break
    state = result.get("state") or {}
    stderr = _decode(result.get("stderr")).strip().splitlines()
    why = "guest timed out" if state.get("timed_out") else f"exit_code={state.get('exit_code')}"
    raise SandboxError(_safe(f"operation {op.get('uuid')}: {what} produced no result line "
                             f"({why}; stderr: {(stderr[-1] if stderr else '')[:200]})"))


def _harness_results(op: dict, items: list[dict], meta: GradeBatchMeta) -> tuple[list[GradeResult], dict]:
    result = (op.get("metadata") or {}).get("result") or {}
    meta.guest_elapsed_s = (result.get("resources") or {}).get("elapsed_time")
    stdout = _decode(result.get("stdout"))
    try:
        results, data = _parse_harness_stdout(stdout, items)
    except (SandboxError, ValueError) as e:
        state = result.get("state") or {}
        stderr = _decode(result.get("stderr")).strip().splitlines()
        why = "guest timed out" if state.get("timed_out") else f"exit_code={state.get('exit_code')}"
        raise SandboxError(_safe(f"operation {op.get('uuid')}: {e} ({why}; stderr: "
                                 f"{(stderr[-1] if stderr else '')[:200]})")) from None
    meta.harness_s, meta.mode, meta.workers = data.get("harness_s"), data.get("mode"), data.get("workers")
    return results, data


# --------------------------------------------------------------------------------------------
# Sealed answer keys (checkpoint images)
# --------------------------------------------------------------------------------------------
@dataclass
class SealedKey:
    """One answer key sealed into a checkpoint image: checker + sealed harness + refs for a fixed row set."""
    checkpoint: str            # result_image_uuid of the seal operation
    seal_operation_id: str
    key_hash: str              # answer_key_hash(checker_code, rows)
    key_sha256: str            # sha256 of the refs file bytes; every branch must report the same value
    code_sha256: str
    rows: dict[str, str]       # item id -> sha256 of its (reference, input)
    seal_s: float
    base_image: str


_SEALS: dict[str, list[SealedKey]] = {}       # sha256(checker_code) -> sealed keys (process lifetime)
_SEAL_FAILED: dict[str, tuple[int, str]] = {}  # key_hash -> (failures, last reason); given up after 2
_SEAL_LOCKS: dict[str, threading.Lock] = {}
_SEAL_GUARD = threading.Lock()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_hash(it: dict) -> str:
    return _sha(json.dumps([it.get("reference"), it.get("input")], sort_keys=True, default=str))


def answer_key_hash(checker_code: str, items: list[dict]) -> str:
    """Identity of an answer key: the checker plus the sorted (id, reference, input) rows."""
    rows = sorted([str(it.get("id")), _row_hash(it)] for it in items)
    return _sha(json.dumps({"code": _sha(checker_code), "rows": rows}))


def _find_seal(checker_code: str, items: list[dict]) -> SealedKey | None:
    """A sealed key for this checker that holds every item's exact (reference, input), if any."""
    with _SEAL_GUARD:
        seals = list(_SEALS.get(_sha(checker_code), []))
    for s in seals:
        if all(s.rows.get(str(it.get("id"))) == _row_hash(it) for it in items):
            return s
    return None


def _drop_seal(seal: SealedKey) -> None:
    with _SEAL_GUARD:
        for seals in _SEALS.values():
            if seal in seals:
                seals.remove(seal)


def _seal(http: httpx.Client, checker_code: str, items: list[dict], key: str) -> SealedKey:
    """Run ONE retained operation that writes checker, harness and refs under SEAL_DIR; return the checkpoint."""
    refs = json.dumps({str(it.get("id")): {"reference": it.get("reference"), "input": it.get("input")}
                       for it in items}, sort_keys=True)
    files = {"checker.py": checker_code, "harness.py": _sealed_harness(), "refs.json": refs}
    script = (_SEAL_SCRIPT.replace("__DIR__", SEAL_DIR).replace("__MARK__", SEAL_MARKER)
              .replace("__FILES__", base64.b64encode(json.dumps(files).encode("utf-8")).decode("ascii"), 1))
    t0 = time.monotonic()
    image = _image_ref(http)
    op = _run_operation(http, _body(image, "exec python3 -I -", script, SEAL_TIMEOUT_S, disposable=False),
                        SEAL_TIMEOUT_S)
    data = _op_stdout(op, SEAL_MARKER, "seal")
    want_key, want_code = _sha(refs), _sha(checker_code)
    if data.get("key_sha256") != want_key or data.get("code_sha256") != want_code or data.get("refs") != len(items):
        raise SandboxError(f"seal {op['uuid']}: guest self-check disagrees with the uploaded key")
    checkpoint = op.get("result_image_uuid")
    if not checkpoint:
        raise SandboxError(f"seal {op['uuid']}: operation returned no result_image_uuid")
    return SealedKey(checkpoint=checkpoint, seal_operation_id=op["uuid"], key_hash=key, key_sha256=want_key,
                     code_sha256=want_code, rows={str(it.get("id")): _row_hash(it) for it in items},
                     seal_s=round(time.monotonic() - t0, 3), base_image=image)


def _get_or_seal(http: httpx.Client, checker_code: str, items: list[dict], key: str) -> tuple[SealedKey, bool]:
    """Reuse a sealed key covering these rows, else seal one (once, even under concurrent callers)."""
    seal = _find_seal(checker_code, items)
    if seal:
        return seal, False
    with _SEAL_GUARD:
        lock = _SEAL_LOCKS.setdefault(key, threading.Lock())
    with lock:
        seal = _find_seal(checker_code, items)
        if seal:
            return seal, False
        seal = _seal(http, checker_code, items, key)
        with _SEAL_GUARD:
            _SEALS.setdefault(seal.code_sha256, []).append(seal)
        return seal, True


def _branch(http: httpx.Client, seal: SealedKey, items: list[dict], timeout_s: int,
            meta: GradeBatchMeta) -> list[GradeResult]:
    """Grade in a disposable operation started FROM the checkpoint; stdin carries only ids + outputs."""
    per_item, budget = _limits(timeout_s)
    payload = {"items": [{"id": str(it.get("id")), "output": it.get("output")} for it in items],
               "per_item": per_item, "budget": budget, "marker": RESULT_MARKER, "max_detail": MAX_DETAIL}
    op = _run_operation(http, _body(seal.checkpoint, f"exec python3 -I {SEAL_DIR}harness.py", json.dumps(payload),
                                    timeout_s, disposable=True), timeout_s)
    meta.operation_id = op["uuid"]
    if op.get("image_uuid") not in (None, seal.checkpoint):
        raise SandboxError(f"branch {op['uuid']} ran on {op.get('image_uuid')}, not checkpoint {seal.checkpoint}")
    results, data = _harness_results(op, items, meta)
    if data.get("key_sha256") != seal.key_sha256:
        raise SandboxError(f"branch {op['uuid']}: sealed answer key hash mismatch")
    meta.key_sha256 = data["key_sha256"]
    return results


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------
def grade_batch_with_meta(checker_code: str, items: list[dict],
                          timeout_s: int = 120) -> tuple[list[GradeResult], GradeBatchMeta]:
    """Same as grade_batch, also returning operation id / timings for the UI and run logs."""
    global LAST_BATCH_META
    meta = GradeBatchMeta(backend="Nebius Sandboxes", networking=False, label="Nebius Sandboxes · one-shot")
    if not items:
        LAST_BATCH_META = meta
        return [], meta
    t0 = time.monotonic()
    script, _ = _build_script(checker_code, items, timeout_s)
    with _http() as http:
        image = _image_ref(http)
        meta.image = f"{IMAGE_TAG} ({image})"
        op = _run_operation(http, _body(image, "exec python3 -I -", script, timeout_s, disposable=True), timeout_s)
    meta.operation_id = op["uuid"]
    results, _ = _harness_results(op, items, meta)
    meta.wall_s = round(time.monotonic() - t0, 3)
    LAST_BATCH_META = meta
    return results, meta


def grade_batch_sealed_with_meta(checker_code: str, items: list[dict],
                                 timeout_s: int = 120) -> tuple[list[GradeResult], GradeBatchMeta]:
    """Grade in a branch of a sealed answer-key checkpoint; same contract as grade_batch_with_meta.

    The first call for a (checker, rows) key seals it (one retained operation); later calls whose rows the
    seal already holds only branch. On a seal or branch failure this falls back to the one-shot operation
    and records `fallback_reason`; it raises SandboxError only if that one-shot also fails."""
    global LAST_BATCH_META
    if not items:
        return grade_batch_with_meta(checker_code, items, timeout_s)
    reason, key = None, answer_key_hash(checker_code, items)
    if os.environ.get("DOWNSHIFT_NO_CHECKPOINT") == "1":
        reason = "checkpoints disabled (DOWNSHIFT_NO_CHECKPOINT=1)"
    elif len({str(it.get("id")) for it in items}) != len(items):
        reason = "duplicate item ids: cannot key the sealed answer file"
    elif _SEAL_FAILED.get(key, (0, ""))[0] >= 2:
        reason = _SEAL_FAILED[key][1]
    if reason is None:
        t0 = time.monotonic()
        meta = GradeBatchMeta(backend="Nebius Sandboxes", networking=False, branched=True)
        try:
            with _http() as http:
                try:
                    seal, meta.sealed_now = _get_or_seal(http, checker_code, items, key)
                except SandboxError as e:
                    n = _SEAL_FAILED.get(key, (0, ""))[0] + 1
                    reason = f"seal failed: {e}"[:300]
                    _SEAL_FAILED[key] = (n, reason)
                    raise
                t1 = time.monotonic()
                try:
                    results = _branch(http, seal, items, timeout_s, meta)
                except SandboxError as e:
                    _drop_seal(seal)   # e.g. checkpoint expired: reseal next time
                    reason = f"branch of checkpoint {seal.checkpoint[:8]} failed: {e}"[:300]
                    raise
        except SandboxError as e:
            reason = reason or f"sandbox: {e}"[:300]
        else:
            meta.checkpoint, meta.seal_operation_id, meta.seal_s = seal.checkpoint, seal.seal_operation_id, seal.seal_s
            meta.image = f"sealed answer key {seal.checkpoint} (from {IMAGE_TAG} {seal.base_image})"
            meta.label = f"Nebius Sandboxes · branch of sealed answer key {seal.checkpoint[:8]}"
            meta.wall_s = round(time.monotonic() - t0, 3)
            meta.extra.update({"branch_wall_s": round(time.monotonic() - t1, 3), "key_hash": key[:16]})
            LAST_BATCH_META = meta
            return results, meta
    results, meta = grade_batch_with_meta(checker_code, items, timeout_s)
    meta.fallback_reason = reason
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


if __name__ == "__main__":  # quick manual check: python -m goodenough.sandbox_grader
    print(json.dumps(sandbox_info(), indent=1))
