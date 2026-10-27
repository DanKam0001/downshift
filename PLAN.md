# Downshift — Nebius x NVIDIA Global AI Hackathon entry

Working name. Replaces Safe Hands (user decision 2026-10-27). Chosen as Option A after judge and competitor research:
see `../nebius_nvidia/research_2026-10-27/`.

**Hard deadline:** Fri 30 Oct 10:00 PT = **Sat 31 Oct 04:00 Melbourne**. Internal: everything ready for the user to
submit by **Fri 30 Oct 18:00 Melbourne**.

## One line
Give it a task and ~20 examples; it finds the **cheapest NVIDIA Nemotron on Nebius Token Factory that does the job at
Ultra quality**, by measuring, rewriting prompts, and only recommending fine-tuning when the numbers say so.

## DONE WHEN (falsifiable)
1. A public GitHub repo with an MIT licence and a README (setup, how Nemotron, Token Factory and Sandboxes are used).
2. A hosted URL where a judge can pick a preset task, press one button, and see a verdict with a held-out quality /
   $ per 1k calls / p50 latency table for each Nemotron tier tried, in **under 4 minutes** and costing **under $0.25** (raised from $0.10 on 2026-10-27 so Super gets a held-out pass).
3. Three real tasks benchmarked on a **held-out** split, written up in `RESULTS.md` with one headline number.
4. One real Token Factory fine-tune job run end to end on the task where prompting falls short, with its cost and time
   recorded honestly (and its quality, if a checkpoint can be served anywhere before Friday).
5. A video under 3:00 uploaded to YouTube, and the Devpost form drafted, so the user only has to paste and submit.

Not done: anything that only works locally, any number measured on the examples the optimiser saw, any claim not
backed by a run log.

## How it works
(Updated 2026-10-27 to match the code.)

1. **Splits.** Tuning rows (the optimiser sees them), validation rows (used only to choose between prompt
   candidates) and held-out rows (scored once per rung). Presets: 60 / 40 / 150 (receipts 49 / 40 / 86). Custom
   tasks: 40% / 20% / 40%. Rows without a reference get one from Ultra (thinking off).
2. **Grader.** Presets ship a deterministic `checker.py`; for a custom task Ultra writes one. It is validated
   (references must score ~1.0, wrong answers low; one retry, then exact match) and **runs in Nebius Sandboxes**
   (network off), with a labelled local fallback.
3. **Reference.** Ultra, plain prompt, full holdout, thinking off and on, 3 repeats each, cached per preset in
   `presets/<slug>/reference.json` by `scripts/measure_reference.py`. The better mode's per-row 3-run mean is the
   reference (thinking on for all three presets). Runs use the cache; a setting (or a stale cache, or a custom task)
   measures Ultra live instead (one pass).
4. **Ladder.** Rungs in order of measured cost per call: Lightning, Nano and Super with thinking off, then
   Lightning with thinking on. Per rung: plain prompt on tuning + validation; unless it already reaches Ultra's
   validation score, Ultra proposes rewrites (instructions + few-shot ids from tuning rows only), each scored on
   tuning rows (minus its shots) and validation rows; the best **validation** score wins and is scored once on the
   holdout. A rung that does not fit the spend cap is skipped.
5. **Verdict.** Paired bootstrap 95% CI of (rung − Ultra) on the same held-out rows. Matches = the CI's lower bound
   is above −5 points (adjustable); "better" only if the lower bound is above 0. The first matching rung wins. If no
   smaller model matches, Ultra with thinking off is compared with Ultra with thinking on ("turn thinking off: same
   quality, Nx cheaper"). Otherwise: keep Ultra, fine-tuning is next. Output: model id, thinking flag, final prompt,
   code snippet, score ± CI, difference ± CI, tuning / validation / held-out scores, $ per 1k calls, p50 latency.
6. **Fine-tune fallback.** Builds a chat dataset from the tuning rows' reference answers and launches a Token
   Factory fine-tune job (off unless `DOWNSHIFT_ALLOW_FINETUNE=1`). Serving the result needs the custom-weights beta
   (requested by email 2026-10-27).

## Stack
Python 3.12, FastAPI, server-sent events for live progress, one static HTML/JS page (no build step). OpenAI SDK
against `https://api.tokenfactory.nebius.com/v1`. Sandboxes via its Python SDK, project header
`aiproject-e00mrs9q3hk2561sxe`. Key from env `NEBIUS_API_KEY` only; never committed, never sent into a sandbox.
Deploy: Docker on Render (or similar) — light app, no Chromium.

## Track and prizes
Best Apps and Agents. Tavily only if it does real work (not planned). Aim for overall top 3.

## Facts established 2026-10-27
- Token Factory fine-tunes Qwen/Llama/Gemma/GPT-OSS/DeepSeek, **not Nemotron**.
- Any fine-tuned weights need the custom-weights beta to be served on Token Factory (on request).
- Tiny LoRA job on Qwen3-0.6B (60 rows, 1 epoch): still running after 20+ min. Fine-tuning is slow; plan around it.
- Sandboxes: spawn allowed, 50 concurrent, 3600 s max per instance.
- Credits: user topped up $15 on 2026-10-27.

## Schedule
- Tue night: plan, smoke tests, engine skeleton.
- Wed: engine + grader in Sandboxes + 3 tasks benchmarked; UI.
- Thu: deploy, fine-tune fallback run, polish, video.
- Fri by 18:00: kit handed to the user (GitHub publish, YouTube upload, Devpost submit).
