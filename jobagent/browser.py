"""Chromium launch helper: uses Playwright's bundled browser, or JOBAGENT_CHROMIUM_PATH if set."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import Browser, Playwright, sync_playwright

FALLBACK_PATHS = ["/opt/pw-browsers/chromium"]


@contextmanager
def launch_chromium(headless: bool = True) -> Iterator[tuple[Playwright, Browser]]:
    with sync_playwright() as p:
        exe = os.environ.get("JOBAGENT_CHROMIUM_PATH")
        try:
            browser = p.chromium.launch(headless=headless, executable_path=exe) if exe else p.chromium.launch(headless=headless)
        except Exception:
            path = next((x for x in FALLBACK_PATHS if os.path.exists(x)), None)
            if not path:
                raise
            browser = p.chromium.launch(headless=headless, executable_path=path)
        try:
            yield p, browser
        finally:
            browser.close()
