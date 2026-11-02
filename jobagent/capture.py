"""Supervised page capture (opt-in): save LinkedIn, Indeed or Naukri job pages you queued, one at a time,
in a visible browser window that you watch and control.

How it works, and what it will not do:
* It opens a visible Chromium window with its own persistent profile (JOBAGENT_HOME/browser/capture-profile).
  You sign in to the site in that window yourself, once. The agent never types, stores or sees passwords.
* For each queued URL it loads the page once and then waits for you. You solve any login prompt or
  CAPTCHA yourself, then press Enter to save the page, `s` to skip, or `q` to stop.
* It only reads the page you are looking at. It never clicks, scrolls, searches, paginates, follows
  links, applies, or runs unattended, and it stops after `max_pages` pages per session.

Risk, stated plainly: this is still a browser driven by automation software. These sites' terms forbid
automated access and they detect automation; using it on your logged-in account can get the account
restricted. The safest path is to save pages from your normal browser with Ctrl+S / Cmd+S into the
saved-pages folder, which needs no automation at all. Use this command only if you accept that risk.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .config import Settings
from .sources.jobsites import JobRef, PageBlocked, identify, parse_job_page, saved_page_name

MAX_PAGES = 25

RISK_NOTICE = ("Supervised capture drives a browser window with automation software. LinkedIn, Indeed and Naukri "
               "forbid automated access in their terms and can restrict accounts they detect. Saving pages yourself "
               "(Ctrl+S / Cmd+S into the saved-pages folder) avoids this entirely.")


@contextmanager
def visible_page(settings: Settings, headless: bool = False) -> Iterator:
    """A page in a persistent, visible browser profile used only for capture."""
    from playwright.sync_api import sync_playwright
    profile_dir = settings.browser_dir / "capture-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        kw = {"headless": headless, "accept_downloads": False, "viewport": None}
        channel = os.environ.get("JOBAGENT_BROWSER_CHANNEL")  # e.g. "chrome" to use your installed Chrome
        exe = os.environ.get("JOBAGENT_CHROMIUM_PATH")
        if channel:
            kw["channel"] = channel
        elif exe:
            kw["executable_path"] = exe
        try:
            ctx = p.chromium.launch_persistent_context(str(profile_dir), **kw)
        except Exception:
            if not os.path.exists("/opt/pw-browsers/chromium"):
                raise
            kw.pop("channel", None)
            ctx = p.chromium.launch_persistent_context(str(profile_dir), executable_path="/opt/pw-browsers/chromium", **kw)
        try:
            yield ctx.pages[0] if ctx.pages else ctx.new_page()
        finally:
            ctx.close()


def capture(page, refs: list[JobRef], out_dir: Path, ask: Callable[[JobRef, int, int], str],
            say: Callable[[str], None] = print, max_pages: int = MAX_PAGES) -> list[dict]:
    """Load each queued job page once, wait for the candidate, save what is on screen. Returns one result per URL."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    todo = refs[:max_pages]
    if len(refs) > max_pages:
        say(f"{len(refs)} URLs queued; this session saves at most {max_pages}. Run it again later for the rest.")
    for i, ref in enumerate(todo, 1):
        try:
            page.goto(ref.url, wait_until="domcontentloaded")
        except Exception as e:  # the candidate can still navigate by hand in the window
            say(f"could not load {ref.url}: {str(e).splitlines()[0]}")
        choice = (ask(ref, i, len(todo)) or "").strip().lower()
        if choice == "q":
            results.append({"url": ref.url, "result": "stopped"})
            break
        if choice == "s":
            results.append({"url": ref.url, "result": "skipped"})
            continue
        html, here = page.content(), page.url
        shown = identify(here) or ref
        try:
            rec = parse_job_page(html, here if identify(here) else ref.url, origin="supervised_capture")
        except PageBlocked as e:
            results.append({"url": ref.url, "result": "blocked", "detail": str(e)})
            say(f"not saved: {e}")
            continue
        except ValueError as e:
            results.append({"url": ref.url, "result": "unreadable", "detail": str(e)})
            say(f"not saved: {e}")
            continue
        path = out_dir / saved_page_name(shown)
        path.write_text(f"<!-- saved from url=({len(shown.url):04d}){shown.url} -->\n" + html)
        results.append({"url": ref.url, "result": "saved", "file": str(path), "title": rec.title,
                        "employer": rec.employer})
        say(f"saved: {rec.title} at {rec.employer} -> {path.name}")
    return results
