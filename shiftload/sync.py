"""One sync, callable from the command line and from the in-process scheduler."""

import json
from datetime import date, datetime, timedelta

from . import config, shifts, store
from .garmin import Garmin, iter_windows


def _save_raw(name: str, payload) -> None:
    config.ensure_dirs()
    path = config.RAW_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (bytes, bytearray)):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload, indent=1))


def _day_of(payload: dict) -> str | None:
    """Garmin sometimes omits calendarDate; str(None) would poison the table with 'None'."""
    raw = payload.get("calendarDate")
    if not raw:
        return None
    return str(raw)[:10]


def collect_wellness(g: Garmin, start: date, end: date) -> dict[str, dict]:
    by_date: dict[str, dict] = {}

    for a, b in iter_windows(start, end, 300):
        rows = g.daily_summaries(a, b)
        _save_raw(f"garmin/daily_{a.isoformat()}_{b.isoformat()}.json", rows)
        for d in rows:
            day = _day_of(d)
            if not day:
                continue
            hr = d.get("heartRate") or {}
            stress = d.get("stress") or {}
            bb = d.get("bodyBattery") or {}
            cal = d.get("calories") or {}
            steps = ((d.get("movement") or {}).get("steps") or {}).get("value")
            by_date.setdefault(day, {}).update(
                steps=steps,
                resting_hr=hr.get("restingValue"),
                hr_min=hr.get("minValue"),
                hr_max=hr.get("maxValue"),
                stress_avg=stress.get("avgLevel"),
                stress_max=stress.get("maxLevel"),
                bb_min=bb.get("minValue"),
                bb_max=bb.get("maxValue"),
                bb_at_wake=d.get("bodyBatteryAtWakeTime"),
                bb_latest=bb.get("latestValue"),
                calories_total=cal.get("burnedTotal"),
                respiration=(d.get("respiration") or {}).get("avgValue"),
            )

    for a, b in iter_windows(start, end, 31):
        rows = g.sleep_summaries(a, b)
        _save_raw(f"garmin/sleep_{a.isoformat()}_{b.isoformat()}.json", rows)
        for s in rows:
            day = _day_of(s)
            if not day:
                continue
            scores = s.get("sleepScores") or {}
            by_date.setdefault(day, {}).update(
                sleep_score=(scores.get("overall") or {}).get("value"),
                sleep_s=s.get("sleepTimeSeconds"),
                sleep_deep_s=s.get("deepSleepSeconds"),
                sleep_rem_s=s.get("remSleepSeconds"),
                sleep_light_s=s.get("lightSleepSeconds"),
                sleep_awake_s=s.get("awakeSleepSeconds"),
                sleep_avg_hr=s.get("avgHeartRate"),
            )

    hrv = g.hrv_range(start, end)
    _save_raw(f"garmin/hrv_{start.isoformat()}_{end.isoformat()}.json", hrv)
    for h in hrv:
        day = _day_of(h)
        if not day:
            continue
        by_date.setdefault(day, {}).update(
            hrv_nightly=h.get("lastNightAvg"),
            hrv_weekly=h.get("weeklyAvg"),
            hrv_status=h.get("status"),
        )
    return {d: v for d, v in by_date.items() if v}


def run_sync(since: date | None = None, ahead: int = config.SHIFT_HORIZON_DAYS,
             verbose: bool = True) -> dict:
    """Pull activities, wellness and shifts, then rebuild the daily load series."""
    since = since or date.fromisoformat(config.BACKFILL_START)
    today = date.today()
    data_until = today                      # Garmin rejects ranges that run into the future
    shifts_until = today + timedelta(days=ahead)

    conn = store.connect()
    run = store.start_run(conn, "sync")
    detail: list[str] = []
    notes: list[str] = []

    try:
        g = Garmin()

        seen, offset, pages = [], 0, 0
        while True:
            page = g.activity_page(offset, 99)
            pages += 1
            _save_raw(f"garmin/activities/page_{offset:04d}.json", page)
            if not page:
                break
            seen.extend(page)
            oldest = min(a["startTimeLocal"][:10] for a in page)
            offset += 99
            if oldest < since.isoformat() or len(page) < 99:
                break
        acts = [a for a in seen if a["startTimeLocal"][:10] >= since.isoformat()]
        store.upsert_activities(conn, acts)
        oldest_kept = min((a["startTimeLocal"][:10] for a in acts), default="none")
        detail.append(f"activities: {len(acts)} rows, {pages} page(s), oldest {oldest_kept}")

        wellness = collect_wellness(g, since, data_until)
        store.upsert_wellness(conn, wellness)
        detail.append(f"wellness: {len(wellness)} days")

        try:
            rows, notes = shifts.build(since, shifts_until, use_calendar=True)
            _save_raw(f"calendar/fire_{datetime.now():%Y%m%dT%H%M%S}.ics",
                      shifts.fetch_fire_calendar())
        except Exception as exc:  # noqa: BLE001 - calendar failure must not block data
            rows, notes = shifts.build(since, shifts_until, use_calendar=False)
            notes.append(f"calendar fetch failed: {exc}")
        store.replace_shifts(conn, rows)
        on_shift = sum(1 for r in rows if r["is_shift"])
        detail.append(f"shifts: {len(rows)} days, {on_shift} on shift, {len(notes)} mismatches")

        from . import metrics
        per_day: dict[str, float] = {}
        for r in conn.execute("SELECT activity_date, SUM(load_trimp) l FROM activities GROUP BY activity_date"):
            if r["l"]:
                per_day[r["activity_date"]] = float(r["l"])
        first = conn.execute("SELECT MIN(date) a FROM shifts").fetchone()["a"] or config.BACKFILL_START
        last = max([today.isoformat()] + list(per_day))
        series = metrics.build_series(
            metrics.date_range(date.fromisoformat(first), date.fromisoformat(last)), per_day)
        shift_map = {r["date"]: (r["is_shift"], r["cycle_day"])
                     for r in conn.execute("SELECT date, is_shift, cycle_day FROM shifts")}
        for row in series:
            row["is_shift"], row["cycle_day"] = shift_map.get(row["date"], (None, None))
        store.replace_daily_load(conn, series)
        detail.append(f"load series: {len(series)} days")

        store.finish_run(conn, run, True, " | ".join(detail))
    except Exception as exc:  # noqa: BLE001
        store.finish_run(conn, run, False, f"failed: {exc} | partial: {' | '.join(detail)}")
        raise

    if verbose:
        for line in detail:
            print(line)
        if notes:
            print("\ncalendar vs cycle mismatches (trades and extras, expected):")
            for n in notes[:20]:
                print("  ", n)
    return {"ok": True, "detail": detail, "notes": notes}
