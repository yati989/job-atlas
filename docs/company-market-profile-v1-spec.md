# Company market profile V1 — specification

## Objective

Persist independently observed company-market values from the two active
sources before deriving any combined value. Levels.fyi observations remain
stored for history but the source is parked:

| Source | Overall rating | WLB rating | India salary |
|---|---:|---:|---:|
| Glassdoor company overview via Bright Data | Yes | Yes | No |
| AmbitionBox | No new collection (legacy values retained) | No new collection (legacy values retained) | Yes |
| Levels.fyi (parked) | No | No | Historical only; excluded from combined values |

Indeed and stored Glassdoor job payloads are not part of this workflow.
Glassdoor pages are not opened locally; its company-overview record comes only
through Bright Data dataset `gd_l7j0bx501ockwldaqf`.

The values are directional research estimates. They do not determine whether
to apply, find contacts, or send outreach.

## Source collection rules

- Resolve and retain the Glassdoor company overview URL/ID and AmbitionBox
  slug before collection.
- Validate the returned company identity against the selected company.
- Accept ratings only in the inclusive range 1–5.
- AmbitionBox salaries must be for India and expressed in INR.
- Use Data Scientist → Data Analyst → Business Analyst for the current target,
  preserving compatible seniority or experience where the source supports it.
- A displayed AmbitionBox salary range contributes its midpoint.
- Missing values remain `NULL`. A source failure is recorded in evidence and
  does not prevent successful source values from being saved.
- AmbitionBox collection is salary-only. Before any AmbitionBox request, the
  public Naukri taxonomy endpoint resolves company names concurrently to
  canonical AmbitionBox slugs. The collector then follows the market-profile
  role ranking through at most three canonical direct-role pages, stopping at
  the first compatible salary. It reads each role page's
  `salaryData.data.summaryData` payload and never requests the broad page or
  crawls arbitrary pagination. AmbitionBox requests use one reused HTTP client
  and one global quota: one request in flight, a 1.0-second initial
  start-to-start dispatch interval, and gradual acceleration after
  sustained success to a 0.75-second floor. A 403/429 pauses all dispatches for
  at least 60 seconds
  and slows the interval; a repeated throttle opens the circuit and records
  untouched targets as deferred. A 404 is terminal for that role and advances
  to the next ranked role within the cap. Taxonomy resolution first uses the
  requested name and then at most one conservative parent-brand query. The
  exact/legal-name identity or a reviewed company-ID-keyed judgment supplies
  a canonical slug/URL, which is cached in evidence for later runs. Requested
  and observed company identities are
  both recorded; the observed AmbitionBox company is accepted even when the
  normalized identities differ. Ratings already stored from earlier runs are
  retained, but the salary-only path does not fetch new AmbitionBox ratings.
  Never launch a browser as a rate-limit fallback.
- Levels.fyi is parked. Normal and recovery runs make no Levels.fyi requests,
  preserve any historical source fields/evidence, and exclude those values
  from combined calculations and completion status.
- Resolved Glassdoor URLs are submitted automatically in one bulk Bright Data
  collection. Offline tests provide records at the Bright Data seam and never
  trigger collection.

## Stored data

Add these nullable source-specific columns to `companies`:

- `glassdoor_employer_id`
- `glassdoor_overall_rating`
- `glassdoor_wlb_rating`
- `glassdoor_review_count`
- `ambitionbox_overall_rating`
- `ambitionbox_wlb_rating`
- `ambitionbox_estimated_salary_lpa`
- `levels_fyi_estimated_salary_lpa`

Continue to retain the combined fields for a later calculation stage:

- `overall_rating`
- `wlb_rating`
- `estimated_salary_lpa`
- `market_profile_evidence`
- `market_profile_status`
- `market_profile_updated_at`

Glassdoor also supplies the existing company-level `employee_count_range`,
`revenue`, and `ownership_type` (`private` or `public`) from its overview
record. The existing `company_type` continues to mean employer versus staffing
firm; it is not an ownership field. Evidence stores the raw Glassdoor values
alongside source URLs/IDs, selected salary role, observed salary range,
experience or seniority where available, retrieval time, and source errors.
V1 keeps no history.

`market_profile_status` is the daily-pipeline selection flag:

- `pending` — never attempted;
- `in_progress` — claimed by the current batch;
- `done` — both active sources reached a terminal checked state, including
  legitimate missing values; parked Levels.fyi state is ignored;
- `partial` — at least one active source was not attempted or failed and can be
  retried.

## Interfaces and ordering

```python
scrape_market_profile(
    session,
    company_id,
    *,
    salary_role,
    source_urls,
    glassdoor_record,
    ambitionbox_record,
) -> Company
```

`glassdoor_record` is the Bright Data seam: callers supply a company-overview
record returned by the automatic bulk collection. `ambitionbox_record` is the
already parsed observation returned by `AmbitionBoxCollector.collect_salaries`; the
scraper never issues AmbitionBox HTTP calls itself. It makes no Levels.fyi
requests, preserves historical Levels.fyi state, and persists active-source
fields and evidence. It does not change any combined field.

```python
AmbitionBoxCollector.collect_salaries(targets) -> Iterator[AmbitionBoxObservation]
```

The collector owns request pacing, bounded retries, circuit breaking, URL
deduplication, observed-identity recording, and salary-page parsing. A separate
concurrent Naukri taxonomy resolver owns bounded candidate harvesting. It may
auto-resolve only exact/legal-suffix identities. Similar names require a
reviewed company-ID-keyed judgment artifact; rejected or unreviewed candidates
never produce AmbitionBox requests. Loading a reviewed artifact also bypasses
repeat taxonomy lookup for those company IDs.
The production adapters use direct HTTP; tests use in-memory transports and a
clock at the same seams.

```python
calculate_market_profile(session, company_id) -> Company
```

This explicit later stage may run only after the source-specific row has been
checked. Glassdoor is authoritative for WLB, and AmbitionBox is authoritative
for the India salary estimate. Neither field is averaged with another source.
Salary is rounded to one decimal. Overall rating remains the mean of available
Glassdoor and AmbitionBox overall ratings, rounded to one decimal.

The caller owns the transaction commit for both interfaces.

## Batch execution

```bash
python -m scripts.run_company_market_profiles --all
```

The batch runner selects eligible companies with an active job posted in the
last 15 days, claims each row, and chooses salary-role context from the
registry search vocabulary. Work mode is not an intrinsic Phase B eligibility
rule: both remote and non-remote postings qualify by default. When the user
explicitly requests a remote-only cohort, pass `--remote-only` and preserve
that same boundary across both enrichment phases. It consumes reviewed Glassdoor Overview URLs
resolved by the agent's bounded two-pass internal web search, resolves
AmbitionBox canonical slugs concurrently through Naukri taxonomy, and makes no
Levels.fyi request. Every returned active-source identity is verified before
saving. Serper and Bright Data SERP are not used by this workflow.

All resolved Glassdoor URLs use one automatic bulk dataset snapshot. The
snapshot ID is checkpointed atomically before the runner begins AmbitionBox
work. The snapshot is deliberately overlapped with concurrent Naukri taxonomy
slug resolution and the paced AmbitionBox collector; do not serially wait for
Glassdoor before starting the salary path. A restart resumes that same
checkpointed snapshot and must never submit a replacement just to save time.
Each company persists and calculates independently, so one failure does not
roll back the batch. Use `--resume-in-progress` after an interrupted run.

An AmbitionBox-only recovery path selects 403/429, deferred, not-attempted,
unresolved legacy 404, and legacy successful-broad-page rows whose salary is
still `NULL`, then commits each result independently:

```bash
python -m scripts.run_company_market_profiles --ambitionbox-only --all \
  --ambitionbox-judgments /path/to/reviewed-naukri-judgments.json
```

This path must never perform Glassdoor SERP resolution, trigger a Bright Data
snapshot, or fetch Levels.fyi. It retains any stored ratings/evidence and
requests only the ranked direct-role URLs, stopping on success or after three
profiles; it does not download the broad page. Titles without a deterministic
route are recorded as terminal missing without an AmbitionBox request.
Canonical candidates are harvested through the concurrent Naukri taxonomy
stage, then pass the exact-identity or reviewed-judgment gate. Rows that already
record a missing resolution are not selected again.

The retained `--levels-fyi-only` option exits with a policy error while the
source is parked. Reactivating it requires an explicit user decision and a
corresponding specification change.

## Display

Keep the existing combined columns in the company Explore table and CSV export.
Source-specific values remain inspectable in the company row/evidence until the
combination rule is approved. Add no new screen, filters, confidence score, or
company metadata.

## Implementation limits

- Seven source-specific ORM columns and the matching manual SQL migration;
  Glassdoor also refreshes the existing employee-count, ownership, and revenue
  metadata fields.
- One source collection module, one adaptive AmbitionBox collection module,
  and one calculation/persistence module.
- Focused offline parser/interface tests only.
- No live migration, paid collection in tests, local Glassdoor browser,
  identity-search framework, extra tables, history, checkpoints, or inferred
  values.
