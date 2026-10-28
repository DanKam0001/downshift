"""Stills of each slide at its final state, for layout checks: python preview.py [names]"""
import json, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
HERE = Path(__file__).resolve().parent
norm = lambda w: re.sub(r"[^a-z0-9%$]", "", w.lower())
RESOLVE = (HERE / "render.py").read_text(encoding="utf-8").split('RESOLVE = """')[1].split('"""')[0]
names = sys.argv[1:] or [p.stem for p in sorted((HERE / "scenes").glob("*.html"))]
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page(viewport={"width": 1920, "height": 1080})
    for n in names:
        words = [{"n": norm(x["w"]), "t": x["t"]} for x in json.loads((HERE / "vo" / f"{n}.words.json").read_text(encoding="utf-8"))]
        page.goto((HERE / "scenes" / f"{n}.html").as_uri()); page.wait_for_load_state("networkidle"); page.evaluate("document.fonts.ready")
        miss = page.evaluate(RESOLVE, words)
        if miss: print(n, "missing cues:", miss)
        page.evaluate("render(999)"); page.screenshot(path=str(HERE / "work" / f"still_{n}.png"))
    b.close()
