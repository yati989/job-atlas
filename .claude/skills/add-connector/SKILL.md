---
name: add-connector
description: Scaffold a new job-board connector following this project's established BaseConnector pattern, mechanism-tier selection, and registration steps
---

# Add Connector

Use this when adding a new job source to job_agent. Follow CLAUDE.md's
architecture section exactly — don't skip the cheapest-mechanism check.

## Steps

1. **Determine mechanism tier, cheapest first.** Never assume a source
   needs Playwright or a login just because a quick look suggests it:
   - Try a plain `httpx` GET first. If it returns real HTML with job data
     → `app/collectors/html/` (BeautifulSoup) or check for a JSON API
     → `app/collectors/api/` if the site's search box hits a clean JSON
     endpoint.
   - If it 403s/challenges or the page is empty until JS runs, distinguish
     **JS-rendered** from **bot-blocked** before picking a pattern — try
     plain headless `playwright.sync_api` (no proxy, no stealth) next. If
     real content renders → still goes in `app/collectors/browser/`, using
     plain Playwright, and registered in `app/pipeline/registry.py` since headless
     needs no display.
   - Only if headless gets challenged/blocked → escalate to
     `session_utils.launch_anonymous()` (headed `patchright`),
     registered in `scripts/run_headed_sources.py` instead (can't run on
     unattended schedule).
   - Only if the site is genuinely candidate-login-gated (not just
     "shows a sign-in banner") → a login-gated connector with session
     persistence (see `cutshort.py` for the Google-OAuth pattern). Before
     committing to this, confirm with headed-anonymous first — LinkedIn
     and Naukri both turned out not to need it.
   - If it's a paid subscription paywall (not free signup) → do not
     build a bypass. That's out of scope regardless of instruction.

2. **Write the connector class** in the right subdirectory, subclassing
   `BaseConnector` (`app/collectors/base.py`). Must implement
   `fetch() -> list[NormalizedJob]` (`app/models/schemas.py`). Match the
   shape of the closest existing connector in the same tier rather than
   inventing new conventions.

   If it's a `browser/` connector, every wait you write is either a
   **render-wait** (let content appear — use `page.wait_for_selector(...)`
   on the element you're about to parse, never a blind `wait_for_timeout(N)`
   or `wait_until="networkidle"`) or **stealth-pacing** (a `human_delay(...)`
   that makes headed anti-bot automation look human — only needed on
   `launch_anonymous()`/headed connectors). See CLAUDE.md's
   "Every browser-connector wait is either a render-wait or stealth-pacing"
   section before writing any `wait_for_timeout`/`networkidle` — a per-instance
   blind wait that looks harmless in isolation becomes the dominant cost once
   the connector is registered across multiple search terms/location modes.

3. **Verify selectors/URLs against the live site**, don't guess from a
   spec sheet or docs — inspect the real DOM/response during development.

4. **Register it**:
   - Headless/no-login → add an instance to `ACTIVE_CONNECTORS` in
     `app/pipeline/registry.py`.
   - Headed/bot-blocked or login-gated → add to `CONNECTORS` in
     `scripts/run_headed_sources.py`.

5. **Add to `scripts/smoke_test_connectors.py`'s connector list** so
   fetch-only verification covers it.

6. **Truncate any free-text field going into a `VARCHAR(255)` column**
   (`location_raw`, `company_name_raw`) — this project has hit
   `StringDataRightTruncation` twice (Himalayas, Analytics Vidhya) from
   unbounded joined/URL-like text.

7. Hand off to `verify-connector` for the standard fetch → upsert →
   re-run idempotency check before calling it done.

8. Update `D:\job_hunt_agent\job_boards_updated.csv` via
   `update-tracking-sheet` once verified.
