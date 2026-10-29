"""How far can input-side levers cut the cost of breed-from-photo on Nemotron 3.5 Super VL?

    python scripts/build_levers_data.py --pet-parquet <oxford test parquet>   # once
    python scripts/levers_pets.py --phase validation    # 37 images, one per breed: picks the prompt
    python scripts/levers_pets.py --phase holdout       # 111 held-out images, every setup, paired vs the top

Data: Oxford-IIIT Pet (CC BY-SA 4.0), 37 cat and dog breeds. Checker: the answer (text after 'Answer:' if present,
else the last line) is normalised (lowercase, punctuation to spaces, a trailing 'cat'/'dog' dropped, a few official
long names mapped to the dataset's label, see ALIASES) and must equal the label exactly.

The top setup is the original photo (Oxford images are ~500 px on the long side), thinking on, with the prompt
picked on validation. Every other setup runs on the same 111 images and is compared with the paired bootstrap
from downshift/stats.py; "within 5" means the 95% CI lower bound of (setup - top) is above -5 points.
Note: 768/1024/2048 are UPSCALED from ~500 px; they show what a larger photo costs in tokens, not extra detail.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from levers_common import (DATA, MODEL, PRICE, ROOT, TOTAL_CAP_USD, Client, add_spent, cheapest_within,  # noqa: E402
                           compare_all, extract_answer, image_part, jpeg_bytes, norm, summarise, table, total_spent)

PETS = DATA / "pets"
META = json.loads((PETS / "rows.json").read_text())
LABELS = META["labels"]
PRETTY = [l.replace("_", " ").title() for l in LABELS]
ALIASES = {"german shorthaired pointer": "german shorthaired", "soft coated wheaten terrier": "wheaten terrier",
           "staffordshire bull terrier": "staffordshire bull terrier", "american pit bull": "american pit bull terrier",
           "pit bull terrier": "american pit bull terrier", "pitbull": "american pit bull terrier",
           "pit bull": "american pit bull terrier", "pitbull terrier": "american pit bull terrier", "yorkie": "yorkshire terrier", "shiba": "shiba inu",
           "english cocker spaniel": "english cocker spaniel", "newfoundland dog": "newfoundland",
           "saint bernard": "saint bernard", "st bernard": "saint bernard", "great pyrenees": "great pyrenees",
           "sphinx": "sphynx", "maine coon": "maine coon", "bengal": "bengal"}

PROMPTS = {
    "plain": "What breed is the cat or dog in this photo? Answer with the breed name only.",
    "labels": ("Identify the breed of the cat or dog in the photo. It is exactly one of these 37 breeds: "
               + ", ".join(PRETTY) + ". Answer with one breed name from the list, exactly as written, and nothing else."),
    "explain": ("Identify the breed of the cat or dog in the photo. It is exactly one of these 37 breeds: "
                + ", ".join(PRETTY) + ". First describe, in 2-3 sentences, the visible features that identify the "
                "breed. Then end with a final line of the form 'Answer: <breed name from the list>'."),
}
# Chosen on the validation set (see demo_runs/levers-pets.json -> validation); answer-only prompts only.
CHOSEN_PROMPT = "labels"


def grade(text: str, label: str) -> float:
    a = norm(extract_answer(text))
    for suffix in (" cat", " dog"):
        if a.endswith(suffix) and a not in {norm(l.replace("_", " ")) for l in LABELS}:
            a = a[: -len(suffix)]
    for prefix in ("a ", "the "):
        if a.startswith(prefix):
            a = a[len(prefix):]
    a = ALIASES.get(a, a)
    return 1.0 if a == norm(label.replace("_", " ")) else 0.0


def setup_name(s: dict) -> str:
    res = "orig" if s["res"] is None else f"{s['res']}px"
    q = "" if s.get("q", 90) == 90 else f" q{s['q']}"
    return f"{s['prompt']} | {res}{q} | think {'on' if s['thinking'] else 'off'}"


async def run(client: Client, rows: list[dict], s: dict) -> tuple[list[dict], list[float]]:
    imgs = {r["id"]: Image.open(PETS / f"{r['id']}.jpg") for r in rows}
    max_tokens = (4096 if s["thinking"] else 0) + (400 if s["prompt"] == "explain" else 40)

    def msgs(r):
        return [{"role": "system", "content": PROMPTS[s["prompt"]]},
                {"role": "user", "content": [image_part(jpeg_bytes(imgs[r["id"]], s["res"], s.get("q", 90)))]}]
    calls = await asyncio.gather(*(client.chat(msgs(r), s["thinking"], max_tokens) for r in rows))
    return calls, [grade(c["text"], r["label"]) if c["ok"] else 0.0 for r, c in zip(rows, calls)]


VALIDATION = [dict(prompt=p, res=None, thinking=t) for p in ("plain", "labels", "explain") for t in (False, True)]
HOLDOUT = (
    [dict(prompt=CHOSEN_PROMPT, res=r, thinking=True) for r in (None, 1024, 768, 512, 384, 256)]
    + [dict(prompt=CHOSEN_PROMPT, res=r, thinking=False) for r in (None, 2048, 1024, 768, 512, 384, 256)]
    + [dict(prompt=CHOSEN_PROMPT, res=None, q=30, thinking=False)]
    + [dict(prompt=p, res=None, thinking=t) for p in ("plain", "explain") for t in (True, False)]
)
TOP = dict(prompt=CHOSEN_PROMPT, res=None, thinking=True)


async def main(phase: str, cap: float) -> None:
    dest = ROOT / "demo_runs" / "levers-pets.json"
    out = json.loads(dest.read_text()) if dest.exists() else {}
    out.update({"task": "pet breed from photo (37 breeds)", "model": MODEL, "prices_usd_per_m": {k: v * 1e6 for k, v in PRICE.items()},
                "dataset": {"name": "Oxford-IIIT Pet, test split", "license": "CC BY-SA 4.0",
                            "source": "https://www.robots.ox.ac.uk/~vgg/data/pets/ (mirror: huggingface.co/datasets/timm/oxford-iiit-pet)",
                            "sample": "seeded stratified: 3/breed held-out (111), 1/breed validation (37)"},
                "checker": "exact match after normalising (see scripts/levers_pets.py grade/ALIASES)",
                "prompts": PROMPTS, "measured_on": str(date.today())})
    room = TOTAL_CAP_USD - total_spent()
    client = Client(cap_usd=min(cap, room), concurrency=8)
    rows = META[phase]
    setups = VALIDATION if phase == "validation" else HOLDOUT
    results = {}
    t0 = time.time()
    for s in setups:
        name = setup_name(s)
        calls, scores = await run(client, rows, s)
        results[name] = summarise(name, s, rows, calls, scores)
        r = results[name]
        print(f"{name:42s} acc {r['accuracy']:.1%}  in {r['mean_prompt_tokens']:.0f} (cached {r['mean_cached_tokens']:.0f})  "
              f"out {r['mean_output_tokens']:.0f}  ${r['usd_per_1k_calls']:.3f}/1k  err {r['errors']} trunc {r['truncated']}  "
              f"(run ${client.spent:.3f})", flush=True)
        if client.stopped:
            print("budget cap hit; stopping")
            break
    top = setup_name(TOP if phase == "holdout" else dict(prompt=CHOSEN_PROMPT, res=None, thinking=True))
    if top in results and not client.stopped:
        compare_all(results, top)
        print(table(results, top))
        best = cheapest_within(results)
        out[phase] = {"top": top, "cheapest_within_5": best, "n": len(rows), "results": results,
                      "spent_usd": round(client.spent, 4), "wall_s": round(time.time() - t0, 1)}
        if best:
            print(f"cheapest within 5 points: {best} -> {results[best]['saving_multiple_vs_top']}x cheaper than top")
    else:
        out[phase + "_partial"] = {"results": results, "spent_usd": round(client.spent, 4)}
    add_spent(client.spent)
    out["total_levers_spend_usd"] = round(total_spent(), 4)
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"spent this run ${client.spent:.4f}; levers total ${total_spent():.4f}; saved {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["validation", "holdout"], required=True)
    ap.add_argument("--max-usd", type=float, default=1.5)
    a = ap.parse_args()
    asyncio.run(main(a.phase, a.max_usd))
