# Results

Measured 2026-10-27 on Nebius Token Factory. Three public datasets, each with a held-out split the prompt
optimiser never saw and never used to choose a prompt. Every number below can be traced to a file in this repo:
the Ultra reference comes from `presets/<slug>/reference.json` and the ladder runs from `demo_runs/<run id>.json`
(open `/?run=<id>` in the app). The tables are printed by `python scripts/results_tables.py demo_runs/*.json`.

## Headline

**On receipt extraction, Nemotron 3 Nano (thinking off) with a rewritten prompt matched Ultra within 5 points at
36x lower cost per call: 92.4% vs Ultra's 92.0%, both the mean of 3 runs on the same 86 held-out receipts, paired
difference +0.5 points (95% CI −2.6 to +3.6).** The ladder run that chose this prompt scored Nano once at 93.6%
(+1.7, CI −1.0 to +4.7); re-running that exact prompt 3 more times gave 91.8%, 91.9% and 93.7%
(`scripts/repeat_verdict.py`, saved in `demo_runs/45015b6df59e.repeats.json`, US$0.03). An earlier ladder run with a
different rewritten prompt scored 90.8%. Read it as "Nano is in Ultra's range on this task", not "Nano beats Ultra".

**Across all three tasks, turning Ultra's thinking on cost 2.4x to 6.6x more per call, and the accuracy gain was
not distinguishable from zero on any of them.** On two of the three (support-triage and receipt-extract) Ultra with
thinking off passes the 5-point rule against Ultra with thinking on, so "turn thinking off" is a valid downshift on
its own.

| Task | Held-out rows | Ultra thinking off: 3 runs (mean) | Ultra thinking on: 3 runs (mean) | On minus off, paired 95% CI | Cost per 1k calls, off → on | p50, off → on | Rows that changed between repeats, off / on |
|---|---|---|---|---|---|---|---|
| support-triage | 150 | 72.0% / 71.3% / 71.3% (71.6%) | 72.0% / 71.3% / 73.3% (72.2%) | +0.7 (−3.6 to +4.7) | 0.554 → 1.319 (**2.4x**) | 1.16 → 1.78 s | 1 / 10 |
| receipt-extract | 86 | 89.2% / 89.7% / 89.8% (89.6%) | 93.7% / 90.7% / 91.4% (92.0%) | +2.4 (−0.3 to +4.9) | 0.739 → 3.693 (**5.0x**) | 1.25 → 3.55 s | 6 / 31 |
| contract-clauses | 150 | 80.7% / 81.3% / 81.3% (81.1%) | 82.0% / 83.3% / 81.3% (82.2%) | +1.1 (−3.3 to +5.6) | 0.347 → 2.301 (**6.6x**) | 0.76 → 2.01 s | 3 / 11 |

The verdicts, with a 5-point tolerance and a US$0.25 cap per run:

| Task | Verdict | Held out vs Ultra (thinking on) | Cost per 1k calls vs Ultra | Run cost, time |
|---|---|---|---|---|
| support-triage | **Turn thinking off** (Ultra) | 71.6% vs 72.2%, −0.7 (−4.7 to +3.6) | $0.554 vs $1.319, 2.4x cheaper | $0.175, 182 s |
| receipt-extract | **Use Nano, thinking off, rewritten prompt** | 93.6% vs 92.0%, +1.7 (−1.0 to +4.7) | $0.103 vs $3.693, 35.8x cheaper | $0.060, 139 s |
| contract-clauses | **Keep Ultra** (thinking on) | best cheaper rung: Lightning 76.0%, −6.2 (−12.4 to +0.0) | – | $0.212, 169 s |

## How these numbers were measured

- **Splits.** Each preset has tuning rows (the optimiser sees their failures and may pick few-shot examples from
  them), validation rows (used only to choose between prompt candidates) and held-out rows (scored once per rung,
  with the single prompt chosen on validation). Sizes: 60 / 40 / 150 for support-triage and contract-clauses, and
  49 / 40 / 86 for receipt-extract, because CORD's test split has only 86 receipts that pass the build filters.
  See `presets/README.md`.
- **Reference.** `scripts/measure_reference.py` ran Ultra with the plain prompt on every held-out row, with thinking
  off and on, 3 times each (temperature 0, same prompt and `max_tokens` as the engine). The reference is the per-row
  mean over the 3 runs of the better mode, which was thinking on for all three tasks.
- **Confidence intervals.** 95% bootstrap over rows (4,000 resamples, fixed seed). Comparisons with Ultra are paired:
  the same resampled rows are used for both sides, so shared row difficulty cancels.
- **Verdict rule.** A rung *matches* Ultra if the 95% CI of (rung − Ultra) stays above −5 points. It is called
  *better* only if the whole CI is above 0, which happened nowhere. Rungs are tried by cost per call (Lightning,
  Nano and Super with thinking off, then Lightning with thinking on). If none matches, Ultra with thinking off is
  checked against Ultra with thinking on. The first match is the verdict.
- **Why 5 points.** At these sizes the paired 95% CI is wide. On the 150-row classification tasks it ran from about
  ±4 points (Ultra off vs on, which agree on most rows) to about ±6 points (a small model vs Ultra, which disagree on
  more rows). With a 3-point rule nothing passes, not even Ultra against itself with thinking toggled (lower bounds
  −4.7, −4.9, −5.6). 5 points is roughly the smallest tolerance a near-identical model can pass at 150 rows. It
  remains strict: Lightning on support-triage was +0.4 points on average and still failed (CI −5.8 to +6.4).
- **Single runs.** Each cheaper rung is measured once per run. The bootstrap covers which rows were sampled, not that
  rung's run-to-run noise or the luck of which prompt Ultra proposed.
- **Live runs.** One run per preset through the web server with the default US$0.25 cap, grading in Nebius
  Sandboxes, using the cached Ultra reference.

## Per task

The "Tuning → validation → held out" column is the chosen prompt's score on each split; the gap is how much the
prompt overfit. The Ultra rows are the cached 3-run means.

### Support ticket triage, 77 banking intents (`support-triage`, run `de04430645fb`)

| Rung | Thinking | Prompt | Tuning → validation → held out | Held out, 95% CI | vs Ultra (thinking on), paired 95% CI | US$ per 1k calls | p50 latency | Status |
|---|---|---|---|---|---|---|---|---|
| Nemotron 3.5 Lightning | off | rewritten | 71.4% → 75.0% → 72.7% | **72.7%** (65.3–79.3) | +0.4 (−5.8 to +6.4) | 0.0971 | 0.71 s | not shown to match |
| Nemotron 3 Nano 30B | off | rewritten | 76.8% → 70.0% → 72.0% | **72.0%** (64.7–79.3) | −0.2 (−6.2 to +6.2) | 0.0999 | 1.07 s | not shown to match |
| Nemotron 3 Super 120B | off | plain | 68.3% → 72.5% → 66.7% | **66.7%** (58.7–74.0) | −5.6 (−10.7 to −0.7) | 0.1703 | 2.20 s | not shown to match |
| Nemotron 3.5 Lightning | on | – | – | – | – | – | – | skipped: spend cap |
| Nemotron 3 Ultra 550B | off | plain | 3 runs: 72.0% / 71.3% / 71.3% | **71.6%** (64.0–78.4) | −0.7 (−4.7 to +3.6) | 0.5543 | 1.16 s | **matches: the verdict** |
| Nemotron 3 Ultra 550B | on | plain | 3 runs: 72.0% / 71.3% / 73.3% | **72.2%** (64.9–79.1) | reference | 1.3193 | 1.78 s | reference |

- **Verdict:** turn Ultra's thinking off: same quality within 5 points, 2.4x cheaper and 35% faster at the median.
- Lightning and Nano scored as well as Ultra on average (+0.4 and −0.2 points) at about 1/13 of the cost, but they
  disagree with Ultra on enough rows that the CI is ±6 points wide. They cannot be shown to be within 5 points on
  150 rows.
- **Overfit gap:** Lightning's chosen prompt scored 71.4% → 75.0% → 72.7%. Nano's tuning score was 4.8 points above
  its held-out score.
- **Run:** US$0.1746, 182 s, 1,430 model calls. One Ultra prompt proposal was cut off and its round skipped.

### Receipt OCR to structured JSON (`receipt-extract`, run `45015b6df59e`)

| Rung | Thinking | Prompt | Tuning → validation → held out | Held out, 95% CI | vs Ultra (thinking on), paired 95% CI | US$ per 1k calls | p50 latency | Status |
|---|---|---|---|---|---|---|---|---|
| Nemotron 3.5 Lightning | off | rewritten | 88.1% → 88.5% → 82.3% | **82.3%** (75.8–88.0) | −9.7 (−16.2 to −3.9) | 0.1014 | 2.76 s | not shown to match |
| Nemotron 3 Nano 30B | off | carried from Lightning | 94.1% → 90.3% → 93.6% | **93.6%** (91.5–95.7) | +1.7 (−1.0 to +4.7) | 0.1031 | 1.85 s | **matches: the verdict** |
| Nemotron 3 Super 120B | off | – | – | – | – | – | – | not needed: a cheaper rung matched |
| Nemotron 3.5 Lightning | on | – | – | – | – | – | – | not needed: a cheaper rung matched |
| Nemotron 3 Ultra 550B | off | plain | 3 runs: 89.2% / 89.7% / 89.8% | **89.6%** (87.4–91.5) | −2.4 (−4.9 to +0.3) | 0.7389 | 1.25 s | cached |
| Nemotron 3 Ultra 550B | on | plain | 3 runs: 93.7% / 90.7% / 91.4% | **92.0%** (89.1–94.6) | reference | 3.6927 | 3.55 s | reference |

- **Verdict:** use Nano with thinking off and the rewritten prompt (it is in the run's code snippet): it matches
  Ultra within 5 points at $0.103 instead of $3.693 per 1k calls (35.8x cheaper) and is about twice as fast at the
  median.
- The same prompt hurt Lightning: it went from 88.5% on validation to 82.3% held out, a 6-point overfit that the
  verdict rule caught.
- Variation between runs: in the previous run (US$0.10 cap, run `7b83f078f3bd`, kept in `demo_runs/superseded/`)
  Nano with a different rewritten prompt scored 90.8% (−1.2, CI −3.8 to +1.6). Both runs put Nano within Ultra's
  range; the exact figure depends on which prompt Ultra proposes.
- **Run:** US$0.0605, 139 s, 606 model calls.

### Contract clause classification, 32 clause types (`contract-clauses`, run `7ee9b72d56be`)

| Rung | Thinking | Prompt | Tuning → validation → held out | Held out, 95% CI | vs Ultra (thinking on), paired 95% CI | US$ per 1k calls | p50 latency | Status |
|---|---|---|---|---|---|---|---|---|
| Nemotron 3.5 Lightning | off | rewritten | 82.1% → 67.5% → 76.0% | **76.0%** (68.7–82.7) | −6.2 (−12.4 to +0.0) | 0.0694 | 0.67 s | not shown to match |
| Nemotron 3 Nano 30B | off | carried from Lightning | 75.0% → 67.5% → 72.0% | **72.0%** (64.7–79.3) | −10.2 (−17.3 to −2.9) | 0.0694 | 0.95 s | not shown to match |
| Nemotron 3 Super 120B | off | carried from Lightning | 80.4% → 77.5% → 74.0% | **74.0%** (66.7–80.7) | −8.2 (−13.3 to −3.1) | 0.3469 | 1.08 s | not shown to match |
| Nemotron 3.5 Lightning | on | – | – | – | – | – | – | skipped: spend cap |
| Nemotron 3 Ultra 550B | off | plain | 3 runs: 80.7% / 81.3% / 81.3% | **81.1%** (74.9–87.3) | −1.1 (−5.6 to +3.3) | 0.3472 | 0.76 s | not shown to match (by 0.6 points) |
| Nemotron 3 Ultra 550B | on | plain | 3 runs: 82.0% / 83.3% / 81.3% | **82.2%** (76.2–87.8) | reference | 2.3011 | 2.01 s | reference: the verdict |

- **Verdict:** keep Ultra with thinking on. Every smaller model with thinking off is 6 to 10 points behind. Ultra
  with thinking off misses the rule by 0.6 points (lower bound −5.6).
- Lightning with thinking on, the rung most likely to close the gap, did not fit the US$0.25 cap after three rungs
  with prompt rewrites.
- **Overfit gap:** Lightning's chosen prompt scored 82.1% on tuning rows, 67.5% on validation and 76.0% held out.
  A 6-point tuning-to-held-out drop is still visible, but it is smaller than the 10-20 points seen before
  validation-based selection.
- **Run:** US$0.2124, 169 s, 1,715 model calls.

## Finding: Ultra's own run-to-run noise

At temperature 0, Ultra is not deterministic on Token Factory. Over 3 back-to-back runs on the same held-out rows:

| Task | Thinking off: spread of 3 runs | rows that changed | Thinking on: spread of 3 runs | rows that changed |
|---|---|---|---|---|
| support-triage (150 rows) | 0.7 points | 1 | 2.0 points | 10 |
| receipt-extract (86 rows) | 0.6 points | 6 | 3.0 points | 31 |
| contract-clauses (150 rows) | 0.7 points | 3 | 2.0 points | 11 |

- With thinking on, the 3-run spread was 3 to 5 times larger and 3 to 10 times as many rows changed. A single run
  of Ultra with thinking on can move 2 to 3 points on its own. This is why the reference is a 3-run mean.
- The earlier problem (Ultra with thinking off scoring 70.0 / 72.5 / 75.0 / 77.5% on the same 40 rows) came from
  runs hours apart. These repeats ran within a few minutes and changed only 1 to 6 rows. Repeats taken close
  together probably understate drift across hours or deployments, so the cached reference carries its date and can
  be re-measured live with one switch (`"reference": "live"`, or the checkbox in Settings).

## Limits

- **Cheaper rungs are single runs inside a ladder run.** A different prompt proposal can move a rung by a few points
  (Nano on receipts: 90.8% with one proposal, 93.6% with the next). For the headline verdict we re-ran the chosen
  prompt 3 times (`scripts/repeat_verdict.py`: 91.8%, 91.9%, 93.7%, mean 92.4%), the same treatment as Ultra's
  reference; the other rungs were not repeated.
- **Even 5 points is strict at 150 rows.** Small models that average the same as Ultra (support-triage) still fail
  because they disagree with Ultra on many individual rows. More held-out rows would narrow the CI.
- **Lightning with thinking on was never reached.** At US$0.25 it fits after one or two rungs, not after three.
- **receipt-extract has 86 held-out rows**, not 150.
- **The prompt optimiser is Ultra with thinking off**, with 2,500 tokens per proposal. One proposal was cut off.

## Fine-tune fallback: one real job

Token Factory fine-tunes Qwen, Llama, Gemma, GPT-OSS and DeepSeek models, not Nemotron, so the fallback is a small
open model trained on Ultra's answers. We ran one job end to end on 2026-10-27 to measure what that path costs in
time: LoRA (r=8) on Qwen3-0.6B, 60 chat rows, 1 epoch (`ftjob-e31dcdd634894b8c92bf1c19071e4b4d`).

| Stage | Time |
|---|---|
| Submitted → training data processed | 58 s |
| Queued before training started | **4 h 19 min** |
| Training (1,881 tokens, 1 step) | 7 min 27 s |
| Status | succeeded, 6 result files, no servable model id |

Serving the trained weights on Token Factory needs its custom-weights access, which is granted on request. We asked,
then withdrew the request because the contest rules bar preferential support. So this job proves the pipeline runs end to end, but its
quality was not measured. The practical lesson is the queue: a fine-tune is a next-day step, not part of a
3-minute run, which is why Downshift exhausts prompting on Nemotron first and only then offers the job.

## Cost of this work

| Item | US$ |
|---|---|
| Ultra reference, support-triage (900 held-out calls + 40 validation) | 0.865 |
| Ultra reference, receipt-extract (516 + 40) | 1.171 |
| Ultra reference, contract-clauses (900 + 40) | 1.205 |
| A first contract-clauses reference attempt that stopped at its cost check | 0.092 |
| One development run, three earlier runs at a US$0.10 cap and a 3-point rule | 0.257 |
| The three runs above (US$0.25 cap, 5-point rule) | 0.447 |
| Nano re-measured 3 times on receipts with the verdict prompt (2 attempts; the first crashed after its calls) | 0.053 |
| **Total** (sum of `runs/usage.jsonl` for this work, excluding the fine-tune job) | **4.09** |

With the cached reference, a judge's live run costs US$0.06 to US$0.21 and takes 139 to 182 seconds.
