# Build log

## 2026-10-27: engine, server, UI (main builder)

All runs below were started through the server (`POST /api/run`) on a preset, graded in Nebius Sandboxes (every
batch, no local fallback), and saved to `runs/<id>.json`. Spend is the sum of `cost_usd` in `runs/usage.jsonl`
for that run id.

### Before the thinking-mode ladder (rungs = models only, thinking off)

| Run | Preset | Cap | Verdict | Held out (cheap / Ultra) | US$ per 1k (cheap / Ultra) | Time | Spend |
|---|---|---|---|---|---|---|---|
| f94bdbe7ffd2 | support-triage | 0.10 | fine-tune (best: Super, rewritten) | 70.0% / 72.5% | – | 72 s | $0.0850 |
| e314ff4f87f1 | support-triage | 0.10 | fine-tune (best: Lightning, rewritten) | 70.0% / 75.0% | – | 72 s | $0.0796 |
| 8a7feb7ec2a0 | contract-clauses | 0.10 | fine-tune (best: Super, rewritten) | 72.5% / 77.5% | – | 108 s | $0.0767 |
| c649bf41ac6b | receipt-extract | 0.10 | **Lightning, rewritten prompt** | 92.0% / 88.7% | 0.1134 / 0.7409 (6.5x) | 85 s | $0.0774 |

Fixes made from these runs: Ultra's prompt proposal was cut off at 1,500 tokens (round skipped), so the limit was raised to 2,500
and the reason is now shown; the tuning target became Ultra's own tuning score, not that minus the tolerance,
because stopping at a noisy train score sent weak prompts to the held-out split; and a budget guard was added that
stops tuning before the cap and still gives a verdict.

### Thinking-mode ladder (rungs = model x thinking on/off; reference = Ultra thinking on unless off scores higher)

| Run | Preset | Cap | Verdict | Held out (chosen / reference) | US$ per 1k (chosen / reference) | Time | Spend |
|---|---|---|---|---|---|---|---|
| 0575921f478c | receipt-extract | 0.30 | **Lightning (thinking off), plain prompt** | 87.8% / 89.5% Ultra off (within one row) | 0.0534 / 0.7388 (13.8x) | 46 s | $0.0542 |
| e29113b9b613 | support-triage | 0.20 | fine-tune (best: Lightning thinking on, 72.5%) | 72.5% / 77.5% Ultra off | – | 214 s | $0.0991 |
| **2c7db97ebcf6** | support-triage | 0.20 | **Lightning (thinking off), rewritten prompt** | 80.0% / 72.5% Ultra thinking on | 0.0566 / 1.1073 (19.6x) | 70 s | $0.0968 |

- 0575921f478c: Ultra with thinking on was skipped because a fixed 6x cost estimate said it would not fit. This was
  replaced with a measured 5-row probe that is then extrapolated.
- e29113b9b613 was run before concurrency went from 8 to 16; Lightning with thinking on had a p50 of 12 s, so that run took 214 s.
- 2c7db97ebcf6 is the reference end-to-end run: every rung type ran (Ultra off on both splits, the Ultra-thinking probe and
  full held-out pass, a Lightning rewrite round, and the held-out confirmation). It stopped at the first rung, so Lightning-thinking, Nano and Super were never
  called.

Showcase copies: `demo_runs/2c7db97ebcf6.json`, `demo_runs/0575921f478c.json` (open `/?run=<id>`).

### Spend

My total Nebius spend: **US$0.569** (sum of `runs/usage.jsonl`, 7 runs, about 2,300 model calls) plus about US$0.001 of
one-call probes at the start. No fine-tune job was launched.

### What the numbers say about noise

Ultra with thinking off scored 72.5%, 75.0%, 77.5% and 70.0% on the **same** 40 support-triage held-out rows in
four runs at temperature 0. That is up to 3 rows of run-to-run variation. A Lightning prompt rewrite scored 70.0%
in one run and 80.0% in another. The default 2-point tolerance is below the 2.5-point row size. The verdict text
refuses to call a gap under one row a win, but pass or fail against the bar can still flip between runs on
support-triage. Options: a tolerance of at least 1 row, more held-out rows, or averaging Ultra over two passes.
