# Downshift

Give it a task and a few dozen examples. Downshift finds the cheapest NVIDIA Nemotron model on Nebius Token
Factory that does the task as well as Nemotron Ultra. It measures every step, rewrites the prompt where that helps,
and recommends fine-tuning only when prompting runs out of room.

The result is a verdict you can paste into code: the model, whether to turn thinking on, the final prompt, the
held-out score with a 95% confidence interval next to Ultra's, the cost per 1,000 calls, and the median latency.
Every number comes from measured calls: the cheaper models are measured in the run, and Ultra comes from a cached
3-run reference with its date shown (or is re-measured live on request).

**Results on three public datasets are in [RESULTS.md](RESULTS.md).** Headline: turning on Ultra's thinking cost
2.4x to 6.6x more per call on all three tasks, and on none of them was the accuracy gain distinguishable from zero.

## How it works

1. **Reference.** If your examples have no expected outputs, Nemotron Ultra writes them.
2. **Grader.** Each preset ships a `checker.py`. For a custom task, Ultra writes one. Before a checker is trusted it
   is tested: the reference answers must score about 1.0, and deliberately wrong answers must score low. If it fails,
   Ultra gets one retry with the failure; after that Downshift falls back to exact match. Checker code is model-written,
   so it runs in **Nebius Sandboxes** with networking off, not on the web server.
3. **Three splits.** Tuning rows: the optimiser sees their failures and may use them as few-shot examples.
   Validation rows: used only to choose between prompt candidates, never shown to the optimiser. Held-out rows:
   scored once per rung, with the one prompt chosen on validation.
4. **Reference.** `scripts/measure_reference.py` runs Ultra on the full held-out split with thinking off and on,
   3 times each, and caches per-run scores, per-row scores and outputs, cost, latency, date and model ids in
   `presets/<slug>/reference.json`. Runs use it by default; the UI shows "Ultra reference measured on <date>, 3 runs,
   mean X ± spread". The reference is Ultra with thinking on unless thinking off scores higher. A setting re-measures
   Ultra live instead (one pass), and the engine falls back to live automatically if the cached file no longer
   matches the prompt or the held-out rows.
5. **Ladder.** Each rung is a model and a thinking mode, in order of measured cost per call: Lightning, Nano and
   Super with thinking off, then Lightning with thinking on (its reasoning tokens make it dearer per call than
   Super). Rungs are measured only when the run reaches them. On each rung:
   - score the plain prompt on the tuning and validation rows;
   - unless it already reaches Ultra's validation score, Ultra proposes rewritten prompts (new instructions plus
     few-shot examples picked from the tuning rows). Each candidate is scored on the tuning rows it did not use as
     examples and on the validation rows; the best **validation** score wins;
   - the winner is scored **once** on the held-out rows, and compared with Ultra on the same rows.
6. **Statistics and verdict.** Held-out scores carry a 95% bootstrap CI over rows (fixed seed). The comparison with
   Ultra is a paired bootstrap of the difference on the same rows. A rung **matches Ultra** only if the lower bound of
   that CI is above −3 points (the tolerance, adjustable); the verdict says "better than Ultra" only if the lower
   bound is above 0. The first rung that matches is the answer and the rungs above it are never called. The verdict
   shows tuning, validation and held-out scores side by side, so any overfitting is visible.
7. **Fine-tune fallback.** When no rung matches, Downshift can build a chat dataset from the tuning rows' reference
   answers and launch a Token Factory fine-tune job (Qwen3-4B by default; Token Factory does not fine-tune
   Nemotron). It is off unless `DOWNSHIFT_ALLOW_FINETUNE=1`, and serving the result needs Token Factory's
   custom-weights access.

A hard per-run spend cap (US$0.10 by default) stops the run cleanly. Before each step the engine estimates its cost,
skips a rung that would not fit, and stops tuning early with a verdict rather than running out mid-step. With the
cached reference a preset run costs US$0.07 to US$0.08 and takes 83 to 145 seconds (measured, see RESULTS.md).

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

Each has 60 tuning, 40 validation and 150 held-out rows (receipt-extract: 49 / 40 / 86, the size of CORD's usable
test split), plus a cached Ultra reference. See `presets/README.md` for how they were built.

## Results

See **[RESULTS.md](RESULTS.md)**: the ladder for each task with 95% CIs, the verdicts, the overfit gaps, Ultra's own
run-to-run noise, the limits of the method, and what it all cost. Reproduce the tables with
`python scripts/results_tables.py demo_runs/*.json`; re-measure a reference with
`python scripts/measure_reference.py <slug>` (about US$0.9 to US$1.2 per preset).

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

Options for `/api/run`: `spend_cap_usd`, `tolerance` (default 0.03), `rounds`, `candidates`, `thinking_rungs`,
`reference` (`"cached"` or `"live"`) and `ultra_thinking` (live reference only).

A custom task is `{"title", "instruction", "output_format", "label_set"?, "examples": [{"input", "reference"?}]}`
with 8 to 200 examples; 40% tune, 20% validate and 40% are held out. Custom tasks have no cached reference, so Ultra
is measured live.

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
downshift/stats.py      bootstrap CIs and the paired comparison
downshift/grading.py    Sandboxes grader with labelled local fallback
downshift/sandbox_grader.py  Nebius Sandboxes harness
downshift/finetune.py   dataset builder and fine-tune job launcher
downshift/tasks.py      presets, custom tasks, prompt assembly
static/                 single-page UI, no build step
presets/                three benchmark tasks: splits, checkers, cached Ultra references
scripts/                build_presets, measure_reference, run_presets, results_tables
```

## Licence

MIT.
