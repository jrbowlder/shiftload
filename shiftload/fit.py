"""FIT files and training load.

Garmin's download endpoint hands back a zip containing the .FIT. We unzip, keep the
FIT on disk as the archive, then walk its record stream to compute a TRIMP score.

TRIMP (Banister, male coefficients) weights time spent at high heart rate exponentially,
which is why a hard hour costs far more than an easy three:

    TRIMP += minutes * hrr * 0.64 * exp(1.92 * hrr)      where hrr = (hr - rest) / (max - rest)
"""

import io
import zipfile
from datetime import datetime, timezone

import fitdecode


def unzip_fit(payload: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".fit"):
                return zf.read(name)
    raise ValueError("no .fit inside the downloaded archive")


def parse_records(fit_bytes: bytes) -> list[dict]:
    """Every record message with the fields we care about, in time order."""
    out = []
    with fitdecode.FitReader(io.BytesIO(fit_bytes)) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA or frame.name != "record":
                continue
            def val(field):
                return frame.get_value(field, fallback=None)
            ts = val("timestamp")
            hr = val("heart_rate")
            if ts is None or not hr:
                continue
            out.append({
                "ts": ts,
                "hr": float(hr),
                "speed": val("enhanced_speed") or val("speed") or 0.0,
                "alt": val("enhanced_altitude") or val("altitude"),
                "dist": val("distance"),
            })
    out.sort(key=lambda r: r["ts"])
    return out


def trimp(records: list[dict], hrmax: float, hrrest: float) -> tuple[float, float]:
    """Return (trimp, minutes of moving time) from parsed records."""
    if len(records) < 2 or hrmax <= hrrest:
        return 0.0, 0.0
    total = 0.0
    moving_s = 0.0
    for prev, cur in zip(records, records[1:]):
        dt = (cur["ts"] - prev["ts"]).total_seconds()
        if dt <= 0 or dt > 60:          # pauses and gaps carry no load
            continue
        hrr = (cur["hr"] - hrrest) / (hrmax - hrrest)
        hrr = max(0.0, min(1.5, hrr))   # clamp sensor spikes
        total += (dt / 60.0) * hrr * 0.64 * 2.718281828459045 ** (1.92 * hrr)
        if cur["speed"] and cur["speed"] > 0.3:
            moving_s += dt
    return round(total, 1), round(moving_s / 60.0, 1)


def fit_summary(fit_bytes: bytes) -> dict:
    """Cheap facts from the session message, useful for sanity checks."""
    info = {}
    with fitdecode.FitReader(io.BytesIO(fit_bytes)) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "session":
                for key in ("sport", "sub_sport", "total_elapsed_time", "total_timer_time",
                            "avg_heart_rate", "max_heart_rate", "total_ascent",
                            "total_distance"):
                    try:
                        info[key] = frame.get_value(key, fallback=None)
                    except KeyError:
                        pass
    return info
