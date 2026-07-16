#!/usr/bin/env python3
"""Quick test: open Outlook Web and print this week's calendar events."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright


async def _poll_page(page, expression, timeout=300000, interval=1000):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        try:
            result = await page.evaluate(expression)
            if result:
                return result
        except Exception:
            pass
        await page.wait_for_timeout(interval)
    raise TimeoutError(f"Timed out after {timeout}ms")

OUTLOOK_SESSION_FILE = Path(__file__).parent / "outlook_session.json"
OUTLOOK_CAL_URL = "https://outlook.office.com/calendar/view/week"


def working_days_this_week():
    today = datetime.today()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(5)]


async def main():
    days = working_days_this_week()
    monday = days[0]

    outlook_state = (
        json.loads(OUTLOOK_SESSION_FILE.read_text())
        if OUTLOOK_SESSION_FILE.exists()
        else None
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx  = await browser.new_context(storage_state=outlook_state)
        page = await ctx.new_page()

        print(f"Navigating to week of {monday.strftime('%Y-%m-%d')}…")
        await page.goto(
            f"{OUTLOOK_CAL_URL}/{monday.strftime('%Y-%m-%d')}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(3000)

        # Handle sign-in (Datacentrix routes through Okta SSO)
        if "login" in page.url or "microsoftonline" in page.url or "okta.com" in page.url:
            print("\nSign-in required — browser window should be open.")
            print("Log in with your Datacentrix Okta credentials, then come back here.")
            print("Waiting up to 5 minutes…")
            # Wait until we land back on outlook.office.com
            await _poll_page(
                page,
                "() => window.location.hostname === 'outlook.office.com'",
                timeout=300000,
            )
            await page.wait_for_timeout(4000)
            print("Signed in!")
            # Navigate to the calendar now that we're authenticated
            await page.goto(
                f"{OUTLOOK_CAL_URL}/{monday.strftime('%Y-%m-%d')}",
                wait_until="domcontentloaded",
                timeout=30000,
            )

        # Save session for future runs
        OUTLOOK_SESSION_FILE.write_text(json.dumps(await ctx.storage_state()))
        print(f"Session saved → {OUTLOOK_SESSION_FILE}\n")

        # Extra wait for calendar grid to fully render
        await page.wait_for_timeout(6000)

        # Diagnostic: show current URL, title, iframe count
        print(f"Current URL:   {page.url}")
        print(f"Page title:    {await page.title()}")
        frames = page.frames
        print(f"Iframes:       {len(frames)}")
        for i, frame in enumerate(frames):
            print(f"  frame[{i}]: {frame.url[:80]}")

        raw = await page.evaluate("""
            () => {
                const events = {};
                const timeRe = /\\d+:\\d+/;
                const dayRe  = /\\b(monday|tuesday|wednesday|thursday|friday)\\b/i;

                // Outlook aria-label format:
                // "GoTurbo Daily Meet, 09:30 to 12:00, Monday, June 15, 2026, By ..., Busy, ..."
                document.querySelectorAll('[role="button"][aria-label]').forEach(btn => {
                    const label = btn.getAttribute('aria-label') || '';
                    if (!timeRe.test(label) || label.length > 500) return;

                    // Skip canceled events
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

        print("── Calendar events this week ─────────────────")
        if raw:
            for day, titles in sorted(raw.items()):
                print(f"  {day.capitalize()}: {', '.join(titles)}")
        else:
            print("  No events found.")

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
