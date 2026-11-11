"""The web app: one page, one JSON endpoint.

Deliberately small. The database holds everything, so the server just reads rows and
hands them to a template. No auth, because it only listens where Tailscale can reach it.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import metrics, recovery, scheduler, store

WEB_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # the daily sync lives in this process on purpose: one container, no cron daemon
    stop = scheduler.start()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="shiftload", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")


def _rows(days: int = 0):
    conn = store.connect()
    rows = conn.execute(
        """SELECT date, load, ctl, atl, acwr, monotony, strain, is_shift, cycle_day
           FROM daily_load ORDER BY date"""
    ).fetchall()
    if days and days < len(rows):
        rows = rows[-days:]
    return [dict(r) for r in rows]


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/series")
def api_series(days: int = 0):
    rows = _rows(days)
    return {
        "dates": [r["date"] for r in rows],
        "load": [r["load"] for r in rows],
        "ctl": [round(r["ctl"], 1) for r in rows],
        "atl": [round(r["atl"], 1) for r in rows],
        "acwr": [r["acwr"] for r in rows],
        "is_shift": [r["is_shift"] for r in rows],
    }


@app.get("/api/recovery")
def api_recovery(days: int = 0):
    conn = store.connect()
    return recovery.series(conn, days)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, days: int = 0):
    rows = _rows(days)
    latest = rows[-1] if rows else None
    loaded = [r for r in rows if r["load"] > 0]
    last_loaded = loaded[-1] if loaded else None
    conn = store.connect()
    last_activity = conn.execute(
        """SELECT activity_date, name, distance_m FROM activities
           ORDER BY start_local DESC LIMIT 1"""
    ).fetchone()
    cycle = recovery.cycle_summary(conn)
    week = [
        {"date": r["date"], "load": r["load"], "is_shift": r["is_shift"],
         "cycle_day": r["cycle_day"], "strain": r["strain"]}
        for r in rows[-14:]
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "latest": latest,
            "last_loaded": last_loaded,
            "last_activity": dict(last_activity) if last_activity else None,
            "acwr_words": metrics.flag(latest["acwr"]) if latest else "",
            "week": week,
            "cycle": cycle,
            "days": days,
            "total_days": len(rows),
        },
    )
