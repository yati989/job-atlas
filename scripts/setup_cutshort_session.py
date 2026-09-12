"""Attended one-time Cutshort Google login; normal collection is HTTP-only."""

from patchright.sync_api import sync_playwright

from app.collectors.html.cutshort import CUTSHORT_SESSION_STATE_PATH


def main() -> None:
    CUTSHORT_SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://cutshort.io/jobs", wait_until="domcontentloaded")
        page.get_by_text("Candidate login", exact=False).first.click()
        checkbox = page.query_selector('input[type="checkbox"]')
        if checkbox:
            checkbox.check(force=True)
        page.get_by_text("Signup or login with Google", exact=False).first.click()
        print("Complete Google login in the opened window (timeout: 5 minutes).")
        page.wait_for_function(
            "() => !document.body.innerText.includes('Candidate login')",
            timeout=300_000,
        )
        context.storage_state(path=str(CUTSHORT_SESSION_STATE_PATH))
        browser.close()
    print(f"Cutshort session saved to {CUTSHORT_SESSION_STATE_PATH}")


if __name__ == "__main__":
    main()
