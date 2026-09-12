# Carry city, India-wide, and remote preferences into collection

Status: accepted, 2026-09-07 (explicit user request)

The public profile previously accepted Indian cities, but many source adapters
still constructed the private Bengaluru/remote query pair. Capability metadata
also rejected other cities. IIMJobs and eFinancialCareers were advertised as
city-agnostic while retaining hidden Bengaluru filters. These restrictions are
implementation defects, not claims about the boards' available geography.

## Contract

- `countries: [IN]` remains the public country boundary.
- With onsite/hybrid selected, each city creates a native city query. An empty
  city list means country-wide local work, not Bengaluru and not remote.
- Remote is a separate India-eligibility choice, independent of local cities.
- A mixed local-or-remote request can use a remote-only board for its remote
  portion. The exact source preview counts only supported query work.
- Source adapters preserve legacy modes and positional arguments. New city
  modes take an explicit keyword-only `location`; country-wide mode is `india`.
- Planning and constructors are network-free. Numeric IDs/coordinates needed
  by Glassdoor, IIMJobs and eFinancialCareers resolve during collection from
  source-owned data. Resolution errors must not become a Bengaluru fallback.
- Native query fan-out and preview counts agree. Connector progress/evidence
  dimensions include the requested city. Completed guided sources remain
  reusable on resume.
- Foundit and TimesJobs receive the same accepted relevance policy for their
  pre-detail screens, so the old personal role/location gate cannot suppress
  descriptions for valid public-profile jobs.

Native city filtering is not a guarantee that every returned job matches.
Promoted rows and broad fallbacks are screened by the frozen public profile.
Shine's tested remote keywords did not reliably filter inventory; its remote
mode uses the broad feed and never stamps all rows remote. Country-wide
collection likewise does not prove country eligibility by itself.

The central gate recognizes common city aliases and an attributed offline
[Indian city reference](../source-evidence/india-city-reference.md). Arbitrary
user-requested cities are still accepted; the reference is evidence recognition,
not a query allowlist. Explicit foreign-country evidence takes precedence over
a matching city name. Unknown country-less locations remain reviewable.

## Verification

`tests/test_india_search_locations.py` exercises actual profile-to-connector
construction for single-city, multi-city, India-wide, remote and mixed searches.
It also drives frozen-plan collection, relevance, SQLite storage, and resume
through the real connector factory with fixture network results.

Live source checks are recorded under `docs/source-evidence/` and the linked
source notes. The existing `verify_source` script still evaluates relevance
using the legacy personal policy; its Bengaluru-based scorecard must not be
represented as the public Pune profile's relevance result. A separate public
guided-run test was performed with bounded Naukri digital-marketing queries:

- Pune-or-remote: 60 fetched, 28 kept, 20 reviewable (remote country unspecified),
  12 rejected on title; resume made zero additional requests.
- India-wide: 30 fetched. Initially 21 kept and nine country-less smaller-city
  listings went to review. Replaying the same saved source evidence through
  the final offline city-reference gate stored all 30, without recollection.
- Indeed's direct live page returned an access challenge. Its request and UI
  fallback paths have focused offline tests; attended live verification remains
  incomplete. No source activation status was changed.

This validates location collection and screening. It is not a claim that the
separate enrichment, tailoring, and export launch rehearsal has been completed.
