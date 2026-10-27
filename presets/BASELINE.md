# Plain-prompt baseline (2026-10-27)

> **Superseded split.** These numbers were measured on the first version of the presets (30 tuning / 40 held-out
> rows, single runs). The presets have since been rebuilt with 60 / 40 / 150 rows (see `README.md`), and Ultra's
> reference is now measured 3 times per thinking mode on the full holdout (`*/reference.json`). Current numbers are
> in `../RESULTS.md`. This file is kept for the per-call cost and latency observations, which still hold.

Each preset's 40-row holdout, run once per model on Nebius Token Factory. Prompt = one user message,
`task.instruction + "\n\nInput:\n" + input`; temperature 0; `max_tokens` 8000; scored with the preset's `checker.py`.
Prices from `GET /v1/models?verbose=true`: Lightning $0.06 / $0.24 per 1M input/output tokens, Ultra $1 / $3.
Reproduce: `python presets/run_baseline.py --no-think`. Raw outputs: `presets/_baseline/*.jsonl`, numbers:
`presets/_baseline/summary.json`.

Both models are reasoning models and **think by default**. "no-think" is the same call with
`extra_body={"chat_template_kwargs": {"enable_thinking": False}}`, which removes the reasoning tokens.

| Task | Model | Thinking | Holdout score | Input tok | Output tok (of which reasoning) | p50 latency | $ this run (40 calls) | $ per 1k calls |
|---|---|---|---|---|---|---|---|---|
| support-triage | Lightning | default (on) | **0.725** | 21,262 | 44,877 (44,604) | 16.0 s | 0.0121 | 0.30 |
| support-triage | Lightning | off | 0.675 | 21,262 | 236 (0) | 0.65 s | 0.0013 | 0.03 |
| support-triage | Ultra | default (on) | **0.700** | 21,262 | 12,685 (12,421) | 2.1 s | 0.0593 | 1.48 |
| support-triage | Ultra | off | 0.725 | 21,262 | 230 (0) | 1.2 s | 0.0220 | 0.55 |
| receipt-extract | Lightning | default (on) | **0.794** | 10,731 | 164,563 (159,473) | 14.9 s | 0.0401 | 1.00 |
| receipt-extract | Lightning | off | 0.864 | 10,731 | 6,067 (0) | 1.1 s | 0.0021 | 0.05 |
| receipt-extract | Ultra | default (on) | **0.939** | 10,731 | 53,701 (47,652) | 4.6 s | 0.1718 | 4.30 |
| receipt-extract | Ultra | off | 0.897 | 10,731 | 6,176 (0) | 1.3 s | 0.0293 | 0.73 |
| contract-clauses | Lightning | default (on) | **0.750** | 13,358 | 64,212 (63,941) | 9.1 s | 0.0162 | 0.41 |
| contract-clauses | Lightning | off | 0.600 | 13,358 | 265 (0) | 0.58 s | 0.0009 | 0.02 |
| contract-clauses | Ultra | default (on) | **0.775** | 13,358 | 30,694 (30,421) | 2.8 s | 0.1054 | 2.64 |
| contract-clauses | Ultra | off | 0.800 | 13,358 | 229 (0) | 0.87 s | 0.0141 | 0.35 |

n = 40 per cell, so one row is 2.5 points; differences under ~10 points are within noise.

## Reading it per task

- **receipt-extract: good gap.** Ultra 0.94 vs Lightning 0.79 (thinking) / 0.86 (no thinking). Lightning's
  thinking run hit the 8,000-token cap on 6 of 40 receipts and never wrote the JSON. Most errors on both models are
  learnable from the train split: "PB1 10%" (Indonesian restaurant tax) filed as discount or service charge, and a
  subtotal invented when the receipt prints none. That is exactly what prompt rewriting / few-shot should fix.
- **contract-clauses: gap only without thinking.** First built with 16 clause types: Lightning 0.875 vs Ultra
  0.900, too close. **Adjusted to 32 clause types** (adds the licence, IP, non-solicit, MFN, escrow, etc. types),
  giving Ultra 0.78-0.80 vs Lightning 0.75 (thinking) / 0.60 (no thinking). Errors are real legal near-misses
  (cap_on_liability vs uncapped_liability, liquidated_damages vs minimum_commitment).
- **support-triage: no gap; Lightning already matches Ultra.** 0.68-0.73 for every model and mode. 9 of the 16
  rows that either model missed are missed by both, and they are Banking77's known near-duplicate intents
  (pending_transfer vs balance_not_updated_after_bank_transfer, verify_my_identity vs unable_to_verify_identity).
  The ceiling is label ambiguity, not model size. Not adjusted: no slice of Banking77 removes the ambiguity without
  cherry-picking rows Ultra gets right. Useful as the "downshift immediately" case in the demo (Lightning with
  thinking off costs $0.03 per 1k calls vs Ultra's $1.48 by default, within 2.5 points), or swap it if every preset must show a gap.

## Things the engine should know

- **Thinking is the biggest cost and latency lever, bigger than model tier.** Lightning with thinking on is often
  slower than Ultra (p50 9-16 s vs 2-5 s) and on receipts costs more per call than Ultra with thinking off
  ($1.00 vs $0.73 per 1k). Lightning thinks 1,000-4,000 tokens on a one-word classification. Treat
  thinking on/off as a ladder rung, and set a `max_tokens` cap.
- When thinking is cut off by `max_tokens`, Lightning's `content` contains the start of its reasoning, not an
  answer; the checker scores it 0.
- Ultra's reasoning output is metered at $3/1M; most of Ultra's cost here is reasoning tokens, not the prompt.

## Spend

All Nebius calls made for this work (including the superseded 16-label contract run, the first receipt build and
a few one-call probes): **about US$0.80**.
