"""
Spike: prove PyInstaller + Playwright works before building the full GUI.
Run this packaged binary on a clean machine to validate the approach.
"""
import asyncio
import sys
from playwright.async_api import async_playwright


async def main():
    print("Starting Playwright spike...")
    async with async_playwright() as p:
        if sys.platform == "win32":
            browser = await p.chromium.launch(channel="msedge", headless=True)
            print("Using: system Edge")
        elif sys.platform == "darwin":
            browser = await p.chromium.launch(channel="chrome", headless=True)
            print("Using: system Chrome")
        else:
            browser = await p.chromium.launch(headless=True)
            print("Using: Playwright Chromium")

        page = await browser.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        title = await page.title()
        print(f"Page title: {title}")
        await browser.close()

    print("SUCCESS — Playwright works packaged.")


if __name__ == "__main__":
    asyncio.run(main())
