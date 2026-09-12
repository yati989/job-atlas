# 0005: Agentic search as the contact-sourcing mechanism, superseding the headed-browser and managed-SERP transports

## Status

Accepted (2026-08-01). **Supersedes [ADR 0002](0002-headed-browser-google-transport.md)**
(headed-browser Google transport) and supersedes the *transport and
classification* decisions of [ADR 0001](0001-contact-discovery-search-engine.md)
— though 0001's query-shape findings survive and are reused below. Spec: issue
#68, which supersedes the sourcing and storage decisions of #20 (which in turn
superseded #6's). #6's data model, per-company seam, and role-ladder concept are
unchanged.

> **Numbering note:** this file is numbered 0005 rather than 0003 because the
> contact-finding branch and `main` forked their ADR sequences — both have a
> different `0002`, and `main` already holds `0003-two-query-search-structure`
> and `0004-relevance-drift-search-model`. 0005 is the first number free on both
> sides, so this ADR keeps its identity through the eventual merge. The
> duplicate `0002` is a pre-existing collision this ADR does not resolve.

## Context

### Three transports failed, each for an unrelated reason

This is the **fourth** sourcing approach for contact discovery. The three
before it were abandoned for causes that share nothing with each other, which
is itself the central argument of this ADR:

1. **Authenticated LinkedIn people-search** (issue #7, the pre-ADR-0001 path) —
   abandoned on an **account limit**. The dedicated account exhausted LinkedIn's
   per-account commercial-use search quota and then got stuck behind a login
   challenge. A single authenticated identity is structurally capped at a few
   hundred searches a month; the backlog is ~3,800 companies. Hitting the cap
   also risked locking the account for the user's own manual use.
2. **Serper.dev free tier** (ADR 0001) — abandoned on a **query-syntax
   restriction**. The free tier hard-rejects both `site:` and quoted phrases
   (undocumented; confirmed live). That forced a loose
   `Company (Title OR Title OR Title) linkedin` query shape and pushed every bit
   of precision into client-side string heuristics — the root of the second
   problem below.
3. **Headed `patchright` against `google.com/search`** (ADR 0002) — abandoned on
   **bot detection**. A 10-query feasibility spike passed cleanly with zero
   blocks, but a real batch run hit an interactive CAPTCHA after roughly 5-6
   companies' worth of navigations, in the visible browser window on the user's
   own desktop. The DDG fallback was confirmed to be no safety net (bot-checked
   after 1-2 calls), so a blocked run went dark for its remainder unless a
   Serper key was configured.

The planned fourth transport was **Bright Data's managed SERP API** (issue #23,
specified in #20). It was never built: no account was ever created, and it means
paying a recurring bill purely to look up public profiles. It is closed as
superseded by this ADR.

An **Apify** pivot was planned and abandoned *before* being built (commit
`e06ac54`, dependency-only). The leftover `APIFY_API_TOKEN` in `.env` is from it
and is dead.

### The quality problem no transport could fix

Independently of sourcing, the contacts the module *did* produce were low-trust
for a structural reason. "Is this person actually a data-science hiring manager
at this company, right now?" is a judgement call, and the module approximates it
with several hundred lines of string matching — `people_search.py`'s
`_title_matches` / `_mentions_company` / leadership- and recruiter-word sets /
former-employer segment stripping, plus `search_source.py`'s separately-written
`_mentions_company` doing the analogous job on SERP rows.

Those heuristics have demonstrably accepted a person at one bank for a search
against a *different* bank, and a founder of an unrelated side company as an
executive at the target company — in both cases because a single shared word
passed the check. Four rounds of live-QA hardening (commits `f3450cc`,
`e28b99f`, `730672b`, `e0072cd`) narrowed the class without eliminating it. It
is also unauditable: the user cannot tell from a stored row whether the person
is real.

## Decision

**Contact discovery becomes the agent's own in-session web search and
reasoning**, run in bounded batches of ten companies, invoked manually — never
an unattended script, never a billed external API. This mirrors the model the
`enrich-jobs` and `enrich-company-domains` skills already use in this repo.

The choice follows directly from the failure analysis: an approach that owns no
account, issues no automated requests from this machine, and pays no vendor
cannot fail in any of the three ways above.

The same change also **replaces the entire heuristic classification stack with
direct judgement**. The agent reads each result and decides whether the person
is genuinely at that company and which tier they occupy, which removes the
word-overlap false-accept class rather than narrowing it again.

Carried forward from the earlier ADRs, because they remain true:

- **Queries stay narrow, ~3 title terms.** ADR 0001's counter-intuitive finding
  — an 8-term disjunctive query returned *zero* usable profiles, buried under
  job-aggregator pages, where the same intent in 3 terms surfaced real people —
  still holds. It is precisely why one query cannot serve all five tiers, and
  why the search budget is per-tier.
- **Restriction to the professional-network domain uses the search tool's
  native domain filter, not an in-query `site:` operator.** Confirmed live: the
  in-query operator is largely ignored and returns job-board pages instead. This
  is a *new* finding that inverts ADR 0002's premise — 0002 chose the headed
  browser specifically to regain `site:`, and `site:` turns out not to be the
  thing worth regaining.

### Deliverability is demoted from a storage gate to a stored attribute

This **reverses the corresponding decision in #20**, which was built and
verified (commit `fed65fb`) before the reversal.

- **#20's rule:** `derive_email()` returns `None` for anything provably
  undeliverable or underivable, and `pipeline._gate_by_deliverability` *drops*
  those contacts. Deliverability decided whether a contact was stored at all.
- **The new rule:** a contact with no derivable email is **stored anyway**, with
  the email left empty and its confidence recorded. A verified-real hiring
  manager behind a probe-blocking mail server is still a lead worth having — the
  profile URL is a usable route even when the address-guess fails.
- Quota counts contacts that *have* an email, at **any** confidence. Requiring
  positive confirmation would make a large share of companies structurally
  unsatisfiable, because catch-all servers accept every address and can
  therefore never confirm a specific one — measured at 3 of the 9 domains
  resolved so far (ibm.com, emerson.com, chicmicstudios.in).

The derivation machinery itself is reused **unchanged**: multi-pattern
candidates, the per-domain MX/catch-all cache, and the SMTP probe all stay
exactly as verified. Only the caller's treatment of the result changes.

### The previous stack is retained dormant, indefinitely

`search_source.py`, `people_search.py`, `linkedin_auth.py`, and
`company_resolution.py` are **left in place and not deleted**, still selectable
through the existing `CONTACT_SOURCE` / `SEARCH_TRANSPORT` settings. They are
retained **indefinitely**, not "until the trial passes" — the point is that a
fallback sourcing path stays on hand without any further decision or rebuild if
agentic search disappoints.

**The tradeoff is named and accepted:** the project carries two sourcing stacks,
only one of which is exercised. The dormant one is **not maintained** and is
expected to bit-rot — its selectors will decay, its transports will keep
failing the way they already do, and nothing will notice. That is the price of
keeping the option, and it is deliberately preferred over deleting working,
tier-validated code that took four rounds of QA to harden. This is the same
"keep it, don't delete it" pattern ADR 0001 used for the authenticated LinkedIn
path and ADR 0002 used for Serper.

## Consequences

- **The thing being trusted is now judgement, which code review cannot audit.**
  This is why the ten-company human trial gate (#75) is a *required* step rather
  than a recommendation, and why the per-contact evidence trail — profile link,
  the text judged, tier and rationale, the search that surfaced them, the
  derived email — is a deliverable rather than a nicety. A standing per-batch
  spot-check holds later batches to the trial's bar.
- **Agentic search may find *fewer* people per query, not more.** The quality
  claim is not recall; it is that the accept/reject decision stops being a
  string-matching approximation of a judgement call and becomes the judgement
  call.
- **Throughput is bounded by conversation turns**, not by quota or cost —
  realistically hundreds of companies per session against ~3,800 eligible. The
  backlog is not expected to be exhausted. That is why prioritisation (#72) is a
  first-class part of the spec rather than an optimisation: the ordering
  determines what actually gets done.
- **Zero recurring cost and no credentials.** #23 is closed as superseded and no
  Bright Data account is created. The epic is no longer blocked on the user
  obtaining anything.
- **Scraped profile text is untrusted input.** Third-party page content must
  never be interpreted as instructions to the agent — a risk that simply did not
  exist when a regex was doing the reading.
- **Domain resolution moves inline** into each contact batch (for companies
  lacking a domain) rather than running as a separate multi-thousand-company
  grind, so the two backlogs are worked as one.
- **ADR 0002 is marked superseded with its body intact**, as is the convention
  here — the record of *why* the headed-browser transport was abandoned is the
  first half of this ADR's argument and must survive.

## Addendum (2026-08-02): search transport revised, judgement model unchanged

The trial (#75) surfaced two problems with pieces of this ADR that are being
corrected without reopening the core decision above — the agent still does
its own reading and tiering of every result; only how results and email
guesses are produced changes.

**Search transport.** The native `WebSearch` tool this ADR specified has no
result-count control and was confirmed live to be non-deterministic — an
identical query returned zero usable results once, then real profiles on an
unchanged same-day re-run. The user has since funded a Bright Data account,
narrowing the "zero recurring cost" consequence above to "zero cost other
than this one paid transport." `search_source.py`'s dormant
`_browser_google_search` selectors are being reused against Bright Data's raw
Google HTML passthrough (real Google, not a tool-interpretation layer, so
`site:` and quoted phrases work as this ADR originally expected of a direct
transport). Its `_parse_profile`/`_mentions_company` heuristic filter is
explicitly **not** reused — carrying that forward would reintroduce exactly
the string-matching false-accept class this ADR replaced with agent
judgement. Only structural filtering (is this URL shape a `/in/` profile,
not a job posting) is reused from that module.

**Deliverability's SMTP probe is dead, not merely demoted.** The "derivation
machinery... reused unchanged" claim above assumed the SMTP RCPT probe could
run; a live diagnostic (2026-08-02) found a raw TCP connect to port 25 times
out for every domain tested, including `gmail.com` as a control — an
outbound port block on the network the agent runs from, not target-server
defensiveness, so the probe can never complete here regardless of target.
Verification becomes MX-only (does the domain have mail records at all).
The email-pattern-from-a-leaked-example mechanism built during the #75 trial
(`pattern_leaked` confidence, `Company.email_pattern_1/2/3`) is also being
dropped going forward — it found nothing for 10 of 10 companies outside the
Indian IT-services profile it was built against. In its place: a fixed set
of 4 candidate address patterns per contact, computed on demand from
`Contact.first_name`/`last_name` and shown together for the human reviewer
to pick from, rather than the system silently picking one guess. This ADR's
"deliverability is a stored attribute, not a storage gate" decision is
unaffected — contacts with no confirmable email are still stored.
