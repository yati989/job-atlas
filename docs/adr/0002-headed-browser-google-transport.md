# 0002: Headed-browser Google transport as the default SERP source, replacing the Apify pivot

## Status

**Superseded (2026-08-01) by [ADR 0005](0005-agentic-search-contact-sourcing.md)**
— the headed-browser transport was CAPTCHA-blocked under real batch load (the
"Empirical findings" section below is that record), and contact sourcing moved
to agent-driven web search. The transport described here is retained in the
codebase as dormant, unmaintained code, not deleted. The body of this ADR is
kept intact: the reasons it was abandoned are load-bearing context for 0005.

Originally accepted (2026-07-21).

## Context

[ADR 0001](0001-contact-discovery-search-engine.md) established search-engine
profile discovery over authenticated LinkedIn search, defaulting to
Serper.dev. That ADR also documented a real gap: Serper's *free tier* hard-
rejects both `site:` and quoted-phrase queries, forcing a loose
`Company (Title OR Title) linkedin` query shape and pushing all precision
into client-side guards. An in-flight "pivot 2" (commit `e06ac54`,
dependency-only, never wired up) was going to recover that precision by
paying for Apify's Google Search Scraper.

This ADR replaces that plan: run our **own headed `patchright` session
against `google.com/search` directly**, which has no query-syntax
restriction at all, reusing the exact anti-bot machinery
(`session_utils.launch_anonymous`) already hardened for CareerJet's
Turnstile-class gate. Same outcome as the Apify pivot (precise
`site:linkedin.com/in "Company" (…)` queries), zero per-query cost, no new
paid dependency.

`SEARCH_TRANSPORT="browser"` is now the **default** for `search_engine`
mode (`app/config/settings.py`). This is a transport choice *within*
`search_engine` mode — unrelated to and not to be confused with
`CONTACT_SOURCE="browser"`, the legacy *authenticated LinkedIn people-search*
path from before ADR 0001. The browser transport here never touches a
LinkedIn login.

## What we did

- `app/contacts/search_source.py`: new `_browser_google_search()` — headed,
  proxied, patchright, real Chrome (via `launch_anonymous`), a precise
  `site:linkedin.com/in "<Company>" (T1 OR T2 OR T3)` query
  (`_build_precise_query`), paginated (`&start=10/20/…`, capped at
  `settings.SERP_MAX_PAGES`, default 3) since a single SERP page (~10
  results, mostly non-`/in/` noise) rarely clears the 2-4-per-category
  target. On a detected block/CAPTCHA page, falls back to Serper (or DDG
  with no key) for that query.
- Selectors (`div.tF2Cxc` container / `h3.LC20lb` title /
  `div[data-sncf], div.VwiC3b, span.aCOpRe, div.MUxGbd` snippet) were
  confirmed live against real result pages before writing the parser, per
  repo convention — Google's markup has no `div.g` anymore (an older,
  previously-documented selector that no longer matches).
- The browser/page is opened once per process and reused across every
  query in a batch (looks like one person browsing, not N fresh sessions).
  Explicit teardown via `close_search_browser()`, called at the end of
  `find_contacts.py`/`smoke_test_contacts.py` and registered with `atexit`
  as a safety net.
- `_parse_profile` now also strips URL fragments, not just query params —
  the browser transport's raw results include duplicate rows for the same
  profile via Google's "jump to text" fragment
  (`...#:~:text=Aleksandr%20Volodarsky...`), which would otherwise dedupe as
  a distinct URL from the plain profile link.
- `pipeline.py`: new `MAX_CONTACTS_PER_COMPANY = 15` hard ceiling across a
  company's combined categories (user decision, 2026-07-21), truncating in
  tier-priority order (`hiring_manager` → `ic` → `exec_fallback`) so the most
  relevant contacts survive when a multi-category company's combined ladder
  walks would otherwise exceed it. Per-category `MIN`/`FETCH_CAP` (2-4)
  unchanged.
- `apify-client` and `tldextract` dropped from `requirements.txt` — neither
  was ever imported by any consuming code; the Apify pivot they were added
  for is superseded by this approach.

## Empirical findings (live testing, 2026-07-21)

**Feasibility spike (throwaway `scripts/spike_google_headed.py`, kept
untracked): 10 queries across 8 real companies, including 3 paginated pages
on one company — zero blocks, 12-19 `/in/` links per page.** Gate: PASS,
proceeded to build the real transport.

**Real batch load told a different story.** A `scripts/find_contacts.py` run
over 10 real companies (with the actual per-category primary + fallback
query pattern, each paginating up to 3 pages) hit a Google block partway
through — after roughly 5-6 companies' worth of real navigations, sooner
than the spike's flat 10-query test suggested. The block manifested as an
**interactive CAPTCHA rendered in the visible headed Chrome window**, not
just an HTTP-level rejection — meaningful because this session is headed on
the user's real desktop, not off in some invisible worker.

Two bugs this surfaced and fixed before calling the transport done:

1. **The window sat on the CAPTCHA page indefinitely.** The pipeline-level
   fallback worked correctly (contacts kept flowing via DDG), but the
   *visible* browser window was never navigated away from the block page —
   so it kept displaying an unsolved interactive CAPTCHA to the user for the
   rest of the run. Fixed: `_browser_google_search` now `page.goto(
   "about:blank")` immediately on block detection, before raising.
2. **The transport retried the browser path on every subsequent company and
   got blocked again each time** (confirmed live: 3 separate "Browser
   transport blocked" events across one 10-company batch), each one
   re-rendering a fresh CAPTCHA in the visible window before falling back.
   Fixed: a module-level `_blocked` latch — once tripped, every remaining
   call in that process skips straight to the fallback. Confirmed live this
   is correct: the block persists, not a one-off. Clears only via
   `close_search_browser()` (i.e. fresh per script invocation).

**Unresolved, and worth flagging explicitly:** the DDG fallback is not a
durable safety net once the browser transport is blocked and no
`SERPER_API_KEY` is configured — confirmed live in this same test that DDG
itself returned a bot-check page ("anomaly-modal") after 1-2 calls, the
exact ~2-request block ADR 0001 already documented. In that state, a batch
run effectively goes dark (zero contacts) for the remainder of its
companies until the next process invocation resets the latch. **A
`SERPER_API_KEY` is recommended for any real batch run** — it's the only
transport in this stack that has actually held up under repeated automated
querying so far.

The original ADR 0001 vs. connector-comment discrepancy over whether the
shared proxy is "datacenter" or "residential" remains unresolved — this
testing didn't isolate that variable. Given Google blocked under real
volume regardless, treat the proxy as unreliable at sustained load either
way, rather than resolving the labeling question.

## Consequences

- No LinkedIn account or Apify subscription touched by the default path.
  Zero per-query cost for the primary transport.
- Query precision matches what the Apify pivot was chasing
  (`site:`/quotes), for free.
- Blocking under real batch volume is real, not hypothetical — mitigated
  (not eliminated) by the fallback + latch, same "accepted flakiness"
  category as CareerJet/ZipRecruiter/FlexJobs per `CLAUDE.md`. Unlike those,
  recovery here needs a configured `SERPER_API_KEY`; without one, a blocked
  run silently starves for its remainder.
- `SERP_MAX_PAGES` (default 3) and the 15-contact company cap are starting
  defaults, same spirit as the original rate-limiting decisions in issue
  #6 — expected to be tuned once more real-world runs are observed.
- Serper is no longer the default but remains fully live as the fallback
  transport (and directly selectable via `SEARCH_TRANSPORT=serper`) — not
  deleted, same pattern ADR 0001 used for the authenticated LinkedIn path.
