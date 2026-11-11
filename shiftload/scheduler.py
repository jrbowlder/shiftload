"""Runs the sync on a schedule without a cron daemon.

One background thread, a target time in his timezone, and a catch-up run at startup if
the last successful sync is more than a day old. That last part is what matters for a
laptop: the machine sleeps, the timer is not magic, but the next start repairs itself.
"""

import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import store
from .sync import run_sync

TZ = ZoneInfo("America/Denver")
SYNC_AT = time(5, 30)
CATCHUP_AFTER = timedelta(hours=20)


def _seconds_until_next_run(now: datetime) -> float:
    target = datetime.combine(now.date(), SYNC_AT, tzinfo=TZ)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _last_ok(conn) -> datetime | None:
    row = conn.execute(
        "SELECT finished_at FROM sync_runs WHERE ok=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or not row["finished_at"]:
        return None
    try:
        return datetime.fromisoformat(row["finished_at"])
    except ValueError:
        return None


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        now = datetime.now(TZ)
        wait = _seconds_until_next_run(now)
        if stop.wait(wait):
            return
        try:
            run_sync(verbose=False)
            print(f"[scheduler] sync finished {datetime.now(TZ):%Y-%m-%d %H:%M}", flush=True)
        except Exception as exc:  # noqa: BLE001 - the thread must survive a bad night
            print(f"[scheduler] sync failed: {exc}", flush=True)


def start() -> threading.Event:
    """Kick off the scheduler thread. Returns the stop event, for tests and shutdown."""
    stop = threading.Event()
    conn = store.connect()
    last = _last_ok(conn)
    now = datetime.now(TZ)
    if last is None or (now.replace(tzinfo=last.tzinfo) - last) > CATCHUP_AFTER:
        threading.Thread(
            target=lambda: _catch_up(stop), name="shiftload-catchup", daemon=True
        ).start()
    threading.Thread(target=_loop, args=(stop,), name="shiftload-scheduler", daemon=True).start()
    nxt = datetime.combine(now.date(), SYNC_AT, tzinfo=TZ)
    if nxt <= now:
        nxt += timedelta(days=1)
    print(f"[scheduler] next run {nxt:%Y-%m-%d %H:%M} {nxt.tzname()}", flush=True)
    return stop


def _catch_up(stop: threading.Event) -> None:
    try:
        run_sync(verbose=False)
        print(f"[scheduler] catch-up sync done {datetime.now(TZ):%Y-%m-%d %H:%M}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] catch-up failed: {exc}", flush=True)
