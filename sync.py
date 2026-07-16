#!/usr/bin/env python3
"""Sync one person's coverage-sheet assignments into a calendar.

Downloads a shared Google Sheet (one tab per week), finds every task assigned
to the configured initials for this week and next week, and makes sure a
matching event exists on the target calendar for each assignment. Events with
the same title already on that date are left alone, so re-running never
creates duplicates.

All configuration comes from environment variables (GitHub Actions secrets) —
see README.md for the full list.
"""

import datetime
import os
import json
import sys
import tempfile
import traceback
import urllib.parse
from zoneinfo import ZoneInfo

import requests

from parse_coverage import parse_assignments

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar",
]


class ConfigError(Exception):
    pass


def _env(name, default=""):
    return os.environ.get(name, "").strip() or default


def _require(name):
    value = _env(name)
    if not value:
        raise ConfigError(f"missing required secret/environment variable: {name}")
    return value


def _parse_renames(raw):
    """Parse 'Old Title=New Title;Other=Another' into a lookup dict."""
    renames = {}
    for pair in raw.split(";"):
        if "=" in pair:
            old, new = pair.split("=", 1)
            renames[old.strip().lower()] = new.strip()
    return renames


def _check(response, action):
    if response.status_code >= 300:
        raise RuntimeError(
            f"{action} failed with HTTP {response.status_code}: {response.text[:300]}"
        )


class Config:
    def __init__(self):
        self.provider = _env("CALENDAR_PROVIDER", "google").lower()
        self.sheet_id = _require("SHEET_ID")
        self.initials = _require("COVERAGE_INITIALS")
        self.timezone = _env("EVENT_TIMEZONE", "America/Los_Angeles")
        self.event_start = _env("EVENT_START", "06:00")
        self.event_end = _env("EVENT_END", "09:00")
        self.renames = _parse_renames(_env("TITLE_RENAMES"))
        self.dry_run = _env("DRY_RUN").lower() in ("1", "true", "yes")
        self.google_sa_json = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
        self.google_calendar_id = _env("GOOGLE_CALENDAR_ID")
        self.google_calendar_name = _env("GOOGLE_CALENDAR_NAME")
        self.ms_tenant_id = _env("MS_TENANT_ID")
        self.ms_client_id = _env("MS_CLIENT_ID")
        self.ms_client_secret = _env("MS_CLIENT_SECRET")
        self.ms_calendar_user = _env("MS_CALENDAR_USER")
        self.ms_calendar_name = _env("MS_CALENDAR_NAME")


def google_access_token(cfg):
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    info = json.loads(cfg.google_sa_json)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=GOOGLE_SCOPES
    )
    credentials.refresh(Request())
    return credentials.token


def download_sheet(cfg, token):
    """Download the coverage sheet as .xlsx and return the local file path."""
    if token:
        response = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{cfg.sheet_id}/export",
            params={"mimeType": XLSX_MIME},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
    else:
        # No Google credentials configured: works only if the sheet is
        # link-shared ("Anyone with the link can view").
        response = requests.get(
            f"https://docs.google.com/spreadsheets/d/{cfg.sheet_id}/export?format=xlsx",
            timeout=60,
        )
    _check(response, "downloading the coverage sheet")
    if not response.content.startswith(b"PK"):
        raise RuntimeError(
            "the sheet download did not return an .xlsx file — check that SHEET_ID "
            "is correct and that the sheet is shared with the service account "
            "(or link-shared, if no Google credentials are configured)"
        )
    path = os.path.join(tempfile.mkdtemp(), "coverage.xlsx")
    with open(path, "wb") as fh:
        fh.write(response.content)
    return path


class GoogleCalendar:
    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, cfg, token):
        if not token:
            raise ConfigError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is required when CALENDAR_PROVIDER is 'google'"
            )
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        calendar_id = cfg.google_calendar_id or self._resolve_name(cfg.google_calendar_name)
        self.calendar_path = f"{self.BASE}/calendars/{urllib.parse.quote(calendar_id)}"

    def _resolve_name(self, name):
        if not name:
            raise ConfigError("set GOOGLE_CALENDAR_ID (recommended) or GOOGLE_CALENDAR_NAME")
        response = self.session.get(
            f"{self.BASE}/users/me/calendarList", params={"maxResults": 250}, timeout=30
        )
        _check(response, "listing Google calendars")
        for item in response.json().get("items", []):
            if item.get("summary", "").strip().lower() == name.strip().lower():
                return item["id"]
        raise ConfigError(
            f"no calendar named {name!r} is visible to the service account. Service "
            "accounts usually cannot see shared calendars by name — set "
            "GOOGLE_CALENDAR_ID instead (Google Calendar > calendar settings > "
            "'Integrate calendar' > Calendar ID)"
        )

    def existing_titles(self, date):
        start = datetime.datetime.combine(date, datetime.time(0, 0), self.tz)
        end = datetime.datetime.combine(date, datetime.time(23, 59, 59), self.tz)
        titles = set()
        page_token = None
        while True:
            params = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "maxResults": 250,
            }
            if page_token:
                params["pageToken"] = page_token
            response = self.session.get(f"{self.calendar_path}/events", params=params, timeout=30)
            _check(response, f"listing events on {date}")
            body = response.json()
            for event in body.get("items", []):
                summary = (event.get("summary") or "").strip().lower()
                if summary:
                    titles.add(summary)
            page_token = body.get("nextPageToken")
            if not page_token:
                return titles

    def create_event(self, title, date):
        body = {
            "summary": title,
            "start": {
                "dateTime": f"{date.isoformat()}T{self.cfg.event_start}:00",
                "timeZone": self.cfg.timezone,
            },
            "end": {
                "dateTime": f"{date.isoformat()}T{self.cfg.event_end}:00",
                "timeZone": self.cfg.timezone,
            },
        }
        response = self.session.post(f"{self.calendar_path}/events", json=body, timeout=30)
        _check(response, f"creating event {title!r} on {date}")


class OutlookCalendar:
    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, cfg):
        for name in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_CALENDAR_USER"):
            _require(name)
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        token_response = requests.post(
            f"https://login.microsoftonline.com/{cfg.ms_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": cfg.ms_client_id,
                "client_secret": cfg.ms_client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        _check(token_response, "acquiring a Microsoft Graph token")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token_response.json()['access_token']}"
        user = urllib.parse.quote(cfg.ms_calendar_user)
        if cfg.ms_calendar_name:
            self.calendar_path = self._resolve_name(user, cfg.ms_calendar_name)
        else:
            self.calendar_path = f"{self.BASE}/users/{user}/calendar"

    def _resolve_name(self, user, name):
        url = f"{self.BASE}/users/{user}/calendars"
        params = {"$top": 100}
        while url:
            response = self.session.get(url, params=params, timeout=30)
            _check(response, "listing Outlook calendars")
            body = response.json()
            for calendar in body.get("value", []):
                if calendar.get("name", "").strip().lower() == name.strip().lower():
                    return f"{self.BASE}/users/{user}/calendars/{calendar['id']}"
            url = body.get("@odata.nextLink")
            params = None
        raise ConfigError(
            f"no Outlook calendar named {name!r} found for {self.cfg.ms_calendar_user}"
        )

    def existing_titles(self, date):
        start = datetime.datetime.combine(date, datetime.time(0, 0), self.tz)
        end = datetime.datetime.combine(date, datetime.time(23, 59, 59), self.tz)
        titles = set()
        url = f"{self.calendar_path}/calendarView"
        params = {
            "startDateTime": start.isoformat(),
            "endDateTime": end.isoformat(),
            "$select": "subject",
            "$top": 100,
        }
        while url:
            response = self.session.get(url, params=params, timeout=30)
            _check(response, f"listing events on {date}")
            body = response.json()
            for event in body.get("value", []):
                subject = (event.get("subject") or "").strip().lower()
                if subject:
                    titles.add(subject)
            url = body.get("@odata.nextLink")
            params = None
        return titles

    def create_event(self, title, date):
        body = {
            "subject": title,
            "start": {
                "dateTime": f"{date.isoformat()}T{self.cfg.event_start}:00",
                "timeZone": self.cfg.timezone,
            },
            "end": {
                "dateTime": f"{date.isoformat()}T{self.cfg.event_end}:00",
                "timeZone": self.cfg.timezone,
            },
        }
        response = self.session.post(f"{self.calendar_path}/events", json=body, timeout=30)
        _check(response, f"creating event {title!r} on {date}")


def report(cfg, today, data, rows, warnings):
    print()
    print(f"Coverage sync — initials {cfg.initials}, {today.isoformat()} ({cfg.timezone})")
    for week in data["weeks"]:
        if week.get("error"):
            print(f"  {week['label']} (Monday {week['monday']}): {week['error']}")
        else:
            print(
                f"  {week['label']}: tab {week['sheet']!r}, "
                f"{len(week['assignments'])} assignment(s) for {cfg.initials}"
            )
    if rows:
        width = max(len(row[3]) for row in rows)
        for _label, date, day, title, status in rows:
            print(f"  {date} {day:<4} {title:<{width}}  {status.upper()}")
    else:
        print("  nothing to sync")
    for message in warnings:
        print(f"::warning::{message}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"## Coverage sync — {today.isoformat()}\n\n")
            if rows:
                fh.write("| Week | Date | Day | Event | Status |\n")
                fh.write("|---|---|---|---|---|\n")
                for label, date, day, title, status in rows:
                    fh.write(f"| {label} | {date} | {day} | {title} | {status} |\n")
            else:
                fh.write("No assignments found — nothing to sync.\n")
            if warnings:
                fh.write("\n")
                for message in warnings:
                    fh.write(f"> :warning: {message}\n")


def main():
    cfg = Config()
    today = datetime.datetime.now(ZoneInfo(cfg.timezone)).date()
    token = google_access_token(cfg) if cfg.google_sa_json else None

    xlsx_path = download_sheet(cfg, token)
    data = parse_assignments(xlsx_path, cfg.initials, today)

    if cfg.provider == "google":
        target = GoogleCalendar(cfg, token)
    elif cfg.provider == "outlook":
        target = OutlookCalendar(cfg)
    else:
        raise ConfigError(
            f"unknown CALENDAR_PROVIDER {cfg.provider!r}; use 'google' or 'outlook'"
        )

    rows = []
    warnings = []
    titles_by_date = {}
    for week in data["weeks"]:
        if week.get("error"):
            warnings.append(f"{week['label']} (Monday {week['monday']}): {week['error']}")
            continue
        for assignment in week["assignments"]:
            title = cfg.renames.get(assignment["task"].lower(), assignment["task"])
            date = datetime.date.fromisoformat(assignment["date"])
            if date not in titles_by_date:
                titles_by_date[date] = target.existing_titles(date)
            if title.strip().lower() in titles_by_date[date]:
                status = "already exists"
            elif cfg.dry_run:
                status = "would create (dry run)"
            else:
                target.create_event(title, date)
                titles_by_date[date].add(title.strip().lower())
                status = "created"
            rows.append((week["label"], assignment["date"], assignment["day"], title, status))

    report(cfg, today, data, rows, warnings)


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        print(f"::error::{exc}")
        sys.exit(1)
    except Exception as exc:
        traceback.print_exc()
        print(f"::error::{exc}")
        sys.exit(1)
