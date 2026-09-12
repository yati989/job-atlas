# Daily decision and outreach pipeline: implementation plan

## Outcome

Replace the current full-pipeline behavior with a two-part, attended daily
workflow:

1. Build and email an immutable, ranked decision run from a user-supplied
   posting-time window.
2. After the user approves that specific run, process exactly the approved
   companies and jobs through resume tailoring, contact discovery, calibrated
   email-pattern testing, Gmail drafts, and a final report.

The default target is 30 fresh-outreach companies per run. The user chooses
the cutoff from the ranked groups; the pipeline never silently substitutes a
different company when contact discovery fails.

This plan supersedes the no-approval, `first_seen_at`-scoped behavior currently
documented in `.claude/skills/full-pipeline/SKILL.md`. It does not replace the
ingestion-only `daily-pipeline` skill or the standalone module skills.

## Locked product contract

### Run window and cohort

- `--since` is mandatory for every run.
- Capture `run_started_at` once, before Gmail reconciliation or ingestion.
- Resolve the window as `since <= jobs.posted_at <= run_started_at`.
- Accept ISO-8601 input. If the input has no UTC offset, interpret it in
  `Asia/Kolkata`; print and persist the resolved offset-aware interval before
  doing work.
- Use `posted_at`, not `first_seen_at` or `last_seen_at`, for membership.
- Include only active canonical jobs (`status='active'` and
  `duplicate_of_job_id IS NULL`).
- A genuine repost whose posting timestamp enters the interval is eligible.
  An unchanged old posting is not.
- Exclude missing-description, missing-posted-date, and future-posted-date
  anomalies from ranking. Report them; do not fail the whole run or the
  company when another job survives.
- Starting a new run expires any still-pending prior decision run. Approval
  must name the currently active run ID; stale approval is rejected.

### Applied-job suppression

- The user marks job-board applications by stable job/posting-version ID.
- Store `applied_at`, source/URL metadata, and the tailored-resume ID when
  available.
- Suppress the applied posting and its cross-source duplicate cluster from
  later decision runs.
- A genuinely new or materially revised/reposted posting version remains
  eligible.

### Hard rejects

Evaluate each job independently. A hard reject requires explicit evidence in
the structured posting fields or job description; missing or ambiguous
evidence never rejects.

- Reject when maximum explicit annual guaranteed salary is `< INR 25 LPA`.
- Reject when minimum mandatory experience is `>= 8 years`.
- Reject when maximum mandatory experience is `<= 2 years`, unless the same
  job explicitly states maximum guaranteed cash `> INR 30 LPA`.
- Exactly INR 25 LPA survives. Exactly INR 30 LPA does not rescue a junior
  job.
- Preferred/nice-to-have experience is not mandatory experience.
- Salary ranges use their maximum.
- Guaranteed fixed cash and guaranteed cash bonus count. Discretionary bonus,
  commission, equity, vague upside, and one-time components do not.
- Reliably convertible monthly/hourly/foreign amounts are annualized to INR;
  persist the original value, currency, period, FX rate, rate timestamp, and
  provider. Unreliable conversion becomes unknown and cannot reject.
- Persist every applicable reason and its exact evidence on the run snapshot.
  Recalculate against each new posting version.
- A rejected job does not reject its company when another job survives.

### Effective salary and four groups

For each surviving job:

```text
effective_salary_lpa =
  reliable explicit job maximum
  else company.ambitionbox_estimated_salary_lpa
  else null
```

The stored AmbitionBox estimate may be used for every eligible job at that
company; no additional role-compatibility gate is required.

Groups are mutually exclusive:

1. Remote and effective salary `>= 20`, or remote and salary unknown.
2. Hybrid/on-site/unknown work mode and effective salary `>= 20`, or those
   work modes and salary unknown.
3. Remote and effective salary `< 20`.
4. Hybrid/on-site/unknown work mode and effective salary `< 20`.

Only `is_remote is True` is remote. Exactly INR 20 LPA belongs to the high
salary side. A company appears once, in its best available group in the order
1, 2, 3, 4. All its surviving cohort jobs remain attached. Its primary job is
the highest-ranked job inside the winning group.

Apply the same ordering inside every group:

1. known effective salary before unknown salary;
2. role family: ML/AI, Data Science, Credit Risk, Analytics, Data Engineering,
   Other/unclassified;
3. qualified Glassdoor WLB descending, null last;
4. effective salary descending;
5. `posted_at` descending;
6. job ID and company ID as deterministic final ties.

Classify role from title first and core responsibilities second, never from a
skills list alone. Mixed roles take the higher priority. The role vocabulary
includes:

- ML/AI: AI Engineer, ML Engineer, MLOps, GenAI/LLM, NLP, computer vision;
- Data Science: Data Scientist, Applied Scientist, Decision Scientist;
- Credit Risk: credit, underwriting, fraud, collections, model/portfolio risk;
- Analytics: Data Analyst, BI, Business Analyst, product/marketing analytics;
- Data Engineering: Data Engineer, Analytics Engineer, ETL,
  warehouse/platform.

Glassdoor WLB is sortable only when `glassdoor_review_count` is at least the
configured minimum, initially 50. A lower or missing count makes WLB null; it
does not reject the company. Keep staffing firms eligible but use their
recruiter-only contact ladder. Preserve the distinction between
`company_type` (`employer`/`staffing`) and Glassdoor `ownership_type`
(`private`/`public`/other source value).

### Decision email and approval

The pre-approval email contains:

- run ID, resolved window, policy/config version;
- counts of surviving companies and jobs;
- hard-reject counts by reason with evidence;
- anomaly counts by source;
- per-group company count and job count;
- role-family, WLB-band, salary-source, and employer/staffing distributions;
- salary P10, P25, median, mean, P75, and P90 per group, using known effective
  salaries only, standard linear interpolation, plus known N and unknown N;
- an attached workbook with one ranked sheet per group, all surviving jobs,
  company/primary-job information, salary source, WLB/review count, ranks,
  rejected jobs/evidence, and anomalies.

Approval names the active run ID and declares group cutoffs/order, for example:

```text
Approve 20260822T041512Z-a3f9: target 30; G1 all; G2 top 20; G3 0; G4 0
```

Normalize and persist the approval as an immutable selection artifact with the
exact ordered company IDs and job IDs. The total fresh-outreach selection is
capped at 30. Do not infer approval from a reply that omits or mismatches the
active run ID.

### Post-approval work

- Every surviving current-window job attached to an approved company is an
  application target and gets a tailored resume.
- Tailoring and every later stage consume the persisted selected IDs, never a
  timestamp query or generic pending-status queue.
- If one tailoring job fails, keep the company. Pair a contact with the best
  function-relevant successfully tailored job, otherwise the company primary
  job with the master resume and an explicit fallback flag.
- Contact target functions are the union represented by the selected
  company's surviving jobs. Staffing companies use recruiter-only targets.
- Reuse valid existing, never-successfully-contacted people first. Run fresh
  billed search only for current target functions whose ladders are not
  satisfied by reusable contacts.
- Create one draft for every eligible contact found. There is no per-company
  draft cap; the user decides which drafts to send.
- Pair each contact with its most function-relevant selected job. If there is
  no function match, pair it with the company primary job.
- One contact receives one fresh draft in the run.
- Do not recheck mutable live job status after approval; the approved snapshot
  controls the run.

### Fresh outreach, application-only companies, and follow-ups

- Track every cold-email attempt that leaves Gmail automatically.
- Initial outreach and follow-ups are separate message kinds and counters.
- Follow-ups never count toward the fresh-outreach threshold and run on their
  own later workflow/thread.
- A successful initial contact is permanently excluded as a new recipient.
- A company remains fresh-outreach eligible while it has fewer than four
  successful initial contacts. At four or more, mark it `outbound_reached`.
- A run that begins with a company already outbound-reached excludes it from
  the 30-company outreach quota and from fresh contact search/drafting. Show
  its eligible jobs in an application-only section; tailor all, none, or
  specified job IDs according to the approval.
- If a company starts below four, prepare drafts for every newly eligible
  contact found. Four is not a per-run draft cap.
- Count a successful initial contact when its message is confirmed by a reply
  or presumed delivered. A bounce does not count.
- Mark a sent message presumed delivered after a configurable 30 minutes with
  no bounce. A later bounce reverses that state and recalculates company and
  domain totals.
- Aggregate history by canonical deduplicated company ID. Also retain canonical
  domain aliases so company aliases cannot evade the threshold.

### Email-pattern calibration

- Candidate shapes remain `first.last`, `first.l`, `f.last`, and
  `first_last`.
- Learn pattern priority per canonical domain, shared by company aliases.
- Persist per pattern: non-bounced/success count, reply-confirmed count,
  bounce count, first/last observed timestamps, and current confidence state.
- One non-bounced message after 30 minutes promotes a pattern provisionally;
  a reply is stronger evidence. A later bounce can demote and recompute it.
- Try patterns sequentially for one calibration contact: only one active Gmail
  draft/address at a time; on bounce prepare the next; stop after the first
  non-bounced attempt.
- Calibration-contact order is Talent Acquisition, relevant IC,
  lowest-collision-risk hiring manager, then function head. If none exists,
  mark `no_calibration_contact`.
- Group drafts by company. Initially release only one calibration draft per
  company. The user sends it and may test further patterns for the same person.
- Start that company's one-hour calibration clock when the first calibration
  message is observed in Sent, not when drafts are generated.
- Reconcile Gmail every ten minutes during the first hour. On learned pattern,
  automatically retarget all still-unsent drafts for that domain and release
  them. Sent messages are immutable.
- If no pattern succeeds within the hour, hold the remaining company drafts
  and mark `pattern_unresolved`.

### Final report

After approved processing, self-email a second workbook/report containing:

- the final ordered 30 fresh-outreach companies;
- application-only companies and approved job scope;
- every selected job and resume result/path/score;
- contacts and their paired job/resume;
- calibration contact, domain-pattern state, and one-hour deadline;
- draft state: held, ready/pushed, sent, bounced, presumed delivered, replied;
- no-contact, no-calibration-contact, tailoring, Gmail, and other failures.

## Architecture

### Deep module: `app/decision_runs/`

Create one module that hides window validation, screening, salary coalescing,
group assignment, company collapse, sorting, distributions, run lifecycle,
approval parsing, and exact-scope retrieval. Its external interface should be
small:

```python
run = create_decision_run(session, since=input_since, cutoff=started_at)
summary = prepare_decision_run(session, run.id, policy=policy_snapshot)
selection = approve_decision_run(session, run.id, approval)
scope = load_approved_scope(session, run.id)
```

Suggested internal layout:

```text
app/decision_runs/
  __init__.py          # only the public operations and value types
  types.py             # immutable policy, finding, rank, approval value objects
  lifecycle.py         # active/pending/expired/approved state transitions
  cohort.py            # posted-at window, canonical/activity/application gates
  screening.py         # pure hard-reject evaluation over extracted evidence
  salary.py            # guaranteed-cash normalization and effective salary
  roles.py             # title/responsibility classifier and fixed priority
  ranking.py           # job sort, company group collapse, deterministic ties
  distributions.py     # counts, bands, percentiles
  approval.py          # strict approval grammar and immutable selection
  persistence.py       # transaction boundary and snapshot reads/writes
  service.py           # orchestration behind the public interface
```

No downstream module may reconstruct membership from live `jobs` or
`companies` rows. It asks `load_approved_scope` for immutable IDs and snapshot
facts.

### Structured screening-evidence seam

Extend `enrich-jobs` and its persistence seam to record explicit, auditable
facts for the current posting version:

```python
record_screening_facts(
    session,
    posting_version_id=...,
    salary=ExplicitCompensation(...),
    minimum_experience=RequirementEvidence(...),
    maximum_experience=RequirementEvidence(...),
)
```

Extraction remains agent reasoning. Deterministic policy evaluation belongs
in `decision_runs.screening`; the skill must not directly assign a group or
hard-reject result.

### Exact-scope adapters for existing agent modules

Add ID-manifest entrypoints instead of widening current generic selectors:

```python
enrich_jobs_for_versions(session, posting_version_ids)
enrich_companies_for_run(session, company_ids, phase_b="never_attempted")
select_jobs_needing_resume(session, job_ids=[...])
contexts_for_approved_companies(session, approved_scope)
pair_for_approved_scope(session, run_id, contact_id)
```

Keep the standalone backlog selectors for standalone skills. The full
pipeline uses only these exact-scope seams.

### Deep module: `app/outreach/state.py`

Make outreach history and calibration one state machine, separate from email
copy generation:

```python
eligibility = fresh_outreach_eligibility(session, company_id)
plan = prepare_company_outreach(session, approved_scope, company_id)
report = reconcile_outreach(session, gmail_port, now=clock.now())
release = advance_calibration(session, company_id, now=clock.now())
```

The module derives the four-success company threshold from immutable delivery
attempts rather than trusting a manually maintained counter. A cached company
state may be stored for fast queries, but reconciliation must be able to
rebuild it.

### Gmail port and adapters

Move orchestration away from raw Gmail API resources behind a narrow port:

```python
class MailboxPort(Protocol):
    def create_or_update_draft(...): ...
    def delete_unsent_draft(...): ...
    def sent_message(...): ...
    def thread_events(...): ...
    def delivery_failures(...): ...
```

- `GmailMailboxAdapter` wraps the current direct API behavior and adds
  idempotent sent-message, reply, and DSN/bounce observation.
- `InMemoryMailbox` drives all state-machine tests with a fake clock.
- Preserve the safety invariant: outreach is drafted only; only
  `send_self_report` may send, and only to `SELF_EMAIL`.

Do not identify a bounce from a display string alone. Parse message headers,
thread linkage, Gmail message IDs, and delivery-status MIME parts; retain an
`unclassified_mail_event` state for uncertain messages.

## Persistence changes

There is no migration framework. Add ORM models for clean databases and a
reviewable, idempotent SQL migration under `docs/migrations/` for live
Postgres. The migration must be applied explicitly; `create_all` is not
sufficient.

### Posting versions and applications

`job_posting_versions`

- `id`, `job_id`, canonical duplicate-root ID;
- `posted_at`, normalized material-content hash, posting-instance key;
- title/description/salary/location snapshots or a bounded snapshot JSON;
- `first_seen_at`, `last_seen_at`;
- unique posting-instance identity.

Update `upsert_job` to refresh all mutable posting fields and append/reuse a
version. Treat a changed posting timestamp as a new posting episode; treat a
material-content change as a new version. Preserve old versions for applied
and decision-run history.

`job_applications`

- `id`, `posting_version_id`, canonical duplicate-root ID;
- `applied_at`, `source`, `job_url_snapshot`, `tailored_resume_id`, notes;
- unique `posting_version_id`.

### Screening facts

`job_screening_facts`

- posting-version ID and extraction version;
- original salary text/value/currency/period/component labels;
- normalized guaranteed minimum/maximum LPA;
- FX rate/provider/as-of and reliability state;
- minimum/maximum mandatory-experience values;
- mandatory/preferred classification and exact evidence snippets;
- extracted timestamp and evidence hash;
- unique `(posting_version_id, extraction_version)`.

### Decision snapshot

`decision_runs`

- opaque run ID, `since_at`, `cutoff_at`, resolved input timezone;
- lifecycle state (`preparing`, `awaiting_approval`, `approved`, `processing`,
  `completed`, `failed`, `expired`);
- policy/config snapshot, target count, timestamps, failure detail.

`decision_run_jobs`

- run ID, posting-version/job/company IDs;
- outcome (`eligible`, `hard_rejected`, `anomaly`, `applied_duplicate`);
- snapshot fields needed to explain the decision;
- explicit salary, AmbitionBox salary, effective salary and source;
- role family/rank, work mode, qualified WLB/review count;
- group number, within-company rank, primary flag;
- unique `(run_id, posting_version_id)`.

`decision_run_findings`

- run-job ID, finding kind and reason code;
- exact evidence, parsed value, threshold, normalized comparison;
- source field/description location and structured details JSON.

`decision_run_companies`

- run ID/company ID, winning group, rank, primary run-job ID;
- employer/staffing classification and outreach-success count at snapshot;
- mode (`ranked`, `fresh_outreach_selected`, `application_only_selected`,
  `not_selected`);
- unique `(run_id, company_id)` and `(run_id, group, rank)`.

`decision_run_approvals` and `decision_run_selected_jobs`

- original approval text, normalized rules, target, approver/timestamps;
- immutable ordered company IDs and selected posting-version IDs;
- selection kind (`fresh_outreach` or `application_only`);
- one approval per run.

### Outreach and domain pattern history

The existing `outreach_drafts.to_email UNIQUE` model cannot represent
sequential address patterns, multiple attempts, late bounces, or initial
versus follow-up history. Introduce the new model alongside it, backfill, then
switch reads before retiring the legacy uniqueness assumption.

`outreach_messages`

- run/company/contact IDs, paired run-job/posting-version ID;
- `message_kind` (`initial` or `follow_up`), calibration flag;
- subject/body/resume snapshot and pairing reason;
- state (`held`, `ready`, `pushed`, `sent`, `delivered`, `replied`, `bounced`,
  `exhausted`, `cancelled`);
- unique fresh initial message per `(run_id, contact_id)`.

`outreach_delivery_attempts`

- message ID, sequence number, exact email, domain, pattern name;
- Gmail draft/thread/message IDs;
- state timestamps for pushed, sent, presumed-delivered, replied, bounced;
- bounce/reply evidence metadata and idempotent Gmail event keys;
- unique `(message_id, sequence_number)` and unique Gmail message ID when set.

`company_domain_aliases`

- canonical company ID, normalized domain, first/last observed timestamps;
- current/verified flags; unique `(company_id, domain)`.

`domain_email_patterns`

- normalized domain and pattern name;
- confidence (`unknown`, `provisional`, `confirmed`, `demoted`);
- successful/non-bounced, reply-confirmed, and bounce counts;
- first/last observation timestamps;
- unique `(domain, pattern_name)`.

`run_company_calibrations`

- run/company/domain and chosen calibration contact/message;
- state (`waiting_to_send`, `testing`, `pattern_confirmed`,
  `pattern_unresolved`, `no_calibration_contact`);
- first sent timestamp, deadline, confirmed pattern, last reconciliation.

Derive successful initial counts from distinct initial messages whose current
delivery outcome is `presumed_delivered` or `replied`. Late-bounce reversal
must be a transaction that updates the attempt, pattern aggregate,
calibration, and derived company state consistently.

### Company fields and configuration

Keep existing company fields:

- `glassdoor_review_count`;
- `employee_count_range`;
- `revenue`;
- `ownership_type` for private/public;
- `company_type` for employer/staffing.

Add validated settings with defaults:

```text
GLASSDOOR_WLB_MIN_REVIEWS=50
FRESH_OUTREACH_SUCCESS_THRESHOLD=4
DAILY_FRESH_COMPANY_TARGET=30
DELIVERY_PRESUMPTION_MINUTES=30
CALIBRATION_WINDOW_MINUTES=60
CALIBRATION_POLL_MINUTES=10
PIPELINE_INPUT_TIMEZONE=Asia/Kolkata
DECISION_POLICY_VERSION=1
```

Persist their resolved values in each decision run. A later configuration
change must not rewrite an old decision.

## Pipeline stages and commands

Refactor `scripts/run_full_pipeline.py` into resumable commands keyed by run
ID rather than date-directory inference:

```bash
python -m scripts.run_full_pipeline start --since 2026-08-21T09:00:00+05:30
python -m scripts.run_full_pipeline prepare-decision --run-id <id>
python -m scripts.run_full_pipeline approve --run-id <id> \
  --selection 'target 30; G1 all; G2 top 20; G3 0; G4 0'
python -m scripts.run_full_pipeline process-approved --run-id <id>
python -m scripts.reconcile_outreach --run-id <id>
python -m scripts.run_full_pipeline final-report --run-id <id>
python -m scripts.mark_job_applied --posting-version-id <id> \
  --tailored-resume-id <id>
```

The repo-authored `full-pipeline` skill remains the user-facing orchestrator
for agent-driven work. Script commands own lifecycle validation, persistence,
report generation, and exact manifests.

### Before approval

1. `start` validates `--since`, captures cutoff, creates the run, and expires
   the old pending run.
2. Reconcile Gmail so fresh-outreach eligibility and learned patterns are
   current.
3. Run ingestion and cross-source job/company dedup.
4. Materialize posting versions and the exact `posted_at` cohort.
5. Enrich cohort jobs lacking current-version extraction; isolate anomalies.
6. Evaluate hard rejects.
7. Refresh Phase A company information for companies with at least one
   surviving job, including posting-derived contact functions.
8. Run Phase B only for a source/company pair never previously attempted;
   reuse completed, terminal-missing, and terminal-error evidence indefinitely
   unless an explicit refresh command is used.
9. Normalize salaries, calculate qualified WLB, group/rank companies, freeze
   all run rows, build the decision workbook, and self-email it.
10. Transition to `awaiting_approval` and stop.

### After approval

1. Parse approval and freeze selected companies/jobs transactionally.
2. Split fresh-outreach and application-only selections based on outreach
   history captured at approval time.
3. Tailor every approved application job using exact posting-version IDs.
4. Reuse eligible contacts and compute function-ladder coverage.
5. Run paid contact search only for uncovered approved functions.
6. Pair each contact to a selected job and generate one message record.
7. Choose one calibration contact per fresh-outreach company; push only its
   first pattern attempt and hold the rest.
8. Reconcile every ten minutes during active calibration windows. Advance on
   bounce or release/retarget held drafts on provisional/confirmed success.
9. After all one-hour windows are resolved or explicitly timed out, build and
   self-email the final workbook. A later reconciliation may still reverse a
   presumed delivery after a late bounce.

Every command is idempotent for a run ID. Re-running a completed stage reads
the persisted artifact and reports `already_complete`; it does not recreate
the cohort, mutate approval, duplicate paid searches, or duplicate Gmail
drafts.

## Reporting implementation

Split `app/reporting/workbook.py` into read-model builders plus renderers:

```text
app/reporting/
  decision_report.py    # reads only decision-run snapshot tables
  final_run_report.py   # reads approved scope and outreach state
  excel.py              # shared cell shaping, styles, links, percentiles
```

The decision workbook should have:

- `Group 1` through `Group 4`, one row per company/job with the primary marked;
- `Rejected jobs`, one row per reason/evidence;
- `Anomalies`;
- `Distributions`;
- `Run summary` and exact policy/config snapshot.

The final workbook should have:

- `Fresh outreach companies`;
- `Application only`;
- `Jobs and resumes`;
- `Contacts and pairing`;
- `Calibration and patterns`;
- `Delivery state`;
- `Failures` and `Run summary`.

Do not scope either report by row creation timestamps. Join through the run
snapshot and approval tables only.

## Implementation sequence

Each numbered item should be a small, independently testable commit. Keep
existing Wave 1–3 enrichment changes intact.

1. **Record the decision.** Add an ADR and update `CONTEXT.md` with decision
   run, posting version, effective salary, qualified WLB, approval artifact,
   initial outreach, successful initial contact, calibration contact, and
   verified domain pattern.
2. **Add configuration.** Introduce validated settings and immutable policy
   value objects, including timezone parsing and policy serialization.
3. **Add posting versions.** Create ORM/SQL migration, update `upsert_job` to
   refresh mutable fields and persist versions, and backfill one baseline
   version per current job.
4. **Add application ledger.** Implement manual mark/query CLI and duplicate-
   cluster/version suppression.
5. **Persist screening evidence.** Add structured evidence models and the
   `enrich-jobs` persistence seam; update the skill without assigning policy
   outcomes inside it.
6. **Implement pure policy functions.** Hard rejects, compensation
   normalization, role classification, qualified WLB, grouping, ranking, and
   percentile calculations. Cover every boundary with table-driven tests.
7. **Add decision-run persistence.** Implement run lifecycle, immutable job
   and company snapshots, findings, approval, and selected-job rows.
8. **Build cohort preparation.** Implement exact `posted_at` membership,
   anomaly isolation, applied suppression, company collapse, and deterministic
   ranks.
9. **Add exact enrichment manifests.** Wire job enrichment and company Phase A
   to run IDs; add Phase B `never_attempted` selection without stale refresh.
10. **Build the pre-approval report.** Add group/reject/anomaly/distribution
    sheets and the guarded self-email.
11. **Add strict approval CLI/parser.** Reject stale/mismatched runs and store
    the exact ordered 30-company-or-smaller artifact.
12. **Make tailoring exact-scope.** Add job-ID/posting-version selection and
    preserve fallback results without dropping the company.
13. **Make contact finding exact-scope.** Calculate selected-job function
    unions, reuse valid contacts, and issue paid queries only for uncovered
    ladder slots. Do not route through `get_company_queue`.
14. **Introduce the outreach ledger.** Add message/attempt/domain-alias/pattern/
    calibration tables, backfill legacy sent and pushed drafts, and dual-write
    temporarily.
15. **Add Gmail port and reconciliation.** Wrap current draft behavior; detect
    sent, DSN bounce, reply, presumed delivery, and late reversal idempotently.
16. **Implement calibration.** Choose the calibration contact, sequentially
    try patterns, retarget unsent drafts, hold/release company batches, and
    enforce the one-hour state transition.
17. **Switch outreach reads.** Make fresh-recipient and four-success company
    eligibility derive from the new ledger; keep follow-ups explicitly out of
    the initial count.
18. **Build the final report.** Render exact approved scope, application-only
    jobs, resumes, contacts/pairings, patterns, delivery states, and failures.
19. **Replace full-pipeline orchestration.** Update
    `scripts/run_full_pipeline.py` and `.claude/skills/full-pipeline/SKILL.md`
    to the start/prepare/approve/process/reconcile/report lifecycle.
20. **Remove compatibility path.** After a real shadow run proves parity and
    migration counts reconcile, stop writing legacy `OutreachDraft` state and
    document its retirement. Do not drop its table in the first rollout.

## Test and verification matrix

All automated tests run offline against in-memory SQLite. Gmail behavior uses
`InMemoryMailbox`; no test calls Gmail, Bright Data, or a live connector.

### Policy unit tests

- timezone-aware and naive `--since`, inclusive start/end, and cutoff captured
  once;
- missing/future dates and missing descriptions become anomalies;
- active/canonical/application/version cohort gates;
- all salary and experience thresholds, inclusive/exclusive boundaries,
  preferred wording, multiple values/ranges, guaranteed versus discretionary
  components, conversion success/failure;
- explicit salary precedence over AmbitionBox and null salary ordering;
- exact INR 20 group boundary;
- title-first role classification, responsibility fallback, mixed-role
  priority, Other last;
- review count 49/50 boundary and WLB null ordering;
- company best-group collapse, primary job, stable tie-breaks;
- percentile values and known/unknown denominators.

### Decision-run integration tests

- starting a run expires the prior pending run;
- a stale or mismatched approval cannot mutate selection;
- rejected job plus surviving sibling keeps the company;
- every report row traces to a run-job snapshot;
- approval freezes exact IDs and downstream live-row changes do not alter it;
- exact target/cutoff behavior across groups, including fewer than 30;
- outbound-reached companies become application-only and do not consume the
  fresh 30 quota;
- re-running each command is idempotent.

### Outreach state-machine tests

- one calibration draft only; clock begins on observed send;
- bounce advances to the next pattern for the same contact;
- 30-minute no-bounce creates provisional success and releases/retargets
  unsent same-domain drafts;
- reply confirms a pattern;
- one-hour unresolved state holds remaining drafts;
- late bounce reverses presumed delivery, pattern counts, and company count;
- sent messages are never retargeted;
- initial and follow-up counters remain separate;
- a successful recipient is permanently excluded from fresh drafts;
- company eligibility at successful counts 3 and 4;
- company/domain aliases share history;
- duplicate Gmail events and repeated reconciliation do not double count.

### Existing-suite regression tests

Run focused tests during each commit, then the complete suite:

```bash
.venv/bin/python -m pytest tests/test_run_full_pipeline.py -q
.venv/bin/python -m pytest tests/test_tailor.py tests/test_outreach_pairing.py -q
.venv/bin/python -m pytest tests/test_outreach_drafts.py tests/test_outreach_gmail_push.py -q
.venv/bin/python -m pytest tests/
```

Add new files for decision policy, run lifecycle, posting versions,
applications, reporting, Gmail reconciliation, calibration, and migration
backfill behavior.

## Rollout and safety gates

1. Take a database backup and count jobs, applications, legacy drafts, sent
   drafts, contacts, and companies before applying SQL.
2. Apply the idempotent migration and run backfills in dry-run/count mode
   first. Verify every existing job has one baseline posting version and every
   legacy sent draft maps to exactly one sent attempt.
3. Run a historical dry-run decision snapshot that sends no email and creates
   no Gmail drafts. Compare cohort membership and ranks manually.
4. Run one live pre-approval decision email. Stop at `awaiting_approval` and
   verify all four sheets, reject evidence, distributions, and exact IDs.
5. Approve a deliberately small pilot (two or three companies). Confirm exact
   resume scope, contact reuse, one calibration draft per company, bounce
   advancement, and held drafts.
6. Run the first full 30-company day attended. Keep legacy outreach dual-write
   enabled and reconcile counts after the one-hour window.
7. Only after that reconciliation should the new ledger become authoritative.
   Preserve legacy rows read-only until a later explicit removal decision.

Activation or source-status changes are outside this plan. Follow the
repository rule that only the user decides whether a connector is activated or
deactivated.

## Acceptance criteria

Implementation is complete when:

- a run cannot start without an explicit `--since` and always exposes its
  resolved `posted_at` interval and run ID;
- the pre-approval email and workbook exactly reproduce the persisted four-
  group decision snapshot and requested distributions;
- no resume, billed contact search, or outreach draft is created before a
  matching active-run approval;
- every downstream artifact traces to an approved company ID and posting-
  version ID;
- all selected application jobs are tailored, while fresh outreach is limited
  to the approved eligible company list;
- Gmail events automatically maintain initial attempts, bounces, replies,
  presumed deliveries, follow-ups, four-success company state, and domain
  pattern priority;
- calibration releases or holds remaining drafts exactly according to the
  30-minute/one-hour rules;
- late bounces are reversible and idempotent;
- the final report explains every selected company, job, resume, contact,
  pairing, pattern state, draft state, and failure;
- the complete offline test suite passes and the first attended pilot matches
  the approval artifact without hidden backlog selection.
