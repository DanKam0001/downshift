"""Build the small image and video sets for the lever measurements into data_media/ (git-ignored).

    python scripts/build_levers_data.py --pet-parquet D:/downshift_data/pet_test.parquet --kth-dir D:/downshift_data/kth

Pets: Oxford-IIIT Pet (Parkhi et al. 2012), CC BY-SA 4.0, test split as mirrored at
huggingface.co/datasets/timm/oxford-iiit-pet (data/test-00000-of-00001.parquet). Stratified, seeded sample:
3 images per breed for held-out (111), 1 per breed for validation (37, used only to pick the prompt). Original JPEG
bytes are kept unchanged.

Video: KTH human actions (Schuldt, Laptev & Caputo, ICPR 2004), www.csc.kth.se/cvap/actions/, "publicly
available for non-commercial use". 6 actions, 25 people, 4 scenarios, 160x120 greyscale, 25 fps. Held-out = the
ICPR'04 test persons (02 03 05 06 07 08 09 10 22) x 6 actions = 54 clips, scenario rotated d1..d4 so all four
appear; validation = persons 11 and 12 (ICPR training persons) x 6 = 12 clips; confirm = the same test
persons in the other three scenarios (162 clips), for a confirmation run on fresh clips. Each clip is the FIRST
subsequence listed in 00sequences.txt; 16 frames are taken evenly across it with ffmpeg and saved losslessly (PNG,
native 160x120).
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data_media"
KTH_TEST = [2, 3, 5, 6, 7, 8, 9, 10, 22]
KTH_VAL = [11, 12]
ACTIONS = ["walking", "jogging", "running", "boxing", "handwaving", "handclapping"]
N_FRAMES = 16


def build_pets(parquet: Path) -> None:
    import pyarrow.parquet as pq
    f = pq.ParquetFile(parquet)
    names = json.loads(f.schema_arrow.metadata[b"huggingface"])["info"]["features"]["label"]["names"]
    by_label: dict[int, list[tuple[str, int]]] = {}
    meta = f.read(columns=["label", "image_id"]).to_pylist()
    for i, r in enumerate(meta):
        by_label.setdefault(r["label"], []).append((r["image_id"], i))
    rng = random.Random(20261029)
    pick = {}
    for lab, items in sorted(by_label.items()):
        items = sorted(items)
        rng.shuffle(items)
        for k, (iid, idx) in enumerate(items[:4]):
            pick[idx] = (iid, names[lab], "validation" if k == 0 else "holdout")
    d = OUT / "pets"
    d.mkdir(parents=True, exist_ok=True)
    rows = {"holdout": [], "validation": []}
    offset = 0
    for g in range(f.num_row_groups):
        t = f.read_row_group(g, columns=["image"]).to_pylist()
        for j, r in enumerate(t):
            if offset + j in pick:
                iid, label, split = pick[offset + j]
                (d / f"{iid}.jpg").write_bytes(r["image"]["bytes"])
                rows[split].append({"id": iid, "label": label})
        offset += len(t)
    for s in rows:
        rows[s].sort(key=lambda r: r["id"])
    (d / "rows.json").write_text(json.dumps({"dataset": "Oxford-IIIT Pet (test split)", "license": "CC BY-SA 4.0",
                                             "labels": names, **rows}, indent=1))
    print(f"pets: {len(rows['holdout'])} holdout, {len(rows['validation'])} validation")


def kth_sequences(kth_dir: Path) -> dict[str, tuple[int, int]]:
    seqs = {}
    for line in (kth_dir / "00sequences.txt").read_text().splitlines():
        m = re.match(r"\s*(person\d+_\w+_d\d)\s+frames\s+(\d+)-(\d+)", line)
        if m:
            seqs[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return seqs


def build_kth(kth_dir: Path) -> None:
    seqs = kth_sequences(kth_dir)
    d = OUT / "kth"
    d.mkdir(parents=True, exist_ok=True)
    rows = {"holdout": [], "validation": [], "confirm": []}
    jobs = []
    for split, persons in (("holdout", KTH_TEST), ("validation", KTH_VAL)):
        for pi, p in enumerate(persons):
            for ai, act in enumerate(ACTIONS):
                jobs.append((split, p, act, (pi + ai) % 4 + 1))
    # confirm: the same test persons in the other three scenarios (162 clips none of the other splits use)
    for pi, p in enumerate(KTH_TEST):
        for ai, act in enumerate(ACTIONS):
            jobs += [("confirm", p, act, sc) for sc in range(1, 5) if sc != (pi + ai) % 4 + 1]
    if True:
        for split, p, act, scen in jobs:
            if True:
                name = f"person{p:02d}_{act}_d{scen}"
                if name not in seqs:
                    print("missing sequence", name)
                    continue
                start, end = seqs[name]
                with zipfile.ZipFile(kth_dir / f"{act}.zip") as z, tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / f"{name}.avi"
                    src.write_bytes(z.read(f"{name}_uncomp.avi"))
                    # frame numbers in 00sequences.txt are 1-based
                    idx = [round(start - 1 + (i + 0.5) * (end - start + 1) / N_FRAMES - 0.5) for i in range(N_FRAMES)]
                    sel = "+".join(f"eq(n\\,{i})" for i in idx)
                    cd = d / name
                    cd.mkdir(exist_ok=True)
                    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vf", f"select='{sel}'",
                                    "-fps_mode", "passthrough", str(cd / "f%02d.png")], check=True)
                    got = len(list(cd.glob("f*.png")))
                    if got != N_FRAMES:
                        raise SystemExit(f"{name}: got {got} frames")
                rows[split].append({"id": name, "label": act, "person": p, "scenario": f"d{scen}",
                                    "frames": [start, end], "frame_idx": idx})
    (d / "rows.json").write_text(json.dumps({"dataset": "KTH human actions", "license": "non-commercial use (KTH page)",
                                             "labels": ACTIONS, **rows}, indent=1))
    print(f"kth: {len(rows['holdout'])} holdout, {len(rows['validation'])} validation, {len(rows['confirm'])} confirm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pet-parquet", type=Path)
    ap.add_argument("--kth-dir", type=Path)
    a = ap.parse_args()
    if a.pet_parquet:
        build_pets(a.pet_parquet)
    if a.kth_dir:
        build_kth(a.kth_dir)
