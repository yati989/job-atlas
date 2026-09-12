"""Shared helpers for active anonymous browser-automation connectors.

These helpers are for sources where plain HTTP or a headless browser is
challenged. A headed ``patchright`` session can access them without a
user account. New sources must still be verified independently before adopting
this mechanism.

Design principles:
- Uses patchright (a stealth-patched Playwright fork) instead of plain
  playwright, since these connectors are actively fighting bot detection
  rather than just rendering JS.
- Every connector here runs headed, always — headless gets
  blocked/challenged even with patchright and even with the real installed
  Chrome (`channel="chrome"`).
"""
import random
import time

from patchright.sync_api import Browser, BrowserContext, Playwright

def human_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def wait_past_interstitial(page, selector: str, max_wait_s: float = 30.0, poll_s: float = 3.0) -> bool:
    """Poll until a client-rendered result selector appears or time expires."""
    elapsed = 0.0
    while elapsed < max_wait_s:
        if page.query_selector(selector):
            return True
        page.wait_for_timeout(poll_s * 1000)
        elapsed += poll_s
    return bool(page.query_selector(selector))


def launch_anonymous(playwright: Playwright) -> tuple[Browser, BrowserContext]:
    """Launch a direct-IP headed Chrome context with no saved login."""
    browser = playwright.chromium.launch(
        headless=False,
        channel="chrome",
    )
    context = browser.new_context()
    return browser, context
