#!/usr/bin/env python3
"""
S-Cubed Timesheet Automation
Automatically creates and optionally submits weekly timesheets on dcxconnect.datacentrix.co.za
Per-day comments are enriched from Outlook Web calendar events, git commit history, and Claude CLI project history.
"""

import fnmatch
import html
import json
import os
import re
import subprocess
import sys

# Fix Windows console encoding for emoji/unicode characters
# sys.stdout/stderr are None when running as a windowed PyInstaller app (no console)
if sys.platform == "win32":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, Browser


class AuthRequired(Exception):
    """Raised when a login page is detected in headless mode."""

# In a frozen PyInstaller build, __file__ resolves inside the onefile temp
# extraction dir (e.g. _MEI*), which is wiped when the process exits. Session,
# catalog, mappings and .env must live somewhere persistent instead.
if getattr(sys, 'frozen', False):
    APP_DATA_DIR = Path.home() / ".scubed-timesheet"
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
else:
    APP_DATA_DIR = Path(__file__).parent

load_dotenv(dotenv_path=APP_DATA_DIR / ".env")

BASE_URL   = "https://dcxconnect.datacentrix.co.za/SCUBED"
API_BASE   = f"{BASE_URL}/pages/tlc_api/Timesheet_Entries.aspx"
LOGIN_URL  = f"{BASE_URL}/scubed.aspx"
SESSION_FILE         = APP_DATA_DIR / "session.json"
OUTLOOK_SESSION_FILE = APP_DATA_DIR / "outlook_session.json"
CLAUDE_HISTORY_FILE  = Path.home() / ".claude" / "history.jsonl"
CATALOG_FILE         = APP_DATA_DIR / "client_catalog.json"
MAPPINGS_FILE        = APP_DATA_DIR / "project_mappings.json"
SUBMITTED_LEDGER_FILE = APP_DATA_DIR / "submitted_weeks.json"

# ── Config from .env ──────────────────────────────────────────────────────────
CLIENT_ID      = int(os.environ["CLIENT_ID"])      if os.getenv("CLIENT_ID")      else None
PROJECT_ID     = int(os.environ["PROJECT_ID"])     if os.getenv("PROJECT_ID")     else None
ACTIVITY_ID    = int(os.environ["ACTIVITY_ID"])    if os.getenv("ACTIVITY_ID")    else None
DESIGNATION_ID = int(os.environ["DESIGNATION_ID"]) if os.getenv("DESIGNATION_ID") else None
EMPLOYEE_ID    = int(os.environ["EMPLOYEE_ID"])    if os.getenv("EMPLOYEE_ID")    else None
ENTITY_ID      = int(os.getenv("ENTITY_ID", "1"))
HOURS_PER_DAY  = float(os.getenv("HOURS_PER_DAY", "8"))
WEEKLY_COMMENT = os.getenv("WEEKLY_COMMENT") or "Software development, analysis and implementation."
SUBMIT_AFTER_SAVE = os.getenv("SUBMIT_AFTER_SAVE", "false").lower() == "true"
USE_CALENDAR   = os.getenv("USE_CALENDAR", "true").lower() == "true"
WORK_DIR       = Path(os.getenv("WORK_DIR", ".")).resolve()
# ─────────────────────────────────────────────────────────────────────────────

INSERT_TIMESTAMP = "AAAAAAAH954="  # constant the site uses for new entries

ENV_FILE = APP_DATA_DIR / ".env"


def ids_configured() -> bool:
    return all([CLIENT_ID, PROJECT_ID, ACTIVITY_ID, DESIGNATION_ID, EMPLOYEE_ID])


def load_catalog() -> dict | None:
    return json.loads(CATALOG_FILE.read_text()) if CATALOG_FILE.exists() else None


def load_mappings() -> dict | None:
    return json.loads(MAPPINGS_FILE.read_text()) if MAPPINGS_FILE.exists() else None


def _load_submitted_weeks() -> dict:
    return json.loads(SUBMITTED_LEDGER_FILE.read_text()) if SUBMITTED_LEDGER_FILE.exists() else {}


def _record_submitted_week(week_key: str, entry_ids: list[int]):
    """Record a successful save so a later re-run of the same week is caught locally.
    This is a same-machine ledger only — it can't see entries created via the S-Cubed
    web UI directly or from another machine, since there's no verified API to list
    existing server-side entries (see REVIEW-batch-save-api-failure.md)."""
    ledger = _load_submitted_weeks()
    ledger[week_key] = {"entry_ids": entry_ids, "saved_at": datetime.now().isoformat(timespec="seconds")}
    SUBMITTED_LEDGER_FILE.write_text(json.dumps(ledger, indent=2))


def resolve_project_ids(repo_name: str, catalog: dict, mappings: dict) -> dict | None:
    """Map a repo folder name to (client_id, project_id, activity_id, designation_id) via mappings."""
    client_id  = mappings.get("default_client_id")
    project_id = mappings.get("default_project_id")

    for m in mappings.get("mappings", []):
        if repo_name and fnmatch.fnmatch(repo_name.lower(), m["pattern"].lower()):
            client_id  = m["client_id"]
            project_id = m.get("project_id")
            break

    client = next((c for c in catalog["clients"] if c["id"] == client_id), None)
    if client is None:
        if client_id is not None:
            # A mapping/default pointed at a client_id that's no longer in the
            # catalog (stale after re-discovery) — don't silently bill a
            # different client instead.
            return None
        if catalog["clients"]:
            client = catalog["clients"][0]
    if client is None or not client["projects"]:
        return None

    if project_id:
        project = next((p for p in client["projects"] if p["id"] == project_id), None)
        if project is None:
            return None
    else:
        project = client["projects"][0]

    return {
        "client_id": client["id"],
        "project_id": project["id"],
        "activity_id": project["activity_id"],
        "designation_id": project["designation_id"],
    }


def split_hours(total: float, n: int) -> list[float]:
    """Split total into n parts; last part absorbs rounding remainder so they sum exactly."""
    per = round(total / n, 2)
    parts = [per] * n
    parts[-1] = round(total - per * (n - 1), 2)
    return parts


def write_env(updates: dict[str, str]):
    """Update key=value pairs in .env and reload the module globals."""
    global CLIENT_ID, PROJECT_ID, ACTIVITY_ID, DESIGNATION_ID, EMPLOYEE_ID, ENTITY_ID, WORK_DIR

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
    WORK_DIR       = Path(os.getenv("WORK_DIR", ".")).resolve()


# ── Date helpers ───────────────────────────────────────────────────────────────

def week_ending_for(date: datetime) -> datetime:
    """Return the most recently completed week-ending Sunday (always <= date)."""
    # weekday(): Mon=0 … Sun=6. Days since the most recent Sunday = (weekday+1) % 7.
    days_since_sunday = (date.weekday() + 1) % 7
    return date - timedelta(days=days_since_sunday)


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

    with open(CLAUDE_HISTORY_FILE, encoding="utf-8", errors="replace") as f:
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


def _git_author_email(repo: Path) -> str | None:
    """Resolve the commit author email for this repo (local override, else global)."""
    try:
        out = subprocess.check_output(
            ["git", "config", "--get", "user.email"],
            cwd=repo, text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        return out.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def get_git_work_for_date(target_date: datetime) -> dict[str, list[str]]:
    """Return {repo_name: [commit_subject, ...]} for repos under WORK_DIR with commits on target_date, authored by the current user."""
    date_str = target_date.strftime("%Y-%m-%d")
    result: dict[str, list[str]] = {}
    try:
        for repo in sorted(WORK_DIR.iterdir()):
            if not (repo / ".git").exists():
                continue
            try:
                author = _git_author_email(repo)
                cmd = [
                    "git", "log", "--format=%s",
                    f"--since={date_str} 00:00:00",
                    f"--until={date_str} 23:59:59",
                ]
                if author:
                    cmd.append(f"--author={author}")
                out = subprocess.check_output(
                    cmd, cwd=repo, text=True, stderr=subprocess.DEVNULL, timeout=5,
                )
                msgs = [m.strip() for m in out.splitlines() if m.strip()]
                if msgs:
                    result[repo.name] = msgs
            except (subprocess.SubprocessError, OSError):
                pass
    except OSError:
        pass
    return result


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
        # Outlook's JS auth check fires after domcontentloaded and may redirect to Okta.
        # Wait up to 12s specifically for an auth redirect; timeout means session is valid.
        auth_redirected = False
        try:
            await page.wait_for_url(
                lambda url: "okta.com" in url or "microsoftonline.com" in url or "login.microsoft" in url,
                timeout=12000,
            )
            auth_redirected = True
        except Exception:
            pass

        # Handle sign-in redirect (Datacentrix routes through Okta SSO)
        if auth_redirected or "login" in page.url or "microsoftonline" in page.url or "okta.com" in page.url:
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

    except AuthRequired:
        raise
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


def build_day_comment(cal_events: list[str], projects: list[str], git_work: dict[str, list[str]] | None = None) -> str:
    """
    Build a timesheet comment.
    Always leads with WEEKLY_COMMENT, then appends git/Claude project work and meeting context.
    """
    git_work = git_work or {}
    sentences = [WEEKLY_COMMENT.rstrip(".") + "."]

    all_projects = list(git_work.keys()) + [p for p in projects if p not in git_work]
    if all_projects:
        work_parts = []
        for proj in all_projects:
            commits = git_work.get(proj, [])
            if commits:
                summary = "; ".join(commits[:3])
                if len(summary) > 80:
                    summary = summary[:77] + "..."
                work_parts.append(f"{proj} ({summary})")
            else:
                work_parts.append(proj)
        sentences.append(f"Worked on {_join_natural(work_parts)}.")

    standups      = [e for e in cal_events if _is_standup(e)]
    work_meetings = [e for e in cal_events if not _is_standup(e)]

    if standups:
        sentences.append(f"Attended {_join_natural(standups)}.")
    if work_meetings:
        sentences.append(f"Participated in {_join_natural(work_meetings)}.")

    return " ".join(sentences)[:200]  # guard against field length limit


# ── Timesheet entry ────────────────────────────────────────────────────────────

def build_entry(date: datetime, hours: float, comment: str, client_id: int, project_id: int, activity_id: int, designation_id: int) -> dict:
    emp_id = EMPLOYEE_ID or (load_catalog() or {}).get("employee_id")
    return {
        "Timesheet_EntryID": -1,
        "EmployeeID": emp_id,
        "ClientID": client_id,
        "ProjectID": project_id,
        "ActivityID": activity_id,
        "DesignationID": designation_id,
        "WeekEnding": None,
        "EntryDate": fmt(date),
        "DayID": None,
        "Hours": hours,
        "InvoicedHours": 0,
        "EntryTimestamp": INSERT_TIMESTAMP,
        "Comment": comment,
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

    print("S-Cubed: please complete login/2FA in the browser window…")
    # Match the exact same signal as the "already logged in" check above (URL AND
    # dashboard element). Checking the element alone can false-positive on an
    # intermediate page during the multi-hop SSO redirect (Microsoft -> Okta ->
    # back to scubed.aspx), reporting success before the browser has actually
    # landed back on the dashboard.
    await page.wait_for_function(
        "() => location.href.includes('/scubed.aspx') && "
        "(document.querySelector('#nav_weeks') || document.querySelector('#ifrm'))",
        timeout=300000,
    )
    await page.wait_for_timeout(1000)

    print("Login successful.")
    return True


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
        except EOFError:
            raise RuntimeError(f"No input available to pick {label} — run this command in an interactive terminal.")
        except ValueError:
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


def _unescape_catalog(data):
    """Recursively decode HTML entities in all string values returned by the S-Cubed API."""
    if isinstance(data, dict):
        return {k: _unescape_catalog(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_unescape_catalog(v) for v in data]
    if isinstance(data, str):
        return html.unescape(data)
    return data


def _derive_patterns(catalog: dict) -> list[dict]:
    """
    Auto-derive glob patterns from project names in the catalog.
    Algorithm: strip everything up to and including the last ' - ', then take the
    first word with 3+ alphanumeric chars, lowercase it, append '*'.
    Each pattern carries the exact project_id so resolve_project_ids routes correctly.
    """
    patterns = []
    seen = set()
    for client in catalog["clients"]:
        for project in client["projects"]:
            name = project["name"]
            # Strip organisational prefix (e.g. "DevOps Project - ", "TES - ")
            if " - " in name:
                name = name.rsplit(" - ", 1)[-1]
            # Find first word with 3+ alphanumeric chars
            keyword = ""
            for word in name.split():
                clean = re.sub(r"[^a-z0-9]", "", word.lower())
                if len(clean) >= 3:
                    keyword = clean
                    break
            if not keyword:
                continue
            pattern = f"{keyword}*"
            if pattern in seen:
                continue
            seen.add(pattern)
            patterns.append({
                "pattern": pattern,
                "client_id": client["id"],
                "project_id": project["id"],
            })
    return patterns


async def discover_all(page: Page):
    """Crawl all clients/projects/activities/designations non-interactively and save catalog + mappings."""
    await navigate_to_timesheets(page)
    iframe = page.frame(name="Content") or page.frames[1]

    emp_id, entity_id = await iframe.evaluate(
        "() => [SCUBED.Employee.ID, SCUBED.Employee.EntityID || 1]"
    )
    print(f"Employee {emp_id} / Entity {entity_id} — crawling catalog...")

    catalog = await iframe.evaluate(
        """async ([empId, entityId]) => {
            const BASE = '/SCUBED/pages/tlc_api/Timesheet_Entries.aspx';
            async function api(endpoint, body) {
                const r = await fetch(BASE + '/' + endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json; charset=utf-8'},
                    body: JSON.stringify(body),
                    credentials: 'include'
                });
                return (await r.json()).d || [];
            }

            const clients = await api('GetEntityClients_DropDownList', {EntityID: entityId, EmployeeID: empId});
            const result = {employee_id: empId, entity_id: entityId, clients: []};

            for (const c of clients) {
                const projects = await api('GetClientProjects_DropDownList', {ClientID: c.Value, EmployeeID: empId});
                const clientEntry = {id: parseInt(c.Value), name: c.Text, projects: []};

                for (const p of (projects || [])) {
                    const [activities, designations] = await Promise.all([
                        api('GetProjectActivities_DropDownList', {ProjectID: p.Value, EntityID: entityId}),
                        api('GetEmployeeDesignations_DropDownList', {EntityID: entityId, EmployeeID: empId, ProjectID: p.Value})
                    ]);
                    const act = (activities || [])[0] || {Value: '1', Text: 'Default'};
                    const des = (designations || [])[0] || {Value: '1', Text: 'Default'};
                    clientEntry.projects.push({
                        id: parseInt(p.Value),
                        name: p.Text,
                        activity_id: parseInt(act.Value),
                        designation_id: parseInt(des.Value)
                    });
                }
                result.clients.push(clientEntry);
            }
            return result;
        }""",
        [emp_id, entity_id],
    )

    # Decode HTML entities (API returns &amp;, &#39; etc. in name strings)
    catalog = _unescape_catalog(catalog)

    CATALOG_FILE.write_text(json.dumps(catalog, indent=2))
    print(f"Catalog saved -> {CATALOG_FILE}")
    for c in catalog["clients"]:
        print(f"  {c['name']} (ID={c['id']}): {len(c['projects'])} project(s)")
        for p in c["projects"]:
            print(f"    - {p['name']} (ID={p['id']}, activity={p['activity_id']}, designation={p['designation_id']})")

    write_env({"EMPLOYEE_ID": str(emp_id), "ENTITY_ID": str(entity_id)})

    if not MAPPINGS_FILE.exists():
        default_client  = catalog["clients"][0] if catalog["clients"] else {}
        default_project = default_client.get("projects", [{}])[0] if default_client else {}
        patterns        = _derive_patterns(catalog)
        mappings = {
            "default_client_id":  default_client.get("id"),
            "default_project_id": default_project.get("id"),
            "mappings": patterns,
        }
        MAPPINGS_FILE.write_text(json.dumps(mappings, indent=2))
        print(f"\nMappings generated -> {MAPPINGS_FILE}  ({len(patterns)} pattern(s))")
        for p in patterns:
            client_name = next((c["name"] for c in catalog["clients"] if c["id"] == p["client_id"]), "?")
            project_name = next(
                (proj["name"] for c in catalog["clients"] for proj in c["projects"] if proj["id"] == p["project_id"]),
                "?"
            )
            print(f"  {p['pattern']!r:20s} -> {client_name} / {project_name}")
        print("\nReview project_mappings.json and adjust patterns if needed.")
    else:
        print(f"Mappings already exist at {MAPPINGS_FILE} — not overwritten.")


async def create_week(page: Page, browser: Browser, target_date: datetime | None = None, headless: bool = False, pre_cal: dict | None = None):
    """Create timesheet entries with per-day comments and auto-selected client/project from catalog."""
    date     = target_date or datetime.today()
    week_end = week_ending_for(date)
    days     = working_days(week_end)
    week_key = fmt(week_end)

    ledger = _load_submitted_weeks()
    if week_key in ledger:
        prev = ledger[week_key]
        print(f"⚠️  Week ending {week_key} was already saved from this machine on {prev.get('saved_at', '?')} "
              f"(entry IDs: {prev.get('entry_ids')}). Skipping to avoid duplicate entries.")
        print(f"   Note: this only catches re-runs from this machine — it can't see entries created")
        print(f"   directly in the S-Cubed web UI or from another machine.")
        print(f"   To re-submit anyway, remove the \"{week_key}\" entry from {SUBMITTED_LEDGER_FILE.name}.")
        return

    catalog  = load_catalog()
    mappings = load_mappings()
    use_auto = catalog is not None and mappings is not None

    if use_auto:
        print(f"\nAuto-select mode: using {CATALOG_FILE.name} + {MAPPINGS_FILE.name}")
    elif ids_configured():
        print(f"\nSingle-client mode: CLIENT_ID={CLIENT_ID}, PROJECT_ID={PROJECT_ID}")
    else:
        print("No client_catalog.json and no CLIENT_ID in .env.")
        print("Run 'python timesheet_bot.py discover_all' to set up the catalog.")
        return

    print(f"Creating timesheet for week ending {fmt(week_end)}")
    print(f"  Days: {', '.join(d.strftime('%a %d %b') for d in days)}")
    print(f"  Hours/day: {HOURS_PER_DAY}  ->  Total: {HOURS_PER_DAY * 5}h")

    # Gather per-day enrichment
    cal_events: dict[str, list[str]] = {}
    if USE_CALENDAR:
        cal_events = pre_cal if pre_cal is not None else await fetch_outlook_calendar(browser, days, headless=headless)

    claude_projects = {fmt(d): get_claude_projects_for_date(d) for d in days}
    git_work        = {fmt(d): get_git_work_for_date(d) for d in days}

    total_commits = sum(len(v) for repo_commits in git_work.values() for v in repo_commits.values())
    print(f"Git history: found {total_commits} commit(s) across the week.")

    print()
    new_entries = []
    for d in days:
        date_str = fmt(d)
        events   = cal_events.get(date_str, [])

        if use_auto:
            # Union of git repos and Claude folders; git repos first (have commit detail)
            git_repos    = list(git_work.get(date_str, {}).keys())
            claude_repos = [p for p in claude_projects.get(date_str, []) if p not in git_repos]
            all_repos    = git_repos + claude_repos

            # Map each repo to (client_id, project_id); group repos by that key
            groups: dict[tuple, dict] = {}
            for repo in all_repos:
                ids = resolve_project_ids(repo, catalog, mappings)
                if ids is None:
                    continue
                key = (ids["client_id"], ids["project_id"])
                if key not in groups:
                    groups[key] = {"ids": ids, "repos": [], "git": {}}
                groups[key]["repos"].append(repo)
                if repo in git_work.get(date_str, {}):
                    groups[key]["git"][repo] = git_work[date_str][repo]

            if not groups:
                # No repos matched — use default client with full hours
                ids = resolve_project_ids("", catalog, mappings)
                if ids:
                    comment = build_day_comment(events, [], {})
                    new_entries.append(build_entry(d, HOURS_PER_DAY, comment, **ids))
                    print(f"  {d.strftime('%a %d %b')} (default) {HOURS_PER_DAY}h  ->  {comment}")
                continue

            hours_list = split_hours(HOURS_PER_DAY, len(groups))
            for (client_id, _), group, hours in zip(groups.keys(), groups.values(), hours_list):
                comment = build_day_comment(events, group["repos"], group["git"])
                new_entries.append(build_entry(d, hours, comment, **group["ids"]))
                print(f"  {d.strftime('%a %d %b')} [client={client_id}] {hours}h  ->  {comment}")
        else:
            comment = build_day_comment(events, claude_projects.get(date_str, []), git_work.get(date_str, {}))
            new_entries.append(build_entry(d, HOURS_PER_DAY, comment, CLIENT_ID, PROJECT_ID, ACTIVITY_ID, DESIGNATION_ID))
            print(f"  {d.strftime('%a %d %b')}  ->  {comment}")

    await navigate_to_timesheets(page)
    iframe = page.frame(name="Content") or page.frames[1]

    result = await iframe.evaluate(
        """async ([newEntries]) => {
            const body = JSON.stringify({
                OldTimesheet_EntryList: [],
                NewTimesheet_EntryList: newEntries,
                Save: true
            });
            const r = await fetch('/SCUBED/pages/tlc_api/Timesheet_Entries.aspx/BatchTransactionTimesheet_Entry', {
                method: 'POST',
                headers: {'Content-Type': 'application/json; charset=utf-8'},
                body: body,
                credentials: 'include'
            });
            const text = await r.text();
            let parsed = null;
            try { parsed = JSON.parse(text); } catch(e) {}
            return {status: r.status, body: parsed, raw: parsed ? null : text.substring(0, 2000)};
        }""",
        [new_entries],
    )

    status = result.get("status")
    body   = result.get("body")

    if status == 200 and body and body.get("d") is not None:
        ids = [e.get("Timesheet_EntryID") for e in (body["d"] or [])]
        print(f"\n✅ Saved {len(ids)} entries  (IDs: {ids})")
        _record_submitted_week(week_key, ids)
        if SUBMIT_AFTER_SAVE and ids:
            await submit_entries(iframe, ids)
    else:
        print(f"\n❌ Save failed (HTTP {status}): {json.dumps(body or result.get('raw', ''))[:500]}")
        if isinstance(body, dict) and "error processing the request" in (body.get("Message") or "").lower():
            print("   This generic error has previously shown up when entries already exist")
            print(f"   for week ending {week_key} — check S-Cubed's timesheet grid manually before retrying.")
        print(f"\n── Request payload (for debugging) ──")
        for i, entry in enumerate(new_entries):
            print(f"  Entry {i}: date={entry['EntryDate']} hours={entry['Hours']} "
                  f"client={entry['ClientID']} project={entry['ProjectID']} "
                  f"activity={entry['ActivityID']} designation={entry['DesignationID']} "
                  f"employee={entry['EmployeeID']}")
            print(f"    Comment: {entry['Comment'][:120]}")
        print(f"  WeekEnding={new_entries[0].get('WeekEnding')} "
              f"DayID={new_entries[0].get('DayID')} "
              f"Timestamp={new_entries[0].get('Timestamp')}")


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


async def _launch(p, headless: bool):
    """Launch the right browser for this platform. Windows uses system Edge so no download is needed."""
    if sys.platform == "win32":
        return await p.chromium.launch(channel="msedge", headless=headless)
    return await p.chromium.launch(headless=headless)


async def test_calendar(target_date: datetime | None = None, headless: bool = False):
    """Standalone calendar test — open Outlook, scrape events, print results. No S-Cubed login needed."""
    date     = target_date or datetime.today()
    week_end = week_ending_for(date)
    days     = working_days(week_end)

    print(f"Testing Outlook calendar for week of {days[0].strftime('%a %d %b')} – {days[-1].strftime('%a %d %b')}")

    async with async_playwright() as p:
        browser = await _launch(p, headless=headless)
        try:
            cal = await fetch_outlook_calendar(browser, days, headless=headless)
        except AuthRequired:
            await browser.close()
            if headless:
                raise
            return
        await browser.close()

    print("\n── Calendar events ───────────────────────────────")
    total = 0
    for d in days:
        date_str = fmt(d)
        events   = cal.get(date_str, [])
        label    = d.strftime("%a %d %b")
        if events:
            print(f"  {label}: {', '.join(events)}")
        else:
            print(f"  {label}: (no events)")
        total += len(events)

    print(f"\n{total} event(s) found.")

    print("\n── Comment preview ───────────────────────────────")
    for d in days:
        date_str = fmt(d)
        events   = cal.get(date_str, [])
        comment  = build_day_comment(events, [], {})
        print(f"  {d.strftime('%a %d %b')}: {comment}")


async def preview_week(browser: Browser, target_date: datetime | None = None, headless: bool = False, pre_cal: dict | None = None):
    """Show what create_week would submit without saving anything."""
    date     = target_date or datetime.today()
    week_end = week_ending_for(date)
    days     = working_days(week_end)

    catalog  = load_catalog()
    mappings = load_mappings()

    if not (catalog and mappings) and not ids_configured():
        print("⚠️  No client_catalog.json/project_mappings.json and no CLIENT_ID in .env.")
        print("   Run 'First-time Setup' (or `python timesheet_bot.py discover_all`) first —")
        print("   client/project below will show as None until that's done.\n")

    print(f"Preview for week ending {fmt(week_end)}")
    print(f"  Days: {', '.join(d.strftime('%a %d %b') for d in days)}")

    cal_events: dict[str, list[str]] = {}
    if USE_CALENDAR:
        cal_events = pre_cal if pre_cal is not None else await fetch_outlook_calendar(browser, days, headless=headless)

    claude_projects = {fmt(d): get_claude_projects_for_date(d) for d in days}
    git_work        = {fmt(d): get_git_work_for_date(d) for d in days}

    total_cal     = sum(len(v) for v in cal_events.values())
    total_commits = sum(len(v) for repo_commits in git_work.values() for v in repo_commits.values())
    print(f"\nCalendar events: {total_cal}  |  Git commits: {total_commits}")
    print()

    for d in days:
        date_str = fmt(d)
        events   = cal_events.get(date_str, [])
        comment  = build_day_comment(events, claude_projects.get(date_str, []), git_work.get(date_str, {}))

        client_id  = CLIENT_ID
        project_id = PROJECT_ID
        if catalog and mappings:
            git_repos = list(git_work.get(date_str, {}).keys())
            ids = resolve_project_ids(git_repos[0] if git_repos else "", catalog, mappings)
            if ids:
                client_id  = ids["client_id"]
                project_id = ids["project_id"]

        print(f"  {d.strftime('%a %d %b')}  [client={client_id}, project={project_id}]  {HOURS_PER_DAY}h")
        print(f"    Events: {events or '(none)'}")
        print(f"    Comment: {comment}")


async def run(headless: bool, command: str, target: datetime | None):
    if command == "test-calendar":
        try:
            await test_calendar(target, headless=headless)
        except AuthRequired:
            print("[AUTH] Outlook sign-in required — opening browser...")
            await test_calendar(target, headless=False)
        return

    async with async_playwright() as p:
        browser = await _launch(p, headless=headless)

        # ── Outlook calendar first (so login order is Outlook → S-Cubed) ──
        pre_cal: dict | None = None
        if command in ("create", "preview") and USE_CALENDAR:
            date     = target or datetime.today()
            week_end = week_ending_for(date)
            days     = working_days(week_end)
            pre_cal  = await fetch_outlook_calendar(browser, days, headless=headless)
            # AuthRequired propagates to main() which retries with headless=False

        # ── S-Cubed ────────────────────────────────────────────────────────
        session_state = json.loads(SESSION_FILE.read_text()) if SESSION_FILE.exists() else None
        context = await browser.new_context(storage_state=session_state)
        page    = await context.new_page()

        logged_in = await login(context, page, headless=headless)
        if not logged_in:
            await browser.close()
            sys.exit(1)

        await save_session(context)

        if command == "discover_all":
            await discover_all(page)
        elif command == "discover":
            await discover_ids(page, save=True)
        elif command == "create":
            await create_week(page, browser, target, headless=headless, pre_cal=pre_cal)
        elif command == "preview":
            await preview_week(browser, target, headless=headless, pre_cal=pre_cal)
        else:
            print(f"Unknown command: {command}")
            print("Usage:  python timesheet_bot.py [discover_all | discover | create | preview | test-calendar [YYYY-MM-DD]]")

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
