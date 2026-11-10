"""Shifts, read live from the FIRE calendar.

The calendar holds one recurring "FIRE SHIFT" event (every 6 days, 48 hours) plus
RECURRENCE-ID overrides for every moved, shortened or traded shift, and EXDATEs for
cancelled ones. Expanding that properly is the reason we run recurring_ical_events
instead of doing date maths ourselves: date maths cannot see a trade.

If the calendar cannot be read, we fall back to the 48/96 cycle anchored at 2026-01-29
and mark those rows as source='cycle' so the page can say it is guessing.
"""

import base64
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import recurring_ical_events
from icalendar import Calendar

from . import config

CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
DAV_NS = "DAV:"

QUERY = """<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
 <D:prop><D:getetag/><C:calendar-data/></D:prop>
 <C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT"/></C:comp-filter></C:filter>
</C:calendar-query>"""


def credentials() -> tuple[str, str]:
    """CalDAV login. Env overrides the vdirsyncer config we already have."""
    user, pw = os.environ.get("CALDAV_USER"), os.environ.get("CALDAV_PASS")
    if user and pw:
        return user, pw
    text = Path(config.VDIRSYNCER_CONFIG).read_text()
    u = re.search(r"^username\s*=\s*\"?([^\"\n]+)\"?", text, re.M)
    p = re.search(r"^password\s*=\s*\"?([^\"\n]+)\"?", text, re.M)
    if not (u and p):
        raise RuntimeError(f"no CalDAV credentials in {config.VDIRSYNCER_CONFIG}")
    return u.group(1).strip(), p.group(1).strip()


def fetch_fire_calendar() -> bytes:
    user, pw = credentials()
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    url = config.CALDAV_BASE + config.FIRE_CALENDAR + "/"
    r = httpx.request(
        "REPORT",
        url,
        content=QUERY.encode(),
        headers={"Authorization": f"Basic {auth}", "Depth": "1",
                 "Content-Type": "application/xml"},
        timeout=90,
    )
    r.raise_for_status()
    return r.content


def _calendar_objects(xml_body: bytes) -> Calendar:
    """Pull every <calendar-data> blob out of a multistatus and merge the events."""
    from xml.etree import ElementTree as ET

    root = ET.fromstring(xml_body)
    merged = Calendar()
    merged.add("PRODID", "-//shiftload//EN")
    merged.add("VERSION", "2.0")
    count = 0
    for node in root.iter():
        if node.tag == f"{{{CALDAV_NS}}}calendar-data" and node.text:
            try:
                cal = Calendar.from_ical(node.text)
            except ValueError:
                continue  # a malformed resource should not sink the whole sync
            for component in cal.walk("VEVENT"):
                merged.add_component(component)
                count += 1
    if not count:
        raise RuntimeError("no events came back from the FIRE calendar")
    return merged


def _day_span(event) -> int:
    end = event.get("DTEND")
    dur = event.get("DURATION")
    start = event.get("DTSTART").dt
    if end is not None:
        stop = end.dt
    elif dur is not None:
        stop = start + dur.dt
    else:
        stop = start + timedelta(days=1)
    if isinstance(start, datetime):
        start = start.date()
    if isinstance(stop, datetime):
        stop = stop.date()
    return max(1, (stop - start).days)


def calendar_shifts(start: date, end: date) -> dict[str, dict]:
    """Expand the live calendar between two dates into per-day shift rows."""
    cal = _calendar_objects(fetch_fire_calendar())
    events = recurring_ical_events.of(cal).between(start, end)
    rows: dict[str, dict] = {}
    for ev in events:
        summary = str(ev.get("SUMMARY", "")).strip()
        status = str(ev.get("STATUS", "")).upper()
        if "FIRE SHIFT" not in summary.upper():
            continue
        if status == "CANCELLED":
            continue  # a cancelled instance means he is not working that day
        day = ev.get("DTSTART").dt
        if isinstance(day, datetime):
            day = day.date()
        span = _day_span(ev)
        hours = 24 * span
        for offset in range(span):
            d = (day + timedelta(days=offset)).isoformat()
            if start.isoformat() <= d <= end.isoformat():
                rows[d] = {"date": d, "is_shift": 1, "hours": hours,
                           "label": summary, "source": "calendar"}
    return rows


def cycle_shifts(start: date, end: date) -> dict[str, dict]:
    """48/96 fallback: two on, four off, anchored at 2026-01-29."""
    anchor = date.fromisoformat(config.CYCLE_ANCHOR)
    rows: dict[str, dict] = {}
    d = start
    while d <= end:
        pos = (d - anchor).days % config.CYCLE_PERIOD
        on = pos < config.SHIFT_DAYS
        rows[d.isoformat()] = {
            "date": d.isoformat(),
            "is_shift": 1 if on else 0,
            "hours": 24 if on else 0,
            "label": "cycle guess" if on else None,
            "source": "cycle",
        }
        d += timedelta(days=1)
    return rows


def build(start: date, end: date, use_calendar: bool = True) -> tuple[list[dict], list[str]]:
    """Return every day in range with its shift state, plus any calendar/cycle disagreements."""
    cycle = cycle_shifts(start, end)
    notes: list[str] = []
    if use_calendar:
        cal_rows = calendar_shifts(start, end)
        merged = {}
        for d, row in cycle.items():
            if d in cal_rows:
                merged[d] = cal_rows[d]
            else:
                merged[d] = {**row, "is_shift": 0, "hours": 0, "label": None}
        # days the calendar grants that the cycle does not (picked up shifts)
        for d, row in cal_rows.items():
            merged.setdefault(d, row)
        for d in sorted(merged):
            if d in cal_rows and cal_rows[d]["is_shift"] != cycle[d]["is_shift"]:
                notes.append(f"{d}: calendar says {'shift' if cal_rows[d]['is_shift'] else 'off'}, "
                             f"cycle says {'shift' if cycle[d]['is_shift'] else 'off'}")
    else:
        merged = cycle
        notes.append("calendar unavailable, every shift row is a cycle guess")
    anchor = date.fromisoformat(config.CYCLE_ANCHOR)
    out = []
    for d in sorted(merged):
        row = dict(merged[d])
        row["cycle_day"] = (date.fromisoformat(d) - anchor).days % config.CYCLE_PERIOD
        out.append(row)
    return out, notes
