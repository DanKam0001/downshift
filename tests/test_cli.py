"""Offline tests for the CLI's log parsing and output (no network)."""
import json
from pathlib import Path

import pytest

from downshift.cli import load_examples, monthly, parse_record, verdict_text

ROOT = Path(__file__).resolve().parent.parent


def test_plain_example_keeps_reference():
    assert parse_record({"input": "where is my parcel", "reference": "shipping"}) == {
        "input": "where is my parcel", "reference": "shipping"}


def test_plain_example_without_reference():
    assert parse_record({"input": "hello"}) == {"input": "hello"}


def test_chat_log_uses_last_user_message_and_response_field():
    rec = {"id": 7, "messages": [{"role": "system", "content": "Route it."},
                                 {"role": "user", "content": "first"},
                                 {"role": "assistant", "content": "x"},
                                 {"role": "user", "content": "refund please"}],
           "response": "refund"}
    assert parse_record(rec) == {"input": "refund please", "reference": "refund", "id": "7"}


def test_chat_log_trailing_assistant_message_is_the_reference():
    rec = {"messages": [{"role": "user", "content": "charged twice"},
                        {"role": "assistant", "content": "billing"}]}
    assert parse_record(rec) == {"input": "charged twice", "reference": "billing"}


def test_chat_log_content_parts_and_choice_shaped_output():
    rec = {"messages": [{"role": "user", "content": [{"type": "text", "text": "reset "}, {"type": "text", "text": "pw"}]}],
           "output": {"message": {"role": "assistant", "content": "account"}}}
    assert parse_record(rec) == {"input": "reset pw", "reference": "account"}


def test_chat_log_without_answer_gets_no_reference():
    assert parse_record({"messages": [{"role": "user", "content": "hi"}]}) == {"input": "hi"}


@pytest.mark.parametrize("rec", [{"text": "no input"}, {"messages": [{"role": "system", "content": "s"}]}, ["x"]])
def test_bad_records_raise(rec):
    with pytest.raises(ValueError):
        parse_record(rec)


def test_load_examples_reports_line_number(tmp_path):
    p = tmp_path / "logs.jsonl"
    p.write_text('{"input": "a"}\n\n{"oops": 1}\n', encoding="utf-8")
    with pytest.raises(SystemExit, match=r"logs.jsonl:3"):
        load_examples(p)


def test_shipped_example_file_parses():
    rows = load_examples(ROOT / "examples" / "support_logs.jsonl")
    assert len(rows) == 40
    assert all(r.get("reference") in {"billing", "shipping", "refund", "account"} for r in rows)


def test_monthly_and_verdict_text_from_a_recorded_run():
    assert monthly(0.1031, 1e6) == pytest.approx(103.1)
    v = json.loads((ROOT / "demo_runs" / "45015b6df59e.json").read_text(encoding="utf-8"))["verdict"]
    text = verdict_text(v, 1e6)
    assert "Nano" in text and "35.8x" in text and "saving $3,589.60 a month" in text
    keep = json.loads((ROOT / "demo_runs" / "7ee9b72d56be.json").read_text(encoding="utf-8"))["verdict"]
    assert verdict_text(keep, 1e6).startswith("Verdict: keep Ultra")
