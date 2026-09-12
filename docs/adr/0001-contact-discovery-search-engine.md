# 0001: Contact discovery via search-engine profile discovery, not authenticated LinkedIn search

## Status

Accepted (2026-07-17)

## Context

Milestone 2 (contact-finding, issue #6) originally drove LinkedIn's people-search
while logged in as one dedicated account (headed `patchright` + session
persistence, the same pattern as `cutshort.py`). That approach hit two hard
walls in the same session: LinkedIn's per-account **commercial-use search
limit** was exhausted, and the account then got stuck behind a **login
challenge** — the exact block/challenge signal the batch loop was designed to
stop on. A single authenticated identity is structurally capped at a few
hundred searches a month; it cannot clear a ~780-company backlog, and hitting
the cap risked the account being locked for the user's own manual LinkedIn use
too.

## What we considered

Every "no-account LinkedIn data" product (HarvestAPI, Proxycurl, and similar
Apify actors) works the same way underneath, regardless of marketing copy:

1. **A farm of real LinkedIn accounts behind rotating residential/mobile
   proxies.** This is what actually gets around the per-account limit — many
   identities, each staying under quota, fronted by IPs that look like real
   home users (not the datacenter proxy this repo already uses for the
   anonymous-but-bot-blocked connectors). Real ban risk to any account
   involved; against LinkedIn's ToS; the option we explicitly ruled out.
2. **A paid SERP-style API** (HarvestAPI itself, Proxycurl) — pay per profile,
   no account risk, but doesn't teach the underlying technique and has a
   nonzero cost at our volume.
3. **Search-engine discovery of public profiles** — search engines already
   index public LinkedIn profile pages. A query like `Quantzig Data Scientist
   linkedin` returns rows whose title/snippet already carry name + headline +
   company + profile URL, with **no LinkedIn session touched at all**. This
   is what we built.

We chose (3): it needs no LinkedIn account (nothing to rate-limit or lock),
it's honestly public-data-via-public-search rather than an auth bypass or
account-farming, and building it ourselves — rather than renting (2) — is the
whole point, since the goal here was learning the technique, not just getting
contacts.

## The technique

`app/contacts/search_source.py` issues one broad query per company/category
(not one per ladder rung — see below) against a search API, parses each
result row into a `RawProfile` (name, headline, URL), and hands the list to
`app/contacts/people_search.classify_profiles`, which reuses the *exact* tier-
validation logic (`_title_matches`, `_mentions_company`, the leadership/
recruiter word-sets) hardened over four rounds of live QA on the authenticated
path. Only the fetch step changed; everything downstream — email guessing,
upsert/dedup, the batch runner's eligibility query and status handling — is
source-agnostic and untouched.

### Transport: why Serper, not Brave or Google CSE

Checked live during implementation, 2026: Brave Search API killed its free
tier in February 2026 (now requires a card, metered billing from the first
query); Google's own Custom Search JSON API is closed to new signups. Serper
was the option that actually worked with no card and no waiting: 2,500 free
queries on signup, real Google organic results (Google indexes LinkedIn
profiles more thoroughly than Bing/DDG). At one query per company/category,
the free credit alone covers the ~780-company backlog.

DuckDuckGo's HTML endpoint is kept as a zero-setup, no-key fallback for
one-off spot-checks, but **confirmed live it is not viable at any real
volume**: a second automated request in the same session got a CAPTCHA
("anomaly-modal" page), even through this repo's own configured proxy —
because that proxy is a datacenter IP, and datacenter IPs are exactly what
gets flagged fast. This is the same lesson as (1) above in miniature: getting
around search-engine bot detection at volume needs residential IPs and
fingerprint-matched browser behavior, which is real infrastructure spend, not
a request-header trick.

### An undocumented Serper free-tier restriction

Not in Serper's published docs, confirmed live: **quoted phrases and the
`site:` operator both get hard-rejected** on the free tier ("Query pattern not
allowed for free accounts", HTTP 400). This ruled out the originally-planned
query shape (`"Company" ("Title A" OR "Title B") site:linkedin.com/in`).
Unquoted `OR` inside parentheses is accepted, so the actual query shape is
`Company (Title A OR Title B OR Title C) linkedin` — and the precision that
quoting/`site:` would have provided is done client-side instead:
- the `/in/` URL-pattern check in `_parse_profile` replaces `site:`;
- a company-mention check (`_mentions_company` in `search_source.py`, a
  different function from `people_search.py`'s of the same name, doing the
  analogous job on SERP rows) replaces the quoted company name.

### Query-length tuning (a real, counter-intuitive finding)

More OR-terms is not better. An 8-term query (2 titles pulled from every
ladder rung) returned **zero** usable `/in/` profiles for a real company —
buried entirely under generic "999 jobs in India" aggregator pages, which
Google apparently ranks higher once an OR clause gets long. The identical
intent with 3 terms (one title from `hiring_manager`, one from `ic`, one from
`exec_fallback` — `talent_acquisition` deliberately excluded, since recruiter
profiles surface as incidental noise regardless) surfaced the same company's
actual Director of Data Science and Co-founder/CEO cleanly. This is now
`pipeline._query_terms_for_ladder`'s fixed shape and
`search_source.MAX_QUERY_TITLE_TERMS = 3`.

### The company-mention filter, and its "former employer" failure mode

Dropping quoted phrases means a bare `ICICI Bank Data Scientist linkedin`
query can rank someone at a **different** company who merely matches on role
words. The naive fix — checking if any company-name word appears anywhere in
title+snippet — has the same failure mode the authenticated path already hit
and fixed (excluding "former"/"ex" headline segments): a candidate's bio
*snippet* can legitimately mention the target company as a **past** employer
("With over 5 years at ICICI Bank...") while their *title* states a different
current employer ("...@ Lloyds Banking Group..."). The fix mirrors that
precedent: if the (suffix-stripped) title states any specific current
employer at all — via `@`/`at`, or LinkedIn's own `<Role> | <Company>` title
convention — that title is authoritative and the snippet is not consulted.
The snippet is trusted only when the title carries no employer signal
whatsoever (a skills-only headline with nothing to check). Also fixed along
the way: generic corporate words ("bank", "technologies", "solutions",
"group"...) are excluded from the company-name word set, the same class of
fix as `people_search._GENERIC_ROLE_WORDS` — "ICICI Bank" degenerating to
just `{"bank"}` had let a Lloyds Banking profile through purely on that word.

## Consequences

- No LinkedIn account is touched by the default contact-finding path
  (`CONTACT_SOURCE=search_engine`) — nothing to rate-limit or lock, and no
  ongoing risk to the user's own LinkedIn account.
- The old authenticated browser path is kept (`CONTACT_SOURCE=browser`,
  `app/contacts/linkedin_auth.py` / `company_resolution.py` /
  `people_search.search_people_for_ladder`) as a selectable fallback, not
  deleted — it is fully tested, tier-validated code that may still be useful
  once the account's limit resets, or for a source this approach can't find.
- **Coverage is a subset of authenticated search** — only profiles the search
  engine has indexed and that rank inside the query's top ~20 results.
  Thinner/newer/small companies (confirmed live: an HR/staffing company
  returned zero contacts across repeated query shapes) may legitimately
  yield nothing, distinct from a broken search.
- **SERP data can lag reality** the same way an authenticated headline could
  — someone's indexed title may be stale. No worse than the prior approach.
- Sister-entity ambiguity is an accepted limit, not fully solved: a company
  name that's a single distinctive word shared across a corporate family
  (e.g. "ICICI Bank" vs. "ICICI Securities") can still cross-match, the same
  class of edge case as the authenticated path's "Founder & CEO of a
  different company" gap (`_mentions_company`'s exec_fallback check there).
- This is public-data discovery via public search results — not an
  authentication bypass, and not account-farming. That boundary is the
  reason this approach was chosen over the alternatives in "What we
  considered."
