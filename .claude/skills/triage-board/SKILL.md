---
name: triage-board
description: Decide the correct scraping-mechanism tier for a newly-discovered job board before building anything, following this project's cheapest-first testing order
---

# Triage Board

Use this before writing any connector code for a job board that hasn't
been touched yet. The goal is to determine the *real* blocker (if any) —
never assume from appearance alone.

## Order of testing (cheapest first, stop at the first that works)

1. **Plain `httpx` GET** on the search/listing page or any obvious API
   endpoint. If real job data comes back → static HTML (`app/collectors/html/`)
   or JSON API (`app/collectors/api/`). Built In and several JSON-API
   sources were wrongly assumed to need a browser before this check.

2. **Plain headless Playwright**, no proxy, no stealth (~5s wait after
   load). If the page was empty/JS-templated under `httpx` but renders
   real content headless → it was JS-rendered, not bot-blocked. Register
   in `app/pipeline/registry.py` (headless needs no display). Do NOT
   escalate to the expensive pattern just because step 1 failed.

3. **Headed `patchright`** (`session_utils.launch_anonymous()`)
   if headless still gets challenged/blocked/empty. If real content shows
   up now with no login at all → register in
   `scripts/run_headed_sources.py` (can't run unattended).

4. **Only if genuinely candidate-login-gated** (a persistent, unavoidable
   "sign in to see this" wall even for headed-anonymous, not just a
   dismissible banner) — check whether it's free signup or a paid
   subscription:
   - Free signup → in scope. Build with dummy-account + session
     persistence (see `cutshort.py`'s Google-OAuth pattern). Ask the user
     to create the account if needed.
   - Paid subscription → out of scope. Do not search for exposed
     APIs/pagination tricks to bypass payment, even under a "don't skip
     any site" instruction — that's circumventing monetized access, a
     different category from scraping public-but-rate-limited data.

5. **If it's a WAF/CAPTCHA challenge** (Cloudflare Turnstile, Incapsula,
   etc.) rather than a login wall — this is an adversarial ML detection
   system, not solvable by trying harder or a custom-built solver. Options
   are a paid solving service (2Captcha etc.) or accepting the block;
   surface this choice to the user rather than attempting a workaround.

6. Watch for **genuine flakiness independent of code correctness**. If retrying
   with a fresh browser context intermittently works, build a retry loop
   (3 attempts, fresh context each time, return `[]` on exhaustion) rather
   than treating it as unsolved — see `zip_recruiter.py` and `foundit.py`
   for the pattern.

7. For click-driven SPA cards with no plain `href`: check for
   `<script type="application/ld+json">` structured data, or intercept
   network requests during a simulated click, before falling back to
   click-then-diff-the-DOM.

## Output

Report which tier the site landed on and why (what step 1/2/3 actually
returned), then hand off to `add-connector` to build it.
