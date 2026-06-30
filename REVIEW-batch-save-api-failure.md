# Code Review: Batch Save API Failure

**Reviewer:** MMdebuka
**Date:** 30/06/2026
**Branch:** master
**Commit reviewed:** f30e06a

---

## Summary

The `create` command now successfully authenticates, fetches calendar events, and builds enriched per-day comments. However, it fails at the final step — saving entries to S-Cubed — with a generic server error. Nothing is written to the timesheet system.

---

## Test Run Output

```
Calendar: found 16 event(s) across the week.
Already logged in (session restored).

Auto-select mode: using client_catalog.json + project_mappings.json
Creating timesheet for week ending 06-28-2026
  Days: Mon 22 Jun, Tue 23 Jun, Wed 24 Jun, Thu 25 Jun, Fri 26 Jun
  Hours/day: 8.0  ->  Total: 40.0h
Git history: found 0 commit(s) across the week.

  Mon 22 Jun (default) 8.0h  ->  Software development, analysis and implementation. Attended Morning Brief and Daily Check-In. Participated in eNetworks Audit.
  Tue 23 Jun (default) 8.0h  ->  Software development, analysis and implementation. Attended Morning Brief and Daily Check-In. Participated in eNetworks Audit.
  Wed 24 Jun (default) 8.0h  ->  Software development, analysis and implementation. Attended Morning Brief and Daily Check-In. Participated in eNetworks Audit.
  Thu 25 Jun (default) 8.0h  ->  Software development, analysis and implementation. Attended Morning Brief and Daily Check-In. Participated in eNetworks Audit.
  Fri 26 Jun (default) 8.0h  ->  Software development, analysis and implementation. Attended Morning Brief and Daily Check-In. Participated in eNetworks Audit and Weekly Timesheet Reminder.

❌ Save failed: {"Message": "There was an error processing the request.", "StackTrace": "", "ExceptionType": ""}
```

---

## What Works

- Session restore: S-Cubed login reused cached session without prompting. ✅
- Outlook calendar: 16 events found and correctly mapped to days. ✅
- Comment enrichment: Comments correctly combine fallback text + standups + work meetings. ✅
- Multi-project mapping: catalog + mappings loaded, correct client selected. ✅

---

## Critical Defect — BatchTransactionTimesheet_Entry API returns error

**File:** `timesheet_bot.py`, `create_week` function
**API endpoint:** `POST /SCUBED/pages/tlc_api/Timesheet_Entries.aspx/BatchTransactionTimesheet_Entry`

The server returns:
```json
{"Message": "There was an error processing the request.", "StackTrace": "", "ExceptionType": ""}
```

This is a generic ASP.NET Web Services error with no stack trace exposed. The entries are not saved.

### Probable Causes (investigate in this order)

**1. Entries already exist for that week**
The week 22–26 June may already have timesheet entries in S-Cubed from a previous attempt or manual entry. The API may reject duplicates. The bot should check for existing entries before attempting to insert.

**2. Wrong project ID for the employee**
The auto-select logic falls back to `project_id: 95` (Admin) when no folder patterns match. Employee 11266 may not be authorised to log time against project 95. The `discover_all` catalog lists project 95 under the employee's clients, but authorisation at the project level may differ from what the API accepts for timesheet entry.

**What must be fixed:**

- Before calling `BatchTransactionTimesheet_Entry`, call `GetTimesheetEntries` (or equivalent) to check whether entries already exist for the target week. If they do, skip or warn rather than attempting a blind insert.
- Log the full request payload (entries JSON) when the save fails so the developer can diagnose which field the server is rejecting.
- Consider adding a check that validates `ProjectID` against what `GetClientProjects_DropDownList` returns before building entries — if the project is not in the employee's allowed list, surface a clear error rather than a generic API failure.

---

## Secondary Issue — Calendar comment is identical across all 5 days

The 16 calendar events found were recurring events (Morning Brief, Daily Check-In, eNetworks Audit) that appear every day. The comment is therefore identical for all 5 days, which may flag as suspicious during approval.

This is not a bug per se, but the comment builder has no awareness of which specific occurrence of a recurring event falls on which day — it appears to assign all weekly events to every day rather than filtering by day.

**What must be checked:** Verify that the calendar scraper is correctly filtering events per day (by the `day_map` logic in `fetch_outlook_calendar`). If recurring events are being assigned to all days instead of only the days they actually occur, that is a scraping bug.

---

## Priority Order

| # | Issue | Priority |
|---|-------|----------|
| 1 | Batch save API fails — nothing written to S-Cubed | 🔴 Critical |
| 2 | No duplicate-check before insert | 🔴 Critical |
| 3 | No payload logging on failure | 🟡 High |
| 4 | Recurring events may be assigned to wrong days | 🟡 High |
