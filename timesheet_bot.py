#!/usr/bin/env python3
"""
S-Cubed Timesheet Automation
Automatically creates and optionally submits weekly timesheets on dcxconnect.datacentrix.co.za
Per-day comments are enriched from Outlook Web calendar events and Claude CLI project history.
"""

import json
import os
import sys

# Fix Windows console encoding for emoji/unicode characters
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, Browser


class AuthRequired(Exception):
    """Raised when a login page is detected in headless mode."""

load_dotenv()

BASE_URL   = "https://dcxconnect.datacentrix.co.za/SCUBED"
API_BASE   = f"{BASE_URL}/pages/tlc_api/Timesheet_Entries.aspx"
LOGIN_URL  = f"{BASE_URL}/scubed.aspx"
SESSION_FILE         = Path(__file__).parent / "session.json"
OUTLOOK_SESSION_FILE = Path(__file__).parent / "outlook_session.json"
CLAUDE_HISTORY_FILE  = Path.home() / ".claude" / "history.jsonl"

# ── Config from .env ──────────────────────────────────────────────────────────
USERNAME       = os.environ["SCUBED_USERNAME"]
PASSWORD       = os.environ["SCUBED_PASSWORD"]
CLIENT_ID      = int(os.environ["CLIENT_ID"])      if os.getenv("CLIENT_ID")      else None
PROJECT_ID     = int(os.environ["PROJECT_ID"])     if os.getenv("PROJECT_ID")     else None
ACTIVITY_ID    = int(os.environ["ACTIVITY_ID"])    if os.getenv("ACTIVITY_ID")    else None
DESIGNATION_ID = int(os.environ["DESIGNATION_ID"]) if os.getenv("DESIGNATION_ID") else None
EMPLOYEE_ID    = int(os.environ["EMPLOYEE_ID"])    if os.getenv("EMPLOYEE_ID")    else None
ENTITY_ID      = int(os.getenv("ENTITY_ID", "1"))
HOURS_PER_DAY  = float(os.getenv("HOURS_PER_DAY", "8"))
WEEKLY_COMMENT = os.getenv("WEEKLY_COMMENT", "Regular weekly hours")
SUBMIT_AFTER_SAVE = os.getenv("SUBMIT_AFTER_SAVE", "false").lower() == "true"
USE_CALENDAR   = os.getenv("USE_CALENDAR", "true").lower() == "true"
WORK_DIR       = Path(os.getenv("WORK_DIR", ".")).resolve()
# ─────────────────────────────────────────────────────────────────────────────

INSERT_TIMESTAMP = "AAAAAAAH954="  # constant the site uses for new entries

ENV_FILE = Path(__file__).parent / ".env"


def ids_configured() -> bool:
    return all([CLIENT_ID, PROJECT_ID, ACTIVITY_ID, DESIGNATION_ID, EMPLOYEE_ID])


def write_env(updates: dict[str, str]):
    """Update key=value pairs in .env and reload the module globals."""
    global CLIENT_ID, PROJECT_ID, ACTIVITY_ID, DESIGNATION_ID, EMPLOYEE_ID, ENTITY_ID

    # Create .env from example if it doesn't exist yet
    if not ENV_FILE.exists():
        example = Path(__file__).parent / ".env.example"
        ENV_FILE.write_text(example.read_text() if example.exists() else "")

    lines = ENV_FILE.read_text().splitlines()
    written = set()
    new_lines = []
    for line in lines:
        key = line.split("=")[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            new_lines.append(line)
    for key, val in updates.items():
        if key not in written:
            new_lines.append(f"{key}={val}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n")

    # Push into os.environ and reload globals
    for key, val in updates.items():
        os.environ[key] = val
    CLIENT_ID      = int(os.environ["CLIENT_ID"])      if os.getenv("CLIENT_ID")      else None
    PROJECT_ID     = int(os.environ["PROJECT_ID"])     if os.getenv("PROJECT_ID")     else None
    ACTIVITY_ID    = int(os.environ["ACTIVITY_ID"])    if os.getenv("ACTIVITY_ID")    else None
    DESIGNATION_ID = int(os.environ["DESIGNATION_ID"]) if os.getenv("DESIGNATION_ID") else None
    EMPLOYEE_ID    = int(os.environ["EMPLOYEE_ID"])    if os.getenv("EMPLOYEE_ID")    else None
    ENTITY_ID      = int(os.getenv("ENTITY_ID", "1"))


# ── Date helpers ───────────────────────────────────────────────────────────────

def week_ending_for(date: datetime) -> datetime:
    """Return the Sunday that ends the week containing `date`."""
    days_until_sunday = (6 - date.weekday()) % 7
    return date + timedelta(days=days_until_sunday)


def working_days(week_end: datetime) -> list[datetime]:
    """Return Mon–Fri dates for the week ending on `week_end`."""
    monday = week_end - timedelta(days=6)
    return [monday + timedelta(days=i) for i in range(5)]


def fmt(date: datetime) -> str:
    return date.strftime("%m-%d-%Y")


# ── Claude CLI history ─────────────────────────────────────────────────────────

def get_claude_projects_for_date(target_date: datetime) -> list[str]:
    """Return unique work-project names Claude was active on for the given date."""
    if not CLAUDE_HISTORY_FILE.exists():
        return []

    date_str = target_date.strftime("%Y-%m-%d")
    projects: set[str] = set()

    with open(CLAUDE_HISTORY_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
                ts = rec.get("timestamp", 0) / 1000
                if datetime.fromtimestamp(ts).strftime("%Y-%m-%d") != date_str:
                    continue
                project_path = Path(rec.get("project", ""))
                try:
                    project_path.relative_to(WORK_DIR)
                    name = project_path.name
                    if name and name != WORK_DIR.name:
                        projects.add(name)
                except ValueError:
                    pass
            except (json.JSONDecodeError, ValueError, OSError):
                pass

    return sorted(projects)


# ── Outlook Web calendar ───────────────────────────────────────────────────────

OUTLOOK_CAL_URL = "https://outlook.office.com/calendar/view/week"


async def fetch_outlook_calendar(browser: Browser, week_dates: list[datetime], headless: bool = False) -> dict[str, list[str]]:
    """
    Open Outlook Web in a separate browser context and scrape events for the week.
    Returns {fmt(date): [event_title, ...]} for each working day.
    Falls back to empty lists on any error so the rest of the flow continues.
    First run: browser window stays open so you can sign in manually.
    Subsequent runs: session is cached in outlook_session.json (headless).
    """
    empty = {fmt(d): [] for d in week_dates}

    try:
        outlook_state = (
            json.loads(OUTLOOK_SESSION_FILE.read_text())
            if OUTLOOK_SESSION_FILE.exists()
            else None
        )
        ctx = await browser.new_context(storage_state=outlook_state)
        page = await ctx.new_page()

        monday = min(week_dates)
        await page.goto(
            f"{OUTLOOK_CAL_URL}/{monday.strftime('%Y-%m-%d')}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(3000)

        # Handle sign-in redirect (Datacentrix routes through Okta SSO)
        if "login" in page.url or "microsoftonline" in page.url or "okta.com" in page.url:
            if headless:
                await ctx.close()
                raise AuthRequired("Outlook sign-in required")
            print("Outlook: please log in in the browser window…")
            await page.wait_for_function(
                "() => window.location.hostname === 'outlook.office.com'",
                timeout=300000,
            )
            await page.wait_for_timeout(3000)
            # Navigate to the calendar after landing on mail
            await page.goto(
                f"{OUTLOOK_CAL_URL}/{monday.strftime('%Y-%m-%d')}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(3000)

        # Persist refreshed session
        OUTLOOK_SESSION_FILE.write_text(json.dumps(await ctx.storage_state()))

        # Give the calendar grid time to render
        await page.wait_for_timeout(3000)

        # day-name → date string map for matching scraped results
        day_map = {d.strftime("%A").lower(): fmt(d) for d in week_dates}

        raw: dict[str, list[str]] = await page.evaluate("""
            () => {
                const events = {};
                const timeRe = /\\d+:\\d+/;
                const dayRe  = /\\b(monday|tuesday|wednesday|thursday|friday)\\b/i;

                // Outlook aria-label format (24h time):
                // "GoTurbo Daily Meet, 09:30 to 12:00, Monday, June 15, 2026, By ..., Busy"
                document.querySelectorAll('[role="button"][aria-label]').forEach(btn => {
                    const label = btn.getAttribute('aria-label') || '';
                    if (!timeRe.test(label) || label.length > 500) return;

                    if (/^canceled:/i.test(label)) return;

                    const dayMatch = label.match(dayRe);
                    const day   = dayMatch ? dayMatch[0].toLowerCase() : 'unknown';
                    const title = label.split(',')[0].trim();
                    if (!title || title.length < 2) return;

                    if (!events[day]) events[day] = [];
                    if (!events[day].includes(title)) events[day].push(title);
                });

                return events;
            }
        """)

        result = {fmt(d): [] for d in week_dates}
        for day_name, titles in raw.items():
            date_str = day_map.get(day_name)
            if date_str:
                result[date_str] = titles

        total = sum(len(v) for v in result.values())
        print(f"Calendar: found {total} event(s) across the week.")

        await ctx.close()
        return result

    except Exception as e:
        print(f"⚠️  Calendar fetch skipped ({e}); using Claude history only.")
        return empty


# ── Comment generation ─────────────────────────────────────────────────────────

# Keywords that flag a meeting as a routine standup/check-in rather than deep work
_STANDUP_KEYWORDS = {"standup", "stand-up", "brief", "check-in", "check in", "daily meet", "daily scrum"}

def _is_standup(title: str) -> bool:
    low = title.lower()
    return any(kw in low for kw in _STANDUP_KEYWORDS)


def _join_natural(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_day_comment(cal_events: list[str], projects: list[str]) -> str:
    """
    Combine meetings + Claude project history into one readable paragraph.
    Formula: standups sentence + work-meetings sentence + projects sentence.
    """
    sentences = []

    standups     = [e for e in cal_events if _is_standup(e)]
    work_meetings = [e for e in cal_events if not _is_standup(e)]

    if standups:
        sentences.append(f"Attended {_join_natural(standups)}.")

    if work_meetings:
        sentences.append(f"Participated in {_join_natural(work_meetings)}.")

    if projects:
        sentences.append(f"Worked on {_join_natural(projects)}.")

    comment = " ".join(sentences) if sentences else WEEKLY_COMMENT
    return comment[:200]  # guard against field length limit


# ── Timesheet entry ────────────────────────────────────────────────────────────

def build_entry(date: datetime, hours: float, comment: str = WEEKLY_COMMENT) -> dict:
    return {
        "Timesheet_EntryID": -1,
        "EmployeeID": EMPLOYEE_ID,
        "ClientID": CLIENT_ID,
        "ProjectID": PROJECT_ID,
        "ActivityID": ACTIVITY_ID,
        "DesignationID": DESIGNATION_ID,
        "WeekEnding": None,
        "EntryDate": fmt(date),
        "DayID": None,
        "Hours": hours,
        "InvoicedHours": 0,
        "EntryTimestamp": INSERT_TIMESTAMP,
        "Comment": json.dumps(comment),
        "Invoiced": False,
        "InvoiceNumber": "",
        "WriteOff": False,
        "WriteOffComment": "",
        "Timestamp": INSERT_TIMESTAMP,
        "ApprovalLevelID": 0,
        "LastApprovedByEmployeeID": 0,
        "LastApprovedOnDate": None,
        "RejectionCount": 0,
        "LastRejectedByEmployeeID": None,
        "LastRejectedOnDate": None,
        "LastRejectionReason": None,
        "Context": None,
    }


# ── S-Cubed auth ───────────────────────────────────────────────────────────────

async def login(context: BrowserContext, page: Page, headless: bool = False) -> bool:
    await page.goto(LOGIN_URL, wait_until="networkidle")
    await page.wait_for_timeout(1000)

    if "scubed.aspx" in page.url and await page.locator("#nav_weeks, #ifrm").count() > 0:
        print("Already logged in (session restored).")
        return True

    if headless:
        raise AuthRequired("S-Cubed login required")

    print("Logging in…")
    await page.locator(
        'input[name*="SystemAccount"], input[id*="SystemAccount"], input[type="text"]'
    ).first.fill(USERNAME)
    await page.locator('input[type="password"]').fill(PASSWORD)
    await page.locator(
        'input[value="Sign In"], a:has-text("Sign In"), button:has-text("Sign In")'
    ).first.click()
    await page.wait_for_timeout(3000)

    # If redirected to Microsoft OAuth, wait for user to complete MFA
    if "microsoftonline.com" in page.url or "login.microsoft" in page.url:
        print("Microsoft SSO detected - complete login/MFA in the browser...")
        await page.wait_for_url("**/scubed.aspx**", timeout=120000)
        await page.wait_for_timeout(2000)

    if "dcxconnect" in page.url and "scubed" in page.url:
        print("Login successful.")
        return True

    print("[WARN] Login failed. Check SCUBED_USERNAME / SCUBED_PASSWORD in .env")
    return False


async def save_session(context: BrowserContext):
    storage = await context.storage_state()
    SESSION_FILE.write_text(json.dumps(storage))
    print(f"Session saved → {SESSION_FILE}")


async def navigate_to_timesheets(page: Page):
    await page.locator('text=My Timesheets').first.click(timeout=60000)
    await page.wait_for_timeout(2000)


# ── Commands ───────────────────────────────────────────────────────────────────

def _pick(label: str, options: list[dict]) -> dict:
    """Print options and return the chosen one. Auto-picks if only one."""
    if len(options) == 1:
        print(f"  {label}: {options[0]['Text']} (auto-selected)")
        return options[0]
    print(f"\n  {label}:")
    for i, opt in enumerate(options, 1):
        print(f"    {i}) {opt['Text']}  (ID={opt['Value']})")
    while True:
        try:
            choice = int(input(f"  Pick {label} [1-{len(options)}]: "))
            if 1 <= choice <= len(options):
                return options[choice - 1]
        except (ValueError, EOFError):
            pass


async def discover_ids(page: Page, save: bool = False):
    """Discover IDs from S-Cubed. If save=True, writes results to .env."""
    await navigate_to_timesheets(page)
    iframe = page.frame(name="Content") or page.frames[1]

    emp_id, entity_id = await iframe.evaluate(
        "() => [SCUBED.Employee.ID, SCUBED.Employee.EntityID || 1]"
    )

    clients_raw = await iframe.evaluate(
        """async ([empId, entityId]) => {
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/GetEntityClients_DropDownList', {
                method:'POST', headers:{'Content-Type':'application/json; charset=utf-8'},
                body: JSON.stringify({EntityID: entityId, EmployeeID: empId}),
                credentials:'include'
            });
            return (await r.json()).d;
        }""",
        [emp_id, entity_id],
    )

    if not clients_raw:
        print("⚠️  No clients returned. Ask your S-Cubed admin to assign you to a client/project first.")
        return

    print(f"\nSetting up IDs for Employee {emp_id}…")
    client = _pick("Client", clients_raw)

    projects_raw = await iframe.evaluate(
        """async ([cid, empId]) => {
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/GetClientProjects_DropDownList', {
                method:'POST', headers:{'Content-Type':'application/json; charset=utf-8'},
                body: JSON.stringify({ClientID: cid, EmployeeID: empId}),
                credentials:'include'
            });
            return (await r.json()).d;
        }""",
        [client["Value"], emp_id],
    )
    if not projects_raw:
        print("⚠️  No projects found for that client.")
        return
    project = _pick("Project", projects_raw)

    activities_raw = await iframe.evaluate(
        """async ([pid, entityId]) => {
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/GetProjectActivities_DropDownList', {
                method:'POST', headers:{'Content-Type':'application/json; charset=utf-8'},
                body: JSON.stringify({ProjectID: pid, EntityID: entityId}),
                credentials:'include'
            });
            return (await r.json()).d;
        }""",
        [project["Value"], entity_id],
    )
    activity = _pick("Activity", activities_raw or [{"Value": "1", "Text": "Default"}])

    designations_raw = await iframe.evaluate(
        """async ([pid, empId, entityId]) => {
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/GetEmployeeDesignations_DropDownList', {
                method:'POST', headers:{'Content-Type':'application/json; charset=utf-8'},
                body: JSON.stringify({EntityID: entityId, EmployeeID: empId, ProjectID: pid}),
                credentials:'include'
            });
            return (await r.json()).d;
        }""",
        [project["Value"], emp_id, entity_id],
    )
    designation = _pick("Designation", designations_raw or [{"Value": "1", "Text": "Default"}])

    ids = {
        "EMPLOYEE_ID": str(emp_id),
        "ENTITY_ID":   str(entity_id),
        "CLIENT_ID":   str(client["Value"]),
        "PROJECT_ID":  str(project["Value"]),
        "ACTIVITY_ID": str(activity["Value"]),
        "DESIGNATION_ID": str(designation["Value"]),
    }

    print("\n── Discovered IDs ───────────────────────")
    for k, v in ids.items():
        print(f"  {k}={v}")

    if save:
        write_env(ids)
        print(f"\n✅ Saved to {ENV_FILE}")


async def create_week(page: Page, browser: Browser, target_date: datetime | None = None, headless: bool = False):
    """Create timesheet entries with per-day comments from calendar + Claude history."""
    date     = target_date or datetime.today()
    week_end = week_ending_for(date)
    days     = working_days(week_end)

    print(f"\nCreating timesheet for week ending {fmt(week_end)}")
    print(f"  Days: {', '.join(d.strftime('%a %d %b') for d in days)}")
    print(f"  Hours/day: {HOURS_PER_DAY}  →  Total: {HOURS_PER_DAY * 5}h")

    # Gather per-day enrichment
    cal_events: dict[str, list[str]] = {}
    if USE_CALENDAR:
        cal_events = await fetch_outlook_calendar(browser, days, headless=headless)

    claude_projects = {fmt(d): get_claude_projects_for_date(d) for d in days}

    # Build entries with per-day comments
    print()
    new_entries = []
    for d in days:
        date_str = fmt(d)
        comment  = build_day_comment(
            cal_events.get(date_str, []),
            claude_projects.get(date_str, []),
        )
        print(f"  {d.strftime('%a %d %b')}  →  {comment}")
        new_entries.append(build_entry(d, HOURS_PER_DAY, comment))

    await navigate_to_timesheets(page)
    iframe = page.frame(name="Content") or page.frames[1]

    result = await iframe.evaluate(
        """async ([newEntries]) => {
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/BatchTransactionTimesheet_Entry', {
                method: 'POST',
                headers: {'Content-Type': 'application/json; charset=utf-8'},
                body: JSON.stringify({
                    OldTimesheet_EntryList: [],
                    NewTimesheet_EntryList: newEntries,
                    Save: true
                }),
                credentials: 'include'
            });
            return r.json();
        }""",
        [new_entries],
    )

    if result.get("d") is not None:
        ids = [e.get("Timesheet_EntryID") for e in (result["d"] or [])]
        print(f"\n✅ Saved {len(ids)} entries  (IDs: {ids})")
        if SUBMIT_AFTER_SAVE and ids:
            await submit_entries(iframe, ids)
    else:
        print(f"\n❌ Save failed: {json.dumps(result)[:400]}")


async def submit_entries(iframe, entry_ids: list[int]):
    print("Submitting for approval…")
    result = await iframe.evaluate(
        """async ([ids, empId]) => {
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/ProcessTimesheet_EntriesForApproval', {
                method: 'POST',
                headers: {'Content-Type': 'application/json; charset=utf-8'},
                body: JSON.stringify({
                    tebam: { processAction: 'approve', ids: ids, comment: '', LastProcessorEmployeeID: empId }
                }),
                credentials: 'include'
            });
            return r.json();
        }""",
        [entry_ids, EMPLOYEE_ID],
    )
    print(f"Submit result: {json.dumps(result)[:300]}")


async def run(headless: bool, command: str, target: datetime | None):
    session_state = json.loads(SESSION_FILE.read_text()) if SESSION_FILE.exists() else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=session_state)
        page    = await context.new_page()

        logged_in = await login(context, page, headless=headless)
        if not logged_in:
            await browser.close()
            sys.exit(1)

        await save_session(context)

        if command == "discover":
            await discover_ids(page, save=True)
        elif command == "create":
            if not ids_configured():
                print("First run — discovering your IDs from S-Cubed…")
                await discover_ids(page, save=True)
            await create_week(page, browser, target, headless=headless)
        else:
            print(f"Unknown command: {command}")
            print("Usage:  python timesheet_bot.py [create [YYYY-MM-DD]]")

        await browser.close()


async def main():
    command  = sys.argv[1] if len(sys.argv) > 1 else "create"
    date_arg = sys.argv[2] if len(sys.argv) > 2 else None
    target   = datetime.strptime(date_arg, "%Y-%m-%d") if date_arg else None

    try:
        await run(headless=True, command=command, target=target)
    except AuthRequired as e:
        print(f"[AUTH] {e} - opening browser...")
        await run(headless=False, command=command, target=target)


if __name__ == "__main__":
    asyncio.run(main())
