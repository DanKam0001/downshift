# Preset tasks

Three real business tasks, each built from a public, permissively licensed dataset with human-made ground truth.
Regenerate everything with `python scripts/build_presets.py` (downloads into `presets/_cache/`, which is git-ignored).
Sampling uses a fixed seed (`20261027`); train and holdout never share an example.

| Slug | Task | Output | Source | Licence | Train / holdout |
|---|---|---|---|---|---|
| `support-triage` | Route a bank customer's message to one of 77 intents | label | Banking77, PolyAI. https://github.com/PolyAI-LDN/task-specific-datasets (also `PolyAI/banking77` on Hugging Face) | CC-BY-4.0 | 30 (official train split) / 40 (official test split) |
| `receipt-extract` | Receipt OCR text to JSON: line items, subtotal, discount, service charge, tax, total, cash, change | json | CORD v2, NAVER Clova. https://huggingface.co/datasets/naver-clova-ix/cord-v2 | CC-BY-4.0 | 30 (validation split) / 40 (test split) |
| `contract-clauses` | Tag a commercial-contract clause with one of 32 clause types | label | CUAD v1, The Atticus Project. https://github.com/TheAtticusProject/cuad, https://zenodo.org/records/4595826 | CC-BY-4.0 | 30 / 40, split by contract |

Attribution (required by CC-BY-4.0):
- Casanueva et al., "Efficient Intent Detection with Dual Sentence Encoders", NLP4ConvAI 2020 (PolyAI).
- Park et al., "CORD: A Consolidated Receipt Dataset for Post-OCR Parsing", 2019 (NAVER Clova AI).
- Hendrycks et al., "CUAD: An Expert-Annotated NLP Dataset for Legal Contract Review", NeurIPS 2021 (The Atticus Project).

The presets are samples and light transformations of these datasets (described below); they are redistributed
under the same CC-BY-4.0 terms.

## Files per preset
- `task.json`: slug, title, business_story, instruction (the plain prompt), output_format, label_set or
  json_schema, source_url, license, attribution, split_note, n_train, n_holdout.
- `train.jsonl`, `holdout.jsonl`: rows of `{"id", "input", "reference"}`. For `receipt-extract` the reference is
  a JSON string. All inputs are under 1,500 characters.
- `checker.py`: `check(output, reference, input) -> (score 0..1, reason)`. Standard library only, deterministic,
  safe to run in a sandbox. Every reference scores 1.0 against itself (asserted by the build script).

## How each preset was made
- **support-triage**: random rows from the official Banking77 splits; all 77 labels are in the prompt. The checker
  accepts the label with any case, spaces or hyphens instead of underscores, quotes, code fences, or a short
  preamble ("Intent: ..."); naming two labels or an unknown label scores 0.
- **receipt-extract**: CORD's own OCR words regrouped into lines (by `row_id`, ordered top to bottom and left to
  right). The reference comes from CORD's `gt_parse`: top-level menu items (name, count, line price) and the
  sub_total/total fields, amounts as integer rupiah. Receipts were dropped if an amount was ambiguous, an item had
  no line total, a field held several values, or there was no total. CORD's validation and test splits share a few
  identical receipts; those are excluded from train. The checker scores 8 fields equally: 7 money fields (exact
  integer match after parsing "Rp 25.000" / "25,000" / 25000; a missing key counts as null) and `items` (F1, an
  item matches on equal price and name similarity >= 0.6).
- **contract-clauses**: clause spans annotated by CUAD's lawyers for 32 of CUAD's 41 categories (dropping parties,
  titles, dates and a few rare ones). A span is kept only if no other CUAD category overlaps it, it is 120 to
  1,500 characters long, and it is deduplicated. Contracts are split in half by a seeded shuffle, so no contract is
  in both train and holdout; samples are stratified across labels.

Known noise: Banking77 has well-documented near-duplicate intents (e.g. `balance_not_updated_after_bank_transfer`
vs `pending_transfer`), and CORD tags the Indonesian restaurant tax "PB1" as tax and leaves zero-priced add-ons out
of the item list. Both are faithful to the source annotations; see `BASELINE.md` for what that does to scores.

`run_baseline.py` runs the plain-prompt baseline; raw outputs go to `_baseline/`.
