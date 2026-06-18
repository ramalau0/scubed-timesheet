#!/usr/bin/env python3
"""
Auth spike: verify Outlook/Graph calendar access before building the full integration.
Run once to check whether device-code flow is allowed on your tenant.

Usage:
    pip install msal requests
    python test_ms_calendar.py
"""

import json
from datetime import datetime, timezone, timedelta

try:
    import msal
    import requests
except ImportError:
    print("Missing deps. Run:  pip install msal requests")
    raise SystemExit(1)

# Azure PowerShell public client — works on most tenants without app registration
CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"
AUTHORITY = "https://login.microsoftonline.com/organizations"
SCOPES    = ["Calendars.Read"]
TOKEN_CACHE_FILE = "ms_token_cache.json"


def load_cache():
    cache = msal.SerializableTokenCache()
    try:
        with open(TOKEN_CACHE_FILE) as f:
            cache.deserialize(f.read())
    except FileNotFoundError:
        pass
    return cache


def save_cache(cache):
    if cache.has_state_changed:
        with open(TOKEN_CACHE_FILE, "w") as f:
            f.write(cache.serialize())


def get_token():
    cache = load_cache()
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    # Try silent first (cached)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            save_cache(cache)
            return result["access_token"]

    # Device-code flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print(f"Device-code flow blocked by tenant: {flow.get('error_description', flow)}")
        print("\n→ Your tenant likely has a Conditional Access policy blocking this.")
        print("→ Tell Claude, and we'll use a browser-based auth flow instead.")
        return None

    print("\n" + flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print(f"Auth failed: {result.get('error_description', result)}")
        return None

    save_cache(cache)
    return result["access_token"]


def fetch_today_events(token):
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end   = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    url = (
        "https://graph.microsoft.com/v1.0/me/calendarView"
        f"?startDateTime={start}&endDateTime={end}"
        "&$select=subject,start,end,isAllDay"
        "&$orderby=start/dateTime"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json().get("value", [])


if __name__ == "__main__":
    token = get_token()
    if not token:
        raise SystemExit(1)

    print("\nAuth succeeded! Fetching today's calendar events…\n")
    events = fetch_today_events(token)

    if not events:
        print("No events today (or calendar is empty).")
    else:
        for e in events:
            subj = e.get("subject", "(no subject)")
            t    = e["start"]["dateTime"][:16].replace("T", " ")
            print(f"  {t}  {subj}")

    print(f"\n✅ Calendar access works. Found {len(events)} event(s) today.")
    print("Token cached → ms_token_cache.json (re-runs skip the device-code step)")
