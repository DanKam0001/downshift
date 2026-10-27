# Downshift

Give it a task and a few dozen examples. Downshift finds the cheapest NVIDIA Nemotron model on Nebius Token
Factory that does the task as well as Nemotron Ultra. It measures every step, rewrites the prompt where that helps,
and recommends fine-tuning only when prompting runs out of room.

The result is a verdict you can paste into code: the model, whether to turn thinking on, the final prompt, the
held-out score next to Ultra's, the cost per 1,000 calls, and the median latency. Every number comes from calls made
in that run.

## How it works

1. **Reference.** If your examples have no expected outputs, Nemotron Ultra writes them.
2. **Grader.** Each preset ships a `checker.py`. For a custom task, Ultra writes one. Before a checker is trusted it
   is tested: the reference answers must score about 1.0, and deliberately wrong answers must score low. If it fails,
   Ultra gets one retry with the failure; after that Downshift falls back to exact match. Checker code is model-written,
   so it runs in **Nebius Sandboxes** with networking off, not on the web server.
3. **Bar.** Ultra runs the task with thinking off on both splits, and with thinking on (its default) on the held-out
   split when that fits the spend cap. The reference is Ultra with thinking on, unless thinking off scores higher. The bar is
   the reference's held-out score minus a tolerance (2 points by default).
4. **Ladder.** Each rung is a model and a thinking mode, cheapest first:
   Lightning (thinking off), Lightning (thinking on), Nano, Super. Rungs are measured only when the run reaches them.
   On each rung:
   - score the plain prompt on the tuning split;
   - if it falls short of Ultra's tuning score, Ultra proposes rewritten prompts (new instructions plus few-shot
     examples picked from the tuning split). Each candidate is scored on the tuning rows it did not use as examples;
   - the best prompt is scored **once** on the held-out split. If it reaches the bar, that is the answer and the rungs
     above it are never called.
5. **Verdict.** "Use Lightning (thinking off) with this prompt", or "prompting tops out at X%; fine-tune". Scores
   are on held-out rows the optimiser never saw. With 40 held-out rows one row is 2.5 points, so gaps smaller than
   one row are reported as a match, not a win.
6. **Fine-tune fallback.** When prompting tops out, Downshift can build a chat dataset from Ultra's answers on the
   tuning split and launch a Token Factory fine-tune job (Qwen3-4B by default; Token Factory does not fine-tune
   Nemotron). It is off unless `DOWNSHIFT_ALLOW_FINETUNE=1`, and serving the result needs Token Factory's
   custom-weights access.

A hard per-run spend cap (US$0.10 by default) stops the run cleanly. Before each step the engine estimates its cost
and stops tuning early with a verdict rather than running out mid-step.

## Where each Nebius / NVIDIA piece is used

| Piece | Used for |
|---|---|
| NVIDIA Nemotron 3.5 Lightning, Nemotron 3 Nano 30B, Super 120B, Ultra 550B | The ladder. Ultra also writes references, checkers and prompt rewrites. |
| Nebius Token Factory (OpenAI-compatible API, `https://api.tokenfactory.nebius.com/v1`) | Every model call. Live prices come from `GET /v1/models?verbose=true`. Thinking is toggled with `extra_body={"chat_template_kwargs": {"enable_thinking": ...}}`. |
| Nebius Sandboxes | Runs checker code on each batch of outputs (one sandbox operation per batch, networking disabled). The UI shows which backend graded each run and says "graded locally" if Sandboxes were unavailable. |
| Token Factory fine-tuning | The fallback when prompting tops out. |

## Presets

| Slug | Task | Source |
|---|---|---|
| `support-triage` | Route bank support messages to one of 77 intents | Banking77 (PolyAI), CC-BY-4.0 |
| `receipt-extract` | Turn OCR'd receipt text into JSON line items and totals | CORD v2 (NAVER Clova), CC-BY-4.0 |
| `contract-clauses` | Tag contract clauses with one of 32 types | CUAD (The Atticus Project), CC-BY-4.0 |

Each has 30 tuning rows and 40 held-out rows. See `presets/BASELINE.md` for plain-prompt baselines.

## Setup

Python 3.11 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env     # or create .env with the two lines below
# NEBIUS_API_KEY=...
# NEBIUS_PROJECT_ID=...   (for Sandboxes)
```

The key is read from the environment or `.env`. It is never logged, saved in runs, or sent into a sandbox.

## Run

```bash
python -m uvicorn app:app --port 8000
# open http://localhost:8000, pick a preset, press "Downshift it"
```

A finished run can be reopened at `/?run=<id>`. Runs are saved to `runs/<id>.json`; runs copied into `demo_runs/`
ship with the image so they stay viewable on a hosted copy.

API:

| Method | Path | |
|---|---|---|
| GET | `/api/health` | backend, `key_present`, Sandboxes info, pricing source |
| GET | `/api/presets` | preset list |
| POST | `/api/run` | `{"preset": "receipt-extract", "options": {"spend_cap_usd": 0.1}}` or `{"custom": {...}}`, returns a run id |
| GET | `/api/run/{id}/events` | server-sent events (replays a finished run) |
| GET | `/api/run/{id}` | saved run with verdict |

A custom task is `{"title", "instruction", "output_format", "label_set"?, "examples": [{"input", "reference"?}]}`
with 8 to 200 examples; half are held out.

## Tests

```bash
python -m pytest            # engine, grader and server tests, no network
python -m pytest -m live    # one live smoke test, costs about US$0.02
```

## Deploy

```bash
docker build -t downshift .
docker run -p 8000:8000 -e NEBIUS_API_KEY=... -e NEBIUS_PROJECT_ID=... downshift
```

The container listens on `$PORT` (default 8000), so it runs as-is on Render, Fly or similar. Set the two secrets in the
host's settings. Useful limits for a public demo: `DOWNSHIFT_MAX_ACTIVE_RUNS` (default 2),
`DOWNSHIFT_MAX_RUN_CAP_USD` (default 0.50), `DOWNSHIFT_SERVER_CAP_USD` (default 5.00 per process).

## Layout

```
app.py                  FastAPI server and SSE
downshift/models.py     rungs, pricing, Token Factory client, per-call accounting (runs/usage.jsonl)
downshift/engine.py     the pipeline, as an async generator of events
downshift/grading.py    Sandboxes grader with labelled local fallback
downshift/sandbox_grader.py  Nebius Sandboxes harness
downshift/finetune.py   dataset builder and fine-tune job launcher
downshift/tasks.py      presets, custom tasks, prompt assembly
static/                 single-page UI, no build step
presets/                three benchmark tasks with checkers
```

## Licence

MIT.
