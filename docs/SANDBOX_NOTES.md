# Sandboxes grader: verified notes (2026-10-27)

Module: `downshift/sandbox_grader.py`. Tests: `tests/test_sandbox_grader.py` (live tests: `DOWNSHIFT_LIVE=1`).

## How it calls Sandboxes
- Plain REST through `httpx` (`https://api.tokenfactory.nebius.com/sandboxes/v1`, headers `Authorization: Bearer`
  and `Project`). The official SDK (`contree-sdk` 0.3.6) was not needed: the REST cookbook covers everything and
  `networking.enabled` is a documented REST field.
- **One operation per batch:** `POST /v1/instances` with `image=<uuid>`, `command="exec python3 -I -"`,
  `shell=true`, `networking={"enabled": false}`, `disposable=true`, `timeout=timeout_s`. The harness script, with
  the checker code and every item embedded as base64 JSON, is sent as **stdin** (no file upload round trip).
  The client polls `GET /v1/operations/{id}` every 0.25 s and sends `DELETE /v1/operations/{id}` if polling fails
  or passes its deadline.
- **Image:** `python:3.12-slim` is already a public image in Sandboxes, UUID
  `4fa16c8f-62a9-35a4-a4ea-07b7e8cd44ae`. It is resolved by tag once per process (`GET /v1/images?tag=`), so
  nothing is imported.
- **Harness in the guest:** the checker is loaded once. Each item runs `check()` in an `os.fork()` child (4 vCPUs,
  8 workers) with stdout/stderr sent to /dev/null and a 1 GiB address-space limit. A child that runs past
  `PER_ITEM_TIMEOUT_S` (3 s) gets SIGKILL. Exceptions, `os._exit`, bad return types and timeouts become
  `GradeResult.error` for that item only. A checker that fails to load gives every item a "failed to load" error.
  Scores are clamped to [0, 1], NaN becomes 0, and `passed = error is None and score >= 0.999`.
- **The key never enters the guest:** the payload holds only code and items (a test checks this). The guest env
  shows no NEBIUS or API-key variables; the only match was `GPG_KEY`, the python image's public release-signing
  key id.

## Live runs (all disposable, all reached SUCCESS)
| Batch | Operation id | Client wall | Guest elapsed | Harness |
|---|---|---|---|---|
| 22 items (20 JSON-field checks + 1 raises + 1 infinite loop), 5 s item limit | `01a17c79-aaa2-7109-8434-f2617a14f0ff` | 8.31 s | 5.10 s | 5.01 s |
| same, repeat | `01a17c79-c9dc-71f5-9fac-ee5012bbfa53` | 7.97 s | 5.10 s | 5.01 s |
| same, repeat | `01a17c79-e898-7694-aa4f-0d0f86bad075` | 8.02 s | 5.10 s | 5.01 s |
| 20 items, simple equality | `01a17c7a-5875-750a-8734-9b6c901f6758` | 3.06 s | 0.10 s | 0.011 s |
| 20 items, repeat | `01a17c7a-62be-755c-a2f7-64fabb4e2912` | 2.59 s | 0.10 s | 0.012 s |
| 100 items | `01a17c7a-6db7-71ab-9f86-cdd4dd9327b7` | 3.00 s | 0.20 s | 0.043 s |
| 500 items | `01a17c7a-7d20-7678-9f36-3435619d5fa6` | 5.89 s | 0.91 s | 0.244 s |
| 2 items, one infinite loop, 3 s item limit (final setting) | `01a17c7a-8d3c-7030-b9e6-2a791916506c` | 5.38 s | n/a | 3.00 s |
| network probe | `01a17c7a-0cbc-7333-bb8c-f7b53d2a593b` | 3.58 s | 0.09 s | 0.024 s |

Takeaway: about **2.5 to 3 s fixed overhead per batch** (submit, VM start, polling). Grading time itself is
negligible. A checker that hangs adds at most the per-item limit (3 s), because items run in parallel.
In the 22-item run the raising item returned `ValueError: deliberate checker crash` and the looping item returned
`timeout: check() exceeded 5.0s`. The other 20 were scored correctly, and results came back in item order.

## Networking-off evidence
The network probe ran with the same grader call (`networking.enabled=false`). From inside the checker:
`/sys/class/net` = `['lo']` (loopback only), `socket.create_connection(("1.1.1.1", 443))` gave
`OSError: [Errno 101] Network is unreachable`, and `getaddrinfo("pypi.org")` gave
`gaierror: [Errno -3] Temporary failure in name resolution`. `os.cpu_count()` = 4.

## Cleanup
`whoami` after all runs showed `operations_stat.running_instances = 0` and `running_imports = 0`. Every operation
was disposable, so no images were saved.

## Limitations
- If the platform fails (submit rejected, operation FAILED or CANCELLED, no result line), `grade_batch` raises
  `SandboxError`. It does **not** fall back to local grading on its own. The caller may call
  `grade_batch_local` and must label the result (`LAST_BATCH_META.backend`).
- `grade_batch_local` runs untrusted code on the host with no isolation, only a subprocess per item and a timeout.
  On Windows `fork` is not available, so it starts one interpreter per item (8 in parallel). That is slower than
  the sandbox's fork mode.
- Payload limit: everything goes over stdin in one request. This was verified up to 500 items of about 200 chars.
  Very large batches (many MB) have not been tested.
- `LAST_BATCH_META` is a module global, so it is not safe for concurrent batches. Use `grade_batch_with_meta()`,
  which returns `(results, meta)`, when batches run in parallel.
