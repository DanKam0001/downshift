"""A paired CI on too few rows must never be read as a match."""
import pytest

from goodenough import stats
from goodenough.stats import beats, matches, paired_diff_ci


@pytest.fixture(autouse=True)
def _real_minimum(monkeypatch):
    monkeypatch.setattr(stats, "MIN_ROWS", 20)  # conftest lowers it for the toy fixture


def test_tiny_perfect_holdout_is_not_a_match():
    d = paired_diff_ci([1.0] * 8, [1.0] * 8)
    assert d["lo"] == 0 and d["hi"] == 0
    assert not matches(d, 0.05) and not beats(d)


def test_enough_rows_still_match():
    d = paired_diff_ci([1.0] * 20, [1.0] * 20)
    assert matches(d, 0.05)
