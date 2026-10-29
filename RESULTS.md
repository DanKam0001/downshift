# Results

Measured 2026-10-27 to 29 on Nebius Token Factory. Three public text datasets, each with a held-out split the prompt
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

## Squeeze: cutting tokens after the verdict (run `150a5ad36713`)

Once a rung is chosen, Downshift tries to cut its tokens, one lever at a time, on the 40 validation receipts. A cut is
kept only if the validation score stays within 1 point of the current one and the cost per call falls below 0.97x.

| Step | Validation | Tokens in / out | $ / 1k calls | Kept? |
|---|---|---|---|---|
| Ultra, thinking on (the reference) | | | 3.693 | |
| Nano, thinking off, as the verdict chose it (4 worked examples) | 92.2% | 1261 / 117 | 0.1037 | start |
| One-line (minified) JSON answers | 90.7% | 1214 / 97 | 0.0960 | dropped, score fell |
| 2 worked examples instead of 4 | 93.5% | 920 / 119 | 0.0836 | **kept** |
| No worked examples | 88.7% | 603 / 144 | 0.0707 | dropped, score fell |
| Ultra rewrites the prompt in half the words | | | | skipped: the rewrite was under 15% shorter |

The squeezed setup, scored once on the 86 held-out receipts: **94.3% (95% CI 92.2 to 96.3), +2.4 points vs Ultra
(paired CI −0.5 to +5.5), US$0.0874 per 1k calls, 42.3x cheaper than Ultra** (the verdict alone was 34.6x in this run).
The same run's photo check: Super VL reading the photo, 93.9%, matches Ultra. Whole run US$0.133, 227 s.

## Beyond text: photos, video and code (measured 2026-10-29)

Full tables: [docs/levers-vision.md](docs/levers-vision.md) and [docs/levers-code.md](docs/levers-code.md). Each
setup runs on the same held-out rows and is compared with the most expensive setup by the same paired bootstrap.

- **Pet breed from a photo** (Oxford-IIIT Pet, CC BY-SA 4.0, 111 held out, 37 breeds; Nemotron 3.5 Super VL, the only
  NVIDIA vision model on Token Factory). Top setup (labels prompt, thinking on): 83.8% at $0.943 / 1k. **Thinking off:
  83.8% at $0.145 / 1k, 6.5x cheaper**, within 5 points (CI −4.5 to +4.5). Shrinking the photo to 256 px: 86.5%, same
  price, because an image costs at least ~300 tokens below ~450 px. Listing the 37 labels in the prompt was worth ~9
  points.
- **Action from a video clip** (KTH human actions, 162 fresh clips, 6 actions; 8 frames at 768 px). Top setup, thinking
  on: 80.2% at $1.70 / 1k. Thinking off lost 13 to 16 points; 4 frames instead of 8 lost 7.4. **No downshift**: telling
  jogging from running needs the model to compare frames.
- **Python from a one-line spec** (MBPP sanitized, CC-BY-4.0, 150 held out), graded only by running every assert in a
  sandbox. Canonical solutions score 100%, every fixed or stub answer 0%. Ultra thinking on 85.3% at $3.705 / 1k;
  Ultra thinking off 85.3% at $0.392; Nano thinking off 74.7%; **Nano thinking on 89.3% at $0.148 / 1k, 25x cheaper,
  +4.0 points (CI +0.0 to +8.7)**; a second run gave 90.0%. A 34x setup (answer only, 2048-token cap) was found by
  trying levers on the held-out rows themselves, so it is exploratory and not claimed.

## Your own task with no answers: SMS spam (run `091a657f0889`)

100 messages from the UCI SMS Spam Collection, pasted with **no labels**. Ultra wrote the reference answers and a
checker; the checker was tested before use (references 100%, wrong answers 0%) and accepted on the first attempt. The
guess check passed: "ham" on every row scores 55%, Ultra 100%. Nano (thinking off) matched Ultra on the 40 held-out
rows at 16.3x cheaper, 17.4x ($0.0058 / 1k) after the squeeze. The run cost US$0.032 and took 402 s, graded in
branches of sealed answer-key checkpoints. As a check on "Ultra wrote the answers": Ultra's labels agreed with the
human UCI labels on 98 of 100 messages (US$0.0099).

This run also found a real bug: Ultra's checker handled ``` fences itself, and our fence-stripping regex cut its code
short, so it failed to compile. The extractor is now greedy to the last fence, with a test.

## Cost levers checked and not claimed

- **Cached prompt tokens.** Token Factory lists them 5 to 8x cheaper. We measured **zero cache hits** on serverless
  Nemotron: with and without `prompt_cache_key`, with long shared prefixes, and across the 15,000+ logged calls in
  this work. No caching saving is assumed anywhere.
- **Batch API.** `/v1/batches` answers, but we found no published batch discount to verify, so none is claimed.
- **Time-of-day prices.** The pricing API (`/v1/models?verbose=true`) has none.

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

### Do you even need the OCR step? Nemotron Super VL reads the photo (`demo_runs/vision-receipt-extract.json`)

receipt-extract's inputs are CORD v2's annotated OCR text: perfect OCR, which a real pipeline would have to pay for
and would get wrong sometimes. So we gave Nemotron 3.5 Super VL (thinking off) the receipt photo instead, with no
OCR at all, and asked for the same JSON: 86 held-out receipts, 3 passes each, graded by the same checker in Nebius
Sandboxes and compared with the same cached Ultra reference (Ultra, thinking on, reading the perfect OCR text).

| Super VL, photo in | 3 runs | Mean | vs Ultra on perfect OCR text, paired 95% CI | US$ per 1k calls | p50 latency |
|---|---|---|---|---|---|
| Plain prompt | 93.4% / 93.5% / 94.0% | **93.6%** | +1.7 (−0.9 to +4.6): matches | 0.438 | 2.05 s |
| Verdict prompt + its 4 text few-shots | 96.0% / 96.0% / 96.0% | **96.0%** | **+4.0 (+1.2 to +7.3): better than Ultra** | 0.720 | 1.88 s |

- Reading the photo directly is as good as Ultra reading perfect OCR text with the plain prompt, and **measurably
  better** with the prompt Downshift had already tuned for Nano, at 5x to 8x cheaper than Ultra. A receipt image is
  about 1,082 input tokens, which is why it costs more than Nano on text ($0.103).
- Caveats: one dataset; the comparison is photo vs perfect text, not photo vs a real OCR engine (which would make the
  text side worse, not better); the few-shots are text, only the final turn is an image.
- A setup error we kept on record: the tuned prompt *without* its few-shots scored 55.7%, because the few-shots are
  where the output keys are defined, and Super VL nested the totals under a `"totals"` key. That run is excluded
  from the table but its cost is in the total below.
- Reproduce: `python scripts/fetch_cord_images.py` then `python scripts/vision_rung.py --repeats 3` (about US$0.20).
- **In the app:** receipt-extract now ships its 86 held-out photos (`presets/receipt-extract/images/`), and a run
  ends with a photo check: Super VL reads the photos with the verdict's prompt, graded and compared exactly like a
  ladder rung. Run `2e794b56ae40` (recorded, US$0.106 for the whole run, 700 calls, 238 s): Nano won the ladder
  again (90.3%, −1.7, CI −4.4 to +1.3, $0.082 per 1k), and the photo check scored **95.2%, +3.2 pts over Ultra
  (95% CI +0.3 to +6.4)** at $0.591 per 1k. One pass, a different tuned prompt from the table above, same direction.

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

## Customer test: my own hackathon judge bot (and the guess check it forced)

The presets are public datasets. To test Downshift on a real workload, I ran it on one of my own bots:
hackathon_scout's judge, which scores every lablab.ai submission 1-5 on the event's 4 criteria to predict winners.
It runs once per submission (100 to 1,100 per event) on Claude Sonnet today. Task: the bot's exact prompt on 105
IBM Bob Hackathon (May 2026) submissions, demo and repo links removed; correct answer = the real judges' average
scores. The data stays private (not in this repo): only the aggregate numbers below are published.

| Judge | Closeness to the real judges' scores (checker) | Rank agreement with the real judges (Spearman of totals) | US$ per 1k |
|---|---|---|---|
| **No model: always answer 3** | **75.4%** | none (no ranking) | 0 |
| Claude Sonnet (the bot today; cached verdicts) | 74.1% | 0.33 | (Claude plan) |
| Nemotron 3 Ultra, thinking off (3 runs) | 72.1% | 0.36 to 0.37 | 2.10 |
| Nemotron 3 Ultra, thinking on (3 runs) | 71.7% | 0.37 to 0.38 | about 4.06 |
| Nemotron 3 Nano, Downshift's rewritten prompt (3 runs) | 71.6% | 0.31 to 0.34 | 0.14 |

- **The first ladder run said "use Nano: matches Ultra, 15x cheaper"** (one run 72.3%; 3 runs 71.6%, −0.5 pts,
  95% CI −3.2 to +2.0). Statistically true, and empty: a fixed answer of 3 on every criterion beats every model on
  this checker, because the real judges' averages sit near 3. A verdict on a checker that a guess passes means nothing.
- **So Downshift now runs a guess check before the ladder** (`engine.guess_check`, `scripts/guess_check.py`): it
  scores fixed answers taken from the tuning rows (the most common answer, and the typical value of every JSON field)
  and stops with **no verdict** unless Ultra is provably better than the best guess (paired 95% CI above zero). It
  costs no model calls. On the judge task it trips (Ultra minus the guess: −3.3 pts, CI −7.2 to +0.7) and the run
  ends in 27 s at US$0.00. On the three presets it passes by 48 to 79 points, so their verdicts stand.
- What the bot actually needs is ranking, and there every text-only model is weak (Spearman 0.31 to 0.38). Nano
  ranks about as well as Sonnet. The real judges watched the demo videos; the lever for this bot is the video, not
  the model size.
- Thinking mode again bought nothing (71.7% on vs 72.1% off, twice the price): now 4 tasks out of 4.
- Nano's rewritten prompt also stopped writing the per-criterion reasons, because the checker only grades the
  scores. A checker defines what "as good" means; anything it does not grade can silently disappear.
- Cost: US$2.11 for the Ultra reference (plus US$0.26 for a first estimate pass), US$0.12 for the ladder run,
  US$0.04 for Nano's repeats. A first reference pass graded every row 0 because the references were stored as
  objects instead of JSON text; the saved outputs were re-graded with no new calls.

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
| Super VL on receipt photos (plain + tuned prompt, 3 runs each, plus the 55.7% setup-error run) | 0.429 |
| Receipts run with the photo check (`2e794b56ae40`) | 0.106 |
| Customer test on my own hackathon judge bot (estimate pass, Ultra reference, ladder run, Nano repeats) | 2.54 |
| **Total** (sum of `runs/usage.jsonl` for this work, excluding the fine-tune job) | **7.17** |

With the cached reference, a judge's live run costs US$0.06 to US$0.21 and takes 139 to 182 seconds.
