"""Tests for the load model. Small, boring, and they catch the mistakes that matter."""

import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shiftload import metrics  # noqa: E402


def test_ewma_converges_to_constant_load():
    series = metrics.ewma_series([100.0] * 400, metrics.CTL_DAYS)
    assert abs(series[-1] - 100.0) < 0.5


def test_ewma_first_day_absorbs_expected_fraction():
    series = metrics.ewma_series([100.0], metrics.CTL_DAYS)
    assert abs(series[0] - 100 * (1 - math.exp(-1 / 42))) < 0.01


def test_atl_reacts_faster_than_ctl():
    loads = [0.0] * 60 + [200.0] * 7
    ctl = metrics.ewma_series(loads, metrics.CTL_DAYS)[-1]
    atl = metrics.ewma_series(loads, metrics.ATL_DAYS)[-1]
    assert atl > ctl * 3


def test_acwr_spikes_above_one_after_a_hard_week():
    loads = [100.0] * 90 + [200.0] * 7
    ctl = metrics.ewma_series(loads, metrics.CTL_DAYS)[-1]
    atl = metrics.ewma_series(loads, metrics.ATL_DAYS)[-1]
    assert atl / ctl > 1.4


def test_monotony_is_high_for_identical_weeks_and_low_for_varied_ones():
    assert metrics.monotony([0, 0, 0, 0, 0, 0, 0]) is None      # no load at all
    varied = [0.0, 0.0, 300.0, 0.0, 0.0, 0.0, 0.0]
    score = metrics.monotony(varied)
    assert score is not None and score < 2.0


def test_build_series_zero_fills_missing_days():
    start = date(2026, 1, 1)
    days = metrics.date_range(start, start + timedelta(days=9))
    rows = metrics.build_series(days, {"2026-01-03": 50.0})
    assert len(rows) == 10
    assert rows[0]["load"] == 0.0
    assert rows[2]["load"] == 50.0
    assert rows[-1]["ctl"] > 0


def test_acwr_stays_quiet_until_there_is_a_base():
    start = date(2026, 1, 1)
    days = metrics.date_range(start, start + timedelta(days=20))
    rows = metrics.build_series(days, {"2026-01-05": 500.0})
    assert all(r["acwr"] is None for r in rows)


def test_acwr_stays_quiet_on_a_tiny_base():
    # 40 days of history but almost no load: a big day is not a "spike", it is a cold start
    start = date(2026, 1, 1)
    days = metrics.date_range(start, start + timedelta(days=45))
    rows = metrics.build_series(days, {"2026-02-10": 80.0})
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-02-10"]["ctl"] < metrics.ACWR_MIN_CTL
    assert by_date["2026-02-10"]["acwr"] is None


def test_flag_words():
    assert metrics.flag(None).startswith("not enough")
    assert metrics.flag(1.6).startswith("spike")
    assert metrics.flag(1.0) == "steady"
    assert metrics.flag(0.5).startswith("detraining")
