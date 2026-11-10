"""Garmin Connect Personal API client.

Two rules this file exists to enforce:
  * mint a fresh access token per run (they last 24 hours, there are no refresh tokens)
  * retry politely, because per-endpoint rate limits are not published
"""

import json
import time
from datetime import date, timedelta

import httpx

from . import config

BASE = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"


class GarminError(RuntimeError):
    pass


class Garmin:
    def __init__(self, key: str | None = None, timeout: float = 90.0):
        self.key = (key or config.GARMIN_API_KEY).strip()
        if not self.key:
            raise GarminError("GARMIN_API_KEY is not set. Put it in .env at the project root.")
        self._token: str | None = None
        self.client = httpx.Client(timeout=timeout)

    # ---------- auth ----------

    def token(self) -> str:
        if self._token:
            return self._token
        r = self.client.post(BASE + TOKEN_PATH, headers={"Authorization": f"ApiKey {self.key}"})
        if r.status_code == 401:
            raise GarminError(
                "Garmin rejected the API key. It has expired or been revoked. "
                "Mint a new one at https://connect.garmin.com/app/settings/personalApiToken"
            )
        r.raise_for_status()
        # The endpoint answers with the raw JWT as text/plain, not a JSON envelope.
        self._token = r.text.strip()
        if not self._token:
            raise GarminError("token endpoint returned an empty body")
        return self._token

    # ---------- transport ----------

    def get(self, path: str, params: dict | None = None) -> bytes:
        refreshed = False
        for attempt in range(4):
            r = self.client.get(
                BASE + path,
                params=params,
                headers={"Authorization": f"Bearer {self.token()}"},
            )
            if r.status_code == 200:
                return r.content
            if r.status_code == 401 and not refreshed:
                self._token = None  # token aged out mid-run, mint once more
                refreshed = True
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise GarminError(f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        raise GarminError(f"GET {path} failed after retries")

    def get_json(self, path: str, params: dict | None = None):
        return json.loads(self.get(path, params))

    # ---------- endpoints ----------

    def activity_page(self, start: int, limit: int = 99) -> list[dict]:
        return self.get_json(
            "/activitylist-service/activities/search/activities",
            {"start": start, "limit": limit},
        )

    def daily_summaries(self, start: date, end: date) -> list[dict]:
        # documented ceiling is one year per call
        payload = self.get_json(
            "/usersummary-service/v2/dailysummaries",
            {"startDate": start.isoformat(), "endDate": end.isoformat()},
        )
        return payload.get("data", [])

    def sleep_summaries(self, start: date, end: date) -> list[dict]:
        # 31 days per call maximum, so the caller loops windows
        payload = self.get_json(
            "/sleep-service/sleep/dailySleepSummariesByDate",
            {"startDate": start.isoformat(), "endDate": end.isoformat()},
        )
        if isinstance(payload, dict):
            return payload.get("sleepSummaries") or payload.get("data") or []
        return payload

    def hrv_range(self, start: date, end: date) -> list[dict]:
        payload = self.get_json(f"/hrv-service/hrv/daily/{start.isoformat()}/{end.isoformat()}")
        return payload.get("hrvSummaries", [])

    def fit_file(self, activity_id: int) -> bytes:
        return self.get(f"/download-service/files/activity/{activity_id}")


def iter_windows(start: date, end: date, days: int):
    """Yield inclusive date windows of at most `days` days."""
    d = start
    while d <= end:
        stop = min(d + timedelta(days=days - 1), end)
        yield d, stop
        d = stop + timedelta(days=1)
