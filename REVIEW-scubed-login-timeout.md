# Code Review: S-Cubed Login Timeout (Case-Sensitivity Bug)

**Reviewer:** MMdebuka
**Date:** 30/06/2026
**Branch:** master
**Commit reviewed:** d2d13eb

---

## Summary

The bot successfully authenticates through the full Okta → Microsoft SSO → S-Cubed redirect chain and lands on the correct S-Cubed URL. However, `wait_for_url` times out every time because the URL pattern is lowercase while the actual URL path is uppercase. This is a Windows case-sensitivity bug. Nothing is saved to S-Cubed.

---

## Evidence

The Playwright navigation log shows the browser reaching S-Cubed successfully:

```
navigated to "https://dcxconnect.datacentrix.co.za/SCUBED/scubed.aspx"
```

But immediately after, the timeout fires:

```
playwright._impl._errors.TimeoutError: Timeout 300000ms exceeded.
waiting for navigation to "*dcxconnect*scubed*" until 'load'
```

---

## Root Cause — Case-Sensitive Glob Pattern

**File:** `timesheet_bot.py`, `login` function, line 436

```python
await page.wait_for_url("*dcxconnect*scubed*", timeout=300000)
```

The glob pattern `*dcxconnect*scubed*` is **all lowercase**.

The actual URL is:
```
https://dcxconnect.datacentrix.co.za/SCUBED/scubed.aspx
```

The path contains `/SCUBED/` in **uppercase**. Playwright's `wait_for_url` glob matching is **case-sensitive on Windows**. The pattern `*scubed*` does not match `/SCUBED/`, so the wait never resolves and the 5-minute timeout expires.

---

## What Must Be Fixed

**Option 1 — Use a case-insensitive regex instead of a glob:**
```python
import re
await page.wait_for_url(re.compile(r"dcxconnect.*scubed", re.IGNORECASE), timeout=300000)
```

**Option 2 — Match only the hostname (which is already lowercase):**
```python
await page.wait_for_url("*dcxconnect.datacentrix.co.za*", timeout=300000)
```

**Option 3 — Check the URL after waiting for page load instead:**
```python
await page.wait_for_load_state("networkidle", timeout=300000)
if "dcxconnect" in page.url.lower() and "scubed" in page.url.lower():
    return True
```

Any of these three will fix the issue. Option 2 is the simplest and most robust.

---

## Impact

This bug blocks all timesheet creation on Windows. Every run since the credential auto-fill was removed has failed at this exact point. The bot does all the hard work (calendar fetch, entry building, comment enrichment) but cannot proceed past login.

---

## Secondary Observation — Duplicate Run on Auth Retry

The output shows the calendar fetch running **twice**:

```
Calendar: found 16 event(s) across the week.
[AUTH] S-Cubed login required - opening browser...
Calendar: found 16 event(s) across the week.
S-Cubed: please complete login/2FA in the browser window…
```

The headless run fetches the calendar, fails on S-Cubed auth, then the non-headless retry fetches the calendar again from scratch. This doubles the Outlook scraping time unnecessarily. The calendar result from the first run should be passed to the retry rather than re-fetched.

---

## Priority

| # | Issue | Priority |
|---|-------|----------|
| 1 | `wait_for_url` case-sensitivity blocks all saves on Windows | 🔴 Critical |
| 2 | Calendar fetched twice on auth retry | 🟢 Low |
