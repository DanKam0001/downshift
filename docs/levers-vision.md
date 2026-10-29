# Input-side levers on photos and video (Nemotron 3.5 Super VL), measured 2026-10-29

One model (`nvidia/Nemotron-3.5-Super-VL`, the only NVIDIA vision model on Token Factory), temperature 0. Costs come
from `usage` at $0.30/M prompt tokens, $0.06/M cached and $0.90/M completion (images are billed as prompt tokens; no
call reported cached tokens). Each setup runs on the same held-out rows and is compared with the most expensive
setup using the paired bootstrap in `downshift/stats.py`. "Within 5" means the 95% CI lower bound of (setup minus top)
is above -5 points. Prompts were chosen on separate validation rows before the held-out runs.
Total Nebius spend: **$2.64** of the $3.00 cap.

Raw per-row outputs: `demo_runs/levers-pets.json`, `demo_runs/levers-video.json`. Scripts:
`scripts/build_levers_data.py`, `scripts/levers_pets.py`, `scripts/levers_video.py`, `scripts/levers_common.py`.
The data lives in `data_media/`, which is git-ignored and docker-ignored.

## What a picture costs (probe, one 366 px pet photo)

| long side | 128-384 | 448-512 | 640 | 768 | 1024 | 1536 | 2048 | 4032 |
|---|---|---|---|---|---|---|---|---|
| prompt tokens | ~303 | ~318 | 353 | 489 | 833 | 1809 | 3233 | 3333 |

Each image has a floor of about 290-300 tokens, and the count stops growing at about 2048 px (the server rescales).
Shrinking below about 450 px saves nothing. Shrinking a phone photo from 4032 px to 512 px cuts input tokens by about 10x.

## Task A: pet breed from a photo (37 breeds)

**Data:** Oxford-IIIT Pet test split, CC BY-SA 4.0 (Stanford Dogs is ImageNet-derived and non-commercial, so I didn't use it).
The sample is seeded and stratified: 111 held-out images (3 per breed) and 37 validation images (1 per breed).
The originals are about 500 px, so 768, 1024 and 2048 are **upscales**.

**Checker:** exact match after normalising, with a few official long names mapped (for example "German Shorthaired Pointer").
"Pitbull" also counts for american_pit_bull_terrier.

**Validation (37 images):** the plain prompt scored 76-78%. The prompt listing the 37 labels scored 89% and was chosen.

| setup | acc | prompt tok | output tok | $/1k calls | diff vs top (95% CI) | within 5 | x cheaper |
|---|---|---|---|---|---|---|---|
| explain, orig, thinking on | 82.9% | 490 | 1019 | $1.064 | -0.9 (-5.4 to +2.7) | no | 0.89x |
| labels, 1024 (up), thinking on | 83.8% | 962 | 848 | $1.052 | +0.0 (-5.4 to +5.4) | no | 0.9x |
| labels, 768 (up), thinking on | 83.8% | 630 | 878 | $0.979 | +0.0 (-3.6 to +3.6) | yes | 0.96x |
| labels, 384, thinking on | 82.0% | 475 | 934 | $0.983 | -1.8 (-5.4 to +1.8) | no | 0.96x |
| labels, 256, thinking on | 80.2% | 474 | 919 | $0.969 | -3.6 (-8.1 to +0.9) | no | 0.97x |
| labels, 512, thinking on | 84.7% | 475 | 892 | $0.945 | +0.9 (-2.7 to +4.5) | yes | 1.0x |
| labels, 2048 (up), thinking off | 84.7% | 3135 | 4 | $0.944 | +0.9 (-3.6 to +5.4) | yes | 1.0x |
| **labels, orig, thinking on (TOP)** | **83.8%** | 468 | 891 | $0.943 | - | - | 1.0x |
| plain, orig, thinking on | 74.8% | 302 | 494 | $0.535 | -9.0 (-17.1 to -1.8) | no | 1.76x |
| labels, 1024 (up), thinking off | 84.7% | 962 | 5 | $0.293 | +0.9 (-3.6 to +5.4) | yes | 3.2x |
| explain, orig, thinking off | 82.0% | 490 | 65 | $0.205 | -1.8 (-7.2 to +2.7) | no | 4.6x |
| labels, 768 (up), thinking off | 85.6% | 630 | 4 | $0.193 | +1.8 (-2.7 to +6.3) | yes | 4.9x |
| labels, 512, thinking off | 82.9% | 475 | 5 | $0.147 | -0.9 (-4.5 to +2.7) | yes | 6.4x |
| labels, 384, thinking off | 83.8% | 475 | 5 | $0.147 | +0.0 (-4.5 to +4.5) | yes | 6.4x |
| labels, 256, thinking off | 86.5% | 474 | 4 | $0.146 | +2.7 (-0.9 to +7.2) | yes | 6.5x |
| labels, orig, thinking off | 83.8% | 468 | 5 | $0.145 | +0.0 (-4.5 to +4.5) | yes | 6.5x |
| **labels, orig, JPEG q30, thinking off** | 85.6% | 468 | 5 | $0.145 | +1.8 (-1.8 to +5.4) | yes | **6.5x** |
| plain, orig, thinking off | 73.0% | 302 | 4 | $0.095 | -10.8 (-18.0 to -3.6) | no | 9.98x |

**Cheapest setup within 5 points:** the original photo (or any size from 256 to 512, or JPEG q30) with the label-list
prompt, thinking off, answer only. It costs $0.145 per 1k calls against $0.943, which is **6.5x cheaper**.
- Thinking adds nothing on this task. Turned on, it writes about 890 tokens per answer, and on/off disagree on only 6 of 111 rows.
- Listing the allowed labels is worth about 10 points. It is the one lever that adds cost (about 170 prompt tokens), and it pays for itself.
- An explanation before the answer costs 13x the output tokens and scores slightly lower.
- Resolution moves nothing here, because 500 px photos already sit at the token floor. 256 px scored highest (86.5%), within noise.
  Upscaling is pure waste: at 2048 px the same accuracy costs 6.5x more. That is the phone-photo case.
- 9 thinking-on calls hit the 4,096-token cap and gave no answer, mostly on one "wheaten terrier" photo that the model reads as a Schnauzer.

## Task B: human action from video (6 actions)

**Data:** KTH human actions (Schuldt, Laptev & Caputo, ICPR 2004), downloaded from the original site
(csc.kth.se/cvap/actions), not YouTube. License: "publicly available for non-commercial use", citing the paper.
The clips are 160x120 greyscale at 25 fps.

**Splits:**
- held-out: the ICPR'04 test people (9) x 6 actions = 54 clips, with the four scenarios rotated.
- validation: 2 other people, 12 clips.
- confirmation: 162 fresh clips (the same test people in their other 3 scenarios).

**Frames:** 16 frames sampled evenly with ffmpeg over the first subsequence of each clip. Every size above 160 px is an upscale.

**Prompt:** validation tied on accuracy. I kept the prompt with action definitions because it cut thinking output from 1,368 to 506 tokens.

**API limit found:** Token Factory rejects more than 8 images per prompt (HTTP 400 "At most 8 image(s) may be provided
in one prompt"). Sending 16 separate frames is impossible, so 16 frames were sent only as tiled grids. The top setup
is 8 frames at 768 px, thinking on.

Held-out, 54 clips:

| setup | acc | prompt tok | output tok | $/1k | diff vs top (95% CI) | x cheaper |
|---|---|---|---|---|---|---|
| **8 frames, 768, thinking on (TOP)** | **77.8%** | 3690 | 668 | $1.708 | - | 1.0x |
| 8f, 256, thinking on | 75.9% | 2362 | 769 | $1.401 | -1.8 (-9.3 to +5.6) | 1.22x |
| 8f, 512, thinking on | 74.1% | 2362 | 683 | $1.323 | -3.7 (-13.0 to +5.6) | 1.29x |
| 1f, 512, thinking on | 59.3% | 445 | 1251 | $1.259 | -18.5 (-33.3 to -3.7) | 1.36x |
| 16f in one 4x4 grid at 1280, thinking on | 72.2% | 1402 | 851 | $1.187 | -5.6 (-18.6 to +9.3) | 1.44x |
| 8f, 768, thinking off | 64.8% | 3690 | 3 | $1.110 | -13.0 (-24.1 to -1.8) | 1.54x |
| 4f, 512, thinking on | 74.1% | 1270 | 804 | $1.105 | -3.7 (-16.7 to +9.3) | 1.55x |
| 16f in one grid, native, thinking on | 61.1% | 502 | 1006 | $1.056 | -16.7 (-27.8 to -5.6) | 1.62x |
| 16f in two grids, native, thinking on | 68.5% | 798 | 798 | $0.958 | -9.3 (-22.2 to +3.7) | 1.78x |
| 8f, native 160 / 512 / 256, thinking off | 63.0% | ~2370 | 3 | $0.71-0.72 | -14.8 (-27.8 to -1.8) | 2.4x |
| 16f in one grid at 1280, thinking off | 59.3% | 1402 | 3 | $0.424 | -18.5 (-31.5 to -5.6) | 4.0x |
| 16f in four grids, native, thinking off | 57.4% | 1322 | 4 | $0.400 | -20.4 (-35.2 to -5.6) | 4.3x |
| 4f, 512, thinking off | 63.0% | 1270 | 3 | $0.384 | -14.8 (-29.6 to +0.0) | 4.5x |
| 16f in two grids, native, thinking off | 55.6% | 798 | 3 | $0.242 | -22.2 (-35.2 to -9.3) | 7.0x |
| 2f, 512, thinking off | 48.1% | 724 | 4 | $0.221 | -29.6 (-42.6 to -16.7) | 7.7x |
| 1f, 768, thinking off | 50.0% | 611 | 4 | $0.186 | -27.8 (-42.6 to -13.0) | 9.2x |
| 16f / 8f / 4f in one grid, native, thinking off | 52-54% | 471-502 | 4 | $0.144-0.154 | about -25 (CI lower bound about -40) | 11-12x |
| 1f, 512, thinking off | 48.1% | 445 | 4 | $0.137 | -29.6 (-44.4 to -14.8) | 12.5x |

No setup passed the 5-point rule on 54 clips: the CIs are about ±10 points wide. So I declared a confirmation run on
the 162 fresh clips, with the top setup and the two 4-frame setups:

| setup (162 fresh clips) | acc | $/1k | diff vs top (95% CI) | x cheaper |
|---|---|---|---|---|
| 8f, 768, thinking on (TOP) | 80.2% | $1.695 | - | 1.0x |
| 4f, 512, thinking on | 72.8% | $1.090 | **-7.4 (-14.2 to -1.2)** | 1.56x |
| 4f, 512, thinking off | 64.2% | $0.384 | **-16.1 (-24.1 to -7.4)** | 4.4x |

**Verdict: video does not downshift.** The cheapest setup within 5 points is the top itself. Dropping to 4 frames
costs a confirmed 7 points. Turning thinking off costs 13-16 points. Most errors are jogging vs walking or running,
and handclapping vs handwaving, which you can't tell apart without comparing frames. That is why thinking (comparing
frames) and frame count matter here, unlike for photos. The one free lever is resolution: on these 160 px clips,
256 and 512 px cost the same floor tokens and score within noise of 768. Tiling 16 frames into one image cuts input
tokens 7x, but loses 17-25 points at native size. Upscaled to 1280 it recovers to -5.6 (CI -18.6 to +9.3):
promising, but unproven at n=54.

## Surprises
- **On photos, thinking is pure cost:** 6.5x more expensive for the same accuracy. **On video it is the biggest quality lever** (+13 to +16 points). The same lever goes in opposite directions on two vision tasks, which is the case for measuring per task.
- Lower resolution did not hurt pets: 256 px scored the best of all (86.5%), within noise. Images below about 450 px all cost the same about 300 tokens.
- The per-image token floor makes "more, smaller frames" costly on video: 8 frames cost at least about 2,360 prompt tokens whatever their size.
- The 8-images-per-prompt API limit was undocumented in our code and rejected every 16-frame request. Those rejected calls billed nothing.
- No call reported cached tokens, even with a repeated system prompt of about 170 tokens.
