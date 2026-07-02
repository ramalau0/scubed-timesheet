# S-Cubed Timesheet Bot

Automatically creates weekly timesheets on the S-Cubed portal (`dcxconnect.datacentrix.co.za`).

Per-day comments are generated from:
- **Outlook calendar** — your meetings for that day (via Outlook Web)
- **Claude CLI history** — which work projects you were active on

---

## Requirements

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed (for project history enrichment)
- Access to S-Cubed and Outlook with your Datacentrix credentials

---

## Setup

**1. Clone and create a virtual environment**

```bash
git clone <repo-url>
cd scubed-timesheet
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

**2. Configure your `.env`**

```bash
cp .env.example .env
```

Leave the IDs blank for now — the `discover` command will find them. There's no username/password to set: the first run opens a browser window for you to complete login and 2FA manually, and the session is cached after that (see [Browser behaviour](#browser-behaviour)).

**3. Discover your IDs**

```bash
python timesheet_bot.py discover
```

A browser will open and log in to S-Cubed. Copy the printed IDs into your `.env`:

```env
EMPLOYEE_ID=12345
CLIENT_ID=10
PROJECT_ID=20
ACTIVITY_ID=5
DESIGNATION_ID=3
```

**4. Authenticate with Outlook** (first run only)

The first time you run `create`, a browser will open for Outlook. Log in with your Datacentrix Okta credentials. After that the session is cached and no browser window appears.

---

## Usage

**Create the most recently completed week's timesheet:**

```bash
python timesheet_bot.py create
```

`create` never targets a week that hasn't finished yet — it always fills Mon–Fri of the most recent week that has already ended (relative to today), even if you run it mid-week. Run it on or after the Monday following the week you want to log.

**Create for a specific past week (any date in that week):**

```bash
python timesheet_bot.py create 2026-06-09
```

**Re-run the ID discovery:**

```bash
python timesheet_bot.py discover
```

---

## How comments are generated

For each working day the bot combines:

1. Your **calendar meetings** — pulled from Outlook Web for that day
2. Your **Claude CLI projects** — which repos under `WORK_DIR` you had open

It produces a sentence like:

> Attended Morning Brief, GoTurbo Daily Meet and Daily Check-In. Participated in DaaS(rebuild). Worked on goturbo-platform.

If nothing is found for a day it falls back to the `WEEKLY_COMMENT` value in `.env`.

---

## `.env` reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `EMPLOYEE_ID` | Yes | — | Run `discover` to find this |
| `CLIENT_ID` | Yes | — | Run `discover` to find this |
| `PROJECT_ID` | Yes | — | Run `discover` to find this |
| `ACTIVITY_ID` | Yes | — | Run `discover` to find this |
| `DESIGNATION_ID` | Yes | — | Run `discover` to find this |
| `ENTITY_ID` | No | `1` | Usually 1, change if discover shows otherwise |
| `HOURS_PER_DAY` | No | `8` | Hours logged per day |
| `WEEKLY_COMMENT` | No | `Regular weekly hours` | Fallback comment when no data found |
| `SUBMIT_AFTER_SAVE` | No | `false` | Set to `true` to auto-submit for approval |
| `USE_CALENDAR` | No | `true` | Set to `false` to skip Outlook and use Claude history only |
| `WORK_DIR` | No | `./` | Path to your work projects folder for Claude history matching. Place the script in your work root, or set this explicitly. |

---

## Browser behaviour

- **Sessions cached** — runs fully headless, no window appears
- **Session expired** — browser opens only for the login step, then closes
- Sessions are saved in `session.json` (S-Cubed) and `outlook_session.json` (Outlook). Delete either file to force re-authentication.

---

## Automating with cron

`create` always fills the most recently *completed* week (Mon–Sun), never the one still in progress. Schedule **only one** cron job — running two on the same calendar week will both resolve to the same target week and create duplicate entries, since there's no dedup check yet.

`cron_weekly.sh` in this repo runs every Monday at 08:00, logging the week that ended the day before:

```bash
crontab -e
```

Add:

```
0 8 * * 1 /path/to/scubed-timesheet/cron_weekly.sh
```

Prefer Friday afternoon instead? Use the same script/command on a Friday schedule — it logs the same "most recently completed week" either way — just don't run both schedules at once:

```
0 16 * * 5 /path/to/scubed-timesheet/cron_weekly.sh
```
