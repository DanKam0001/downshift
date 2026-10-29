"""Unit-test checker for Python code generation (MBPP sanitized), written for scripts/build_code_preset.py.

check(output, reference, input) -> (score, reason). The reference is NOT an answer: it is a JSON string
{"entry_point", "test_imports", "tests"} holding the dataset's assert statements. The checker pulls the code
out of the model's reply, runs it, then runs every assert. Score 1.0 only if all asserts pass, else 0.0
(the reason says how many passed). Standard library only. Meant to run inside a Nebius Sandbox with
networking off, in a forked child with a wall-clock limit; it also sets its own CPU timer per stage so a
hanging solution fails instead of taking the whole item down
(2 s per stage, 2.6 s per item).

Code extraction: reasoning blocks are removed; then the candidates are, in order, all ```python fences joined,
each fence on its own (longest first), and the raw reply when it has no fences. The first candidate that
defines the entry-point function and runs without error is tested. Example-usage lines that crash are not
held against a candidate that also has a clean variant.
"""
import contextlib
import io
import json
import re
import signal
import sys
import time

STAGE_LIMIT_S = 2.0   # one exec stage (the code, or one assert)
TOTAL_LIMIT_S = 2.6   # whole check(); the sandbox harness kills the item at 3 s
_DEADLINE = [0.0]


class _Timeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise _Timeout("time limit")


@contextlib.contextmanager
def _limit(seconds):
    if hasattr(signal, "setitimer"):
        old = signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
    else:  # local Windows test path: no timer, the harness's per-item limit still applies
        yield


def _strip_think(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.I)[-1]
    return text


def _candidates(output):
    text = _strip_think(output)
    blocks = re.findall(r"```[ \t]*(?:python|py|Python|python3)?[ \t]*\n(.*?)```", text, flags=re.S)
    if not blocks:  # an unclosed fence at the end of a truncated reply
        m = re.search(r"```[ \t]*(?:python|py)?[ \t]*\n(.*)$", text, flags=re.S)
        if m:
            blocks = [m.group(1)]
    out = []
    if len(blocks) > 1:
        out.append("\n\n".join(blocks))
    out.extend(sorted(blocks, key=len, reverse=True))
    if not blocks:
        out.append(text)
    seen, uniq = set(), []
    for c in out:
        c = c.strip("\n")
        if c.strip() and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _run(code, ns):
    sink = io.StringIO()
    old_in = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink), _limit(max(0.05, min(STAGE_LIMIT_S, _DEADLINE[0] - time.monotonic()))):
            exec(compile(code, "solution.py", "exec"), ns)
    finally:
        sys.stdin = old_in


def check(output, reference, input):
    _DEADLINE[0] = time.monotonic() + TOTAL_LIMIT_S
    try:
        ref = json.loads(reference)
    except ValueError:
        return 0.0, "reference is not JSON"
    tests = ref.get("tests") or []
    imports = "\n".join(ref.get("test_imports") or [])
    entry = ref.get("entry_point") or ""
    if not tests:
        return 0.0, "no tests in reference"
    cands = _candidates(output)
    if not cands:
        return 0.0, "no code in reply"
    ns, why = None, "no candidate ran"
    for code in cands:
        trial = {"__name__": "solution", "__builtins__": __builtins__}
        try:
            if imports:
                _run(imports, trial)
            _run(code, trial)
        except SystemExit:
            why = "code called exit()"
            continue
        except BaseException as e:  # noqa: BLE001 - the code under test can raise anything
            why = "code failed to run: %s: %s" % (type(e).__name__, str(e)[:80])
            continue
        if entry and not callable(trial.get(entry)):
            why = "no function named %s" % entry
            continue
        ns = trial
        break
    if ns is None:
        return 0.0, why
    passed, first_fail = 0, ""
    for t in tests:
        try:
            _run(t, ns)
            passed += 1
        except BaseException as e:  # noqa: BLE001
            if not first_fail:
                first_fail = "%s on: %s" % (type(e).__name__, t[:90])
    if passed == len(tests):
        return 1.0, "all %d tests pass" % len(tests)
    return 0.0, "%d/%d tests pass; %s" % (passed, len(tests), first_fail)
