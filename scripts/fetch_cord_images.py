"""Download the CORD v2 receipt photos behind receipt-extract's rows into presets/_cache/cord-v2/images/<id>.jpg.

    python scripts/fetch_cord_images.py

The preset itself is text (CORD's annotated OCR); the photos are only needed for the vision comparison in
scripts/vision_rung.py. Images are CC-BY-4.0 (NAVER Clova, CORD v2), resized so the long side is at most 1280 px.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "presets" / "_cache" / "cord-v2" / "images"
ROWS = "https://datasets-server.huggingface.co/rows?dataset=naver-clova-ix/cord-v2&config=default&split={split}&offset=0&length=100"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    want = set()
    for split in ("train", "validation", "holdout"):
        for line in (ROOT / "presets" / "receipt-extract" / f"{split}.jsonl").read_text(encoding="utf-8").splitlines():
            want.add(json.loads(line)["id"])
    got = 0
    with httpx.Client(timeout=60, follow_redirects=True) as http:
        for split, prefix in (("test", "cordtest"), ("validation", "cordval")):
            for r in http.get(ROWS.format(split=split)).json()["rows"]:
                rid = f"{prefix}-{r['row_idx']:04d}"
                dest = OUT / f"{rid}.jpg"
                if rid not in want or dest.exists():
                    got += dest.exists() and rid in want
                    continue
                img = Image.open(io.BytesIO(http.get(r["row"]["image"]["src"]).content)).convert("RGB")
                img.thumbnail((1280, 1280))
                img.save(dest, quality=88)
                got += 1
    print(f"{got} of {len(want)} receipt images in {OUT}")
    if got < len(want):
        sys.exit(1)


if __name__ == "__main__":
    main()
