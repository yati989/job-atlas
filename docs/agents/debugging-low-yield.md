# Debugging a low-yield source

Trigger: any source returning **fewer than ~20 jobs over the last 6 months**
(or an outright zero), or fetching many jobs but failing the relevance
scorecard, is suspicious enough to investigate before accepting either "this
site just doesn't have much" or "the central gate will clean it up."

## The core discipline

**A connector's docstring claim about what a site does or doesn't support is
a claim about the site's state *when it was tested* — not a permanent fact.**
Sites add search boxes, change pagination, go down and come back up. Before
accepting a docstring's "search is dead" / "no pagination exists" /
"thin inventory" as still true, re-verify the specific claim live. Don't
inherit a stale conclusion.

This cuts both ways: re-testing a "dead" claim and finding it still dead is
just as valid an outcome as finding it changed — the point is that the
verdict has to come from a live check, not from trusting old prose.

**Low relevance requires a native-query check.** Role-heavy drops mean the
connector may be pulling a broad inventory instead of using a supported role
query; location-heavy drops may mean a real location filter is unused. Before
accepting those drops, test the site's own search/API parameters live. When a
parameter is verified, use the canonical `SEARCH_TERMS` and supported location
values rather than repeatedly downloading an avoidably irrelevant broad feed.

## Checklist — cheapest first

Work through these in order; stop and fix as soon as one reproduces the
under-yield. Each item names where to look in the code.

1. **Is the connector's own cap below its depth tier's real ceiling?**
   Cheapest check, no live request needed — grep the connector's `__init__`
   defaults (`max_results`, `max_pages`) against the tier constants in
   `app/collectors/depth.py` (`TIER1_PAGE_CEILING`, `TIER2_MAX_PAGES` /
   `TIER2_MAX_RESULTS`, `TIER3_MAX_PAGES` / `TIER3_MAX_RESULTS`). A
   connector can be correctly wired to `tier2_cursor()` and still be capped
   well below it by a separate, older `max_results` default left over from
   when the connector was first smoke-tested.

2. **Does the connector paginate at all, or fetch one page and stop?**
   If it's a single fetch with no page loop, check live whether the site
   actually has more: a `page=`/`p=`/`offset=` URL param, a "load more"
   click, or scroll-triggered loading. Confirm by diffing job IDs between
   two pages for genuine zero overlap — a page param that silently returns
   the same results isn't real pagination.

3. **Is there a native search/filter the connector isn't using?**
   If the docstring says the site's search is "dead" or "not wired," that's
   exactly the claim to re-test live before trusting it (see: core
   discipline above). Watch the network requests a real page load or
   real search interaction fires — the underlying API call is often
   cleaner and cheaper to hit directly than scraping the rendered page.

4. **If a native search exists, is it being driven with the right terms —
   and all of them?**
   A working search endpoint wired to a single hardcoded term, a truncated
   term list, or a stale/narrow query still under-yields even though
   "search is wired" would look true on inspection. Check the registry
   instantiation (`app/pipeline/registry.py` /
   `scripts/run_headed_sources.py`) against the canonical fetch vocabulary
   in `app/config/categories.py` (`SEARCH_TERMS`) — a connector reduced to
   2-3 terms "for smoke testing" that never got expanded back out is the
   same shape of leftover as the stale caps in check 1. Also worth a live
   sanity check: does the term actually match what a human would type (the
   right phrasing, not an internal slug or an over-narrow synonym), and
   does merging a location word into the query (`"data scientist
   bangalore"`) genuinely re-rank/narrow results, or get silently ignored
   (test both, per the "zero results doesn't prove a filter works" note
   below) — a working merged-query pass can add real signal a bare-term
   search misses.

5. **Is location/India scoping wired to a real site filter, or pulling a
   global default and leaning entirely on the gate?**
   A high `location=foreign` drop share on the gate's yield log usually
   means the connector never engaged the site's own location/country
   parameter — check whether one exists before accepting the gate's drop
   rate as the ceiling.

6. **Did the fetch actually succeed?**
   Zero raw rows fetched means a block, a network blip, or a dead
   selector — not a relevance problem. Isolate and retest
   (`scripts/verify_source.py`, see below) before touching connector logic;
   a transient failure looks identical to a real block from one sample.

7. **Do selectors/fields still parse?**
   Cards returning 0 on an otherwise-200 page, or key fields (location,
   date, description) consistently empty, point at DOM drift — check the
   connector's selectors against the live page.

8. **Read the gate's drop signature as the tiebreaker.**
   The per-source yield log in `app/pipeline/runner.py`
   (`Fetched N … kept M (dropped: role/seniority/location/recency;
   location_detail foreign/no_signal)`) tells you which axis is actually
   eating the results: role-heavy drops point at checks 2–4 (wrong
   inventory source or query); `location=no_signal` points at a capture bug
   (site has real location data, connector isn't scraping it — see
   `scripts/audit_no_signal_locations.py`); `location=foreign` points at a
   genuinely out-of-scope source or a missing location filter (check 5);
   recency-heavy drops point at an over-tight date param.

## Verifying live, not by assumption

- **Watch real network traffic before concluding a param is fake.** A plain
  `curl`/static-HTML check can miss a client-rendered search entirely —
  drive it with a real browser and inspect the requests it fires
  (`page.on("request", ...)`). The underlying JSON endpoint, once found, is
  often usable directly (no browser needed at all going forward).
- **"Zero results" alone doesn't prove a filter works.** Some search
  endpoints never return a clean zero — they fall back to a ranked/fuzzy
  result set even for a nonsense query. Test a real query *and* a nonsense
  query side by side and diff the actual results, not just whether a count
  is zero.
- **Diff two pages' result IDs for exact-zero overlap** before trusting
  that pagination is real rather than the same page served twice under a
  different URL.
- **Use `scripts/verify_source.py` as the before/after yardstick.**
  Fetch-only, no DB writes; `--spec "module.path:ClassName:{json kwargs}"`
  builds a connector directly so you can test a fix without touching the
  registry. Compare `fetched`, `relevance_pct`, and the per-axis
  `relevance_drop_counts` before and after.
- **Retry before concluding a block is real.** A momentary network blip on
  your end looks identical to a genuine site block from a single failed
  request — confirm with a second attempt (and a plain connectivity check
  to an unrelated site) before writing off a source as blocked.

## When not to fix

A genuinely thin, stale, or currently-down site is a valid outcome — not
every low-yield source has a bug. The bar is that the verdict must be
*earned*: re-checked live against current site state, not carried forward
from an old docstring or a single sample. Once a check has been verified
live and still comes back negative, accept it and move on rather than
digging for a fix that isn't there.

## This is a set of directions, not a script

A source can fail every check above and still legitimately be a no-fix —
the checklist's job is to make sure that conclusion is reached by live
evidence, not inherited. Conversely, the biggest finds don't always come
from the checklist itself: a user noticing a search box in a screenshot that
a docstring claimed didn't exist is exactly the kind of signal worth
re-testing immediately, checklist order be damned.
