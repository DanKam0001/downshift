# Good Enough: prove the cheaper Nemotron does the job

*Formerly Downshift, which is why the live app still lives at downshift-qcuv.onrender.com.*

Give it a task and a few dozen examples. Good Enough finds the cheapest NVIDIA Nemotron model on Nebius Token
Factory that does the task as well as Nemotron Ultra, and proves it on held-out data. It measures every step,
rewrites the prompt where that helps, and recommends fine-tuning only when prompting runs out of room.

The result is a verdict you can paste into code: the model, whether to turn thinking on, the final prompt, the
held-out score with a 95% confidence interval next to Ultra's, the cost per 1,000 calls, and the median latency.
**Every number comes from real Token Factory calls graded in Nebius Sandboxes. Nothing is mocked or simulated.**

## For judges: two minutes

- **Live app:** https://downshift-qcuv.onrender.com (pick a preset and press "Prove it": about 2 to 3 minutes
  and US$0.06 to US$0.21 of real calls, or open a recorded run below).
- **Recorded runs, replayed at 4x with no model calls:**
  [receipts → use Nano, then squeeze: 42x cheaper](https://downshift-qcuv.onrender.com/?run=150a5ad36713&speed=4) ·
  [your own task: 100 SMS with no answers → Nano, 17x](https://downshift-qcuv.onrender.com/?run=091a657f0889&speed=4) ·
  [receipts → use Nano (the headline run)](https://downshift-qcuv.onrender.com/?run=45015b6df59e&speed=4) ·
  [receipt photos → Super VL beats Ultra with no OCR](https://downshift-qcuv.onrender.com/?run=2e794b56ae40&speed=4) ·
  [support triage → turn thinking off](https://downshift-qcuv.onrender.com/?run=de04430645fb&speed=4) ·
  [contract clauses → no single model, but a chain: 2.5x](https://downshift-qcuv.onrender.com/?run=cf4758c38c24&speed=4) ·
  [SMS with an audited answer key](https://downshift-qcuv.onrender.com/?run=87cd203cfa2a&speed=4) ·
  [contract clauses → keep Ultra (before the chain)](https://downshift-qcuv.onrender.com/?run=7ee9b72d56be&speed=4)
- **Proof certificate** (the one card to share): [https://downshift-qcuv.onrender.com/?run=150a5ad36713&cert=1](https://downshift-qcuv.onrender.com/?run=150a5ad36713&cert=1)
- **Results:** [RESULTS.md](RESULTS.md). Every figure traces to a file in `demo_runs/` or `presets/*/reference.json`.
- **Reproduce the headline yourself** (needs a Nebius key, about US$0.03):
  `python scripts/repeat_verdict.py demo_runs/45015b6df59e.json --repeats 3`

## What it found

| Task (held-out rows) | Verdict | Quality vs Ultra | Cost per 1k calls |
|---|---|---|---|
| Receipt OCR to JSON (86) | **Use Nemotron 3 Nano, thinking off** | 92.4% vs 92.0%, both 3-run means; paired diff +0.5 pts (95% CI −2.6 to +3.6) | $0.103 vs $3.693, **36x cheaper** |
| Bank support triage, 77 intents (150) | **Keep Ultra, turn thinking off** (a 29 Oct re-run found Lightning with thinking on, 4.6x: an edge case, see RESULTS) | 71.6% vs 72.2%, −0.7 pts (−4.7 to +3.6) | $0.554 vs $1.319, 2.4x cheaper |
| Contract clauses, 32 types (150) | No single cheaper model; **chain: Super + Lightning, Ultra when they disagree** | 81.3% vs 82.2%, −0.9 pts (−4.0 to +2.2) | $0.914 vs $2.301, **2.5x cheaper** |

And one finding we did not expect: **on all three tasks, Ultra's thinking mode cost 2.4x to 6.6x more per call, and
its accuracy gain was not distinguishable from zero** (paired 95% CIs of on minus off all include zero, 3 runs per
mode).

**Then I ran it on my own bot.** My hackathon judge bot (Claude Sonnet today) scores lablab submissions to predict
winners. On 105 real submissions, scored against the real judges, Good Enough first said "use Nano, matches Ultra,
15x cheaper". True, and empty: **a fixed answer of 3 on every criterion beat every model** on that checker (75.4% vs
Ultra 72.1%, Sonnet 74.1%). So Good Enough now runs a **guess check** before spending anything: if Ultra can't
provably beat a fixed answer, the checker can't tell skill from guessing and there is **no verdict**. On the three
presets the check passes by 48 to 79 points; on my bot it trips, in 27 s for US$0.00
([details](RESULTS.md#customer-test-my-own-hackathon-judge-bot-and-the-guess-check-it-forced)).

And a follow-up: **does the receipt pipeline even need its OCR step?** Nemotron 3.5 Super VL reading the photo
directly scored 93.6% with a plain prompt and **96.0% with Good Enough's tuned prompt, +4.0 pts over Ultra reading
perfect OCR text (95% CI +1.2 to +7.3)**, at $0.72 per 1k calls, 3 runs each
([details](RESULTS.md#do-you-even-need-the-ocr-step-nemotron-super-vl-reads-the-photo-demo_runsvision-receipt-extractjson)).
This is now a step in the app: for a task that ships its photos, a **photo check** runs after the verdict and puts
Super VL on the ladder as a "reads the photo" rung. In the recorded run above it scored 95.2%, +3.2 pts over Ultra
(95% CI +0.3 to +6.4), in one pass with that run's own tuned prompt.

A whole ladder run costs cents: the receipts run was 606 model calls, US$0.06 and 139 seconds.

**After the verdict, it squeezes.** The model is picked; now Good Enough cuts tokens off it, one lever at a time:
shorter answers (minified JSON or label only), fewer worked examples (2, then none) and an Ultra-compressed prompt.
Each cut is tried on validation rows and kept only if the score holds within 1 point and the cost falls; the final
setup is then scored once on the held-out rows and must still match Ultra. On receipts, 2 worked examples instead of
4 held (93.5% on validation, 920 prompt tokens instead of 1,261); no examples and one-line answers both fell and were
dropped. Result: **94.3% on the 86 held-out receipts at US$0.087 per 1k calls, 42x cheaper than Ultra** (paired
diff +2.4, 95% CI −0.5 to +5.5), run `150a5ad36713`.

**Beyond text.** The same "measure the lever" approach on photos, video and code
([vision](docs/levers-vision.md), [code](docs/levers-code.md)):

| Task (held-out) | Cheapest setup that still matches the top one | Saving |
|---|---|---|
| Pet breed from a photo, 37 breeds (111, Oxford-IIIT Pet) | Super VL with thinking **off**: 83.8% vs 83.8% | 6.5x |
| Action from a video clip, 6 actions (162, KTH) | none: thinking off lost 13 to 16 points, 4 frames instead of 8 lost 7 | 1x, keep it |
| Python from a one-line spec, graded only by unit tests (150, MBPP) | Nano with thinking **on**: 89.3% vs Ultra 85.3% | 25x |

Same lever, opposite answers: thinking off is free accuracy-wise on photos and fatal on video, and on code the small
model *needs* thinking while Ultra's thinking added nothing. That is why Good Enough measures per task.

**Your own task, with no answers.** 100 real UCI SMS messages, no labels: Ultra wrote the answers and a checker,
the checker passed its own test, the guess check passed ("ham" every time scores 55%, Ultra 100%), and Nano matched
Ultra at 16x cheaper, 17x after the squeeze, for US$0.03 in total (run `091a657f0889`). With that run's instruction, Ultra's answers
agreed with the human UCI labels on 98 of 100 (a later run with different wording: 90, see the audit below).

**No single cheap model? Ask two.** When no cheaper rung matches on its own, Good Enough tries an *escalation
chain*: two cheap rungs answer every row, the task's own checker asks whether their two answers agree, and only
disagreements go to Ultra. The pair is chosen on validation rows, the rule is fixed before scoring, and every answer
it uses was already measured on the held-out rows, so the check costs one grading batch and no model calls. On
contract clauses, where every single model failed (best: Lightning, CI down to −11.1 points), Super and Lightning
agreed on 114 of 150 held-out clauses; with Ultra on the other 36 the chain scored **81.3% vs Ultra's 82.2% (paired
CI −4.0 to +2.2) at $0.914 per 1k calls instead of $2.301: 2.5x cheaper**, run `cf4758c38c24`. Escalated rows are
scored with Ultra's measured per-row scores.

**The answer key audits itself.** When Ultra writes the answers, a second Ultra pass (thinking on) answers every row
again, and rows where the checker says the two passes disagree are dropped rather than counted against a cheaper
model. On 100 UCI SMS messages (run `87cd203cfa2a`) it dropped 8. Against the human labels, 6 of those 8 were ones
Ultra's first pass had wrong, and the answer key went from 90 of 100 right to 88 of 92 (95.7%). The cost: fewer
held-out rows (37), so one cheap-model mistake decides the proof; in that run only Ultra with thinking off passed.

**Price a mistake.** Next to calls per month, type what one wrong answer costs you. The savings box adds mistakes to
the token bill and gives the break-even: for the squeezed receipts setup, even at the bad end of its 95% CI (−0.5
points), it costs less than Ultra unless one wrong receipt costs more than **US$0.78**.

**A proof certificate.** Every run ends with one card: the setup, the evidence (held-out size, CI vs Ultra, guess
check, Ultra's 3-run wobble, the sealed Sandboxes checkpoint, the audit) and the saving, with a link that opens just
the certificate (`/?run=<id>&cert=1`).

**Cost levers we checked and do not claim.** Token Factory lists cached prompt tokens 5 to 8x cheaper, but across
15,000+ logged Nemotron calls, with and without `prompt_cache_key`, we measured **zero cache hits**, so no caching
saving is assumed. The Batch API (`/v1/batches`) exists, but we found no published discount to verify, so none is
claimed. There are no time-of-day prices in the pricing API.

## Why not a model router?

Routers pick a model per request from heuristics or a learned predictor. Good Enough answers a different question,
once per task: *is a cheaper model provably good enough for this job?* It measures that on held-out rows with a
non-inferiority rule (the 95% CI of the difference must stay within 5 points of Ultra), tunes the prompt for the
cheaper model first, says "keep Ultra" when the evidence is not there, and hands you the config to ship.

## Bring your own task

![The "Your own" tab: a title, an instruction, an output format, optional labels and 8 to 200 example rows](docs/own-task.png)

Open the **Your own** tab, describe the task, and paste 8 to 200 examples as JSON lines. Rows without a `reference`
get one from Ultra. Ultra also writes the checker (a Python `check(output, reference, input)` returning 0 to 1),
which is tested before use: references must score about 1.0 and deliberately wrong answers must score low, or it
falls back to exact match. The checker runs in Nebius Sandboxes with networking off. The same task can be sent to
`POST /api/run` as `{"custom": {...}}` (see API below).

To add a permanent preset with a hand-written checker, copy a folder in `presets/` (it holds `task.json`,
`train/validation/holdout.jsonl` and `checker.py`), then cache its Ultra reference with
`python scripts/measure_reference.py <slug>`.

## Run it on your own traffic (CLI)

Point it at a JSONL export of real requests. Each line is either a plain example, `{"input": ..., "reference": ...}`,
or an OpenAI-style chat log, `{"messages": [...], "response": ...}`: the last user message becomes the input and the
logged answer (from `response`, `output`, `completion`, or a trailing assistant message) becomes the reference that
cheaper models are graded against. Rows without an answer get one from Ultra.

```bash
python -m goodenough.cli examples/support_logs.jsonl   --instruction "Route the customer email to one queue."   --labels billing,shipping,refund,account --output-format "One label, lowercase"   --cap 0.25 --calls-per-month 1000000 --json verdict.json
```

Progress streams to stderr. The verdict prints the model and thinking flag to use, the held-out score with its 95%
CI next to Ultra's, cost per 1k calls, and the monthly and yearly saving at your volume; `--json` also saves the
final prompt and a ready-to-paste Token Factory snippet. The run is a custom task (40% tuning, 20% validation, 40%
held out) with Ultra measured live, so it obeys the same spend cap and grading in Nebius Sandboxes as the web app.

`examples/support_logs.jsonl` is **synthetic**: 40 made-up support emails we wrote to show both input formats, not
real customer data. Use real logs, and use enough of them: 8 to 200 rows are accepted, but the confidence interval
is only informative with a sizeable held-out split (we used 86 to 150 held-out rows per preset; with 40 rows there are
only 16). A 20-row smoke test on this file cost US$0.009.

## How it works

1. **Reference.** If your examples have no expected outputs, Nemotron Ultra writes them.
2. **Grader.** Each preset ships a `checker.py`. For a custom task, Ultra writes one. Before a checker is trusted it
   is tested: the reference answers must score about 1.0, and deliberately wrong answers must score low. If it fails,
   Ultra gets one retry with the failure; after that Good Enough falls back to exact match. Checker code is model-written,
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
   Ultra is a paired bootstrap of the difference on the same rows.
   **Matches = the 95% CI of the difference stays within 5 points of Ultra** (its lower bound is above −5 points).
   The verdict says "better than Ultra" only if the whole CI is above 0. The tolerance is adjustable per run.
   The first rung that matches is the answer and the rungs above it are never called. If no smaller model matches
   and Ultra with thinking off matches Ultra with thinking on, the verdict is "turn thinking off: same quality,
   Nx cheaper". The verdict shows tuning, validation and held-out scores side by side, so any overfitting is visible.

   *Why 5 points:* at 150 held-out rows the measured paired 95% CI is about ±4 points for two near-identical models
   (Ultra with thinking off vs on) and about ±6 points for a small model vs Ultra. With 3 points nothing can pass,
   not even Ultra against itself with thinking toggled (lower bounds −4.7, −4.9 and −5.6 on the three tasks).
   5 points is about the smallest tolerance a model that agrees with Ultra can pass at this sample size; it is still
   strict (a small model averaging the same as Ultra but disagreeing on many rows fails). See RESULTS.md.
7. **Squeeze.** After a cheaper verdict, the chosen setup's tokens are cut one lever at a time (brief answers,
   2 then 0 few-shot examples, an Ultra-compressed prompt). A cut is kept only if validation stays within 1 point
   of the current score and the cost per call falls below 0.97x; the result is scored once on held-out rows and is
   used only if it still matches Ultra. Turn it off with the `squeeze` option.
8. **Escalation chain.** If no cheaper rung matches alone, the two cheap rungs with the best validation scores
   are paired; on each held-out row the cheap answer is kept when the checker says the two agree, and Ultra's
   (measured) answer is used otherwise. The chain is used only if it matches Ultra and costs less than the verdict.
9. **Answer-key audit.** For rows whose references Ultra wrote, Ultra with thinking on answers again; rows where
   the two passes disagree are dropped from every split before anything is scored.
10. **Fine-tune fallback.** When no rung matches, Good Enough can build a chat dataset from the tuning rows' reference
   answers and launch a Token Factory fine-tune job (Qwen3-4B by default; Token Factory does not fine-tune
   Nemotron). It is off unless `DOWNSHIFT_ALLOW_FINETUNE=1`, and serving the result needs Token Factory's
   custom-weights access.

A hard per-run spend cap (US$0.25 by default; the server also clamps each run to `DOWNSHIFT_MAX_RUN_CAP_USD` and
stops at `DOWNSHIFT_SERVER_CAP_USD` per process) stops the run cleanly. Before each step the engine estimates its
cost, skips a rung that would not fit, and stops tuning early with a verdict rather than running out mid-step. With
the cached reference a preset run cost US$0.06 to US$0.21 and took 139 to 182 seconds (measured, see RESULTS.md).

## Where each Nebius / NVIDIA piece is used

| Piece | Used for |
|---|---|
| NVIDIA Nemotron 3.5 Lightning, Nemotron 3 Nano 30B, Super 120B, Ultra 550B | The ladder. Ultra also writes references, checkers and prompt rewrites. |
| Nebius Token Factory (OpenAI-compatible API, `https://api.tokenfactory.nebius.com/v1`) | Every model call. Live prices come from `GET /v1/models?verbose=true`. Thinking is toggled with `extra_body={"chat_template_kwargs": {"enable_thinking": ...}}`. |
| Nebius Sandboxes | Runs checker code on each batch of outputs, networking disabled. The checker and hidden answers are **sealed once** into a checkpoint image (a non-disposable operation); every batch is graded in a **disposable branch** of that checkpoint, so every model is marked against byte-identical answers (same image id, same SHA-256). A branch starts in 1.4 to 2.1 s vs 2.4 s for a fresh instance. The run log names the checkpoint each batch was graded in, and the UI says "graded locally" if Sandboxes were unavailable. |
| Token Factory fine-tuning | The fallback when prompting tops out. |

## Presets

| Slug | Task | Source |
|---|---|---|
| `support-triage` | Route bank support messages to one of 77 intents | Banking77 (PolyAI), CC-BY-4.0 |
| `receipt-extract` | Turn OCR'd receipt text into JSON line items and totals | CORD v2 (NAVER Clova), CC-BY-4.0 |
| `contract-clauses` | Tag contract clauses with one of 32 types | CUAD (The Atticus Project), CC-BY-4.0 |
| `code-mbpp` | Write a Python function from a one-line spec, graded only by running its unit tests in a sandbox | MBPP sanitized (Google Research), CC-BY-4.0 |

Each has 60 tuning, 40 validation and 150 held-out rows (receipt-extract: 49 / 40 / 86, the size of CORD's usable
test split; code-mbpp: 30 / 30 / 150), plus a cached Ultra reference (code-mbpp has none yet, so a run measures
Ultra live; its tests are the answer key, so it uses no few-shot references). See `presets/README.md` for how they were built.

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
# open http://localhost:8000, pick a preset, press "Prove it"
```

A finished run can be reopened at `/?run=<id>`. Runs are saved to `runs/<id>.json`; runs copied into `demo_runs/`
ship with the image so they stay viewable on a hosted copy.

API:

| Method | Path | |
|---|---|---|
| GET | `/api/health` | backend, `key_present`, Sandboxes info, pricing source |
| GET | `/api/presets` | preset list |
| POST | `/api/run` | `{"preset": "receipt-extract", "options": {"spend_cap_usd": 0.1}}` or `{"custom": {...}}`, returns a run id |
| GET | `/api/run/{id}/events` | server-sent events (replays a finished run; `?speed=4` paces it at 4x the recorded time) |
| GET | `/api/run/{id}` | saved run with verdict |

Options for `/api/run`: `spend_cap_usd`, `tolerance` (default 0.05), `rounds`, `candidates`, `thinking_rungs`,
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
docker build -t good-enough .
docker run -p 8000:8000 -e NEBIUS_API_KEY=... -e NEBIUS_PROJECT_ID=... good-enough
```

The container listens on `$PORT` (default 8000), so it runs as-is on Render, Fly or similar. Set the two secrets in the
host's settings. Useful limits for a public demo: `DOWNSHIFT_MAX_ACTIVE_RUNS` (default 2),
`DOWNSHIFT_MAX_RUN_CAP_USD` (default 0.50), `DOWNSHIFT_SERVER_CAP_USD` (default 5.00 per process).

## Layout

```
app.py                  FastAPI server and SSE
goodenough/models.py     rungs, pricing, Token Factory client, per-call accounting (runs/usage.jsonl)
goodenough/engine.py     the pipeline, as an async generator of events
goodenough/stats.py      bootstrap CIs and the paired comparison
goodenough/grading.py    Sandboxes grader with labelled local fallback
goodenough/sandbox_grader.py  Nebius Sandboxes harness
goodenough/finetune.py   dataset builder and fine-tune job launcher
goodenough/cli.py        run Good Enough on your own logged traffic (JSONL)
goodenough/tasks.py      presets, custom tasks, prompt assembly
static/                 single-page UI, no build step
presets/                three benchmark tasks: splits, checkers, cached Ultra references
scripts/                build_presets, measure_reference, run_presets, results_tables, repeat_verdict
```

## Licence

MIT.
