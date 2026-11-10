"""Daily load, fitness and fatigue.

Three things get computed here:

  CTL  42 day exponentially weighted average of daily load. "Fitness". Slow, it lags.
  ATL  7 day exponentially weighted average. "Fatigue". Fast, it spikes and it fades.
  ACWR ATL divided by CTL. The ratio is the interesting part: 1.0 means you are exactly
       as loaded as your recent base can absorb, above about 1.5 means the recent week
       outran the base.

Monotony and strain (Foster) describe the shape of a week rather than its size. Monotony
is mean daily load divided by its standard deviation: seven identical days score high.
Strain is weekly load multiplied by monotony, so a big monotonous week scores worst.

All of it is plain arithmetic on a daily series, which is why it is testable without a
database or an API.
"""

import math
from collections.abc import Sequence
from datetime import date, timedelta

CTL_DAYS = 42
ACWR_MIN_HISTORY = 28   # chronic load needs a base; before this the ratio is noise
ACWR_MIN_CTL = 5.0   # a ratio on a base this small is noise, not a warning
ATL_DAYS = 7
ACWR_HIGH = 1.5
ACWR_CAUTION = 1.3


def ewma_series(daily_load: list[float], days: int) -> list[float]:
    """Exponentially weighted average with the given time constant, one value per day.

    Each day absorbs a fraction of the gap to today's load, set by the time constant.
    A 42 day constant means today's load is worth 1/42 of the way to the new value,
    which is what makes CTL slow and ATL twitchy.
    """
    if not daily_load:
        return []
    alpha = 1.0 - math.exp(-1.0 / days)
    out = []
    running = 0.0
    for load in daily_load:
        running = running + (load - running) * alpha
        out.append(running)
    return out


def rolling_mean(values: list[float], window: int) -> list[float]:
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1): i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def monotony(week: Sequence[float]) -> float | None:
    """Foster monotony for one week of daily loads. None when every day is identical."""
    if len(week) < 2:
        return None
    mean = sum(week) / len(week)
    if mean == 0:
        return None
    var = sum((v - mean) ** 2 for v in week) / len(week)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return round(mean / sd, 2)


def build_series(days: list[date], daily_load: dict[str, float]) -> list[dict]:
    """One row per calendar day, gaps filled with zero load, all metrics attached."""
    loads = [float(daily_load.get(d.isoformat(), 0.0)) for d in days]
    ctl = ewma_series(loads, CTL_DAYS)
    atl = ewma_series(loads, ATL_DAYS)
    rows = []
    for i, d in enumerate(days):
        week = loads[max(0, i - 6): i + 1]
        mono = monotony(week) if len(week) == 7 else None
        strain = round(sum(week) * mono, 1) if mono else None
        rows.append({
            "date": d.isoformat(),
            "load": round(loads[i], 1),
            "ctl": round(ctl[i], 1),
            "atl": round(atl[i], 1),
            # a ratio against four days of history is meaningless, so stay quiet
            # until there is a real base underneath it
            "acwr": (round(atl[i] / ctl[i], 2)
                     if i >= ACWR_MIN_HISTORY and ctl[i] >= ACWR_MIN_CTL else None),
            "monotony": mono,
            "strain": strain,
        })
    return rows


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def flag(acwr: float | None) -> str:
    """Plain words for the ratio, so the page never shows a bare number."""
    if acwr is None:
        return "not enough recent load"
    if acwr >= ACWR_HIGH:
        return "spike, recent week well above the base"
    if acwr >= ACWR_CAUTION:
        return "high, worth watching"
    if acwr >= 0.8:
        return "steady"
    return "detraining or a quiet stretch"
