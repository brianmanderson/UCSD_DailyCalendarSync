# Coverage Sheet → Calendar Sync

A GitHub Actions workflow that keeps a personal calendar in sync with a shared
coverage/staffing spreadsheet. Once a day it:

1. Downloads a shared Google Sheet that has one tab per week.
2. Scans the tabs for **this week and the next two** for every task assigned
   to a set of initials (e.g. `BA`).
3. Makes the target calendar match the sheet — creating an event for every
   assignment that doesn't have one yet (using task-specific working hours,
   see [Event hours](#event-hours)), never duplicating events that already
   exist, and deleting events for coverage tasks you are no longer assigned
   (see [How the calendar is kept in sync](#how-the-calendar-is-kept-in-sync)).

It runs entirely inside GitHub Actions on a schedule, so nothing has to run on
your own machine. All credentials and personal settings live in GitHub Actions
**Secrets**, so the repository itself contains nothing private and can be
shared or forked as-is.

Supported calendar targets:

- **Google Calendar** (default) — via a Google Cloud service account.
- **Outlook / Microsoft 365** — via Microsoft Graph, or by simply subscribing
  to the synced Google Calendar (see [Syncing to Outlook](#syncing-to-outlook)).

## Expected spreadsheet layout

Each weekly tab must follow this layout:

|       | A (section)    | B (task)  | C … I (Mon … Sun)  | K and beyond |
| ----- | -------------- | --------- | ------------------ | ------------ |
| **1** |                |           | dates              | ignored      |
| **2** |                |           | weekday names      | ignored      |
| **3+**| section header | task name | assignee initials  | ignored      |

- One tab per week. Cell **C1** must hold that week's **Monday** date — tabs
  are found by this cell's value, not by the tab's name.
- Row 1, columns C–I hold the seven dates; row 2 holds the weekday names.
- Column B holds the coverage task name (e.g. "Clinic Primary", "Secondary");
  cells C–I in that row hold the initials of whoever is assigned that day.
- Column A holds section headers. Everything at or below a section whose
  header starts with **"Resident"** is ignored, as are columns K and beyond
  (legends, notes).
- Only cells that exactly equal the configured initials (trimmed,
  case-insensitive) count as assignments.

## Setup — Google Calendar (default)

### 1. Create a Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   create a project (or reuse one).
2. Under **APIs & Services → Library**, enable the **Google Drive API** and
   the **Google Calendar API**.
3. Under **IAM & Admin → Service Accounts**, create a service account. No
   roles are needed.
4. Open the service account → **Keys → Add key → Create new key → JSON** and
   download the key file. You'll paste its entire contents into a GitHub
   secret in step 3, then delete the local copy.
5. Note the service account's email address (looks like
   `something@your-project.iam.gserviceaccount.com`).

### 2. Share the sheet and the calendar with the service account

1. **The coverage sheet:** open it in Google Sheets → **Share** → add the
   service account's email as a **Viewer**.
2. **The target calendar:** open [Google Calendar](https://calendar.google.com)
   → hover the calendar → **Settings and sharing** → under **Share with
   specific people or groups**, add the service account's email with
   **"Make changes to events"**.
3. While you're in the calendar's settings, scroll to **Integrate calendar**
   and copy the **Calendar ID** (looks like `…@group.calendar.google.com`).

### 3. Add the GitHub secrets

In your fork/copy of this repo: **Settings → Secrets and variables → Actions →
New repository secret**. Add:

| Secret                        | Required | Example / notes                                                        |
| ----------------------------- | -------- | ---------------------------------------------------------------------- |
| `SHEET_ID`                    | yes      | The ID from the sheet's URL: `docs.google.com/spreadsheets/d/<THIS>/edit` |
| `COVERAGE_INITIALS`           | yes      | e.g. `BA` — the initials to look for in the grid                        |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes      | Paste the **entire contents** of the downloaded JSON key file           |
| `GOOGLE_CALENDAR_ID`          | yes*     | The Calendar ID from step 2.3 (recommended)                             |
| `GOOGLE_CALENDAR_NAME`        | no*      | Alternative to the ID — see note below                                  |

*One of `GOOGLE_CALENDAR_ID` / `GOOGLE_CALENDAR_NAME` is required. Prefer the
ID: a calendar shared with a service account does **not** automatically appear
in the service account's calendar list, so lookup by name usually fails unless
the calendar was explicitly added to that list. The ID always works.

Optional settings (all have defaults):

| Secret              | Default               | Purpose                                                             |
| ------------------- | --------------------- | ------------------------------------------------------------------- |
| `CALENDAR_PROVIDER` | `google`              | `google` or `outlook`                                               |
| `EVENT_TIMEZONE`    | `America/Los_Angeles` | IANA timezone for "today", event times, and duplicate checks        |
| `EVENT_START`       | `05:30`               | Fallback start time for tasks no hour rule matches (24-hour `HH:MM`, local to `EVENT_TIMEZONE`) |
| `EVENT_END`         | `07:30`               | Fallback end time                                                   |
| `TASK_HOURS`        | *(empty)*             | Semicolon-separated `Task Substring=HH:MM-HH:MM` rules that override the built-in hour rules — see [Event hours](#event-hours) |
| `TITLE_RENAMES`     | `External Beam=Hillcrest;All Clinic=Encinitas` | Semicolon-separated `Sheet Task=Event Title` pairs — renames a task before it becomes an event title. Set to `-` to disable the defaults. |
| `PRUNE_REMOVED`     | `true`                | Delete events for coverage tasks you're no longer assigned. Set to `false` to make the sync add-only. |

### Event hours

Each event's start/end times come from the assignment's task name (and its
column-A section, treated as the site), checked in this order — first match
wins:

| Rule (case-insensitive)                             | Hours         |
| --------------------------------------------------- | ------------- |
| Task contains `hdr` and the word `am`                | 07:00–13:00   |
| Task contains `hdr` and the word `pm`                | 13:00–16:00   |
| Task contains `hdr` (anything else)                  | 07:00–16:00   |
| Task contains `external beam`                        | 07:00–16:00   |
| Section contains `enc` (Encinitas)                   | 07:00–16:00   |
| Task contains `primary` at Hillcrest (`hillcrest`/`hc` in section or task) | 06:30–16:00 |
| Task contains `primary` (anywhere else)              | 06:00–13:00   |
| Task contains `secondary`                            | 08:30–16:30   |
| Task contains `late`                                 | 13:00–19:00   |
| Task contains `plan check`                           | 08:30–16:30   |
| Task contains `ethos`                                | 08:30–16:30   |
| Anything else                                        | `EVENT_START`–`EVENT_END` (default 05:30–07:30) |

To override or extend these without editing code, set the `TASK_HOURS` secret
to semicolon-separated `Task Substring=HH:MM-HH:MM` rules. They are matched
against the task name in the order given, **before** the built-in rules, e.g.:

```
Secondary=09:00-17:00;Plan Check=08:00-16:00
```

(Hours are only applied when an event is created; already-existing events are
never modified.)

### 4. Test it

Go to the repo's **Actions** tab → **Sync coverage to calendar** →
**Run workflow**, and tick **dry run** for the first attempt. The run's
summary shows every assignment found and whether an event would be created or
already exists. When it looks right, run it again without dry run.

### 5. Schedule

The workflow runs daily at `0 1 * * *` (01:00 UTC = 5 PM Pacific in winter,
6 PM during daylight saving). Edit the `cron` line in
[.github/workflows/sync-coverage.yml](.github/workflows/sync-coverage.yml) to
change it — remember GitHub cron is always **UTC**.

Two GitHub quirks worth knowing:

- Scheduled runs can start several minutes late during busy periods.
- GitHub **disables scheduled workflows after ~60 days with no repository
  activity** and emails you first. Any commit (or re-enabling from the Actions
  tab) keeps it alive.

## Syncing to Outlook

### Option A — subscribe to the Google Calendar (no extra setup)

If you already sync to a Google Calendar, the easiest way to see it in Outlook
is to subscribe:

1. In Google Calendar → the calendar's **Settings and sharing** →
   **Integrate calendar** → copy the **Secret address in iCal format**.
2. In Outlook: **Add calendar → Subscribe from web** and paste the URL.

Caveat: Outlook refreshes internet-calendar subscriptions on its own schedule
(anywhere from a few hours up to about a day), so changes are not instant.
This works with any Outlook account, including personal `@outlook.com` ones.

### Option B — write events directly via Microsoft Graph

The sync script can create events straight in an Outlook / Microsoft 365
calendar. This requires a **work or school (Microsoft 365) tenant** — personal
`@outlook.com` / `@hotmail.com` accounts cannot grant the unattended
app-only permission this uses; for those, use Option A.

1. Go to [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID →
   App registrations → New registration** (single tenant is fine).
2. In the app: **Certificates & secrets → New client secret**. Copy the secret
   **value** immediately (it's shown only once).
3. **API permissions → Add a permission → Microsoft Graph → Application
   permissions → `Calendars.ReadWrite`**, then click **Grant admin consent**
   (requires a tenant admin).
4. Strongly recommended: scope the app to just the one mailbox with an
   [application access policy](https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access)
   (`New-ApplicationAccessPolicy` in Exchange Online PowerShell). Otherwise
   the app permission covers every mailbox in the tenant.
5. Add these GitHub secrets:

| Secret              | Required | Notes                                                        |
| ------------------- | -------- | ------------------------------------------------------------ |
| `CALENDAR_PROVIDER` | yes      | Set to `outlook`                                             |
| `MS_TENANT_ID`      | yes      | From the app registration's **Overview** page                |
| `MS_CLIENT_ID`      | yes      | "Application (client) ID" on the same page                   |
| `MS_CLIENT_SECRET`  | yes      | The secret value from step 2                                 |
| `MS_CALENDAR_USER`  | yes      | Email/UPN of the mailbox whose calendar to write to          |
| `MS_CALENDAR_NAME`  | no       | A named calendar in that mailbox; defaults to the primary calendar |

The **source is still a Google Sheet**, so the workflow also needs read access
to it — either keep `GOOGLE_SERVICE_ACCOUNT_JSON` + `SHEET_ID` set (the
service account only needs Viewer access to the sheet; no calendar sharing
required), or make the sheet link-shared ("Anyone with the link can view"),
in which case only `SHEET_ID` is needed.

## How the calendar is kept in sync

The script lists the target calendar's events for each day the sheet covers
and compares titles (trimmed, case-insensitive).

**Duplicates.** If an event with the same title already exists that day, it is
skipped. This makes the sync idempotent: re-running it — or running it every
day — never creates duplicates, and events you edit or events created by hand
with the same title are respected. Hours are only applied when an event is
created; existing events are never modified.

**Removals.** If the sheet changes and you're no longer assigned a task, the
event is deleted on the next run. Say you were down for Plan Checks on 7/28
and the sheet is edited that morning to give it to someone else — the next run
removes "Plan Checks" from your 7/28.

Deletion is deliberately narrow. An event is only a candidate if **its title
matches a task name in column B of that week's tab** (either as written or
after `TITLE_RENAMES`). Everything else on your calendar — meetings, PTO,
appointments — is invisible to the sync, because those titles never appear on
the sheet. Three more guards:

- **Only today and later.** Past days are never touched, so the calendar keeps
  its record of what you actually covered even if the sheet is edited
  retroactively.
- **Only weeks that parsed.** If a week's tab is missing, that week is skipped
  entirely rather than treated as "you have no assignments" — a missing tab
  can't wipe a week of events.
- **Nothing at all in `DRY_RUN`.** Dry runs report `would delete` and stop
  there.

The caveat: an event of your own that happens to be titled exactly like a
coverage task (e.g. a personal event named "Secondary") *will* be deleted on a
day you aren't assigned it. Rename it, or set `PRUNE_REMOVED` to `false` to
make the sync add-only.

## Running locally

```bash
pip install -r requirements.txt

# macOS / Linux
export SHEET_ID="..." COVERAGE_INITIALS="BA" \
       GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)" \
       GOOGLE_CALENDAR_ID="...@group.calendar.google.com"
DRY_RUN=true python sync.py
```

```powershell
# Windows PowerShell
$env:SHEET_ID = "..."
$env:COVERAGE_INITIALS = "BA"
$env:GOOGLE_SERVICE_ACCOUNT_JSON = Get-Content service-account.json -Raw
$env:GOOGLE_CALENDAR_ID = "...@group.calendar.google.com"
$env:DRY_RUN = "true"
python sync.py
```

You can also test just the spreadsheet parsing against a downloaded `.xlsx`:

```bash
python parse_coverage.py coverage.xlsx BA            # uses today's date
python parse_coverage.py coverage.xlsx BA 2026-07-16 # pretend it's this date
```

## Troubleshooting

- **"the sheet download did not return an .xlsx file"** — `SHEET_ID` is wrong,
  or the sheet isn't shared with the service account (or link-shared when
  running without Google credentials).
- **HTTP 403/404 creating events (Google)** — the calendar isn't shared with
  the service account with "Make changes to events", or `GOOGLE_CALENDAR_ID`
  is wrong.
- **"no tab whose first date cell (C1) is …"** — the weekly tab for that
  Monday doesn't exist yet or C1 doesn't contain a real date value. This is
  reported as a warning, not a failure. Expect it routinely for the later
  weeks: the sync looks three weeks out, and the sheet is often only filled in
  a week or two ahead. Nothing is created or deleted for a week that's
  missing.
- **Graph 403 (Outlook)** — admin consent wasn't granted, or an application
  access policy blocks the mailbox.
- **Nothing found for your initials** — cells must match exactly (aside from
  spaces/case); check for stray characters in the grid cells.
