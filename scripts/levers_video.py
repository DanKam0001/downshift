"""How far can input-side levers cut the cost of action-from-video on Nemotron 3.5 Super VL?

    python scripts/build_levers_data.py --kth-dir <dir with the six KTH zips + 00sequences.txt>   # once
    python scripts/levers_video.py --phase validation   # 12 clips (persons 11, 12): picks the prompt
    python scripts/levers_video.py --phase holdout      # 54 clips (ICPR'04 test persons), every setup vs the top
    python scripts/levers_video.py --phase confirm      # 162 fresh clips, top + two 4-frame setups (tighter CI)

The model takes images, not video, so each clip is 16 frames sampled evenly over one KTH subsequence (ffmpeg,
see build_levers_data.py) and k of them are sent as k image_url parts in time order. Levers: frames per clip
(8/4/2/1 separate, or 16 only when tiled, evenly spaced subsets of the 16), frame size (long side 768/512/256 px, or native 160 px; KTH is
160x120, so every size above 160 is an UPSCALE), thinking on/off, and tiling: all k frames pasted into ONE image
grid (native size), which pays the per-image token floor once instead of k times.

Checker: the answer (last line / after 'Answer:') normalised must equal one of the 6 labels (a few spellings such
as 'hand waving' accepted). The top setup is 8 separate frames at 768 px, thinking on (the API refuses more
than 8 images per prompt).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import date
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from levers_common import (DATA, MODEL, PRICE, ROOT, TOTAL_CAP_USD, Client, add_spent, cheapest_within,  # noqa: E402
                           compare_all, extract_answer, image_part, jpeg_bytes, norm, summarise, table, total_spent)

KTH = DATA / "kth"
META = json.loads((KTH / "rows.json").read_text())
LABELS = META["labels"]

BASE = ("You will see {what} from one short video clip of a single person, in time order, evenly spaced over about "
        "2-4 seconds. Classify the person's action as exactly one of: walking, jogging, running, boxing, handwaving, "
        "handclapping. Answer with that one word only.")
PROMPTS = {
    "plain": BASE,
    "defs": BASE + (" Definitions: walking = moving across the scene at a slow pace; jogging = moving across at a "
                    "medium pace with a light bounce; running = moving across fast with long strides and the body leaning "
                    "forward; boxing = standing and punching forward with fists; handwaving = standing and waving both "
                    "arms up over the head; handclapping = standing and clapping the hands in front of the chest. "
                    "Compare the frames: how far the person moves between frames tells the pace."),
}
# Chosen on the validation clips (see demo_runs/levers-video.json -> validation).
CHOSEN_PROMPT = "defs"
SYNONYMS = {"hand waving": "handwaving", "waving": "handwaving", "hand clapping": "handclapping", "clapping": "handclapping",
            "walk": "walking", "jog": "jogging", "run": "running", "box": "boxing", "punching": "boxing"}


def grade(text: str, label: str) -> float:
    a = norm(extract_answer(text)).rstrip(" .")
    a = SYNONYMS.get(a, a).replace(" ", "")
    return 1.0 if a == label else 0.0


def pick(k: int, n: int = 16) -> list[int]:
    return [min(n - 1, int((i + 0.5) * n / k)) for i in range(k)]


def frames(clip: str, k: int) -> list[Image.Image]:
    return [Image.open(KTH / clip / f"f{i + 1:02d}.png").convert("RGB") for i in pick(k)]


def tile(imgs: list[Image.Image]) -> Image.Image:
    cols = math.ceil(math.sqrt(len(imgs)))
    rows = math.ceil(len(imgs) / cols)
    w, h = imgs[0].size
    g = Image.new("RGB", (cols * w, rows * h), "white")
    for i, im in enumerate(imgs):
        g.paste(im, ((i % cols) * w, (i // cols) * h))
    return g


def setup_name(s: dict) -> str:
    size = "native160" if s["res"] is None else f"{s['res']}px"
    lay = f"{s['k']}f tiled-in-{int(s['tile'])}" if s.get("tile") else f"{s['k']}f"
    return f"{lay} | {size} | think {'on' if s['thinking'] else 'off'}" + ("" if s["prompt"] == CHOSEN_PROMPT else f" | {s['prompt']}")


def messages(s: dict, clip: str) -> list[dict]:
    imgs = frames(clip, s["k"])
    if s.get("tile"):
        g = int(s["tile"])  # number of images; frames split evenly across them, each image a grid
        per = s["k"] // g
        cols = math.ceil(math.sqrt(per))
        if g == 1:
            what = f"one image that is a grid of {s['k']} frames ({cols} per row, read left to right, top to bottom)"
        else:
            what = (f"{g} images, each a grid of {per} frames ({cols} per row, read left to right, top to bottom; "
                    f"the images are in time order too), {s['k']} frames in all")
        parts = [image_part(jpeg_bytes(tile(imgs[i * per:(i + 1) * per]), s["res"], 92)) for i in range(g)]
    else:
        what = f"{s['k']} frames" if s["k"] > 1 else "one frame"
        parts = [image_part(jpeg_bytes(im, s["res"], 92)) for im in imgs]
    return [{"role": "system", "content": PROMPTS[s["prompt"]].format(what=what)}, {"role": "user", "content": parts}]


async def run(client: Client, rows: list[dict], s: dict):
    max_tokens = (6144 if s["thinking"] else 0) + 20
    calls = await asyncio.gather(*(client.chat(messages(s, r["id"]), s["thinking"], max_tokens) for r in rows))
    return calls, [grade(c["text"], r["label"]) if c["ok"] else 0.0 for r, c in zip(rows, calls)]


VALIDATION = [dict(prompt=p, k=8, res=512, thinking=t) for p in ("plain", "defs") for t in (False, True)]
# Token Factory rejects more than 8 images per prompt (HTTP 400 "At most 8 image(s) may be provided in one prompt"),
# so 8 separate frames is the most-expensive sendable setup; 16 frames only fit tiled. The six 16-separate-frame
# setups in REJECTED were attempted once and all 54 calls returned that 400 (no tokens billed).
TOP = dict(prompt=CHOSEN_PROMPT, k=8, res=768, thinking=True)
REJECTED = (
    [dict(prompt=CHOSEN_PROMPT, k=16, res=r, thinking=True) for r in (768, 512)]
    + [dict(prompt=CHOSEN_PROMPT, k=16, res=r, thinking=False) for r in (768, 512, 256, None)]
)
HOLDOUT = (
    [TOP, dict(prompt=CHOSEN_PROMPT, k=8, res=256, thinking=True)]
    + [dict(prompt=CHOSEN_PROMPT, k=16, res=1280, tile=1, thinking=t) for t in (True, False)]
    + [dict(prompt=CHOSEN_PROMPT, k=16, res=None, tile=2, thinking=t) for t in (True, False)]
    + [dict(prompt=CHOSEN_PROMPT, k=16, res=None, tile=4, thinking=False)]
    + [dict(prompt=CHOSEN_PROMPT, k=8, res=None, tile=1, thinking=False)]
    + [dict(prompt=CHOSEN_PROMPT, k=8, res=None, thinking=False)]
) + [s for s in (
    [dict(prompt=CHOSEN_PROMPT, k=k, res=512, thinking=True) for k in (16, 8, 4, 1)]
    + [dict(prompt=CHOSEN_PROMPT, k=16, res=None, tile=True, thinking=True)]
    + [dict(prompt=CHOSEN_PROMPT, k=16, res=r, thinking=False) for r in (768, 512, 256, None)]
    + [dict(prompt=CHOSEN_PROMPT, k=k, res=512, thinking=False) for k in (8, 4, 2, 1)]
    + [dict(prompt=CHOSEN_PROMPT, k=8, res=r, thinking=False) for r in (768, 256)]
    + [dict(prompt=CHOSEN_PROMPT, k=1, res=768, thinking=False)]
    + [dict(prompt=CHOSEN_PROMPT, k=k, res=None, tile=True, thinking=False) for k in (16, 4)]
) if s.get("k") <= 8 or s.get("tile")]


# Confirmation on 162 fresh clips (the test persons' other three scenarios), declared after the holdout table showed
# the 54-clip CIs were too wide (+-10 points) to call any setup a match: the top plus the two 4-frame setups.
CONFIRM = [TOP, dict(prompt=CHOSEN_PROMPT, k=4, res=512, thinking=True), dict(prompt=CHOSEN_PROMPT, k=4, res=512, thinking=False)]


async def main(phase: str, cap: float) -> None:
    dest = ROOT / "demo_runs" / "levers-video.json"
    out = json.loads(dest.read_text()) if dest.exists() else {}
    out.update({"task": "human action from video frames (6 actions)", "model": MODEL,
                "prices_usd_per_m": {k: v * 1e6 for k, v in PRICE.items()},
                "dataset": {"name": "KTH human actions (Schuldt, Laptev & Caputo, ICPR 2004)",
                            "license": "KTH: 'publicly available for non-commercial use'; cite the ICPR'04 paper",
                            "source": "https://www.csc.kth.se/cvap/actions/ (original zips, not YouTube)",
                            "sample": "held-out: ICPR'04 test persons 02 03 05 06 07 08 09 10 22 x 6 actions = 54 clips, "
                                      "scenario rotated d1-d4; validation: persons 11, 12 x 6 = 12 clips; first "
                                      "subsequence of each file, 16 frames evenly spaced, native 160x120 greyscale"},
                "checker": "exact match to one of 6 labels after normalising (see scripts/levers_video.py grade)",
                "prompts": PROMPTS, "measured_on": str(date.today())})
    client = Client(cap_usd=min(cap, TOTAL_CAP_USD - total_spent()), concurrency=8)
    rows = META[phase]
    setups = {"validation": VALIDATION, "holdout": HOLDOUT, "confirm": CONFIRM}[phase]
    results = {}
    prev = (out.get("holdout") or {}).get("results", {}) if phase == "holdout" else {}
    out["rejected_by_api"] = {setup_name(s): "HTTP 400: At most 8 image(s) may be provided in one prompt"
                              for s in REJECTED} if phase == "holdout" else out.get("rejected_by_api")
    t0 = time.time()
    for s in setups:
        name = setup_name(s)
        if name in prev and prev[name]["errors"] == 0 and [r["id"] for r in prev[name]["rows"]] == [r["id"] for r in rows]:
            results[name] = {k: v for k, v in prev[name].items() if k not in ("diff_vs_top", "within_5_points", "beats_top", "saving_multiple_vs_top")}
            results[name]["reused_from_earlier_run"] = True
            print(f"{name:40s} reused: acc {results[name]['accuracy']:.1%}", flush=True)
            continue
        calls, scores = await run(client, rows, s)
        results[name] = summarise(name, s, rows, calls, scores)
        r = results[name]
        print(f"{name:40s} acc {r['accuracy']:.1%}  in {r['mean_prompt_tokens']:.0f} (cached {r['mean_cached_tokens']:.0f})  "
              f"out {r['mean_output_tokens']:.0f}  ${r['usd_per_1k_calls']:.3f}/1k  err {r['errors']} trunc {r['truncated']}  "
              f"(run ${client.spent:.3f})", flush=True)
        if client.stopped:
            print("budget cap hit; stopping")
            break
    top = setup_name(TOP) if phase in ("holdout", "confirm") else setup_name(dict(VALIDATION[-1]))
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
    ap.add_argument("--phase", choices=["validation", "holdout", "confirm"], required=True)
    ap.add_argument("--max-usd", type=float, default=1.0)
    a = ap.parse_args()
    asyncio.run(main(a.phase, a.max_usd))
