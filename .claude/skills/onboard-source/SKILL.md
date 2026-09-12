---
name: onboard-source
description: Full per-site onboarding playbook — wire verified filters into a connector, capture every field the site actually offers, and pass the verify_source scorecard gate. The unit of work for making any one source production-grade.
---

# Onboard Source

Takes ONE source (connector) from "fetches something" to "fetches the
right jobs with complete data, provably." The reference implementation is
[linkedin.py](app/collectors/browser/linkedin.py) — read it first; it
demonstrates every pattern below. This skill is written to be executable
by a dispatched agent with no further conversation: every decision rule
is stated; when genuinely blocked, report the blocker in your result
rather than guessing.

## Inputs
- Source name + its connector file under `app/collectors/{api,feed,html,browser}/`.
- The filter methodology (fixed): broad keyword set from
  `app/config/categories.py:all_keywords()`; posted within last 180 days;
  location = remote-India OR Bengaluru/Bangalore.

## Stage A — Right jobs (filters, verified not assumed)

1. **Live-explore the site's real filter surface.** Use its own search
   UI/API and observe what params it produces. Check, in order of
   cheapness: URL query params from the search form; a JSON API the page
   calls (watch network requests — the Foundit `/middleware` lesson);
   JSON-LD blocks. Never trust a param from documentation or guesswork.
2. **Test every candidate param against returned data** before wiring it
   in. Known traps, all confirmed live in this project:
   - A param can be silently dropped (LinkedIn `&start=25` for anon users).
   - A param can change results without doing what you think
     (verify the *content* matches the filter's claim, not just that
     results differ).
   - A search parameter may be silently ignored; always compare a real query
     with a nonsense query and inspect returned content.
3. **Wire in what's real, by board type:**
   - **Global board with location search** (Indeed, Glassdoor,
     ZipRecruiter…): two passes like LinkedIn —
     remote scoped to India (never bare/global remote: confirmed to
     return mostly US jobs) + a Bengaluru pass. Parameterize via a
     `location_mode` constructor arg (see `LinkedInConnector.__init__`).
   - **India-native board** (Naukri, Shine, TimesJobs, Instahyre,
     IIMJobs, Foundit…): already India-scoped; add a
     Bengaluru/city filter if the site has one, plus a remote filter if
     offered.
   - **Remote-only board** (Himalayas, We Work Remotely, Working Nomads…):
     no location search may exist. Post-filter on a candidate-eligible-region
     field when the source exposes one. If no such field,
     document the gap; do not fake it.
   - **Date**: use a real date-posted param if one exists (LinkedIn
     `f_TPR=r{seconds}`); else post-filter on `posted_at` when the source
     provides dates; else document the gap.
   - **Experience**: never approximate with seniority buckets — leave to
     the enrichment pipeline (standing user rule).
4. Keyword breadth: the connector should be instantiable per-keyword
   (registry registers one instance per keyword × location mode, like
   LinkedIn in `app/pipeline/registry.py`). Sites with no native
   search fetch broadly and rely on the runner-level keyword safety net
   (already applied in both runners — do not remove per-connector
   filtering that exists, it's harmless).

## Stage B — Complete data capture (the "About the company" lesson)

1. **Full data inventory FIRST, then code.** Dump one real list
   card and one real detail page — full `inner_text` plus any JSON
   payloads — and enumerate every field visible anywhere on them
   against this checklist:
   `description, posted_at, employment_type, seniority, salary,
   apply_url, company industry, company size/employee count, company
   about-text`. Check detail-page sidebars/footers/criteria blocks, not
   just the obvious card. (LinkedIn's guest detail pages turned out to
   carry a criteria block with seniority/employment-type/industries that
   the connector ignored for months.)
2. **Add description capture** if missing (39/47 connectors were missing
   it):
   - API source: usually just mapping an existing response field.
   - HTML source: `httpx` fetch of each job's detail page + BeautifulSoup.
   - Browser source: the LinkedIn pattern — open each job's detail page
     in the same context, selector-with-fallback, `None` on failure,
     always close the page. Gate behind a `fetch_descriptions=True`
     constructor flag so a fast card-only pass stays possible.
3. **Scraped beats inferred**: wherever the site shows real
   `posted_at`/`employment_type`/`seniority`/industry text, capture it
   (industry/company facts go into `raw_payload` under keys like
   `industry_scraped` — the company-enrichment pass treats them as ground
   truth over LLM inference).
4. **Truncate every free-text value bound for a VARCHAR(255) column**
   (`location_raw`, `company_name_raw`, `salary_raw`, `employment_type`,
   `seniority`) with `[:255]` — this bug class has hit production twice.
5. Respect per-site constraints that already exist: retry-loop patterns
   for flaky sites (ZipRecruiter and Foundit), session
   reuse for cutshort, `wait_past_interstitial` where used. Don't rip
   out working machinery while adding capture.

## Acceptance gate

Run: `python -m scripts.verify_source <source_name>`
(add the source's instances to the appropriate registry first —
`app/pipeline/registry.py` for headless, `scripts/run_headed_sources.py`
for display-required).

Targets: relevance ≥60%, india-or-remote ≥60%, description completeness
≥90%. The scorecard JSON lands in `scorecards/<source>.json`.
- PASS → commit (see below) and report the scorecard.
- FAIL → fix and re-run. If a dimension is genuinely unsupportable by the
  site (no dates anywhere, no location data), say so explicitly in your
  report and in the tracking-sheet note — a documented gap is acceptable,
  a silent one is not.

## Done criteria (all required)
1. Scorecard passes (or gaps explicitly documented).
2. `python -m scripts.smoke_test_connectors` still passes for this
   connector (fetch-only sanity).
3. Committed with a per-site message
   ("Onboard <source>: filters + full data capture"), pushed.
4. Tracking sheet `D:\job_hunt_agent\job_boards_updated.csv` notes column
   updated with scorecard summary + any documented gaps (check the file
   isn't locked by Excel first — `tasklist //FI "IMAGENAME eq EXCEL.EXE"`).
5. Report back: scorecard numbers, what filters were wired (with the
   verified mechanism), what fields were added, and any gaps/blockers.

## Hard boundaries
- Blocked/paid sites are out of scope: Jooble, Turing, icrunchdata
  (WAF/CAPTCHA — no solver attempts), Surely Remote (paid subscription —
  no bypass attempts). If a previously-working site now shows a
  WAF/login wall, stop and report; don't engineer around it.
- Don't create accounts; don't automate past CAPTCHAs or OTP challenges.
- Proxy is shared and fixed: for browser sources, run sequentially within
  your task, keep `human_delay` pacing, never parallelize page loads.
