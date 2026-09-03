"""Private subprocess probe for Playwright's version-matched Chromium path."""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright


async def _main() -> None:
    async with async_playwright() as playwright:
        sys.stdout.write(playwright.chromium.executable_path)


if __name__ == "__main__":
    asyncio.run(_main())
