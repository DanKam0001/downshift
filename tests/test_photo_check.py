"""The photo check: after the verdict, a vision model reads the task's photos instead of the OCR text."""
import base64

from conftest import FakeLLM, collect
from downshift.engine import photo_system, run_downshift
from downshift.models import PHOTO_RUNG, CallResult, call_cost


class PhotoLLM(FakeLLM):
    """FakeLLM, plus a vision model that reads the row id written into each fake photo."""

    def __init__(self, task, photo_right=1.0, **kw):
        super().__init__(task, **kw)
        self.by_id = {str(r["id"]): r["reference"] for r in task["holdout"]}
        self.photo_right = photo_right
        self.photo_systems = []

    async def chat(self, model, messages, max_tokens=512, temperature=0.0, purpose="", thinking=False):
        if model != PHOTO_RUNG["model"]:
            return await super().chat(model, messages, max_tokens, temperature, purpose, thinking)
        self.meter.check()
        self.calls.append((model, purpose, thinking))
        self.photo_systems.append(messages[0]["content"])
        rid = base64.b64decode(messages[-1]["content"][0]["image_url"]["url"].split(",", 1)[1]).decode()
        right = (sum(map(ord, rid)) % 100) / 100 < self.photo_right
        text = self.by_id[rid] if right else "nothing"
        cost = call_cost(model, 1100, 20)
        self.meter.add(model, cost)
        return CallResult(model, text, 1100, 20, cost, 800.0)


def _with_photos(task, tmp_path):
    d = tmp_path / "photos"
    d.mkdir()
    for r in task["holdout"]:
        (d / f"{r['id']}.jpg").write_bytes(str(r["id"]).encode())
    return dict(task, photos={"dir": str(d), "text_says": "Route the ticket", "photo_says": "Route the photographed ticket",
                              "lead": "The image is a photo of a ticket. "})


def _by_type(events, t):
    return [e for e in events if e["type"] == t]


def test_photo_check_runs_after_the_verdict(task, grader, tmp_path):
    t = _with_photos(task, tmp_path)
    llm = PhotoLLM(t)
    events = collect(run_downshift(t, llm=llm, grader=grader))
    types = [e["type"] for e in events]
    assert types.index("verdict") < types.index("photo") < types.index("done")
    assert PHOTO_RUNG["key"] in [x["key"] for x in _by_type(events, "started")[0]["tiers"]]
    p = _by_type(events, "photo")[0]
    assert p["passed"] and p["holdout_score"] == 1.0
    ho = [e for e in _by_type(events, "eval") if e["tier_key"] == PHOTO_RUNG["key"]]
    assert len(ho) == 1 and ho[0]["split"] == "holdout" and ho[0]["n"] == len(t["holdout"])
    # The winning prompt was tuned, so the photo check reuses it, with the photo lead in front.
    v = _by_type(events, "verdict")[0]["verdict"]
    assert all(s == "The image is a photo of a ticket. " + v["system_prompt"] for s in llm.photo_systems)


def test_a_bad_photo_reader_is_not_a_match(task, grader, tmp_path):
    t = _with_photos(task, tmp_path)
    events = collect(run_downshift(t, llm=PhotoLLM(t, photo_right=0.3), grader=grader))
    p = _by_type(events, "photo")[0]
    assert not p["passed"] and not p["beats"]


def test_no_photos_no_photo_check(task, grader, tmp_path):
    events = collect(run_downshift(task, llm=FakeLLM(task), grader=grader))
    assert not _by_type(events, "photo")
    t = _with_photos(task, tmp_path)
    events = collect(run_downshift(t, options={"photo_check": False}, llm=PhotoLLM(t), grader=grader))
    assert not _by_type(events, "photo")


def test_missing_photos_are_skipped_not_failed(task, grader, tmp_path):
    t = _with_photos(task, tmp_path)
    next((tmp_path / "photos").iterdir()).unlink()
    events = collect(run_downshift(t, llm=PhotoLLM(t), grader=grader))
    assert not _by_type(events, "photo") and events[-1]["ok"]
    assert any(e["type"] == "skipped" and e["tier_key"] == PHOTO_RUNG["key"] for e in events)


def test_plain_prompt_swaps_its_ocr_wording(task):
    t = dict(task, photos={"text_says": "Classify", "photo_says": "From the photo, classify", "lead": "LEAD "})
    verdict = {"kind": "finetune", "thinking_off_only": False, "fewshots": [], "system_prompt": "x"}
    system, shots, how = photo_system(t, verdict)
    assert how == "the plain prompt" and shots == []
    if "Classify" in t["instruction"]:
        assert "From the photo, classify" in system and not system.startswith("LEAD")
    else:
        assert system.startswith("LEAD ")
