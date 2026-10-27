# Results

Measured 2026-10-27 on Nebius Token Factory. Three public datasets, each with a held-out split the prompt
optimiser never saw and never used to choose a prompt. Every number below is reproducible from files in this repo:
the Ultra reference from `presets/<slug>/reference.json` and the ladder runs from `demo_runs/<run id>.json` (open
`/?run=<id>` in the app). The tables are printed by `python scripts/results_tables.py demo_runs/*.json`.

## Headline

**Turning on Nemotron Ultra's thinking multiplied its cost per call by 2.4x to 6.6x on all three tasks, and on none
of them was the accuracy gain distinguishable from zero.** The best estimates of the gain are +0.7, +2.4 and +1.1
points; the paired 95% confidence intervals are −3.6 to +4.7, −0.3 to +4.9 and −3.3 to +5.6 points (3 runs per mode
on 86 to 150 held-out rows). Thinking also made Ultra 1.5x to 2.8x slower at the median and noisier from run to run.

| Task | Held-out rows | Ultra thinking off: 3 runs (mean) | Ultra thinking on: 3 runs (mean) | On minus off, paired 95% CI | Cost per 1k calls, off → on | p50, off → on | Rows that changed between repeats, off / on |
|---|---|---|---|---|---|---|---|
| support-triage | 150 | 72.0% / 71.3% / 71.3% (71.6%) | 72.0% / 71.3% / 73.3% (72.2%) | +0.7 (−3.6 to +4.7) | 0.554 → 1.319 (**2.4x**) | 1.16 → 1.78 s | 1 / 10 |
| receipt-extract | 86 | 89.2% / 89.7% / 89.8% (89.6%) | 93.7% / 90.7% / 91.4% (92.0%) | +2.4 (−0.3 to +4.9) | 0.739 → 3.693 (**5.0x**) | 1.25 → 3.55 s | 6 / 31 |
| contract-clauses | 150 | 80.7% / 81.3% / 81.3% (81.1%) | 82.0% / 83.3% / 81.3% (82.2%) | +1.1 (−3.3 to +5.6) | 0.347 → 2.301 (**6.6x**) | 0.76 → 2.01 s | 3 / 11 |

What this does and does not say: thinking may still help a little (receipt-extract's interval only just reaches
below zero), but no gain larger than 5.6 points is consistent with the data on any task,
and the price of finding out is 2.4x to 6.6x per call. It is the first thing to check before moving to a smaller
model.

## How these numbers were measured

- **Splits.** Each preset has tuning rows (the optimiser sees their failures and may pick few-shot examples from
  them), validation rows (used only to choose between prompt candidates) and held-out rows (scored once per rung,
  with the single prompt chosen on validation). Sizes: 60 / 40 / 150 for support-triage and contract-clauses, and
  49 / 40 / 86 for receipt-extract, because CORD's test split has only 86 receipts that pass the build filters.
  See `presets/README.md`.
- **Reference.** `scripts/measure_reference.py` ran Ultra with the plain prompt on every held-out row, with thinking
  off and on, 3 times each (temperature 0, same prompt and `max_tokens` as the engine). The reference is the
  per-row mean over the 3 runs of the better mode (thinking on, on all three tasks). Runs use this cache by default;
  the UI shows "Ultra reference measured on 2026-10-27, 3 runs, mean X ± half the range".
- **Confidence intervals.** 95% bootstrap over rows (4,000 resamples, fixed seed). Comparisons with Ultra are paired:
  the same resampled rows are used for both sides, so shared row difficulty cancels.
- **Verdict rule.** A cheaper rung matches Ultra only if the lower bound of the paired 95% CI of (rung − Ultra) is
  above −3 points. The wording says "better than Ultra" only if that lower bound is above 0.
- **Live runs.** One run per preset through the web server with the default US$0.10 cap, grading in Nebius Sandboxes.

## Per task

The "Tuning → validation → held out" column is the chosen prompt's score on each split; the gap between them is how
much the prompt overfit. Ultra rows are the cached 3-run means.

### Support ticket triage, 77 banking intents (`support-triage`, run `73ad158d7950`)

| Rung | Thinking | Prompt | Tuning → validation → held out | Held out, 95% CI | vs Ultra (thinking on), paired 95% CI | US$ per 1k calls | p50 latency | Status |
|---|---|---|---|---|---|---|---|---|
| Nemotron 3.5 Lightning | off | rewritten | 68.3% → 77.5% → 70.0% | **70.0%** (62.7–77.3) | −2.2 (−8.7 to +4.2) | 0.0866 | 0.70 s | not shown to match |
| Nemotron 3 Nano 30B | off | carried from Lightning | 66.7% → 67.5% → 66.0% | **66.0%** (58.0–73.3) | −6.2 (−13.3 to +0.7) | 0.0866 | 1.20 s | not shown to match |
| Nemotron 3 Super 120B | off | – | – | – | – | – | – | skipped: spend cap |
| Nemotron 3.5 Lightning | on | – | – | – | – | – | – | skipped: spend cap |
| Nemotron 3 Ultra 550B | off | plain | 3 runs: 72.0% / 71.3% / 71.3% | **71.6%** (64.0–78.4) | −0.7 (−4.7 to +3.6) | 0.5543 | 1.16 s | cached |
| Nemotron 3 Ultra 550B | on | plain | 3 runs: 72.0% / 71.3% / 73.3% | **72.2%** (64.9–79.1) | reference | 1.3193 | 1.78 s | reference |

- **Verdict:** keep Ultra. Lightning (thinking off) scored 70.0% against Ultra's 72.2%, a difference of −2.2 points
  with a 95% CI of −8.7 to +4.2, so it could not be shown to be within 3 points. It costs 15x less per call.
- **Overfit gap:** the chosen Lightning prompt scored 68.3% on tuning rows, 77.5% on validation and 70.0% held out.
  Tuning and held out agree (+1.7 points). The validation score is 7.5 points too high: picking the best of three
  candidates on 40 rows flatters the winner, which is why the final number only comes from held-out rows.
- **Run:** US$0.0761, 83 s, 798 model calls.

### Receipt OCR to structured JSON (`receipt-extract`, run `7b83f078f3bd`)

| Rung | Thinking | Prompt | Tuning → validation → held out | Held out, 95% CI | vs Ultra (thinking on), paired 95% CI | US$ per 1k calls | p50 latency | Status |
|---|---|---|---|---|---|---|---|---|
| Nemotron 3.5 Lightning | off | plain | 83.9% → 87.8% → 88.2% | **88.2%** (86.0–90.4) | −3.7 (−6.2 to −1.1) | 0.0533 | 1.88 s | not shown to match |
| Nemotron 3 Nano 30B | off | rewritten | 93.2% → 90.2% → 90.8% | **90.8%** (88.4–93.0) | −1.2 (−3.8 to +1.6) | 0.1000 | 2.49 s | not shown to match |
| Nemotron 3 Super 120B | off | plain | 92.1% → 92.0% → not scored | – | – | – | – | not scored on held out: spend cap |
| Nemotron 3.5 Lightning | on | – | – | – | – | – | – | skipped: spend cap |
| Nemotron 3 Ultra 550B | off | plain | 3 runs: 89.2% / 89.7% / 89.8% | **89.6%** (87.4–91.5) | −2.4 (−4.9 to +0.3) | 0.7389 | 1.25 s | cached |
| Nemotron 3 Ultra 550B | on | plain | 3 runs: 93.7% / 90.7% / 91.4% | **92.0%** (89.1–94.6) | reference | 3.6927 | 3.55 s | reference |

- **Verdict:** keep Ultra, narrowly. Nano (thinking off) with a rewritten prompt scored 90.8% against Ultra's 92.0%:
  −1.2 points, 95% CI −3.8 to +1.6, so the lower bound misses the −3 point rule by 0.8 points. It is 37x cheaper per
  call than Ultra with thinking on, and 1.2 points above Ultra with thinking off. This is the closest any cheaper
  rung came, and it is the case where more held-out rows would most likely change the verdict.
- **Overfit gap:** Nano's chosen prompt: 93.2% tuning → 90.2% validation → 90.8% held out, a 2.4-point drop from
  tuning to held out.
- Super scored 92.1% / 92.0% on tuning and validation with the plain prompt, but the US$0.10 cap ran out before its
  held-out pass; it is the obvious next rung to measure with a larger cap.
- **Run:** US$0.0685, 145 s, 618 model calls.

### Contract clause classification, 32 clause types (`contract-clauses`, run `b35268c96e18`)

| Rung | Thinking | Prompt | Tuning → validation → held out | Held out, 95% CI | vs Ultra (thinking on), paired 95% CI | US$ per 1k calls | p50 latency | Status |
|---|---|---|---|---|---|---|---|---|
| Nemotron 3.5 Lightning | off | rewritten | 67.9% → 67.5% → 68.7% | **68.7%** (61.3–76.0) | −13.6 (−21.1 to −5.8) | 0.0673 | 0.68 s | not shown to match |
| Nemotron 3 Nano 30B | off | plain | 58.3% → 60.0% → 57.3% | **57.3%** (49.3–65.3) | −24.9 (−32.7 to −17.1) | 0.0212 | 0.74 s | not shown to match |
| Nemotron 3 Super 120B | off | – | – | – | – | – | – | skipped: spend cap |
| Nemotron 3.5 Lightning | on | – | – | – | – | – | – | skipped: spend cap |
| Nemotron 3 Ultra 550B | off | plain | 3 runs: 80.7% / 81.3% / 81.3% | **81.1%** (74.9–87.3) | −1.1 (−5.6 to +3.3) | 0.3472 | 0.76 s | cached |
| Nemotron 3 Ultra 550B | on | plain | 3 runs: 82.0% / 83.3% / 81.3% | **82.2%** (76.2–87.8) | reference | 2.3011 | 2.01 s | reference |

- **Verdict:** keep Ultra. The small models with thinking off are clearly worse here (Lightning −13.6 points, CI
  −21.1 to −5.8): this is a real gap, not noise.
- **Overfit gap, and the validation split doing its job:** in round 2 Ultra proposed a Lightning prompt that scored
  **78.6% on the tuning rows but 65.0% on validation**. Under the old method (select on tuning rows) it would have
  been the winner. It was rejected; the chosen prompt scored 67.9% → 67.5% → 68.7%, a tuning-to-held-out gap of
  +0.8 points. Before this change, tuning scores of 0.85-0.92 were followed by held-out scores 10-20 points lower.
- **Run:** US$0.0820, 105 s, 1,175 model calls.

## Finding: Ultra's own run-to-run noise

At temperature 0, Ultra is not deterministic on Token Factory. Over 3 back-to-back runs on the same held-out rows:

| Task | Thinking off: spread of 3 runs | rows that changed | Thinking on: spread of 3 runs | rows that changed |
|---|---|---|---|---|
| support-triage (150 rows) | 0.7 points | 1 | 2.0 points | 10 |
| receipt-extract (86 rows) | 0.6 points | 6 | 3.0 points | 31 |
| contract-clauses (150 rows) | 0.7 points | 3 | 2.0 points | 11 |

- With thinking on, Ultra's 3-run spread was 3 to 5 times larger and 3 to 10 times as many rows changed. A single run of Ultra with thinking on can move by 2 to 3
  points on its own, which is as large as the 3-point tolerance. That is why the reference is a 3-run mean and why
  a single-run comparison against it should not be trusted to the point.
- The earlier problem (Ultra with thinking off scoring 70.0 / 72.5 / 75.0 / 77.5% on the same 40 rows) came from
  runs hours apart. These three repeats ran within the same few minutes, and changed only 1 to 6 rows. Runs bunched
  in time probably understate drift across hours or deployments, so the cached reference carries its date and can be
  re-measured with one switch (`"reference": "live"` or the checkbox in Settings).

## Limits, stated plainly

- **The 3-point rule is strict at these sizes.** With 150 rows the paired 95% CI on a classification task is about
  ±4 to ±7 points wide. Even Ultra with thinking off, 0.7 to 2.4 points behind Ultra with thinking on, never passes
  the rule (its lower bounds are −4.7, −4.9 and −5.6). In practice a cheaper rung has to be about as good as Ultra
  row for row, or better on average, to be recommended. That is the honest cost of a confidence interval; the fix is
  more held-out rows, not a looser rule.
- **Only one side of the comparison is averaged.** The reference is a 3-run mean; each cheaper rung is one run.
  The bootstrap covers row sampling, not that rung's own run-to-run noise (small for thinking-off rungs, per the
  table above).
- **The US$0.10 cap reaches two or three rungs.** With 150-row holdouts and validation scoring, each run measured
  Lightning and Nano; Super and Lightning with thinking on were skipped or stopped before their held-out pass. The
  verdicts are therefore "no rung shown to match among those measured", not "no Nemotron matches".
- **receipt-extract has 86 held-out rows**, not 150 (the size of CORD's usable test split).
- **The prompt optimiser is Ultra with thinking off**, capped at 2,500 tokens per proposal. One support-triage
  proposal was cut off and its round skipped.

## Cost of this work

| Item | US$ |
|---|---|
| Ultra reference, support-triage (900 held-out calls + 40 validation) | 0.865 |
| Ultra reference, receipt-extract (516 + 40) | 1.171 |
| Ultra reference, contract-clauses (900 + 40) | 1.205 |
| A first contract-clauses reference attempt that stopped at its cost check | 0.092 |
| One development run | 0.030 |
| The three runs above | 0.227 |
| **Total** (sum of `runs/usage.jsonl` for this work) | **3.59** |

A judge's live run with the cached reference costs US$0.07 to US$0.08 and takes 83 to 145 seconds.
