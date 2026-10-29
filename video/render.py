"""Render the demo video: scenes -> frames -> mp4, narration on, crossfades between."""
import json
import re
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
S, VO, W = HERE / "scenes", HERE / "vo", HERE / "work"
W.mkdir(exist_ok=True)
FPS, OFF, XF = 30, 0.45, 0.35
HOLD = {}  # keep in step with rec.py
OUT = HERE / "demo.mp4"


def run(args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                                capture_output=True, text=True).stdout)


def norm(w):
    return re.sub(r"[^a-z0-9%$]", "", w.lower())


# Resolve data-cue="word" / "word#2" to data-at, from the narration's word timings.
RESOLVE = """(words) => {
  const missing = [];
  document.querySelectorAll('[data-cue]').forEach(el => {
    const [w, n] = el.dataset.cue.split('#');
    const hits = words.filter(x => x.n === w || x.n.startsWith(w));
    const hit = hits[(Number(n) || 1) - 1];
    if (hit) el.dataset.at = hit.t; else { el.dataset.at = 0; missing.push(el.dataset.cue); }
  });
  return missing;
}"""

names = [l[0] for l in json.loads((HERE / "vo.json").read_text(encoding="utf-8"))]
segs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1920, "height": 1080})
    for i, name in enumerate(names):
        mp3 = VO / f"{name}.mp3"
        words = [{"n": norm(x["w"]), "t": x["t"]} for x in json.loads((VO / f"{name}.words.json").read_text(encoding="utf-8"))]
        d = OFF + dur(mp3) + (1.4 if i == len(names) - 1 else 0.7) + HOLD.get(name, 0)
        rec = W / f"rec_{name}.mp4"
        if rec.exists():   # a real recording of the reference mockup (rec.py) replaces the slide
            silent = rec
        else:
          page.goto((S / f"{name}.html").as_uri()); page.wait_for_load_state("networkidle"); page.evaluate("document.fonts.ready")
          missing = page.evaluate(RESOLVE, words)
          if missing:
              print(f"  {name}: cue words not found in narration: {missing}")
          silent = W / f"scene_{name}.mp4"
          ff = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-i", "-",
                               "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", str(silent)], stdin=subprocess.PIPE)
          for f in range(round(d * FPS)):
              page.evaluate(f"render({f / FPS})")
              ff.stdin.write(page.screenshot(type="jpeg", quality=94))
          ff.stdin.close(); ff.wait()
        seg = W / f"seg_{name}.mov"
        ms = int(OFF * 1000)
        run(["-i", str(silent), "-i", str(mp3), "-filter_complex",
             f"[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},apad,atrim=0:{d}[a]",
             "-map", "0:v", "-map", "[a]", "-t", str(d), "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "pcm_s16le", str(seg)])
        segs.append((seg, d)); print(f"  {name:12} {d:5.2f}s")
    b.close()



def build(segs, names, out):
    inputs, fc, off = [], [], 0.0
    for s, _ in segs:
        inputs += ["-i", str(s)]
    vprev, aprev = "[0:v]", "[0:a]"
    for i in range(1, len(segs)):
        off += segs[i - 1][1] - XF
        rec_edge = any((W / f"rec_{names[j]}.mp4").exists() for j in (i - 1, i))
        fc.append(f"{vprev}[{i}:v]xfade=transition={'fadeblack' if rec_edge else 'fade'}:duration={XF}:offset={off:.3f}[v{i}]")
        fc.append(f"{aprev}[{i}:a]acrossfade=d={XF}[a{i}]")
        vprev, aprev = f"[v{i}]", f"[a{i}]"
    total = off + segs[-1][1]
    fc.append(f"{vprev}fade=t=out:st={total - 0.6:.3f}:d=0.6[v]")
    fc.append(f"{aprev}afade=t=out:st={total - 0.6:.3f}:d=0.6,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
    run([*inputs, "-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "slow",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)])
    print(f"-> {out} ({total:.1f}s)")


build(segs, names, OUT)
