"""Record the Good Enough site, each action on a narration word."""
import json, re, shutil, subprocess, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright
HERE = Path(__file__).resolve().parent
URL = "http://localhost:8011/"   # local: has the squeeze; Render gets it on the next push
VO, W, OFF = HERE / "vo", HERE / "work", 0.45
SCALE = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].replace(".", "").isdigit() else 4 / 3
OUTW, OUTH = int(1440 * SCALE), int(810 * SCALE)
CAP = """(() => { if (document.getElementById('vcap')) return; const c = document.createElement('div'); c.id = 'vcap';
 c.style.cssText = 'position:fixed;left:50%;bottom:34px;transform:translateX(-50%);width:max-content;max-width:1100px;padding:12px 24px;border-radius:14px;background:rgba(8,10,14,.9);border:1px solid #2a3346;color:#e8ecf4;font:600 26px Inter,system-ui;text-align:center;z-index:9999;opacity:0;transition:opacity .35s';
 document.body.appendChild(c); window.cap = t => { c.innerHTML = t; c.style.opacity = t ? 1 : 0; }; })()"""
GLOW = """(() => { const st = document.createElement('style'); document.head.appendChild(st);
 const vg = document.createElement('style'); document.head.appendChild(vg);
 st.textContent = '.vglow{outline:3px solid #76b900 !important;outline-offset:3px;box-shadow:0 0 0 8px rgba(118,185,0,.18) !important}';
 const rule = sel => sel + '{outline:3px solid #76b900 !important;outline-offset:4px;border-radius:6px;background:rgba(118,185,0,.12) !important}';
 window.glow = sel => { vg.textContent += rule(sel); };   // stylesheet rules survive the app re-rendering its HTML
 window.unglow = () => { vg.textContent = ''; };
 window.go = (sel, block = 'start') => document.querySelector(sel).scrollIntoView({behavior: 'smooth', block});
 window.big = () => { st.textContent += '.wrap{max-width:860px !important}'; document.body.style.zoom = 1.6; };
 document.body.style.zoom = 1.22; })()"""
RUN = "150a5ad36713"   # receipts, with the squeeze and photo check (demo_runs/)
OWN = "091a657f0889"   # the custom SMS spam task: 100 rows, no answers
HOLD = {}              # extra seconds on screen after the narration
SPEED = 10             # shown on screen by the app's replay badge
SMS = W / "sms_examples.jsonl"   # 100 UCI SMS messages, inputs only
OWN_FILL = "(() => { const f = document.querySelector('#custom-form'); const set = (n, v) => { const el = f.elements[n]; el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); };" + \
    " set('title', 'SMS spam filter'); set('instruction', 'Is this text message spam or a normal message (ham)?'); set('output_format', 'One label, lowercase'); set('label_set', 'spam, ham');" + \
    " set('examples', " + json.dumps(SMS.read_text(encoding='utf-8').strip()) + "); })()"
SQ = "#v-squeeze ol li:nth-child"
SCENES = {
    "07-demo": (GLOW + ";big(); document.querySelector('[data-slug=\"receipt-extract\"]').click(); scrollTo(0, 0)", [
        ("pick", "glow('[data-slug=\"receipt-extract\"]')"),
        ("climbs", f"unglow(); history.pushState(null, '', '/?run={RUN}&speed={SPEED}'); dispatchEvent(new PopStateEvent('popstate')); setTimeout(() => go('#replay-note'), 900)"),
        ("ultra", "cap('<b>1</b> · Ultra rewrites the prompt from <b>tuning examples</b> only')"),
        ("best", "cap('<b>2</b> · the best rewrite is picked on <b>validation examples</b>')"),
        ("only", "cap('<b>3</b> · only that prompt is scored, <b>once</b>, on <b>locked-away receipts</b>')"),
        ("lightning", "cap(''); glow('#costs .cost:nth-child(1)')"),
        ("nano", "unglow(); glow('#costs .cost:nth-child(2)')"),
    ]),
    "09-squeeze": (GLOW + f";big(); (() => {{ history.replaceState(null, '', '/?run={RUN}'); dispatchEvent(new PopStateEvent('popstate')); setTimeout(() => go('#v-squeeze'), 500); }})()", [
        ("squeezes", "go('#v-squeeze'); glow('#v-squeeze h3')"),
        ("cuts", f"unglow(); glow('{SQ}(2)'); cap('Nano as the verdict picked it: <b>4</b> worked examples, <b>1,261</b> tokens in')"),
        ("2", f"unglow(); glow('{SQ}(4)'); cap('<b>2</b> worked examples: 93.5% on validation, <b>920</b> tokens in. <b>Kept</b>')"),
        ("no", f"unglow(); glow('{SQ}(5)'); glow('{SQ}(3)'); cap('no examples: 88.7% · one-line answers: 90.7%. <b>Dropped</b>')"),
        ("result", "unglow(); cap(''); glow('#v-squeeze .sq-call')"),
        ("rechecked", "unglow(); glow('#v-squeeze > p:nth-of-type(2)'); cap('<b>94.3%</b> on the 86 locked-away receipts · still matches Ultra')"),
    ]),
    "14-own": (GLOW + ";big(); document.querySelector('#tab-custom').click(); scrollTo(0, 0)", [
        ("own", "glow('#tab-custom')"),
        ("pasted", "unglow(); " + OWN_FILL + "; glow('#custom-form textarea[name=examples]'); go('#custom-form textarea[name=examples]', 'center')"),
        ("answers", "cap('<b>100</b> real text messages · <b>no</b> answers given')"),
        ("ultra", f"unglow(); cap(''); history.pushState(null, '', '/?run={OWN}'); dispatchEvent(new PopStateEvent('popstate')); setTimeout(() => {{ document.querySelector('#log-panel details').open = true; go('#log-panel'); }}, 600)"),
        ("checker", "cap('Ultra wrote the <b>answers</b> and a <b>checker</b>')"),
        ("passed", "cap('the checker passed its own test before any model was graded')"),
        ("nano", "cap(''); go('#verdict-panel'); glow('#v-headline')"),
        ("17", "unglow(); go('#v-squeeze', 'center'); glow('#v-squeeze .sq-call')"),
        ("whole", "unglow(); cap('the whole run, every model and the answer key: <b>US$0.032</b>')"),
    ]),
    "15b-cert": (GLOW + f";big(); document.body.classList.add('cert-only'); history.pushState(null, '', '/?run={RUN}&cert=1'); dispatchEvent(new PopStateEvent('popstate')); scrollTo(0, 0)", [
        ("certificate", "glow('#v-cert .c-call')"),
        ("setup", "unglow(); glow('#v-cert .c-col:nth-child(1)')"),
        ("evidence", "unglow(); glow('#v-cert .c-col:nth-child(2)')"),
        ("saving", "unglow(); glow('#v-cert .c-col:nth-child(3)')"),
        ("link", "unglow(); glow('#cert-link')"),
    ]),
}
norm = lambda w: re.sub(r"[^a-z0-9%$]", "", w.lower())
def cue(words, c):
    w, n = (c.split("#") + ["1"])[:2]
    hits = [x["t"] for x in words if norm(x["w"]) == w] or [x["t"] for x in words if norm(x["w"]).startswith(w)]
    if len(hits) < int(n): raise SystemExit(f"cue {c!r} missing")
    return hits[int(n) - 1]
dur = lambda p: float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout)
W.mkdir(exist_ok=True)
names = [a for a in sys.argv[1:] if a in SCENES] or list(SCENES)
with sync_playwright() as p:
    b = p.chromium.launch()
    for name in names:
        setup, steps = SCENES[name]
        words = json.loads((VO / f"{name}.words.json").read_text(encoding="utf-8"))
        d = OFF + dur(VO / f"{name}.mp3") + 0.7 + HOLD.get(name, 0)
        pg = b.new_page(viewport={"width": 1440, "height": 810}, device_scale_factor=SCALE, color_scheme="dark")
        pg.goto(URL); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(2500); pg.evaluate("document.fonts.ready")
        pg.evaluate(CAP); pg.evaluate("document.documentElement.style.scrollBehavior='auto'"); pg.evaluate(setup); pg.wait_for_timeout(1200)
        pg.evaluate("document.documentElement.style.scrollBehavior='smooth'")
        tl = "[" + ",".join(f"[{cue(words, c) + OFF:.2f}, () => {{ {js} }}]" for c, js in steps) + "]"
        pg.evaluate(f"(tl => tl.forEach(([t, f]) => setTimeout(f, t * 1000)))({tl})")
        fdir = W / f"rec_{name}"; shutil.rmtree(fdir, ignore_errors=True); fdir.mkdir()
        t0, frames = time.perf_counter(), []
        while (t := time.perf_counter() - t0) < d:
            fp = fdir / f"{len(frames):05d}.jpg"; fp.write_bytes(pg.screenshot(type="jpeg", quality=93)); frames.append((t, fp))
        with (fdir / "list.txt").open("w") as fh:
            for i, (t, fp) in enumerate(frames):
                fh.write(f"file '{fp.as_posix()}'\nduration {(frames[i + 1][0] if i + 1 < len(frames) else d) - t:.4f}\n")
            fh.write(f"file '{frames[-1][1].as_posix()}'\n")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(fdir / "list.txt"),
                        "-vf", f"fps=30,scale={OUTW}:{OUTH}:flags=lanczos,format=yuv420p", "-t", f"{d:.3f}", "-c:v", "libx264", "-crf", "16", str(W / f"rec_{name}.mp4")], check=True)
        print(f"  {name:10} {d:5.1f}s {len(frames)} frames ({len(frames) / d:.1f} fps)"); pg.close()
    b.close()
