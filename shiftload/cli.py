"""Command line entry point: probe, sync, shifts, status."""

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import config, fit, metrics, recovery, shifts, store
from .garmin import Garmin, GarminError, iter_windows


def _save_raw(name: str, payload) -> None:
    config.ensure_dirs()
    path = config.RAW_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (bytes, bytearray)):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload, indent=1))


def cmd_probe(args) -> int:
    g = Garmin()
    print(f"token minted, length {len(g.token())}")
    acts = g.activity_page(0, 3)
    for a in acts:
        print(f"  {a['startTimeLocal'][:16]}  {a['activityType']['typeKey']:<14} {a['activityName']}")
    st = g.daily_summaries(date.today(), date.today())
    if st:
        bb = st[0].get("bodyBattery") or {}
        print(f"today: steps {st[0].get('movement',{}).get('steps',{}).get('value')}, "
              f"resting HR {st[0].get('heartRate',{}).get('restingValue')}, "
              f"body battery {bb.get('minValue')}-{bb.get('maxValue')}")
    return 0


def _day_of(payload: dict) -> str | None:
    """Garmin sometimes omits calendarDate; str(None) would poison the table with 'None'."""
    raw = payload.get("calendarDate")
    if not raw:
        return None
    return str(raw)[:10]


def _wellness(g: Garmin, start: date, end: date) -> dict[str, dict]:
    by_date: dict[str, dict] = defaultdict(dict)

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
            by_date[day].update(
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
            by_date[day].update(
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
        by_date[day].update(
            hrv_nightly=h.get("lastNightAvg"),
            hrv_weekly=h.get("weeklyAvg"),
            hrv_status=h.get("status"),
        )
    return {d: v for d, v in by_date.items() if v}


def cmd_sync(args) -> int:
    since = date.fromisoformat(args.since or config.BACKFILL_START)
    today = date.today()
    data_until = today                     # Garmin rejects ranges that run into the future
    shifts_until = today + timedelta(days=args.ahead)
    conn = store.connect()
    run = store.start_run(conn, "sync")
    g = Garmin()
    detail = []

    # 1. activities, newest first, paging until we pass the since date
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
    conn_acts = store.upsert_activities(conn, acts)
    detail.append(f"activities: {conn_acts} rows from {pages} page(s), oldest kept {min((a['startTimeLocal'][:10] for a in acts), default='none')}")

    # 2. wellness
    wellness = _wellness(g, since, data_until)
    store.upsert_wellness(conn, wellness)
    detail.append(f"wellness: {len(wellness)} days")

    # 3. shifts, live calendar with cycle fallback
    try:
        rows, notes = shifts.build(since, shifts_until, use_calendar=True)
        _save_raw(f"calendar/fire_{datetime.now():%Y%m%dT%H%M%S}.ics", shifts.fetch_fire_calendar())
    except Exception as exc:  # noqa: BLE001 - calendar failure must not block data sync
        rows, notes = shifts.build(since, shifts_until, use_calendar=False)
        notes.append(f"calendar fetch failed: {exc}")
    store.replace_shifts(conn, rows)
    on_shift = sum(1 for r in rows if r["is_shift"])
    detail.append(f"shifts: {len(rows)} days, {on_shift} on shift, {len(notes)} disagreements")

    store.finish_run(conn, run, True, " | ".join(detail))
    for line in detail:
        print(line)
    if notes:
        print("\ncalendar vs cycle disagreements (trades and moves, expected to exist):")
        for n in notes[:20]:
            print("  ", n)
        if len(notes) > 20:
            print(f"   ... {len(notes)-20} more")
    return 0


def cmd_recovery(args) -> int:
    """Print recovery metrics bucketed by position in the 48/96 cycle."""
    conn = store.connect()
    summary = recovery.cycle_summary(conn, since=args.since)
    fields = ["sleep_score", "sleep_hours", "hrv_nightly", "resting_hr", "bb_min"]
    print(f"{'position':14}" + "".join(f"{recovery.RECOVERY_FIELDS[f]:>16}" for f in fields))
    print(f"{'':14}" + "".join(f"{'mean (n)':>16}" for _ in fields))
    for row in summary:
        cells = []
        for f in fields:
            v, n = row.get(f), row.get(f"{f}_n", 0)
            cells.append(f"{v if v is not None else '-':>10} ({n:>2})")
        print(f"{row['label']:14}" + "".join(f"{c:>16}" for c in cells))

    print()
    for f in ("hrv_nightly", "resting_hr", "sleep_score"):
        gap = recovery.biggest_gap(summary, f)
        if gap:
            print(f"{recovery.RECOVERY_FIELDS[f]}: worst {gap['low']['label']} at {gap['low'][f]}, "
                  f"best {gap['high']['label']} at {gap['high'][f]}, spread {gap['diff']}")
        else:
            print(f"{recovery.RECOVERY_FIELDS[f]}: not enough paired days yet")
    return 0


def cmd_fit(args) -> int:
    """Download FIT files and compute TRIMP for activities that do not have it yet."""
    conn = store.connect()
    hrmax, hrrest = store.load_inputs(conn)
    if args.hrmax:
        hrmax = args.hrmax
    if args.hrrest:
        hrrest = args.hrrest
    print(f"using HRmax {hrmax:.0f}, HRrest {hrrest:.0f}")
    sql = "SELECT activity_id, activity_date, name, duration_s, max_hr, load_trimp FROM activities"
    if not args.force:
        sql += " WHERE load_trimp IS NULL"
    sql += " ORDER BY start_local"
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("nothing to do, every activity already has a load")
        return 0

    g = Garmin()
    config.ensure_dirs()
    fit_dir = config.RAW_DIR / "fit"
    fit_dir.mkdir(parents=True, exist_ok=True)
    print(f"{'date':10} {'mins':>5} {'dur':>6} {'maxHR':>6} {'TRIMP':>7} {'GarminTE':>9}  name")
    total = 0.0
    for r in rows:
        aid = r["activity_id"]
        try:
            payload = fit_download(g, aid)
            path = fit_dir / f"{aid}.fit.zip"
            path.write_bytes(payload)
            raw = fit.unzip_fit(payload)
            (fit_dir / f"{aid}.fit").write_bytes(raw)
            recs = fit.parse_records(raw)
            load, minutes = fit.trimp(recs, hrmax, hrrest)
            conn.execute("UPDATE activities SET load_trimp=?, trimp_minutes=?, fit_path=?, hr_max_obs=? WHERE activity_id=?",
                         (load, minutes, str(path), max((x["hr"] for x in recs), default=None), aid))
            conn.commit()
            total += load
            te = conn.execute("SELECT aerobic_te FROM activities WHERE activity_id=?", (aid,)).fetchone()[0]
            print(f"{r['activity_date']:10} {minutes:5.0f} {r['duration_s']/60 if r['duration_s'] else 0:6.0f} "
                  f"{(r['max_hr'] or 0):6.0f} {load:7.0f} {(te or 0):9.1f}  {r['name'][:40]}")
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the backfill
            print(f"{r['activity_date']:10} FAILED {aid}: {exc}")
    print(f"\ntotal load across {len(rows)} activities: {total:.0f}")
    return 0


def cmd_recompute(args) -> int:
    """Rebuild the daily load series from stored activities and shifts."""
    conn = store.connect()
    per_day: dict[str, float] = {}
    for r in conn.execute("SELECT activity_date, SUM(load_trimp) l FROM activities GROUP BY activity_date"):
        if r["l"]:
            per_day[r["activity_date"]] = float(r["l"])

    first = conn.execute("SELECT MIN(date) a FROM shifts").fetchone()["a"] or config.BACKFILL_START
    last = max([date.today().isoformat()] + list(per_day))
    days = metrics.date_range(date.fromisoformat(first), date.fromisoformat(last))
    rows = metrics.build_series(days, per_day)

    shift_map = {r["date"]: (r["is_shift"], r["cycle_day"])
                 for r in conn.execute("SELECT date, is_shift, cycle_day FROM shifts")}
    for row in rows:
        row["is_shift"], row["cycle_day"] = shift_map.get(row["date"], (None, None))

    store.replace_daily_load(conn, rows)

    span = [r for r in rows if r["load"] > 0]
    print(f"daily series: {len(rows)} days ({rows[0]['date']} to {rows[-1]['date']}), "
          f"{len(span)} with load")
    last_loaded = span[-1] if span else None
    if last_loaded:
        print(f"last day with load: {last_loaded['date']} "
              f"(CTL {last_loaded['ctl']}, ATL {last_loaded['atl']}, ACWR {last_loaded['acwr']})")
    peak = max((r for r in rows if r["acwr"]), key=lambda r: r["acwr"], default=None)
    if peak:
        print(f"peak ACWR: {peak['acwr']} on {peak['date']} ({metrics.flag(peak['acwr'])})")
    biggest = max(rows, key=lambda r: r["load"])
    print(f"biggest day: {biggest['date']} {biggest['load']} load")

    print("\nlast 14 days:")
    print(f"{'date':12}{'shift':7}{'load':>6}{'CTL':>7}{'ATL':>7}{'ACWR':>6}  ")
    for r in rows[-14:]:
        shift = "ON" if r.get("is_shift") else ("off" if r.get("is_shift") == 0 else "?")
        print(f"{r['date']:12}{shift:7}{r['load']:6.0f}{(r['ctl'] or 0):7.1f}"
              f"{(r['atl'] or 0):7.1f}{(r['acwr'] if r['acwr'] is not None else 0):6.2f}")
    return 0


def fit_download(g: Garmin, activity_id: int) -> bytes:
    return g.get(f"/download-service/files/activity/{activity_id}")


def cmd_shifts(args) -> int:
    since = date.fromisoformat(args.since or config.BACKFILL_START)
    until = date.fromisoformat(args.until) if args.until else date.today() + timedelta(days=config.SHIFT_HORIZON_DAYS)
    try:
        rows, notes = shifts.build(since, until, use_calendar=True)
        print(f"calendar read OK, {len(notes)} disagreements with the plain cycle")
        for n in notes[:15]:
            print("  ", n)
    except Exception as exc:  # noqa: BLE001
        print(f"calendar fetch failed: {exc}")
        rows, notes = shifts.build(since, until, use_calendar=False)
    today = date.today()
    print("\nnext 3 weeks:")
    for r in rows:
        d = date.fromisoformat(r["date"])
        if today <= d <= today + timedelta(days=21):
            mark = "ON " if r["is_shift"] else "   "
            hours = f"{int(r['hours']):>3}h" if r["is_shift"] else "    "
            print(f"  {r['date']} {d.strftime('%a')} {mark}{hours}  {r['label'] or ''}  [{r['source']}]")
    return 0


def cmd_serve(args) -> int:
    """Run the dashboard. Binds where Tailscale can reach it, never the open internet."""
    import uvicorn
    print(f"serving on http://{args.host}:{args.port}")
    uvicorn.run("shiftload.web.app:app", host=args.host, port=args.port, log_level="warning")


def cmd_status(args) -> int:
    conn = store.connect()
    for table in ("activities", "daily_wellness", "shifts", "sync_runs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:15} {n:>5} rows")
    row = conn.execute("SELECT activity_date, name, distance_m FROM activities ORDER BY start_local DESC LIMIT 1").fetchone()
    if row:
        print(f"\nlatest activity: {row['activity_date']}  {row['name']}  {round((row['distance_m'] or 0)/1000,1)} km")
    row = conn.execute("SELECT MIN(date) a, MAX(date) b FROM daily_wellness").fetchone()
    print(f"wellness span:   {row['a']} -> {row['b']}")
    row = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        print(f"last sync:       {row['finished_at']} ok={row['ok']}\n  {row['detail']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shiftload")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="prove the key works and print the newest activities")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("sync", help="pull activities, wellness and shifts into the database")
    sp.add_argument("--since", default=None, help="ISO date, default 2026-01-01")
    sp.add_argument("--ahead", type=int, default=config.SHIFT_HORIZON_DAYS,
                    help="how many days of shifts to plan forward")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("shifts", help="show shift days straight from the calendar")
    sp.add_argument("--since", default=None)
    sp.add_argument("--until", default=None)
    sp.set_defaults(func=cmd_shifts)

    sp = sub.add_parser("fit", help="download FIT files and compute TRIMP")
    sp.add_argument("--force", action="store_true", help="recompute activities that already have a load")
    sp.add_argument("--hrmax", type=float, default=None)
    sp.add_argument("--hrrest", type=float, default=None)
    sp.set_defaults(func=cmd_fit)

    sp = sub.add_parser("recompute", help="rebuild the daily load series from stored data")
    sp.set_defaults(func=cmd_recompute)

    sp = sub.add_parser("recovery", help="recovery metrics by cycle position")
    sp.add_argument("--since", default=None)
    sp.set_defaults(func=cmd_recovery)

    sp = sub.add_parser("serve", help="run the dashboard")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=4534)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("status", help="what is in the database")
    sp.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except GarminError as exc:
        print(f"garmin error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
