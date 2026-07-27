#!/usr/bin/env python3
"""Sync one person's coverage-sheet assignments into a calendar.

Downloads a shared Google Sheet (one tab per week), finds every task assigned
to the configured initials for this week and the next two, and makes the target
calendar match: missing assignments are created, and events for tasks we are
no longer assigned are deleted. Events with the same title already on that
date are left alone, so re-running never creates duplicates. Only titles that
appear as task names on the sheet are ever deleted — unrelated events on the
same day are untouched.

All configuration comes from environment variables (GitHub Actions secrets) —
see README.md for the full list.
"""

import datetime
import os
import json
import re
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
DEFAULT_RENAMES = "External Beam=Hillcrest;All Clinic=Encinitas;LJ & HC On-Call=On Call"
ALL_DAY = (None, None)  # sentinel returned instead of (start, end) for all-day shifts


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


def _parse_hour_rules(raw):
    """Parse 'Task Substring=HH:MM-HH:MM;...' into ordered (substring, hours)
    rules. The hours may also be 'all day' for a shift that covers the day."""
    rules = []
    for pair in raw.split(";"):
        if not pair.strip():
            continue
        problem = ConfigError(
            f"TASK_HOURS entry {pair.strip()!r} must look like 'Task=HH:MM-HH:MM' or 'Task=all day'"
        )
        if "=" not in pair:
            raise problem
        substring, hours = pair.split("=", 1)
        if not substring.strip():
            raise problem
        if hours.strip().lower().replace("-", " ") == "all day":
            rules.append((substring.strip().lower(), ALL_DAY))
            continue
        match = re.fullmatch(r"\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*", hours)
        if not match:
            raise problem
        rules.append((substring.strip().lower(), (match.group(1).zfill(5), match.group(2).zfill(5))))
    return rules


def builtin_hours(task, section):
    """Task/site-specific shift hours, ported from ScheduleToOutlook's GetActualHours."""
    task = task.lower()
    section = (section or "").lower()
    if "on-call" in task or "on call" in task:
        return ALL_DAY  # on-call covers the whole day, not a shift
    if "hdr" in task:
        # Word boundaries so "beam"/"pm" inside other words don't match
        if re.search(r"\bam\b", task):
            return ("07:00", "13:00")  # Brachy AM
        if re.search(r"\bpm\b", task):
            return ("13:00", "16:00")  # Brachy PM
        return ("07:00", "16:00")  # Brachy
    if "external beam" in task:
        return ("07:00", "16:00")  # Hillcrest external beam
    if "enc" in section:
        return ("07:00", "16:00")  # Encinitas
    if "primary" in task:
        # HC (Hillcrest) Primary keeps longer hours than other sites
        hillcrest = (
            "hillcrest" in section
            or re.search(r"\bhc\b", section)
            or re.search(r"\bhc\b", task)
        )
        return ("06:30", "16:00") if hillcrest else ("06:00", "13:00")
    if "secondary" in task:
        return ("08:30", "16:30")
    if "late" in task:
        return ("13:00", "19:00")
    if "plan check" in task:
        return ("08:30", "16:30")
    if "ethos" in task:
        return ("08:30", "16:30")
    return None


def event_hours(cfg, task, section):
    """Return (start, end) for an assignment — or ALL_DAY: TASK_HOURS
    overrides, then the built-in rules, then the EVENT_START/EVENT_END
    fallback."""
    task_lower = task.lower()
    for substring, hours in cfg.hour_rules:
        if substring in task_lower:
            return hours
    builtin = builtin_hours(task, section)
    if builtin is None:
        return (cfg.event_start, cfg.event_end)
    return builtin  # may be ALL_DAY, which is falsey-looking but meaningful


def hours_label(hours):
    return "all day" if hours == ALL_DAY else f"{hours[0]}-{hours[1]}"


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
        self.event_start = _env("EVENT_START", "05:30")
        self.event_end = _env("EVENT_END", "07:30")
        self.renames = _parse_renames(_env("TITLE_RENAMES", DEFAULT_RENAMES))
        self.hour_rules = _parse_hour_rules(_env("TASK_HOURS"))
        self.dry_run = _env("DRY_RUN").lower() in ("1", "true", "yes")
        self.prune = _env("PRUNE_REMOVED", "true").lower() not in ("0", "false", "no")
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

    def existing_events(self, date):
        start = datetime.datetime.combine(date, datetime.time(0, 0), self.tz)
        end = datetime.datetime.combine(date, datetime.time(23, 59, 59), self.tz)
        events = {}
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
                summary = (event.get("summary") or "").strip()
                if summary:
                    events.setdefault(summary.lower(), []).append((event["id"], summary))
            page_token = body.get("nextPageToken")
            if not page_token:
                return events

    def create_event(self, title, date, start, end):
        tomorrow = date + datetime.timedelta(days=1)
        if start is None:  # all-day: Google uses bare dates, end is exclusive
            when = {"start": {"date": date.isoformat()}, "end": {"date": tomorrow.isoformat()}}
        else:
            when = {
                "start": {
                    "dateTime": f"{date.isoformat()}T{start}:00",
                    "timeZone": self.cfg.timezone,
                },
                "end": {
                    "dateTime": f"{date.isoformat()}T{end}:00",
                    "timeZone": self.cfg.timezone,
                },
            }
        response = self.session.post(
            f"{self.calendar_path}/events", json={"summary": title, **when}, timeout=30
        )
        _check(response, f"creating event {title!r} on {date}")
        return response.json().get("id")

    def delete_event(self, event_id, title, date):
        response = self.session.delete(
            f"{self.calendar_path}/events/{urllib.parse.quote(event_id, safe='')}", timeout=30
        )
        if response.status_code in (404, 410):
            return  # already gone — nothing to do
        _check(response, f"deleting event {title!r} on {date}")


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

    def existing_events(self, date):
        start = datetime.datetime.combine(date, datetime.time(0, 0), self.tz)
        end = datetime.datetime.combine(date, datetime.time(23, 59, 59), self.tz)
        events = {}
        url = f"{self.calendar_path}/calendarView"
        params = {
            "startDateTime": start.isoformat(),
            "endDateTime": end.isoformat(),
            "$select": "id,subject",
            "$top": 100,
        }
        while url:
            response = self.session.get(url, params=params, timeout=30)
            _check(response, f"listing events on {date}")
            body = response.json()
            for event in body.get("value", []):
                subject = (event.get("subject") or "").strip()
                if subject:
                    events.setdefault(subject.lower(), []).append((event["id"], subject))
            url = body.get("@odata.nextLink")
            params = None
        return events

    def create_event(self, title, date, start, end):
        body = {"subject": title}
        if start is None:
            # Graph all-day events must run midnight to midnight; end is exclusive.
            tomorrow = date + datetime.timedelta(days=1)
            body["isAllDay"] = True
            start_at, end_at = f"{date.isoformat()}T00:00:00", f"{tomorrow.isoformat()}T00:00:00"
        else:
            start_at, end_at = f"{date.isoformat()}T{start}:00", f"{date.isoformat()}T{end}:00"
        body["start"] = {"dateTime": start_at, "timeZone": self.cfg.timezone}
        body["end"] = {"dateTime": end_at, "timeZone": self.cfg.timezone}
        response = self.session.post(f"{self.calendar_path}/events", json=body, timeout=30)
        _check(response, f"creating event {title!r} on {date}")
        return response.json().get("id")

    def delete_event(self, event_id, title, date):
        response = self.session.delete(
            f"{self.calendar_path}/events/{urllib.parse.quote(event_id, safe='')}", timeout=30
        )
        if response.status_code == 404:
            return  # already gone — nothing to do
        _check(response, f"deleting event {title!r} on {date}")


def sheet_titles(cfg, week):
    """Every event title this week's tab could produce — each task name as
    written, plus its renamed form, so titles left behind by an older
    TITLE_RENAMES setting are still recognised as ours."""
    titles = set()
    for task in week["tasks"]:
        titles.add(task.strip().lower())
        titles.add(cfg.renames.get(task.lower(), task).strip().lower())
    return titles


def prune_removed(cfg, target, data, today, events_on, assigned_titles, warnings):
    """Delete events for tasks we are no longer assigned.

    An event is only a candidate if its title matches a task name on that
    week's tab, so anything the sheet never mentions — meetings, PTO, personal
    events — is left alone. Days before today are left alone as well: the
    calendar keeps its record of what actually happened.
    """
    rows = []
    for week in data["weeks"]:
        if week.get("error"):
            continue  # tab missing — we have no idea what belongs on those days
        known = sheet_titles(cfg, week)
        for day in week["days"]:
            date = datetime.date.fromisoformat(day["date"])
            if date < today:
                continue
            keep = assigned_titles.get(date, set())
            for key, events in sorted(events_on(date).items()):
                if key not in known or key in keep:
                    continue
                for event_id, title in events:
                    if cfg.dry_run:
                        status = "would delete (dry run)"
                    else:
                        try:
                            target.delete_event(event_id, title, date)
                            status = "deleted"
                        except RuntimeError as exc:
                            warnings.append(str(exc))
                            status = "delete failed"
                    rows.append((week["label"], day["date"], day["day"], title, "-", status))
    return rows


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
        for _label, date, day, title, hours, status in rows:
            print(f"  {date} {day:<4} {title:<{width}}  {hours}  {status.upper()}")
    else:
        print("  nothing to sync")
    for message in warnings:
        print(f"::warning::{message}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"## Coverage sync — {today.isoformat()}\n\n")
            if rows:
                fh.write("| Week | Date | Day | Event | Hours | Status |\n")
                fh.write("|---|---|---|---|---|---|\n")
                for label, date, day, title, hours, status in rows:
                    fh.write(f"| {label} | {date} | {day} | {title} | {hours} | {status} |\n")
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
    events_by_date = {}
    assigned_titles = {}

    def events_on(date):
        if date not in events_by_date:
            events_by_date[date] = target.existing_events(date)
        return events_by_date[date]

    for week in data["weeks"]:
        if week.get("error"):
            warnings.append(f"{week['label']} (Monday {week['monday']}): {week['error']}")
            continue
        for assignment in week["assignments"]:
            title = cfg.renames.get(assignment["task"].lower(), assignment["task"])
            date = datetime.date.fromisoformat(assignment["date"])
            key = title.strip().lower()
            hours = event_hours(cfg, assignment["task"], assignment.get("section"))
            assigned_titles.setdefault(date, set()).add(key)
            if key in events_on(date):
                status = "already exists"
            elif cfg.dry_run:
                status = "would create (dry run)"
            else:
                event_id = target.create_event(title, date, *hours)
                events_on(date).setdefault(key, []).append((event_id, title))
                status = "created"
            rows.append(
                (week["label"], assignment["date"], assignment["day"], title,
                 hours_label(hours), status)
            )

    if cfg.prune:
        rows.extend(prune_removed(cfg, target, data, today, events_on, assigned_titles, warnings))

    rows.sort(key=lambda row: (row[1], row[3]))
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
