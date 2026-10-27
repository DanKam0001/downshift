"""Tasks (presets or custom) and how a prompt is turned into chat messages."""
from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = ROOT / "presets"
FIXTURES_DIR = ROOT / "tests" / "fixtures"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _task_dirs() -> list[Path]:
    dirs = sorted(p.parent for p in PRESETS_DIR.glob("*/task.json")) if PRESETS_DIR.exists() else []
    if not dirs:  # development fallback until real presets land
        dirs = sorted(p.parent for p in FIXTURES_DIR.glob("*/task.json"))
    return dirs


def load_task_dir(d: Path) -> dict:
    task = json.loads((d / "task.json").read_text(encoding="utf-8"))
    task.setdefault("slug", d.name)
    task["train"] = _read_jsonl(d / "train.jsonl")
    task["validation"] = _read_jsonl(d / "validation.jsonl")
    task["holdout"] = _read_jsonl(d / "holdout.jsonl")
    ref = d / "reference.json"
    task["cached_reference"] = json.loads(ref.read_text(encoding="utf-8")) if ref.exists() else None
    checker = d / "checker.py"
    task["checker_code"] = checker.read_text(encoding="utf-8") if checker.exists() else None
    task["source"] = "fixture" if FIXTURES_DIR in d.parents else "preset"
    return task


def list_presets() -> list[dict]:
    out = []
    for d in _task_dirs():
        try:
            t = load_task_dir(d)
        except Exception:  # noqa: BLE001
            continue
        out.append({k: t.get(k) for k in ("slug", "title", "business_story", "instruction", "output_format",
                                          "label_set", "source_url", "license", "source")}
                   | {"n_train": len(t["train"]), "n_validation": len(t["validation"]), "n_holdout": len(t["holdout"]),
                      "has_checker": bool(t["checker_code"]),
                      "reference_measured": (t["cached_reference"] or {}).get("measured_on")})
    return out


def load_preset(slug: str) -> dict:
    for d in _task_dirs():
        if d.name == slug:
            return load_task_dir(d)
    raise KeyError(slug)


def custom_task(spec: dict, seed: int = 7) -> dict:
    """Build a task from {title?, instruction, output_format?, label_set?, examples:[{input, reference?}]}."""
    instruction = (spec.get("instruction") or "").strip()
    examples = spec.get("examples") or []
    if not instruction:
        raise ValueError("instruction is required")
    if len(examples) < 8:
        raise ValueError("give at least 8 examples")
    if len(examples) > 200:
        raise ValueError("at most 200 examples")
    rows = []
    for i, ex in enumerate(examples):
        if isinstance(ex, str):
            ex = {"input": ex}
        if "input" not in ex:
            raise ValueError(f"example {i} has no 'input'")
        rows.append({"id": str(ex.get("id", f"c{i}")), "input": ex["input"], "reference": ex.get("reference")})
    random.Random(seed).shuffle(rows)
    # 40% tuning, 20% validation (prompt selection only), 40% held out.
    n_train = max(4, (len(rows) * 2) // 5)
    n_val = max(2, len(rows) // 5)
    return {
        "slug": "custom", "title": spec.get("title") or "Custom task",
        "business_story": spec.get("business_story") or "", "instruction": instruction,
        "output_format": spec.get("output_format") or "", "label_set": spec.get("label_set"),
        "json_schema": spec.get("json_schema"), "train": rows[:n_train],
        "validation": rows[n_train:n_train + n_val], "holdout": rows[n_train + n_val:],
        "checker_code": None, "cached_reference": None, "source": "custom",
    }


def as_text(x) -> str:
    if isinstance(x, str):
        return x
    return json.dumps(x, ensure_ascii=False)


def _schema_fields(schema) -> list[str]:
    if isinstance(schema, dict):
        return list((schema.get("properties") or {}).keys())
    return []


def ensure_labels(task: dict, system: str) -> str:
    """Append the allowed labels if a (rewritten) system prompt dropped any of them."""
    labels = [str(x) for x in (task.get("label_set") or [])]
    if labels and not all(lbl in system for lbl in labels):
        system = system.rstrip() + "\n\nAllowed labels:\n" + "\n".join(labels)
    return system


def plain_system(task: dict) -> str:
    instruction = task["instruction"].strip()
    parts = [instruction]
    fmt = (task.get("output_format") or "").strip()
    if fmt and len(fmt.split()) > 1:  # a sentence, not just a type tag like "label" / "json"
        parts.append("Output format: " + fmt)
    elif fmt == "json":
        parts.append("Reply with one JSON object only, no code fences.")
    schema = task.get("json_schema")
    if schema and not all(f in instruction for f in _schema_fields(schema)):
        parts.append("JSON schema: " + as_text(schema))
    if fmt != "json":
        parts.append("Reply with the answer only.")
    return ensure_labels(task, "\n\n".join(parts))


def build_messages(system: str, fewshots: list[dict], item_input) -> list[dict]:
    msgs = [{"role": "system", "content": system}]
    for ex in fewshots:
        msgs.append({"role": "user", "content": as_text(ex["input"])})
        msgs.append({"role": "assistant", "content": as_text(ex["reference"])})
    msgs.append({"role": "user", "content": as_text(item_input)})
    return msgs
