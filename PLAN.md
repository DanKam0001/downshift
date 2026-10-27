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
   $ per 1k calls / p50 latency table for each Nemotron tier tried, in **under 4 minutes** and costing **under $0.10**.
3. Three real tasks benchmarked on a **held-out** split, written up in `RESULTS.md` with one headline number.
4. One real Token Factory fine-tune job run end to end on the task where prompting falls short, with its cost and time
   recorded honestly (and its quality, if a checkpoint can be served anywhere before Friday).
5. A video under 3:00 uploaded to YouTube, and the Devpost form drafted, so the user only has to paste and submit.

Not done: anything that only works locally, any number measured on the examples the optimiser saw, any claim not
backed by a run log.

## How it works
1. **Reference.** Ultra (`nvidia/Nemotron-3-Ultra-550b-a55b`) produces reference outputs when the user gives none.
2. **Grader.** Ultra writes a Python checker for the task (exact fields, JSON schema, label match, numeric tolerance)
   plus an LLM-judge rubric for fuzzy criteria. The checker is model-written code, so it **runs in Nebius Sandboxes**
   (network off), never on our server. The checker is validated against the references before use.
3. **Ladder.** Lightning, Nano, Super, Ultra each run the task with a plain prompt on the train split; score, cost,
   latency recorded.
4. **Downshift loop.** For the cheapest tier below the bar: Ultra rewrites the prompt and picks few-shot examples
   from the train split; each candidate is scored on the train split in a Sandboxes batch; best candidate is then
   scored once on the **held-out split**. Steps up a tier only if the cheaper one cannot reach the bar.
5. **Verdict.** Model id + final prompt + code snippet to paste, cost per 1k calls vs Ultra, and a sentence:
   "Use Lightning with this prompt" / "Use Super" / "Fine-tune: prompting tops out at X%".
6. **Fine-tune fallback.** Builds a dataset from Ultra outputs and launches a Token Factory fine-tune job; shows job
   status and cost. Serving the result needs the custom-weights beta (requested by email 2026-10-27).

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
