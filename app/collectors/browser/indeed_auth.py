"""Manual login and persistent-profile helpers for Indeed India."""

from pathlib import Path

from patchright.sync_api import Playwright, sync_playwright

from app.config.settings import INDEED_PROFILE_DIR


INDEED_LOGIN_URL = "https://secure.indeed.com/account/login"
INDEED_SESSION_CHECK_URL = "https://in.indeed.com/jobs?q=data%20scientist&l=Bengaluru"


def launch_indeed_context(
    playwright: Playwright,
    *,
    headless: bool = False,
    profile_dir: Path = INDEED_PROFILE_DIR,
):
    """Open the dedicated persistent Chrome context."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        channel="chrome",
    )


def page_reports_logged_in(page) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const data = document.querySelector('#mosaic-data')?.textContent || '';
                return data.includes('\\"isLoggedIn\\":true');
            }"""
        )
    )


def login_headed(*, profile_dir: Path = INDEED_PROFILE_DIR) -> None:
    """Open Indeed for one-time manual login in the dedicated profile."""
    with sync_playwright() as playwright:
        context = launch_indeed_context(
            playwright, headless=False, profile_dir=profile_dir
        )
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(INDEED_LOGIN_URL, timeout=45_000, wait_until="domcontentloaded")

        print("A dedicated Chrome window has opened for Indeed.")
        print("Log into the dummy account manually, including any verification.")
        print("After login, navigate to in.indeed.com/jobs in that window.")
        print("Waiting up to 10 minutes for Indeed to confirm the session...")

        page.wait_for_function(
            """() => {
                if (location.hostname !== 'in.indeed.com') return false;
                const data = document.querySelector('#mosaic-data')?.textContent || '';
                return data.includes('\\"isLoggedIn\\":true');
            }""",
            timeout=600_000,
        )
        print(f"Indeed login confirmed. Profile saved at {INDEED_PROFILE_DIR}")
        context.close()


def check_session(*, profile_dir: Path = INDEED_PROFILE_DIR) -> bool:
    """Return whether Indeed sees the dedicated profile as authenticated."""
    with sync_playwright() as playwright:
        context = launch_indeed_context(
            playwright, headless=False, profile_dir=profile_dir
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(INDEED_SESSION_CHECK_URL, timeout=45_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)
        logged_in = page_reports_logged_in(page)
        context.close()
        return logged_in
