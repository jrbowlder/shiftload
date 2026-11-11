"""Recovery, grouped by where a day sits in the 48/96 cycle.

The question this answers is not "what was my HRV yesterday", it is "does my HRV sit
lower on shift day two than on off day three". That comparison needs days bucketed by
cycle position, not a single line chart read by eye.

Every summary reports its sample size, because most of these buckets are small and a
mean over nine nights is not evidence of anything.
"""

from datetime import date

RECOVERY_FIELDS = {
    "sleep_score": "sleep score",
    "sleep_hours": "sleep hours",
    "hrv_nightly": "HRV overnight",
    "resting_hr": "resting HR",
    "bb_min": "body battery low",
    "stress_avg": "stress average",
}

CYCLE_LABELS = {
    0: "shift day 1",
    1: "shift day 2",
    2: "off day 1",
    3: "off day 2",
    4: "off day 3",
    5: "off day 4",
}


def _mean(values):
    return round(sum(values) / len(values), 1) if values else None


def _sd(values):
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return round((sum((v - m) ** 2 for v in values) / len(values)) ** 0.5, 1)


def cycle_summary(conn, since: str | None = None) -> list[dict]:
    """One row per cycle position: mean and sample size for each recovery metric."""
    sql = """SELECT w.date, w.sleep_score, w.sleep_s, w.hrv_nightly, w.resting_hr,
                    w.bb_min, w.stress_avg, s.cycle_day
             FROM daily_wellness w JOIN shifts s ON s.date = w.date
             WHERE s.cycle_day IS NOT NULL"""
    params = []
    if since:
        sql += " AND w.date >= ?"
        params.append(since)
    rows = conn.execute(sql, params).fetchall()

    buckets: dict[int, dict[str, list]] = {i: {} for i in range(6)}
    for r in rows:
        day = r["cycle_day"]
        for field in RECOVERY_FIELDS:
            if field == "sleep_hours":
                value = round(r["sleep_s"] / 3600, 2) if r["sleep_s"] else None
            else:
                value = r[field]
            if value is not None:
                buckets[day].setdefault(field, []).append(float(value))

    out = []
    for day in range(6):
        entry = {"cycle_day": day, "label": CYCLE_LABELS[day], "days": 0}
        counts = [len(v) for v in buckets[day].values()]
        entry["days"] = max(counts) if counts else 0
        for field, label in RECOVERY_FIELDS.items():
            values = buckets[day].get(field, [])
            entry[field] = _mean(values)
            entry[f"{field}_n"] = len(values)
            entry[f"{field}_sd"] = _sd(values)
        out.append(entry)
    return out


def series(conn, days: int = 0) -> dict:
    """Daily recovery numbers with the shift flag, for charting."""
    sql = """SELECT w.date, w.resting_hr, w.hrv_nightly, w.sleep_score, w.bb_min, w.stress_avg,
                    s.is_shift
             FROM daily_wellness w LEFT JOIN shifts s ON s.date = w.date
             ORDER BY w.date"""
    rows = conn.execute(sql).fetchall()
    if days and days < len(rows):
        rows = rows[-days:]
    return {
        "dates": [r["date"] for r in rows],
        "resting_hr": [r["resting_hr"] for r in rows],
        "hrv": [r["hrv_nightly"] for r in rows],
        "sleep_score": [r["sleep_score"] for r in rows],
        "bb_min": [r["bb_min"] for r in rows],
        "is_shift": [r["is_shift"] for r in rows],
    }


def biggest_gap(summary: list[dict], field: str) -> dict | None:
    """Which cycle position is worst on this metric, with the difference to the best."""
    best = None
    for row in summary:
        v = row.get(field)
        if v is None or row.get(f"{field}_n", 0) < 10:
            continue
        if best is None:
            best = {"field": field, "low": row, "high": row}
        else:
            if v < best["low"][field]:
                best["low"] = row
            if v > best["high"][field]:
                best["high"] = row
    if best is None:
        return None
    best["diff"] = round(best["high"][field] - best["low"][field], 1)
    return best
