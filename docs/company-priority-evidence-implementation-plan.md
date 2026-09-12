# Company rating and total-CTC evidence: implementation plan

## Outcome

Build a bounded evidence layer that answers two questions for a specific
company and job:

1. What published work-life-balance rating does the company have?
2. What recurring annual total compensation is plausible for this job's role,
   seniority, and India location?

This is evidence infrastructure for the later P1-P5 company-priority policy.
It does **not** choose WLB thresholds, salary pass rules, resume-score cut-offs,
or final priority bands in this phase.

The user-facing entrypoint remains the repo-authored `enrich-companies` skill.
Source collection and aggregation live behind a separate deep module so that
company profiling does not absorb browser, quota, normalization, and history
details.

## Confirmed v1 boundary

### Included

- Glassdoor: company overall, WLB, and work-culture ratings; relevant salary
  aggregates.
- AmbitionBox: the same published rating categories where present; relevant
  salary aggregates.
- Indeed: the same published rating categories where present; relevant salary
  aggregates.
- Levels.fyi: salary/compensation only.
- The job description or official careers page: authoritative compensation
  when it explicitly states the value and basis.
- Exact role/title pages first; department pages as labelled fallbacks.
- One current value plus a changed-value history.
- Per-source values, consensus values, confidence, disagreement, freshness,
  sample counts, scope, and evidence URLs.
- Active, canonical jobs from the existing 60-day window, with at most the
  three best candidate jobs researched per company.

### Explicitly excluded

- Individual reviews, review text, reviewer identity, helpful votes, and NLP.
- Saved page HTML, routine screenshots, and page archives.
- Invented bonus, stock, or allowances for a base-only salary.
- Combining incompatible pay bases.
- Guessing an aggregator profile when company identity is ambiguous.
- P1-P5 assignment and contact/outreach gating until their thresholds and
  resume-score treatment are decided.

The earlier research note in
`docs/research/company-wlb-and-total-ctc-evidence-system.md` remains the source
background. This plan supersedes its review-corpus/NLP proposal for v1.

## Domain model

Use these terms consistently:

- **Company evidence target**: a verified company plus up to three active jobs
  whose role/location/seniority determine which salary pages are relevant.
- **Source profile**: the verified Glassdoor, AmbitionBox, Indeed, or Levels.fyi
  page representing the same employing entity.
- **Rating snapshot**: aggregate company ratings displayed by one source at a
  particular scope and time.
- **Salary snapshot**: a displayed compensation aggregate for one company,
  role family, seniority, location, and pay basis.
- **Pay basis**: `total_ctc`, `base_plus_additional`, `base_only`,
  `annual_salary_ambiguous`, or `unknown`.
- **Job evidence assessment**: derived WLB and salary evidence for one job.
- **Company evidence assessment**: the best single job assessment for a
  company. Criteria from different jobs are never mixed.
- **Unknown**: no verified or compatible evidence. Unknown never silently
  becomes zero, below-threshold, or passing.

The role-family order used to pick research targets is:

1. AI/ML Engineering
2. Data Science
3. Credit Risk
4. Data Engineering
5. Analytics

## Architecture

Create `app/company_evidence/` as the deep module. Its small public surface
should be sufficient for the skill, dashboard, and later priority engine:

```python
targets = select_evidence_targets(session, company_ids=None, limit=15)
result = refresh_company_evidence(session, target, sources=None)
assessment = assess_job_evidence(session, job_id)
best = select_best_company_assessment(session, company_id)
```

Callers must not know how browser profiles, source URLs, quotas, snapshots, or
pay compatibility work.

Suggested internal layout:

```text
app/company_evidence/
  __init__.py             # deliberately small public API
  types.py                # immutable collection and assessment value objects
  role_family.py          # title -> normalized role family and rank
  targets.py              # active/nonduplicate/top-three target selection
  identity.py             # source-profile match rules and unresolved state
  normalization.py        # rating, currency, period, and pay-basis rules
  aggregation.py          # pure WLB/salary consensus and confidence logic
  persistence.py          # current snapshot + changed-history semantics
  quota.py                # rolling quotas, global/source concurrency limits
  expiry.py               # permission stop and scoped evidence purge
  service.py              # orchestration behind refresh_company_evidence
  cli.py                  # bounded seam invoked by enrich-companies
  sources/
    base.py               # typed source adapter protocol
    levels.py
    indeed.py
    glassdoor.py
    ambitionbox.py
```

The adapters return typed snapshots. They do not write SQL rows and do not
decide consensus, confidence, or priority.

### Skill seam

Extend `.claude/skills/enrich-companies/SKILL.md` after canonical-domain and
employing-entity resolution:

1. Select the company's top three evidence-target jobs.
2. Resolve or verify source profiles.
3. Invoke the bounded company-evidence collection seam.
4. Report rating/salary coverage and unresolved profiles.

`enrich-companies` continues to own company facts, domain, company type,
contact-search groups, and pain points. The new module owns published ratings,
salary evidence, and derived evidence assessments. `enrich-jobs` remains
job-row-only.

## Persistence model

Do not add dozens of sparse source fields to `companies`. Add normalized
tables and keep derived values rebuildable.

### `company_source_profiles`

One current identity resolution per company and source:

- `id`, `company_id`, `source`
- `source_company_id`, `profile_url`, `display_name`
- `match_status`: `verified`, `unresolved`, `rejected`
- `match_basis`: domain/name-alias/location/industry evidence labels
- `first_seen_at`, `last_checked_at`, `verified_at`
- unique `(company_id, source)`

Only `verified` profiles may feed evidence. Similar names, subsidiaries,
staffing/client ambiguity, or inconsistent metadata produce `unresolved`.

### `company_rating_snapshots`

One current row per company/source/scope plus rows created only when content
changes:

- identity: `company_id`, `source_profile_id`, `source`
- scope: `scope_kind`, `role_family`, `location_scope`
- values: `overall_rating`, `wlb_rating`, `work_culture_rating`, `scale_max`
- support: `rating_count`, `review_count`, `count_basis`,
  `source_updated_at`, `source_url`
- lifecycle: `content_hash`, `is_current`, `first_seen_at`,
  `last_checked_at`, `last_changed_at`

`count_basis` distinguishes an actual category-rating sample from a source's
broader company review count. Do not claim a role- or location-specific rating
unless the page explicitly labels the aggregate at that scope. A filtered list
of reviews is not proof that the headline rating was recalculated.

### `company_salary_snapshots`

One current row per company/source/role/seniority/location/pay basis, again
with new history rows only on change:

- identity: `company_id`, `source_profile_id`, `source`
- target: `role_family`, `source_role_label`, `seniority_scope`,
  `location_scope`, `match_kind` (`exact_role` or `department_fallback`)
- original display: `displayed_text`, `currency`, `period`, `pay_basis`
- normalized annual INR: `low_lpa`, `mid_lpa`, `high_lpa`
- optional components: `base_lpa`, `bonus_lpa`, `equity_lpa`,
  `other_recurring_lpa`, `one_time_lpa`
- support: `sample_count`, `source_updated_at`, `source_url`
- lifecycle: the same hash/current/timestamps as rating snapshots

Do not infer missing components. Public stock may be annualized when the
source provides the value and vesting period. Private-option paper value stays
separate/uncertain and cannot independently prove recurring CTC.

### `company_source_collection_state`

The resumability and operational record:

- `company_id`, `source`, `ratings_status`, `salary_status`
- `last_attempt_at`, `last_success_at`, `next_refresh_at`
- `attempt_count`, `failure_code`, `failure_detail_safe`
- `checkpoint`, `session_status`
- unique `(company_id, source)`

Failure details must exclude credentials, cookies, query strings, and page
content.

### `job_evidence_assessments`

One rebuildable current assessment per job:

- `job_id`, `policy_version`, `assessed_at`
- role and work context: `role_family`, `role_rank`, `work_mode`
- WLB: `wlb_consensus_rating`, `wlb_source_count`, `wlb_rating_spread`,
  `wlb_confidence`, `wlb_freshest_at`
- salary: `ctc_low_lpa`, `ctc_mid_lpa`, `ctc_high_lpa`,
  `salary_evidence_status`, `salary_confidence`, `salary_freshest_at`
- provenance: source snapshot IDs used in each calculation

Do not store WLB pass/fail, salary pass/fail, or priority band yet. Those are
policy outputs, not evidence facts.

The company view selects and records `best_job_id` from these rows, but the
job assessment remains the source of truth. This prevents a remote job from
supplying work mode while another job supplies salary or role rank.

## Collection and normalization rules

### Target selection

- Use `status='active'`, `duplicate_of_job_id IS NULL`, and the configured
  60-day recency window.
- Rank candidate jobs by role-family order, work mode, recency, and evidence
  completeness.
- Research no more than three jobs per company.
- Reassess when a materially newer or higher-ranked job appears.

### WLB

- Store Glassdoor, Indeed, and AmbitionBox separately.
- Normalize to a 1-5 scale only when a source explicitly publishes its scale.
- `wlb_consensus_rating` is the median of current compatible source ratings.
- Overall and work-culture ratings remain independent fields and never fill a
  missing WLB value.
- Preserve `wlb_source_count` and max-min `wlb_rating_spread`.
- Confidence is separate from the number:
  - high: multiple fresh, sufficiently supported, closely agreeing sources;
  - medium: one strong source or moderate disagreement;
  - low: small support, stale evidence, or large disagreement;
  - unknown: no verified WLB rating.
- Initial exact count/spread boundaries remain configuration values to be set
  after a pilot distribution is reviewed.

### Salary/CTC

Evidence precedence:

1. Explicit exact-JD recurring total CTC.
2. Levels.fyi compatible company/role/level/India total compensation.
3. Glassdoor total pay with compatible component/basis labels.
4. AmbitionBox annual salary with ambiguous components, supporting only.
5. Indeed base salary, supporting only.
6. Adjacent roles/levels and market figures, context only.

Recurring CTC is base + target bonus + annualized equity + recurring fixed
allowances. One-time/sign-on/relocation values are stored separately.

For compatible evidence only:

- preserve each source's low/mid/high values;
- build a per-source range first;
- use the median of source midpoints for the central estimate;
- derive a consensus low/high without letting a high-volume source swamp the
  others;
- keep contradictions visible;
- let exact-JD total CTC override external estimates while retaining them for
  comparison.

Salary confidence is based on exact company/role/seniority/India match, pay
basis, freshness, sample count, compatible-source count, and agreement. It
does not modify the displayed estimate.

### Freshness and failure

- Salary evidence: prefer observations/pages updated within 24 months;
  schedule refresh after 90 days.
- WLB aggregates: schedule refresh after 180 days.
- A materially newer target JD may force earlier reassessment.
- On refresh failure, retain the last successful value and mark it stale.
- Never replace a value with zero or `not found` because a page failed.

## Authenticated browser and permission controls

- Use one isolated dummy-account persistent browser profile per authenticated
  source under the existing gitignored browser-session area.
- The first login is attended. Credentials are typed into the site and are
  never stored in the DB, repo, logs, or chat.
- Validate session state before a collection run. Stop and require attended
  re-login when invalid.
- Do not bypass CAPTCHAs or challenges. Pause for attended handling if one
  unexpectedly appears.
- Enforce eight workers globally, start at two per source, and allow no more
  than four on one source.
- Use a rolling per-source ceiling of 8,000 requests per 12 hours, leaving a
  safety margin below the user-attested 10,000 allowance.
- Checkpoint after every completed company/source unit.
- Pause a source on repeated throttling, access-denied responses, unexpected
  page shape, or session loss.
- Encode only the user-attested operational facts, not the confidential
  permission document: allowed source, aggregate field classes, quota, and
  expiry date.
- Hard-stop Glassdoor, Indeed, and AmbitionBox collection no later than
  7 July 2028. Purge evidence derived from those three permission-based
  sources at expiry while leaving core company/job rows and independently
  authorized Levels.fyi evidence intact. Rebuild affected derived assessments
  from whatever eligible evidence remains. The purge path must be scoped,
  idempotent, counted, and tested before it is wired to the expiry action.

## Dashboard contract

Extend the company Explore surface rather than creating a second competing
company table. Add:

- best supporting job title and clickable JD URL;
- role family and work mode;
- per-source WLB ratings;
- WLB consensus, source count, spread, confidence, and freshness;
- per-source salary ranges and pay-basis labels;
- consensus CTC low/mid/high, evidence status, confidence, and freshness;
- unresolved/stale flags and evidence links.

Allow sorting by WLB consensus, CTC midpoint/high, confidence, and freshness.
Do not add a WLB pass filter or priority selector until the thresholds and
P1-P5 policy are approved.

Keep dashboard query functions Streamlit-free and update
`scripts.verify_dashboard_queries` for every added query path.

## Pipeline integration

The full pipeline currently enriches every new company and then performs
billed contact search for every new company. Introduce evidence collection
inside the enrichment stage, but do **not** silently change contact/outreach
eligibility in this phase.

Proposed stage sequence:

```text
ingest -> job/company dedup -> enrich jobs -> enrich company profile
       -> collect company evidence -> assess jobs -> skills dedup
       -> tailor -> find contacts -> draft -> report
```

Record company-evidence coverage in the existing `enrich` stage detail until
it merits a separately reported stage. The later priority-engine change will
explicitly replace the full pipeline's current no-gate contact behavior after
the user approves thresholds, bands, and selection semantics.

## Implementation phases

### Phase 0: pilot contract and source discovery

1. Select a fixed pilot of 10 companies covering large/small employers,
   staffing, ambiguous names, strong/weak source coverage, and the five role
   families.
2. Produce a current-state browser specification for one source at a time.
3. Confirm visible fields, pagination/filter semantics, authentication state,
   pay labels, and stable identifiers against the live site.
4. Do not write a parser for fields the browser tester cannot verify.

Browser-dependent work follows `docs/agents/codex-scraper-development.md`:
discovery gate, planner, implementer, and independent `browser_tester` verdict.
The implementer cannot certify its own browser correctness.

### Phase 1: schema and pure policy core

1. Add ORM tables and an explicit manual Postgres migration script/instructions;
   `create_all` alone does not alter the live database.
2. Add role-family normalization and top-three target selection.
3. Add pure rating and salary normalization/aggregation functions.
4. Add current-row/changed-history persistence behavior.
5. Add collection-state, quota, session, and expiry guards.

This phase is offline-testable and should land before any live adapter.

### Phase 2: adapters, one source at a time

Implement sequentially so each source has isolated fixtures and browser QA:

1. Levels.fyi salary adapter: structured total-comp foundation.
2. Indeed ratings/base-salary adapter: reuse the existing persistent-profile
   pattern.
3. Glassdoor ratings/total-pay adapter.
4. AmbitionBox ratings/annual-salary adapter.

For each source: discovery artifact -> fixture parser tests -> adapter -> one
bounded live pilot -> independent browser QA -> accept/fix before beginning
the next source. Do not run the broad all-connector smoke suite for a
single-source change.

### Phase 3: skill and assessment integration

1. Extend `enrich-companies` with the evidence step and bounded reporting.
2. Build job assessments and best-single-job company selection.
3. Stop after the first 10 companies for human review of identity, ratings,
   salary basis, and source URLs.
4. Expand only after the pilot is accepted.

### Phase 4: dashboard and reporting

1. Extend pure dashboard queries and the company Explore table.
2. Add sorting, confidence/freshness flags, JD links, and evidence links.
3. Add workbook fields only after the dashboard representation is stable.

### Phase 5: priority policy, separately approved

After real distributions are available, grill and decide:

- WLB threshold and minimum WLB confidence;
- salary threshold and whether an upper-bound-only estimate can pass;
- stale-evidence eligibility;
- resume-match cutoff and its role as the fifth criterion;
- exact P1-P5 boolean combinations;
- company selection UX;
- contact-finding and outreach gating.

Then implement a pure `app/prioritization/` policy that consumes evidence and
existing job/resume facts. Do not make source adapters aware of priorities.

## Verification plan

### Offline tests

- role-family order, including Credit Risk before Data Engineering;
- top-three active/nonduplicate/60-day target selection;
- source-profile identity resolution and ambiguity rejection;
- rating scale normalization, median, spread, confidence, and missing values;
- exact/department-fallback scoping;
- currency and period normalization;
- pay-basis compatibility and refusal to inflate base-only values;
- exact-JD override;
- cross-source median without submission-count swamping;
- current snapshot update versus changed-value history insert;
- stale-value retention after collection failure;
- quota windows, eight-worker global cap, source cap, checkpoints, and resume;
- permission expiry hard stop and precisely scoped purge;
- assessment provenance and best-single-job selection;
- dashboard filters, sorting, empty states, and clickable URLs.

Use in-memory SQLite for the normal suite and add a reviewed manual Postgres
migration check because SQLite will not catch every production DDL issue.

### Live acceptance per source

- verified dummy-account login/session reuse;
- correct company identity on 10 pilot companies;
- exact displayed rating/category/count captured;
- exact salary label, range, currency, period, role, location, level, and
  sample count captured;
- missing fields remain null rather than guessed;
- URL attribution resolves to the evidence page;
- second run is idempotent when values are unchanged;
- forced interruption resumes from the last company/source checkpoint;
- no credentials, cookies, query strings, or confidential permission text in
  logs or DB.

### Pilot acceptance report

For the 10 companies, show side by side:

- company and chosen JD link;
- verified source-profile links;
- all source rating values and consensus/confidence;
- all salary values/pay bases and consensus/confidence;
- unresolved, missing, conflicting, and stale cases;
- requests used, wall time, and failures per source.

Human acceptance of this report is the gate before broad collection.

## Safe commit sequence

Keep commits narrow and reversible:

1. domain types, schema, and migration instructions;
2. pure normalization/aggregation with tests;
3. persistence/history/state/quota/expiry with tests;
4. Levels.fyi adapter and acceptance fixtures;
5. Indeed adapter and independent browser QA fixes;
6. Glassdoor adapter and independent browser QA fixes;
7. AmbitionBox adapter and independent browser QA fixes;
8. `enrich-companies` integration;
9. dashboard/reporting integration;
10. documentation and pilot acceptance evidence.

No commit in this sequence activates a priority gate or changes which
companies receive contact discovery or outreach drafts.
