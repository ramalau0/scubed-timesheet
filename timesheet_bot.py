#!/usr/bin/env python3
"""
S-Cubed Timesheet Automation
Automatically creates and optionally submits weekly timesheets on dcxconnect.datacentrix.co.za
"""

import json
import os
import sys
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext

load_dotenv()

BASE_URL   = "https://dcxconnect.datacentrix.co.za/SCUBED"
API_BASE   = f"{BASE_URL}/pages/tlc_api/Timesheet_Entries.aspx"
LOGIN_URL  = f"{BASE_URL}/scubed.aspx"
SESSION_FILE = Path(__file__).parent / "session.json"

# ── Config from .env ──────────────────────────────────────────────────────────
USERNAME   = os.environ["SCUBED_USERNAME"]          # RDebeila@datacentrix.co.za
PASSWORD   = os.environ["SCUBED_PASSWORD"]
CLIENT_ID  = int(os.environ["CLIENT_ID"])
PROJECT_ID = int(os.environ["PROJECT_ID"])
ACTIVITY_ID   = int(os.environ["ACTIVITY_ID"])
DESIGNATION_ID = int(os.environ["DESIGNATION_ID"])
HOURS_PER_DAY  = float(os.getenv("HOURS_PER_DAY", "8"))
WEEKLY_COMMENT = os.getenv("WEEKLY_COMMENT", "Regular weekly hours")
SUBMIT_AFTER_SAVE = os.getenv("SUBMIT_AFTER_SAVE", "false").lower() == "true"
EMPLOYEE_ID = int(os.getenv("EMPLOYEE_ID", "10745"))
ENTITY_ID   = int(os.getenv("ENTITY_ID", "1"))
# ─────────────────────────────────────────────────────────────────────────────

INSERT_TIMESTAMP = "AAAAAAAH954="  # constant the site uses for new entries


def week_ending_for(date: datetime) -> datetime:
    """Return the Sunday that ends the week containing `date`."""
    days_until_sunday = (6 - date.weekday()) % 7
    return date + timedelta(days=days_until_sunday)


def working_days(week_end: datetime) -> list[datetime]:
    """Return Mon–Fri dates for the week ending on `week_end`."""
    monday = week_end - timedelta(days=6)
    return [monday + timedelta(days=i) for i in range(5)]  # Mon=0 … Fri=4


def fmt(date: datetime) -> str:
    return date.strftime("%m-%d-%Y")


def build_entry(date: datetime, hours: float) -> dict:
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
        "Comment": json.dumps(WEEKLY_COMMENT),
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


async def api_post(page: Page, endpoint: str, data: dict) -> dict:
    """POST to a TLC API endpoint using the page's authenticated session."""
    url = f"{API_BASE}/{endpoint}"
    result = await page.evaluate(
        """async ([url, body]) => {
            const r = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json; charset=utf-8'},
                body: JSON.stringify(body),
                credentials: 'include'
            });
            return r.json();
        }""",
        [url, data],
    )
    return result


async def login(context: BrowserContext, page: Page) -> bool:
    """Log in with username + password. Returns True on success."""
    await page.goto(LOGIN_URL, wait_until="networkidle")
    await page.wait_for_timeout(1000)

    # Check if already logged in (redirected to dashboard)
    if "scubed.aspx" in page.url and await page.locator("#nav_weeks, #ifrm").count() > 0:
        print("Already logged in (session restored).")
        return True

    print("Logging in…")
    await page.locator('input[name*="SystemAccount"], input[id*="SystemAccount"], input[type="text"]').first.fill(USERNAME)
    await page.locator('input[type="password"]').fill(PASSWORD)
    await page.locator('input[value="Sign In"], a:has-text("Sign In"), button:has-text("Sign In")').first.click()
    await page.wait_for_timeout(3000)

    if "scubed.aspx" in page.url:
        print("Login successful.")
        return True

    print("⚠️  Login failed. Check SCUBED_USERNAME / SCUBED_PASSWORD in .env")
    return False


async def save_session(context: BrowserContext):
    storage = await context.storage_state()
    SESSION_FILE.write_text(json.dumps(storage))
    print(f"Session saved → {SESSION_FILE}")


async def navigate_to_timesheets(page: Page):
    """Click My Timesheets in the sidebar."""
    # The content lives inside an iframe; click the link in the outer page sidebar
    await page.locator('text=My Timesheets').first.click()
    await page.wait_for_timeout(2000)


async def discover_ids(page: Page):
    """Print available ClientID / ProjectID / ActivityID / DesignationID values."""
    await navigate_to_timesheets(page)

    iframe = page.frame(name="Content") or page.frames[1]

    clients_raw = await iframe.evaluate(
        """async () => {
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/GetEntityClients_DropDownList', {
                method:'POST', headers:{'Content-Type':'application/json; charset=utf-8'},
                body: JSON.stringify({EntityID: SCUBED.Employee.EntityID || 1, EmployeeID: SCUBED.Employee.ID}),
                credentials:'include'
            });
            return (await r.json()).d;
        }"""
    )

    if not clients_raw:
        print("\n⚠️  No clients returned. Ask your S-Cubed admin to assign you to a client/project first.")
        return

    print("\n── Clients ──────────────────────────────")
    for c in clients_raw:
        print(f"  ClientID={c['Value']}  Name={c['Text']}")

    # Show projects for first client
    first_client = clients_raw[0]["Value"]
    projects_raw = await iframe.evaluate(
        """async (cid) => {
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/GetClientProjects_DropDownList', {
                method:'POST', headers:{'Content-Type':'application/json; charset=utf-8'},
                body: JSON.stringify({ClientID: cid, EmployeeID: SCUBED.Employee.ID}),
                credentials:'include'
            });
            return (await r.json()).d;
        }""",
        first_client,
    )

    print(f"\n── Projects for ClientID={first_client} ──")
    for p in projects_raw or []:
        print(f"  ProjectID={p['Value']}  Name={p['Text']}")

    if projects_raw:
        first_project = projects_raw[0]["Value"]
        activities_raw = await iframe.evaluate(
            """async (pid) => {
                const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/GetProjectActivities_DropDownList', {
                    method:'POST', headers:{'Content-Type':'application/json; charset=utf-8'},
                    body: JSON.stringify({ProjectID: pid, EntityID: SCUBED.Employee.EntityID || 1}),
                    credentials:'include'
                });
                return (await r.json()).d;
            }""",
            first_project,
        )
        print(f"\n── Activities for ProjectID={first_project} ──")
        for a in activities_raw or []:
            print(f"  ActivityID={a['Value']}  Name={a['Text']}")

        designations_raw = await iframe.evaluate(
            """async (pid) => {
                const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/GetEmployeeDesignations_DropDownList', {
                    method:'POST', headers:{'Content-Type':'application/json; charset=utf-8'},
                    body: JSON.stringify({EntityID: SCUBED.Employee.EntityID || 1,
                                          EmployeeID: SCUBED.Employee.ID,
                                          ProjectID: pid}),
                    credentials:'include'
                });
                return (await r.json()).d;
            }""",
            first_project,
        )
        print(f"\n── Designations for ProjectID={first_project} ──")
        for d in designations_raw or []:
            print(f"  DesignationID={d['Value']}  Name={d['Text']}")

    print("\nAdd the IDs above to your .env file, then run:  python timesheet_bot.py create")


async def create_week(page: Page, target_date: datetime | None = None):
    """Create timesheet entries for the working week that contains target_date (default: today)."""
    date = target_date or datetime.today()
    week_end = week_ending_for(date)
    days = working_days(week_end)

    print(f"\nCreating timesheet for week ending {fmt(week_end)}")
    print(f"  Days: {', '.join(d.strftime('%a %d %b') for d in days)}")
    print(f"  Hours/day: {HOURS_PER_DAY}  →  Total: {HOURS_PER_DAY * 5}h")

    await navigate_to_timesheets(page)
    iframe = page.frame(name="Content") or page.frames[1]

    new_entries = [build_entry(d, HOURS_PER_DAY) for d in days]

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
        print(f"✅ Saved {len(ids)} entries  (IDs: {ids})")

        if SUBMIT_AFTER_SAVE and ids:
            await submit_entries(iframe, ids)
    else:
        print(f"❌ Save failed: {json.dumps(result)[:400]}")


async def submit_entries(iframe, entry_ids: list[int]):
    """Submit saved entries for approval."""
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


async def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "create"

    session_state = json.loads(SESSION_FILE.read_text()) if SESSION_FILE.exists() else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=session_state)
        page = await context.new_page()

        logged_in = await login(context, page)
        if not logged_in:
            await browser.close()
            sys.exit(1)

        await save_session(context)

        if command == "discover":
            await discover_ids(page)
        elif command == "create":
            # Parse optional date arg: python timesheet_bot.py create 2026-06-16
            date_arg = sys.argv[2] if len(sys.argv) > 2 else None
            target = datetime.strptime(date_arg, "%Y-%m-%d") if date_arg else None
            await create_week(page, target)
        else:
            print(f"Unknown command: {command}")
            print("Usage:  python timesheet_bot.py [discover|create [YYYY-MM-DD]]")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
