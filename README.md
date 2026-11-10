# shiftload

A self-hosted training load dashboard for my own Garmin data, read against a 48/96
firefighter shift schedule.

It pulls activities and daily wellness numbers from the official Garmin Connect Personal
API, expands my shift calendar properly (recurring shifts, trades, extras, 24s,
cancellations), and stores all of it in a local SQLite file. Nothing leaves the house.

## Status

Phase 1 of the plan in [PLAN.md](PLAN.md) is working: ingest runs end to end and the
database is idempotent. Metrics, charts and the web app are next.

## Quick start

```bash
# 1. mint a personal API key at
#    https://connect.garmin.com/app/settings/personalApiToken
# 2. put it in .env
echo 'GARMIN_API_KEY=your-key' > .env && chmod 600 .env
# 3. install and run
uv sync
uv run shiftload probe        # prove the key works
uv run shiftload sync --since 2026-01-01
uv run shiftload shifts       # shift days straight from the calendar
uv run shiftload status       # what landed in the database
```

CalDAV credentials are read from `~/.config/vdirsyncer/config`, or from `CALDAV_USER`
and `CALDAV_PASS` if those are set.

## Layout

```
shiftload/          package: garmin client, store, shifts, cli
PLAN.md             the plan, including phase status
~/shiftload-data/   database and raw JSON/ICS archive (gitignored)
```

## Licence

MIT
