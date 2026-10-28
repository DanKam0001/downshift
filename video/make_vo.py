"""Narration in the user's ElevenLabs voice clone, one mp3 per line, plus word timings."""
import json, sys, time, subprocess, urllib.request
from pathlib import Path
from dotenv import dotenv_values
HERE = Path(__file__).resolve().parent
# keys come from the hackathon_scout bot's own .env (filled from Bots/master_env by sync_env.py)
ENV = dotenv_values(Path.home() / "Desktop/Bots/hackathon_scout/.env")
VOICE = ENV.get("ELEVENLABS_VOICE_ID")  # the user's voice clone
OUT = HERE / "vo"; OUT.mkdir(exist_ok=True)
redo = set(sys.argv[1:])

def aai(url, data=None, ctype="application/json"):
    req = urllib.request.Request(url, data, {"authorization": ENV["ASSEMBLYAI_API_KEY"], "content-type": ctype})
    return json.load(urllib.request.urlopen(req, timeout=120))

for slug, text in json.loads((HERE / "vo.json").read_text(encoding="utf-8")):
    mp3 = OUT / f"{slug}.mp3"
    if not mp3.exists() or slug in redo:
        body = json.dumps({"text": text, "model_id": "eleven_multilingual_v2",
                           "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "style": 0.15, "use_speaker_boost": True, "speed": 1.1}}).encode()
        req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=mp3_44100_128", body,
                                     {"xi-api-key": ENV["ELEVENLABS_API_KEY"], "content-type": "application/json"})
        mp3.write_bytes(urllib.request.urlopen(req, timeout=60).read())
        (OUT / f"{slug}.words.json").unlink(missing_ok=True)
    wj = OUT / f"{slug}.words.json"
    if not wj.exists():
        up = aai("https://api.assemblyai.com/v2/upload", mp3.read_bytes(), "application/octet-stream")
        tid = aai("https://api.assemblyai.com/v2/transcript", json.dumps({"audio_url": up["upload_url"]}).encode())["id"]
        while (r := aai(f"https://api.assemblyai.com/v2/transcript/{tid}"))["status"] not in ("completed", "error"):
            time.sleep(2)
        wj.write_text(json.dumps([{"w": w["text"], "t": w["start"] / 1000} for w in r["words"]]), encoding="utf-8")
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                               capture_output=True, text=True).stdout)
    print(f"{slug:12} {dur:5.1f}s")
