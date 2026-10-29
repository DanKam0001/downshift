# Code generation, graded by unit tests in Nebius Sandboxes (measured 2026-10-29)

**The idea:** you don't need the right answer, only a way to check one. On this task the answer key is the
dataset's unit tests, not a reference answer. Every model reply is run inside a **Nebius Sandbox (networking
off)** against all the asserts. It scores 1 only if every assert passes.

- **Task:** write a Python function from a one-line spec. The model sees the problem plus the first assert, which
  gives it the function name and call shape. It is graded on all asserts (usually 3), so at least two of them are
  hidden from it.
- **Data:** MBPP sanitized, Austin et al. 2021, Google Research, **CC-BY-4.0**
  (github.com/google-research/google-research/tree/master/mbpp; also HF `google-research-datasets/mbpp`, config
  `sanitized`). There are 150 held-out rows from the test id range, plus 30 validation and 30 tuning rows from the
  validation and train ranges.
- **Preset:** `presets/code-mbpp/` (task.json, checker.py, train/validation/holdout.jsonl). It is built by
  `scripts/build_code_preset.py` and loads with `goodenough.tasks.load_preset("code-mbpp")`.
- **Measurement:** `scripts/code_levers.py` (calibrate / run / report). Per-row outputs, tokens, cost, score,
  checker reason and sandbox op id for every config are saved in `demo_runs/code-mbpp/`.
- **Settings:** temperature 0, concurrency 8. With thinking on, max_tokens is 1024 + 4096, the engine's budget. A
  reply that runs out of tokens while reasoning counts as no answer.

## Do the tests measure skill? (sandbox only, $0)

| Answer fed to the checker (150 held-out rows) | Pass |
|---|---|
| Dataset's canonical solutions | **100.0%** |
| `def f(*a): return None` | 0.0% |
| Correct function name, `return None` / `True` / `False` / `0` | 0.0% each |
| Correct name, echo the first argument | 0.7% (1 row) |
| Infinite loop | 0.0% (killed by the time limit) |
| Empty reply | 0.0% |

One row (mbpp-123) was dropped before any model run. Its canonical solution takes more than the 2.6 s
per-item limit inside the sandbox, so even a correct answer could fail on it.

## The ladder (150 held-out rows, default prompt; diff is paired vs Ultra with thinking on)

| Setup | Pass | Mean in | Mean out | $ / 1k calls | Diff vs Ultra-think [95% CI] | Within 5 pts |
|---|---|---|---|---|---|---|
| **Ultra, thinking on (reference)** | 85.3% | 113 | 1198 | $3.705 | reference | - |
| Ultra, thinking off | 85.3% | 113 | 93 | $0.392 | +0.0 [-4.7, +4.7] | yes |
| Super, thinking off | 81.3% | 113 | 355 | $0.354 | -4.0 [-8.7, +0.7] | no |
| Nano, thinking off | 74.7% | 113 | 72 | $0.024 | -10.7 [-17.3, -4.0] | no |
| **Nano, thinking on** | **89.3%** | 113 | 590 | **$0.148** | **+4.0 [+0.0, +8.7]** | **yes** |
| Lightning, thinking off | 81.3% | 113 | 119 | $0.035 | -4.0 [-10.0, +2.0] | no |
| Lightning, thinking on | 87.3% | 113 | 1311 | $0.321 | +2.0 [-2.0, +6.0] | yes |

**Verdict:** **Nano 30B with thinking on** is the cheapest setup that does the job: $0.148 per 1k calls against
$3.705, **25x cheaper than Ultra-think**. It is also **2.6x cheaper than Ultra with thinking off**, the cheapest
way to run Ultra itself. Its point estimate is above Ultra's.

**Repeat check:** temperature 0 is not deterministic, so the key configs were run a second time.

| Setup | Run 1 | Run 2 | Rows that flipped |
|---|---|---|---|
| Ultra-think | 85.3% | 85.3% | 8 |
| Nano-think | 89.3% | 90.0% | 5 |
| Nano-think + answer-only | 90.7% | 89.3% | 2 |
| Lightning-think | 87.3% | 86.0% | 2 |

- Averaging both runs, Nano-think vs Ultra-think is +4.3 [+0.7, +8.3]. That clears 5-point non-inferiority, and
  the CI lower bound is above 0.
- Inside a single run pair, run 2 vs run 2 gave +4.7 [-0.7, +10.0]. That still matches, but it does not beat.
- So "matches" is solid. "Beats" depends on the run, and we should not claim it.

## Levers (on the cheap rungs; same 150 rows)

| Setup | Pass | Mean in | Mean out | $ / 1k | Diff vs Ultra-think [95% CI] | Within 5 |
|---|---|---|---|---|---|---|
| Nano-think + answer-only ("code block only, no explanation") | 90.7% | 130 | 477 | $0.122 | +5.3 [+1.3, +9.3] | yes |
| Nano-think + short prompt (1 line) | 87.3% | 80 | 610 | $0.151 | +2.0 [-3.3, +7.3] | yes |
| Nano-think + long prompt (7 guidelines) | 90.7% | 244 | 749 | $0.194 | +5.3 [+1.3, +10.0] | yes |
| Nano-think, whole reply capped at 1024 tokens | 80.7% | 113 | 448 | $0.114 | -4.7 [-10.0, +0.0] | **no** |
| Nano-think, whole reply capped at 2048 tokens | 89.3% | 113 | 505 | $0.128 | +4.0 [+0.0, +8.0] | yes |
| **Nano-think + answer-only + cap 2048** | 88.7% | 130 | 418 | **$0.108** | +3.3 [+0.0, +7.3] | **yes** |
| Lightning-think, whole reply capped at 2048 | 80.0% | 113 | 1029 | $0.254 | -5.3 [-10.7, -0.7] | no |
| Lightning-off + answer-only | 80.0% | 130 | 59 | $0.022 | -5.3 [-11.3, +0.7] | no |
| Lightning-off + short prompt | 82.7% | 80 | 125 | $0.035 | -2.7 [-8.7, +3.3] | no |
| Lightning-off + long prompt | 83.3% | 244 | 100 | $0.039 | -2.0 [-8.0, +4.0] | no |
| Lightning-off, answer capped at 256 | 77.3% | 113 | 105 | $0.032 | -8.0 [-14.7, -1.3] | no |
| Nano-off + answer-only | 76.0% | 130 | 54 | $0.021 | -9.3 [-15.3, -3.3] | no |
| Nano-off + long prompt | 74.7% | 244 | 51 | $0.027 | -10.7 [-17.3, -4.0] | no |
| Super-off + answer-only | 78.0% | 130 | 60 | $0.093 | -7.3 [-13.3, -1.3] | no |

**The cheapest lever stack within 5 points is Nano-think + answer-only + a 2048-token cap: $0.108 per 1k, 34x
cheaper than Ultra-think.** Caveat: the levers were explored on the held-out rows, not chosen on validation. So
treat 34x as exploratory and **25x (plain Nano-think) as the clean number.**

What the levers did:
- **Answer-only** cut Nano-think's output by 19% (590 to 477 tokens) at no loss in quality.
- **A cap kills quality if it is too tight:**
  - On Nano-think, a 2048 cap is free, but 1024 drops 9 points (19 truncations).
  - On Lightning-think, 2048 drops 7 points, because Lightning reasons for about 1300 tokens on average.
  - On Lightning-off, an answer cap of 256 drops 4 points.
- **Prompt length barely matters** (±3 points, inside the noise) for every rung.
- **No lever lifts a thinking-off small model into range.** Nano-off stays at about 75% and Lightning-off at
  80-83%. On code, the jump comes from turning thinking on, not from rewording the prompt.

## Surprises

1. **Ultra with thinking on is no better than Ultra with thinking off (85.3% both, CI ±4.7), at 9.4x the price.**
   - Ultra-think hit the 5,120-token limit on 14 of 150 rows in both repeats. That scores 0 on rows that other
     setups solve in 1 line: 11 of the 14 are passed by some other setup.
   - On the rows where it finished, Ultra-think passes 94%. The model is fine; the overthinking costs it.
2. **Super with "thinking off" still thinks aloud.** It writes untagged reasoning into the answer: mean 355 output
   tokens, and 15 rows hit the 1024-token limit while still working, mostly before any finished code block. Answer-only stops the rambling (60 tokens),
   but the pass rate falls to 78%. Super is the one rung where turning thinking off does not mean "short".
3. **9 rows fail for every setup.** These are known MBPP test quirks (e.g. mbpp-310 expects
   `string_to_tuple("python 3.0")` to drop the space). They put the ceiling near 94%. No checker leniency was added
   for them, because the tests are the spec.
4. **Thinking matters much more for small models on code:** Nano +14.7 points, Lightning +6, Ultra +0.

## Sandbox evidence

- **Backend:** Nebius Sandboxes, image `python:3.12-slim` (4fa16c8f-62a9-35a4-a4ea-07b7e8cd44ae),
  `networking: false`.
- **Batching:** 1 disposable operation grades each 150-row batch. A batch takes about 1 s of guest time and 5-15 s
  wall time; the infinite-loop guess batch took about 45 s.
- **Example operation ids:**
  - Ultra-think `01a184d7-9bb4-77b0-aadb-da0a9d252f4d`
  - Nano-think `01a184e1-885f-721e-b4bf-1f97eab2dcbc`
  - Nano-think + answer-only + cap 2048 `01a184ed-2ead-7012-bb39-085985addfaf`
  - Canonical check `01a184d6-ac96-7326-aac8-2701be73ac91`
  - Infinite-loop guess `01a184d7-1a35-7231-8e27-637f41952be3`
- All ids are in each `demo_runs/code-mbpp/<config>.summary.json` under `sandbox`.

## Spend

The total Nebius inference spend for this whole experiment was **$1.69** against a $2.50 cap. It covers probes, 7
rungs, 14 lever configs and 4 repeats; Ultra-think was $1.14 of it across two runs. Sandbox grading is not
included in that figure. The per-config breakdown is in `demo_runs/code-mbpp/spend.json`.

## Notes for wiring into the app

- `reference` holds tests (a JSON string), not an answer. Few-shot examples built from `reference` would be
  wrong for this preset: the engine's `build_messages` uses the reference as the assistant turn. So run it with
  no few-shots, or teach the engine to skip them when `grading` is set.
- `task.json` sets `max_answer_tokens: 1024`. `reference.json` (the cached Ultra reference) was not written, so the
  app would measure Ultra live. Alternatively, convert `demo_runs/code-mbpp/ultra-think*.jsonl` into one.
