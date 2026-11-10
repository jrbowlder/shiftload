"""SQLite storage. The database is derived: the raw JSON and FIT files on disk are the
archive, so any metric can be recomputed without asking Garmin again."""

import json
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id     INTEGER PRIMARY KEY,
    start_local     TEXT NOT NULL,
    activity_date   TEXT NOT NULL,
    activity_type   TEXT,
    name            TEXT,
    distance_m      REAL,
    duration_s      REAL,
    moving_s        REAL,
    avg_hr          REAL,
    max_hr          REAL,
    ascent_m        REAL,
    descent_m       REAL,
    calories        REAL,
    training_load   REAL,
    aerobic_te      REAL,
    anaerobic_te    REAL,
    hr_zones_json   TEXT,
    raw_json        TEXT,
    ingested_at     TEXT NOT NULL,
    load_trimp      REAL,
    trimp_minutes   REAL,
    fit_path        TEXT,
    hr_max_obs      REAL
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(activity_date);

CREATE TABLE IF NOT EXISTS daily_wellness (
    date            TEXT PRIMARY KEY,
    steps           INTEGER,
    resting_hr      REAL,
    hr_min          REAL,
    hr_max          REAL,
    stress_avg      REAL,
    stress_max      REAL,
    bb_min          REAL,
    bb_max          REAL,
    bb_at_wake      REAL,
    bb_latest       REAL,
    sleep_score     REAL,
    sleep_s         REAL,
    sleep_deep_s    REAL,
    sleep_rem_s     REAL,
    sleep_light_s   REAL,
    sleep_awake_s   REAL,
    sleep_avg_hr    REAL,
    hrv_nightly     REAL,
    hrv_weekly      REAL,
    hrv_status      TEXT,
    respiration     REAL,
    calories_total  REAL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shifts (
    date            TEXT PRIMARY KEY,
    is_shift        INTEGER NOT NULL,
    hours           REAL,
    label           TEXT,
    source          TEXT NOT NULL,        -- 'calendar' or 'cycle'
    cycle_day       INTEGER,              -- 0 and 1 are shift days, 2..5 are off
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_load (
    date        TEXT PRIMARY KEY,
    load        REAL NOT NULL,
    ctl         REAL,
    atl         REAL,
    acwr        REAL,
    monotony    REAL,
    strain      REAL,
    is_shift    INTEGER,
    cycle_day   INTEGER,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    detail      TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MIGRATIONS = {
    "activities": {
        "load_trimp": "REAL",
        "trimp_minutes": "REAL",
        "fit_path": "TEXT",
        "hr_max_obs": "REAL",
    },
}


def _migrate(conn) -> None:
    """Add columns that later phases introduced, without losing the database."""
    for table, columns in MIGRATIONS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


def connect(path=None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# ---------- activities ----------

def upsert_activities(conn, items: list[dict]) -> int:
    ts = now()
    rows = 0
    for a in items:
        start_local = a.get("startTimeLocal") or ""
        conn.execute(
            """INSERT INTO activities (activity_id, start_local, activity_date, activity_type, name,
                    distance_m, duration_s, moving_s, avg_hr, max_hr, ascent_m, descent_m, calories,
                    training_load, aerobic_te, anaerobic_te, hr_zones_json, raw_json, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(activity_id) DO UPDATE SET
                    start_local=excluded.start_local, activity_date=excluded.activity_date,
                    activity_type=excluded.activity_type, name=excluded.name,
                    distance_m=excluded.distance_m, duration_s=excluded.duration_s,
                    moving_s=excluded.moving_s, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr,
                    ascent_m=excluded.ascent_m, descent_m=excluded.descent_m,
                    calories=excluded.calories, training_load=excluded.training_load,
                    aerobic_te=excluded.aerobic_te, anaerobic_te=excluded.anaerobic_te,
                    hr_zones_json=excluded.hr_zones_json, raw_json=excluded.raw_json,
                    ingested_at=excluded.ingested_at""",
            (
                a.get("activityId"),
                start_local,
                start_local[:10],
                (a.get("activityType") or {}).get("typeKey"),
                a.get("activityName"),
                a.get("distance"),
                a.get("duration"),
                a.get("movingDuration"),
                a.get("averageHR"),
                a.get("maxHR"),
                a.get("elevationGain"),
                a.get("elevationLoss"),
                a.get("calories"),
                a.get("activityTrainingLoad"),
                a.get("aerobicTrainingEffect"),
                a.get("anaerobicTrainingEffect"),
                json.dumps({f"z{i}": a.get(f"hrTimeInZone_{i}") for i in range(1, 6)}),
                json.dumps(a),
                ts,
            ),
        )
        rows += 1
    conn.commit()
    return rows


def set_load(conn, activity_id: int, load: float, minutes: float, fit_path: str, hr_max: float) -> None:
    conn.execute(
        """UPDATE activities SET load_trimp=?, trimp_minutes=?, fit_path=?, hr_max_obs=?
           WHERE activity_id=?""",
        (load, minutes, fit_path, hr_max, activity_id),
    )
    conn.commit()


def load_inputs(conn) -> tuple[float, float]:
    """Max heart rate seen in the data, and the median resting heart rate."""
    mx = conn.execute("SELECT MAX(hr_max_obs) a, MAX(max_hr) b FROM activities").fetchone()
    rhr = conn.execute("SELECT resting_hr FROM daily_wellness WHERE resting_hr IS NOT NULL").fetchall()
    vals = sorted(r["resting_hr"] for r in rhr if r["resting_hr"])
    median_rest = vals[len(vals) // 2] if vals else 55
    observed_max = max([v for v in (mx["a"], mx["b"]) if v] or [0])
    return float(observed_max or 190), float(median_rest)


# ---------- wellness ----------

def upsert_wellness(conn, by_date: dict[str, dict]) -> int:
    ts = now()
    for d, w in by_date.items():
        conn.execute(
            """INSERT INTO daily_wellness (date, steps, resting_hr, hr_min, hr_max, stress_avg,
                    stress_max, bb_min, bb_max, bb_at_wake, bb_latest, sleep_score, sleep_s,
                    sleep_deep_s, sleep_rem_s, sleep_light_s, sleep_awake_s, sleep_avg_hr,
                    hrv_nightly, hrv_weekly, hrv_status, respiration, calories_total, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                    steps=COALESCE(excluded.steps, steps),
                    resting_hr=COALESCE(excluded.resting_hr, resting_hr),
                    hr_min=COALESCE(excluded.hr_min, hr_min),
                    hr_max=COALESCE(excluded.hr_max, hr_max),
                    stress_avg=COALESCE(excluded.stress_avg, stress_avg),
                    stress_max=COALESCE(excluded.stress_max, stress_max),
                    bb_min=COALESCE(excluded.bb_min, bb_min),
                    bb_max=COALESCE(excluded.bb_max, bb_max),
                    bb_at_wake=COALESCE(excluded.bb_at_wake, bb_at_wake),
                    bb_latest=COALESCE(excluded.bb_latest, bb_latest),
                    sleep_score=COALESCE(excluded.sleep_score, sleep_score),
                    sleep_s=COALESCE(excluded.sleep_s, sleep_s),
                    sleep_deep_s=COALESCE(excluded.sleep_deep_s, sleep_deep_s),
                    sleep_rem_s=COALESCE(excluded.sleep_rem_s, sleep_rem_s),
                    sleep_light_s=COALESCE(excluded.sleep_light_s, sleep_light_s),
                    sleep_awake_s=COALESCE(excluded.sleep_awake_s, sleep_awake_s),
                    sleep_avg_hr=COALESCE(excluded.sleep_avg_hr, sleep_avg_hr),
                    hrv_nightly=COALESCE(excluded.hrv_nightly, hrv_nightly),
                    hrv_weekly=COALESCE(excluded.hrv_weekly, hrv_weekly),
                    hrv_status=COALESCE(excluded.hrv_status, hrv_status),
                    respiration=COALESCE(excluded.respiration, respiration),
                    calories_total=COALESCE(excluded.calories_total, calories_total),
                    updated_at=excluded.updated_at""",
            (
                d,
                w.get("steps"), w.get("resting_hr"), w.get("hr_min"), w.get("hr_max"),
                w.get("stress_avg"), w.get("stress_max"), w.get("bb_min"), w.get("bb_max"),
                w.get("bb_at_wake"), w.get("bb_latest"), w.get("sleep_score"), w.get("sleep_s"),
                w.get("sleep_deep_s"), w.get("sleep_rem_s"), w.get("sleep_light_s"),
                w.get("sleep_awake_s"), w.get("sleep_avg_hr"), w.get("hrv_nightly"),
                w.get("hrv_weekly"), w.get("hrv_status"), w.get("respiration"),
                w.get("calories_total"), ts,
            ),
        )
    conn.commit()
    return len(by_date)


# ---------- shifts ----------

def replace_shifts(conn, rows: list[dict]) -> int:
    ts = now()
    conn.executemany(
        """INSERT INTO shifts (date, is_shift, hours, label, source, cycle_day, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET
                is_shift=excluded.is_shift, hours=excluded.hours, label=excluded.label,
                source=excluded.source, cycle_day=excluded.cycle_day, updated_at=excluded.updated_at""",
        [
            (r["date"], r["is_shift"], r.get("hours"), r.get("label"), r["source"],
             r.get("cycle_day"), ts)
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def replace_daily_load(conn, rows: list[dict]) -> int:
    ts = now()
    conn.execute("DELETE FROM daily_load")
    conn.executemany(
        """INSERT INTO daily_load (date, load, ctl, atl, acwr, monotony, strain,
                                   is_shift, cycle_day, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(r["date"], r["load"], r["ctl"], r["atl"], r["acwr"], r["monotony"], r["strain"],
          r.get("is_shift"), r.get("cycle_day"), ts) for r in rows],
    )
    conn.commit()
    return len(rows)


def start_run(conn, kind: str) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs (kind, started_at, ok, detail) VALUES (?,?,0,'running')",
        (kind, now()),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, ok: bool, detail: str) -> None:
    conn.execute(
        "UPDATE sync_runs SET finished_at=?, ok=?, detail=? WHERE id=?",
        (now(), 1 if ok else 0, detail, run_id),
    )
    conn.commit()
