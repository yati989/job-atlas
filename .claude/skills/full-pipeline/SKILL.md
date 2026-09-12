---
name: full-pipeline
description: Guide a reviewed private search profile through collection, evidence, optional selection, tailoring, and links-only LinkedIn profile discovery.
---

# Full Pipeline

Run the public guided job-search workflow. This skill never discovers email
addresses, creates outreach drafts, invokes Gmail, sends messages, or requires
Google Sheets. Python commands provide validation and persistence seams;
agent-owned enrichment, tailoring, and profile judgement must use their
matching skills.

## 1. Review configuration

Accept either natural-language preferences translated into a version-1
`SearchProfile`, or the user's YAML/JSON file. Both paths must pass through:

Before generating that profile, propose a coverage-efficient search rather
than selecting every mechanically compatible source. Aim for roughly 90% of
the useful jobs that the available sources are expected to expose with the
fewest queries, prerequisites, paid calls, and attended-browser steps. This is
an informed coverage target, not a guaranteed percentage; say what evidence
or assumptions the estimate uses.

- Consolidate synonymous or overlapping search terms into the broadest native
  terms likely to retain the intended roles. Keep separate terms only when a
  broader query would plausibly miss a distinct job family.
- Rank sources by fit for the profession and geography, expected unique yield,
  reliability, overlap, runtime, and prerequisites. Mechanical compatibility
  alone is not a reason to include a source.
- Make the first pass from the smallest set of high-yield, low-friction
  sources. Defer low-likelihood specialist boards, highly duplicative sources,
  authenticated sources, attended browsers, and more expensive transports
  when the first pass can reasonably cover the same inventory.
- Describe a second-pass expansion and the evidence that would trigger it,
  such as too few eligible jobs, poor employer diversity, a failed major
  source, or a clearly uncovered role family. Respect explicit requests for
  named sources or exhaustive collection, but still show their marginal cost.

```bash
job-atlas plan --config <path>
```

Show the effective profession/relevance terms, hard rejects, geography,
arrangements, experience, job-type policy, named sources, incompatibilities,
prerequisites, source count, and query-instance count. Explain the
query-instance arithmetic by source, why each first-pass source earns its
place, which sources were deferred, and what would cause expansion. The
pre-fetch summary must say that internships and part-time jobs are excluded by
default. Set `include_internships` or `include_part_time` to true only when the
user explicitly asks to include that category, and state that opt-in in the
summary. Do not infer it merely because the user is a student or recent
graduate. Do not execute generated settings until the user accepts them.

Use a 14-day source search window when the user does not request one. The public
profile may use any positive value up to 30 days and must reject a larger
value. The approval contract must show the exact number of days and state that
the pipeline asks each source for that period where the source supports it.
This is not a relevance rejection rule: if a source returns an older posting,
keep it when it passes the other gates and show its actual age. A posting with
no usable date also remains eligible with its age shown as unknown; never
invent a date or label it old.

Write the plan for a general job seeker. Prefer ordinary descriptions such as
"individual board searches" over internal terms such as "query instances,"
and omit runtime labels that do not help the user decide. Explain every setup
requirement at first mention. In particular, explain that an attended browser
opens a visible browser window and may need the user to sign in or handle a
site check, while a Chromium requirement means the pipeline needs a compatible
browser engine installed so it can load that source in the background. Keep
the explanation concrete and brief.

## 2. Freeze and collect

After acceptance:

```bash
job-atlas accept-profile --config <path> --accept
job-atlas start-run --profile <name> --confirm
job-atlas collect --run-id <id>
```

Use `--allow-attended` only with an available desktop for an attended source
such as Indeed. Never expand or silently reconstruct the frozen source plan.
Collection automatically normalizes and deduplicates what each source returns,
then pauses at a private role-review queue. Review the queue with the model
running this skill; do not call an external LLM API:

```bash
job-atlas role-review-context --run-id <id> --mode titles
job-atlas apply-role-review --run-id <id> --input <title-results.json>
```

Judge every row from its title, company, location, and the accepted search
intent. Use `relevant`, `irrelevant`, or `uncertain`, with a short reason. Do
not use literal keyword matching as a substitute for semantic judgment. When
a title could plausibly belong to the intended work but is not clear enough,
mark it `uncertain`; never reject ambiguity automatically. Submit exactly the
candidate IDs and context fingerprint returned by the command.

Then inspect full descriptions only for the uncertain rows:

```bash
job-atlas role-review-context --run-id <id> --mode details
job-atlas apply-role-review --run-id <id> --input <detail-results.json>
```

Resolve those rows as `relevant` or `irrelevant` from the description. Once
the queue is resolved, the pipeline applies the factual job-type, experience,
and location checks. It does not reject an otherwise eligible job because it
is managerial, senior, or entry-level. If a relevant job says only `Remote`
or only `India`, inspect its source and posting evidence rather than guessing:

```bash
job-atlas location-review-context --run-id <id>
job-atlas apply-location-review --run-id <id> --input <results.json>
```

Use `eligible` only when the native search route or posting supplies concrete
evidence that the job is remote and open to India; otherwise use `ineligible`.
The completed review stores eligible jobs, runs cross-source deduplication,
and opens Phase A. Report kept, rejected, Needs review, and partial source
failures separately. Resume only unfinished sources or unresolved review rows.

## 3. Build evidence

Invoke `enrich-jobs` and `enrich-companies` for reusable Phase A job/company
evidence through the public SQLite seam:

```bash
job-atlas phase-a-context --run-id <id>
job-atlas apply-phase-a --run-id <id> --input <results.json>
```

Phase A must establish company identity and configurable function evidence
before profile search, but never requires MX or email capability. Explain
optional paid Phase B/Glassdoor work separately and obtain its own scope/budget
approval; skipping it leaves honest unknowns.

## 4. Offer Phase B, then confirm the final selection

Offer optional Phase B before applying final saved filters so its supported WLB
or India salary evidence can inform those filters. If the user requests it,
first confirm the exact all-eligible research denominator (this is an internal
immutable research scope, not a claim that the final shortlist is decided):

```bash
job-atlas freeze-selection --run-id <id> \
  --mode all_eligible --phase-b-research --confirm
```

Then use that returned scope with the portable budget seam:

```bash
job-atlas phase-b-plan --scope-id <scope-id> --source glassdoor
job-atlas authorize-phase-b --scope-id <scope-id> \
  --source glassdoor --maximum-calls <ceiling> --confirm
```

Resolve every authorized company with the agent's internal web search exactly
as described in `enrich-companies`: at most two shallow searches, accept only a
reviewed Glassdoor Overview employer identity, and record accepted or unresolved
for every company in the company-ID-keyed artifact. Never substitute a SERP API.
Run the reviewed artifact through the same bulk collector as the private workflow:

```bash
job-atlas run-glassdoor-phase-b --run-id <run-id> \
  --authorization-id <authorization-id> \
  --reviewed-artifact <reviewed-glassdoor-resolutions.json> --confirm
```

This command deduplicates shared employer IDs, commits reservations before
provider I/O, submits one Bright Data dataset snapshot, atomically checkpoints
its snapshot ID, resumes that exact snapshot after interruption, and persists
each company independently. The lower-level `reserve-phase-b` and
`apply-phase-b` commands remain available for other reviewed providers.

The reservation must commit before provider I/O. AmbitionBox is also supported
for India salary evidence when explicitly selected. Reuse existing terminal
source evidence and never exceed the global authorization ceiling.

After Phase B completes—or immediately when it is skipped—offer all eligible
jobs, a manual subset, or validated saved filters. Preview retained, excluded,
and unresolved jobs plus unique-company counts and reasons. Preview file or
natural-language filters with `selection-preview`, then persist the accepted
final all/manual/filtered posting-version/company set with `freeze-selection
--confirm`. The earlier all-eligible research scope is purpose-marked
`phase_b_research`: it is not the final choice and cannot be used for tailoring,
profile discovery, or export. A final choice creates a distinct immutable
`selection` revision. It never deletes collected observations or silently
expands completed work.

Do not freeze any scope until every included posting version has stored Phase A
evidence and every included company has completed Phase A identity/function
classification; the CLI enforces this boundary.

## 5. Offer independent optional stages

- Invoke `tailor-resumes` for the selected posting versions when requested;
  use `tailoring-context` and `save-tailored` against the same private SQLite.
- Invoke `find-profile-links` for the selected companies when requested.
- Either stage may be skipped without blocking the other.

Before profile research, show the exact company denominator, existing reusable
links, generated query variants, initial query count, retry allowance, and
maximum authorized provider calls. Execute only after approval. The
`find-profile-links` skill reuses the existing contact search plan, provider
transport, ledgers, relevance gates, and agent judgement, but stops at
`record_profile_links` before all email-related work. Retain every relevant
canonical LinkedIn profile found within the approved plan; never pad results.

## 6. Report and resume

Export reviewable jobs, outcome reasons, selected scope, LinkedIn URLs with
minimal professional evidence, and verified resume artifacts. Never put raw
provider snippets, harvested email addresses, credentials, or outreach drafts
in pipeline storage, logs, dashboards, or exports. Report completed, partial,
failed, skipped, and awaiting-approval stages distinctly and reuse durable
work on resume.

Use `status` to show source and post-collection stage state. When the user
declines an optional stage, persist that choice with `set-stage --status
skipped --confirm` so resume does not ask again.

Create the local workbook with:

```bash
job-atlas export --run-id <id> --scope-id <scope-id> \
  --out <private-path.xlsx>
```

The workbook opens with a **Start here** sheet. Its **Shortlist** sheet contains
every frozen job with a `follow_up_decision` dropdown set to `Approved` by
default. Explain this control whenever you deliver the workbook. The user may
change any row to `Declined`, save it, and return it to the agent. Import the
complete reviewed workbook with:

```bash
job-atlas apply-workbook-shortlist --run-id <id> \
  --scope-id <exported-scope-id> --input <reviewed.xlsx> --confirm
```

Use the returned scope for resume tailoring and LinkedIn profile discovery.
The importer rejects missing, added, duplicated, or reassigned posting rows;
only the dropdown decision is editable. Importing a shortlist does not itself
authorize paid research, resume generation, profile discovery, applications,
or outreach.

`draft-outreach` may be mentioned after completion only. It is a separate,
explicitly invoked workflow that may use the repository's full contact/email
discovery and Gmail Draft capabilities. It is never invoked, resumed, or
counted by this pipeline, and it never sends mail automatically.
