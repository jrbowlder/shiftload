# Shiftload: a training-load dashboard for my Garmin data

Plan v1, written 2026-11-10. This is the source of truth for the project. Each work session starts by reading this file and ends by updating the phase status here.

## 1. What it is

A self-hosted dashboard that pulls my Garmin activities and daily wellness numbers, computes training load and recovery, and charts them against my 48/96 shift schedule. Personal use first. A coaching-facing view comes later, if it earns its keep.

End state: a Docker container on this laptop, reachable from my iPhone over Tailscale, holding several years of my data in a local SQLite file. No third-party account, no cloud, no telemetry.

## 2. Locked decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Data source | Garmin Connect **Personal API** (free, official, since Oct 2026) | Official and stable, replaces the fragile community scraper |
| Auth | Personal API key in `.env`, exchanged for a 24 hour access token per run | That is how the personal tier works; no refresh tokens exist |
| Language | Python 3.14 + `uv` | Same toolchain I am learning on Boot.dev |
| Web | FastAPI + Jinja templates, Chart.js vendored locally | Server-rendered pages are easy to read; vendored JS keeps the browser from calling a CDN |
| Storage | SQLite at `~/shiftload-data/shiftload.db`, raw JSON and FIT archive beside it | No server to babysit, easy to back up |
| Hosting | Docker Compose on port 4534, bound to `0.0.0.0` for Tailscale access | Matches how Navidrome and Securo already run here |
| Repo | Public GitHub, `jrbowlder/shiftload` | Portfolio piece; secrets stay in `.env`, never committed |
| Device | Forerunner 255 | Has sleep, HRV Status, Body Battery, stress, resting HR. Does **not** have Training Readiness, so we compute load ourselves |
| Sport scope | Trail running only for v1 | Keeps metrics honest; other sports can be added once the model is trusted |
| Shift source | **Live FIRE calendar over CalDAV**, expanded with `recurring_ical_events`, cycle maths only as a fallback | Corrected 2026-11-10: the calendar is accurate and holds a recurring shift plus per-date overrides for trades, extras, 24s and cancellations. The stale local mirror was what looked wrong |

## 3. How we work

I give direction, Bobcat writes the code, then recaps what was built and why in plain language. At the end of each phase Bobcat quizzes me on what the code actually does. If I cannot answer the quiz, we do not move to the next phase, we shore up that one.

Time budget: 2 to 3 hours a week. Phases are sized to that. One phase per week is the target, not a promise.

## 4. One-time setup I do myself

1. Log in to Garmin Connect on the web, open Settings, Personal API Key page: `https://connect.garmin.com/app/settings/personalApiToken`
2. Create Key, accept terms, choose a **1 year** expiration, copy the key. It is shown exactly once.
3. Put it in `~/projects/shiftload/.env` as `GARMIN_API_KEY=...`, then `chmod 600` that file.
4. Add a calendar reminder for the key expiration, about a week out. Garmin also emails a warning.

The key is never pasted into chat and never committed. If it leaks, delete it on that same page and make a new one.

## 5. Data plumbing (verified against Garmin's docs)

Token: `POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token` with header `Authorization: ApiKey <key>`. Returns an access token valid 24 hours.

| Purpose | Endpoint |
| --- | --- |
| Activity list (paged, default 99) | `GET /activitylist-service/activities/search/activities?start=&limit=` |
| Activity detail (summary, splits, training effect) | `GET /activity-service/activity/{activityId}` |
| Raw FIT file | `GET /download-service/files/activity/{activityId}` |
| Sleep summaries, 31 day max per call | `GET /sleep-service/sleep/dailySleepSummariesByDate?startDate=&endDate=` |
| HRV nightly summaries, date range | `GET /hrv-service/hrv/daily/{fromDate}/{toDate}` |
| Daily wellness (steps, stress, Body Battery, RHR) | `GET /usersummary-service/v2/dailysummaries?startDate=&endDate=` |
| Body Battery detail, 31 day max | `GET /wellness-service/wellness/bodyBattery/reports/daily?startDate=&endDate=` |
| Weight | `GET /weight-service/weight/range/{startDate}/{endDate}` |

Backfill plan: Phase 1 probes how far back the activity list pages. If it stops early, I request my full Garmin account export from Garmin Connect (Account, Data Management, Export), unzip the FIT files once, and we load them from disk instead. Either way the code has one `ingest` path that accepts both an API page and a local FIT file.

Rate limit politeness: sequential calls with a small delay, retry on 429 and 5xx with backoff, and a resume point so a killed sync does not start over.

## 6. Shift model

The FIRE calendar is the source of truth, read live over CalDAV on every sync. It holds one recurring `FIRE SHIFT` event (`DTSTART 2026-01-29`, `RRULE:FREQ=DAILY;INTERVAL=6`, `DURATION:P2D`) plus 72 `RECURRENCE-ID` overrides and 4 `EXDATE`s. That is how real life is recorded: a traded shift shows up as an override with a new date, a picked up extra shows up as its own event (for example `FIRE SHIFT - EXTRA` on 2026-11-09, 24 hours), and a vacation or sick day shows up as a cancelled or moved instance.

So the app expands the recurrence properly instead of doing date maths, and stores one row per day. Trade, extra, 24 hour and cancelled instances all fall out of that expansion for free, and any change made in Fastmail is picked up by the next sync with no code change.

The 48/96 cycle (anchor 2026-01-29, two on, four off) still exists in the code, but only as a fallback if the calendar cannot be fetched. Those rows are marked `source='cycle'` so the page can say it is guessing. On a healthy sync the calendar and the cycle disagree on about 6 days out of 374, and every one of those is a real event the calendar knows about.

Cycle position (shift day 1, shift day 2, off day 1 to 4) is stored per day as `cycle_day` and is the right grouping for recovery questions: compare distributions per position, not the raw series by eye.

## 7. Metrics

- **Activity load**: TRIMP from heart rate reserve, plus vertical gain recorded as a secondary field for trail runs. Garmin's own training effect is stored too, and the page shows both so I can see when they disagree.
- **CTL** (fitness): 42 day exponentially weighted average of daily load.
- **ATL** (fatigue): 7 day exponentially weighted average.
- **ACWR**: ATL divided by CTL, with a caution band drawn on the chart.
- **Monotony and strain**: Foster's weekly monotony, kept because shift weeks genuinely are monotonous or spiky in a way normal running weeks are not.
- **Recovery**: sleep score and duration, nightly HRV average and status, resting HR, Body Battery low and high, average stress.

**Strength work (decided 2026-11-10).** He lifts in the gym on shift days and has not been logging it. Unlogged lifting makes shift days look like rest days, which would quietly corrupt every recovery conclusion, so it has to enter the data. The route is to record it on the watch (the 255 has a strength profile) rather than adding a manual entry screen. In Phase 3 those sessions get a load value of their own and the page shows a separate strength marker, so a shift day with lifting never reads as an empty day.

## 8. Phases

**Phase 0, plumbing (45 min). DONE 2026-11-10.** Repo at `~/Projects/shiftload`, `.env` with the key at 600, and a `probe` command that mints a token and prints the last three activities. *Gate passed: probe printed the Standhope 60K Fail on 2026-07-25 and live values for today.*

**Phase 1, ingest (2 hrs). DONE 2026-11-10.** SQLite schema, activity paging, wellness ranges in 31 day windows, live calendar shifts, raw archive under `~/shiftload-data/raw`. *Gate passed: 37 activities since 2026-01-01, 240 wellness days, 374 shift days, and a second identical sync changed no row counts.*

**Phase 2, FIT parsing (2 hrs). DONE 2026-11-10.** FIT download (the endpoint returns a zip), record stream decoding with `fitdecode`, TRIMP from heart rate reserve, FIT files archived under `~/shiftload-data/raw/fit/`. *Gate passed: the Standhope 60K comes out at 729 TRIMP over 335 minutes, and computed average heart rate is within 1.1 bpm of Garmin's number on all 8 activities checked.*

Load runs 130 to 162 TRIMP per hour on hard days and 40 to 80 per hour on easy ones, which is the range these coefficients normally produce. Total across 37 activities: 5,073.

Heart rate inputs are derived from his own data, not guessed: HRmax 188 (highest seen in a FIT record), HRrest 59 (median of 240 resting readings). Both are overridable with `--hrmax` and `--hrrest` if a max effort later shows a higher peak.

**Phase 3, the model (2 hrs). DONE 2026-11-10.** Daily load series with gap days filled at zero, CTL (42 day), ATL (7 day), ACWR, plus Foster monotony and strain. Nine tests cover convergence, reaction speed, spike detection, monotony, zero filling and the cold start rule. *Gate passed: an independent hand recomputation for 2026-05-27 gave CTL 42.15, ATL 68.01, ACWR 1.614 against 42.10 / 68.00 / 1.61 in the database.*

Two judgement calls worth remembering:

- **ACWR stays silent for the first 28 days and until CTL reaches 5.** Without that gate the curve reported a 5.66 "spike" on 2026-02-12 on the back of a CTL of 1.9, which is a cold start dressed up as a warning.
- **Low base numbers still read high on purpose.** The highest ACWR in the year is 5.09 on 2026-02-15, when CTL was 5.4 and a single 155 load day more than tripled his weekly average. That is honestly a spike from nothing, so it stands, but February values are about starting cold rather than about danger.

The series currently reads: CTL peaked at 42.1 on 2026-05-27, biggest single day was the 60K at 729 load, and CTL has decayed to 2.7 by 2026-11-10 because nothing has been recorded since July.

**Phase 4, the chart (2.5 hrs). DONE 2026-11-10.** FastAPI plus Jinja, Chart.js vendored locally (no CDN at runtime), load bars with CTL and ATL over the top, shift days shaded as vertical bands, a second chart for ACWR with the 1.5 and 0.8 reference lines, and a two week table. Range buttons for 90 days, 180 days or everything. *Gate passed: the page renders in a browser, and the shaded bands match the database pattern day for day, including the three day block around 2026-11-07 to 11-09 where the extra shift sits.*

Two fixes worth remembering: the load bars get their own left axis, because a single 729 day flattened the fitness and fatigue curves into the floor when they shared a scale, and the shift bands are drawn from the API's own `is_shift` array, so calendar edits show up without touching the chart code.

**Phase 5, recovery (2 hrs). DONE 2026-11-10.** Recovery chart with resting HR, overnight HRV and sleep score against shift bands, plus a table averaging every metric by cycle position, each mean carrying its sample size. *Gate passed, and the answer is the opposite of what we assumed. HRV on shift day 2 averages 38.7 over 29 nights against 34.4 on off day 3 over 12 nights, resting HR is lower on shift days (58.0 against 60.5 to 61.4), and sleep score is highest on shift day 2 at 86.6.*

What that does and does not mean:

- It does not mean shift days are restorative. The pairs are unbalanced, off day 2 has only seven nights, and the paired weeks cluster in May to July when he was training hardest.
- It does mean the obvious story ("shift days wreck my recovery") is not in this data, so we should not build a dashboard that implies it. Recovery conclusions wait for more paired nights, which he is now collecting.
- Shift-day sleep may simply be sleep at the station, where bedtime is earlier and more regular than at home with kids. That is a real explanation rather than a data error, and worth checking once there is more of it.

**Phase 6, make it live (2 hrs). DONE 2026-11-11.** Dockerfile and compose file, an in-process scheduler that syncs at 05:30 America/Denver with a catch-up run on startup when the last good sync is over 20 hours old, `/healthz` with a container healthcheck, `restart: unless-stopped`, and a README with screenshots. *Gate passed: the container is healthy, serves the page, runs a full sync inside itself, survives a restart, and the catch-up path was proven by planting an old sync row, restarting, and watching it fire.*

Three things this phase cost, worth keeping:

- **`uv sync` inside the image produced an editable `.pth` that the container ignored**, so the console script could not import the package even though the app worked from the work directory. Fixed by installing a real wheel (`--no-editable`) and by telling uv the project is a package (`[tool.uv] package = true`).
- **`docker compose stop` then `start` does not pick up a new image.** It restarted the old container, which sent me chasing a packaging bug that was already fixed. `docker compose create --force-recreate` before `start` is the move.
- **The scheduler lives in the web process on purpose.** One container, no cron daemon, and the catch-up run covers the laptop being asleep at 05:30, which it will be.

**Phase 7, coaching view (later).** A read-only weekly summary page that looks presentable. Note: the Personal API is single-user only. Real multi-athlete support means the free Business/Starter tier with push delivery, and that is a different architecture. Decide when we get there.

## 9. Repo layout

```
shiftload/
  .env                 (gitignored)
  PLAN.md
  pyproject.toml
  docker-compose.yml
  shiftload/
    cli.py             probe, sync, backfill, recompute, serve
    garmin.py          token, endpoints, retry
    store.py           SQLite schema and upserts
    fit.py             FIT parsing, TRIMP
    metrics.py         CTL, ATL, ACWR, monotony
    shifts.py          cycle math, calendar cross-check
    web/               routes, templates, vendored chart.js
  tests/
  data/                (gitignored) raw json and fit archive
```

## 10. Explicitly not in v1

Multi-athlete coaching, nutrition, weight trends, strength and cycling metrics, Apple Health or phone data, predictions or training recommendations, anything that writes back to Garmin.

## 11. Risks and the answer

- **Key expiry** (max 1 year): calendar reminder, and the app fails loudly with a clear message when the token call returns 401.
- **API shape changes**: all endpoint paths live in one module, so a change is one file.
- **History shortage**: covered by the account export fallback above.
- **FIT decoding surprises**: `fitdecode` handles the standard streams; if a field is missing we store what exists and never crash the sync.
- **No Training Readiness on the 255**: fine, our own CTL/ACWR numbers are the deliverable, not a Garmin number.
- **Motivation dip**: phases are short enough that each week ends with something visible on screen.

## 12. Phase status

| Phase | Status | Notes |
| --- | --- | --- |
| 0 | done 2026-11-10 | probe prints real activities |
| 1 | done 2026-11-10 | 37 activities, 240 wellness days, 374 shift days, idempotent |
| 2 | done 2026-11-10 | TRIMP for 37 activities, avg HR within 1.1 bpm of Garmin |
| 3 | done 2026-11-10 | CTL/ATL/ACWR/monotony/strain, 9 tests passing, hand check matched |
| 4 | done 2026-11-10 | load chart, ACWR chart, shift bands, 90/180/all ranges |
| 5 | done 2026-11-10 | recovery chart and cycle-position table, no shift penalty visible |
| 6 | done 2026-11-11 | container healthy, syncs at 05:30, survives restart, README has screenshots |

## 13. Quiz bank

Asked at the end of each phase, two or three at a time.

- Why does the app create a new access token every run instead of storing one?
- What happens if the sync is interrupted halfway through paging activities?
- Why do we page the activity list 99 at a time instead of asking for five years at once?
- What is the difference between CTL and ATL, in one sentence?
- Why does a shift day of heavy work push ATL up faster than CTL?
- Why is the shift calendar only a cross-check and not the source of truth?
- What does ACWR above 1.5 mean, and why does it not mean "stop running"?
- Why do we store the raw JSON and FIT files if the database already has the numbers?
- If the dashboard number and Garmin's training effect disagree, which do we trust, and why?
- What single file would we edit if Garmin changed an endpoint path?

## 14. What the data actually looks like (measured 2026-11-10)

Backfill ran from 2026-01-01. Coverage by month, days with data:

| Month | Wellness days | Sleep | HRV | Body Battery | Activities |
| --- | --- | --- | --- | --- | --- |
| Jan | 23 | 5 | 4 | 20 | 0 |
| Feb | 28 | 9 | 9 | 24 | 4 |
| Mar | 31 | 9 | 9 | 27 | 3 |
| Apr | 30 | 12 | 12 | 29 | 9 |
| May | 31 | 23 | 23 | 31 | 15 |
| Jun | 30 | 24 | 22 | 29 | 5 |
| Jul | 27 | 24 | 24 | 27 | 1 |
| Aug | 0 | 0 | 0 | 0 | 0 |
| Sep | 0 | 0 | 0 | 0 | 0 |
| Oct | 30 | 13 | 1 | 30 | 0 |
| Nov | 10 | 8 | 8 | 9 | 0 |

Three things this changes about the build:

- **August and September are empty.** No daily summaries at all. Whatever the watch was doing, it was not syncing.
- **The last recorded activity is 2026-07-25** (Standhope 60K Fail). Nothing after that, while steps and Body Battery keep coming in. So the load curves will be flat from August onward until activities resume.
- **Sleep and HRV are patchy before May.** Body Battery and steps are near complete for the same months. That points at nights without the watch rather than a data problem.

Consequence for Phase 3: CTL/ATL is still worth building, but the honest first chart is the Jan to Jul window plus whatever comes back. The recovery panel gets more value out of the wellness data because it is the only thing with real coverage in October and November.
