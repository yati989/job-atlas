---
name: enrich-jobs
description: Enrich pending job rows from stored descriptions with supported experience, education, qualifications, and hard/soft skills. Use for job-level extraction only; company facts, pain points, domains, company type, and contact search groups belong to enrich-companies.
---

# Enrich Jobs

Enrich job rows using the agent's reasoning over stored `description_raw`.
Do not call an external LLM API. Company-level enrichment is explicitly out
of scope; invoke `enrich-companies` for it.

## Public guided-run mode

When invoked by the public `full-pipeline`, do not use `app.db.session`, the
legacy PostgreSQL backlog, or legacy Decision Run manifests. Read only the
exact kept rows from the user's private SQLite database:

```bash
job-atlas phase-a-context --run-id <run-id> \
  --private-home <private-home>
```

Reason over those posting-version inputs using the extraction rules below.
Copy the returned `context_fingerprint` unchanged into the result payload;
the apply seam rejects the batch if its underlying job/company context changed.
Write one JSON result per returned job under `jobs`, with
`posting_version_id`, `status`, supported requirement fields, hard/soft skills,
and evidence-bearing salary/minimum/maximum-experience objects. Persist the
reviewed batch together with the company results through:

```bash
job-atlas apply-phase-a --run-id <run-id> \
  --input <results.json> --private-home <private-home>
```

This seam rejects IDs outside the exact kept run, validates ranges, reuses
completed evidence, and stores `JobSkill` and posting-version screening facts
in the same public SQLite database used by collection and selection. The
legacy modes below remain available only outside the public guided workflow.

## Workflow

1. Select 30-40 in-scope rows where `enrichment_status='pending'` and the
   description exists. Also reselect `no_description` rows whose description
   was later populated.
2. Mark genuinely descriptionless pending rows `no_description`; never delete
   them.
3. Extract only directly supported fields:
   - `experience_min_years` / `experience_max_years` (plausible integers;
     reject implausible ranges);
   - `education_requirement` without interpreting incidental `be`/`me` text
     as degrees;
   - `qualification_other` for explicitly stated certifications or notable
     requirements;
   - hard and soft skills explicitly present in the posting.
   Treat the posting-version snapshot's non-empty `salary_raw` as first-class
   compensation evidence alongside explicit salary passages in
   `description_raw`; never inspect the description while silently ignoring
   the connector's salary field.
4. Update the job to `done`, set `enriched_at`, and insert `job_skills` with
   `ON CONFLICT DO NOTHING` on `(job_id, skill, skill_type)`.
   For a decision-run posting version, also persist only directly supported
   compensation and mandatory-experience evidence through
   `app.jobs.screening_facts.record_screening_facts`.  Record original
   evidence/snippets and leave unknowns null; never assign a decision group
   or hard-reject outcome here. When `salary_raw` is non-empty, the salary fact
   itself must not be null: record an evidence-backed `guaranteed_max_lpa`
   when currency and period support conversion, otherwise record the original
   evidence with an explicit `unusable_reason` (for example `period_unknown`
   or `currency_unknown`).
5. Commit about 40 jobs per transaction so interrupted work resumes cleanly.
6. Report enriched, `no_description`, and remaining pending counts. Verify
   valid experience ranges, no stale `no_description` rows, consistent skill
   rows, and zero JSON-null salary facts for posting snapshots whose
   `salary_raw` is non-empty.

For an attended Decision Run, never select the global pending backlog. First
freeze its canonical posting-version manifest through
`app.decision_runs.freeze_job_enrichment_manifest`, then obtain only those
versions through `app.decision_runs.enrich_jobs_for_versions`. After each
completed agent batch, report its exact posting-version outcomes through
`app.decision_runs.report_job_enrichment`; reusable `done` and
`no_description` rows count as Enriched, while pending and failed IDs remain
separate. A failure may advance to screening only when explicitly recorded as
the typed `non_blocking` or `excluded` disposition, separately from its reason.

Missing evidence remains null. Prefer scraped `seniority` and
`employment_type` values over inference; this workflow does not overwrite
them.

## LinkedIn pre-relevance combined mode

LinkedIn is the sole exception to the ordinary post-approval timing. Its
logged-out remote search does not expose workplace type, so before the central
relevance gate the full-pipeline coordinator runs:

```bash
python -m scripts.run_full_pipeline linkedin-location-input --run-id <id>
```

Read only the emitted `linkedin-location-input.jsonl`. Read each complete
description once and return both its location judgment and, when
`full_job_enrichment_requested=true`, the ordinary supported job enrichment:
experience, education, other qualifications, hard/soft skills, and the
evidence-bearing screening facts. This reusable enrichment is persisted during
ingest so later enrichment freezes it as already done. Do not call an external
LLM API.

Stop after reading the LinkedIn description. Do not search the company, its
domain, its ATS, an official careers page, or any other external source for
location classification. Write exactly one final matching JSONL result per
input row to the emitted result path.

Each result repeats `source`, `external_job_id`, and `description_sha256`, and
contains `decision`, `evidence`, `reason`, `confidence`, and optional
`required_location`. Results use the default
`evidence_source=linkedin_description`. Valid decisions are `remote_india`,
`remote_unspecified`, `remote_foreign_only`, `bengaluru_workplace`,
`onsite_outside_bengaluru`, and `unclear`.

When full enrichment is requested, every result also contains
`job_enrichment` with `status`, supported experience/education/qualification
fields, deduplicated hard and soft skills, and evidence-bearing salary and
mandatory-experience objects. A row with no description uses
`status=no_description` and contains no extracted facts. If `salary_raw` is
present on a described row, salary must record its shortest exact evidence and
either `guaranteed_max_lpa` or an `unusable_reason`.

- Use `remote_india` only when the role can be performed fully remotely while
  based in India. Remote-first, work-from-anywhere, or a genuine home-working
  choice qualify when India is allowed.
- Use `remote_foreign_only` when the remote role explicitly excludes India.
- Bengaluru hybrid/office/flexible work is `bengaluru_workplace`, never remote,
  unless the description explicitly makes the role remote-first or
  work-from-anywhere.
- Required office work outside Bengaluru is `onsite_outside_bengaluru`.
- Use `unclear` when the description does not establish the work arrangement;
  absence of evidence is not remote evidence.
- `evidence` must be the shortest exact excerpt proving the work arrangement;
  country eligibility alone is insufficient.

For LinkedIn `remote_india` search instances, the sole automatic fallback is a
description judgment of `unclear` with `confidence=low` or `confidence=medium`
whose raw LinkedIn
listing location, after trimming and case-folding, is exactly `India`. Tag only
that combination as `Remote — India`. Negative decisions
(`onsite_outside_bengaluru` and
`remote_foreign_only`) are rejected at every confidence level. Bengaluru
workplace results continue to pass as Bengaluru and are not relabeled remote.
For a still-unclear remote-instance row, preserve the original listing
location. A raw Bengaluru/Bangalore listing remains eligible as Bengaluru even
when work nature is unclear; other unresolved locations still reject.
Role and seniority remain earlier gate axes, so no location fallback rescues a
job that already failed either one.
This policy applies only to LinkedIn `query_location_mode=remote_india`; do
not apply it to Bengaluru instances or any other connector.

The automatic India tag is a query-scoped fallback, not fabricated evidence:
preserve the agent's `unclear` judgment, confidence, and exact description
evidence while recording that the fallback supplied `Remote — India`.

The coordinator must cover every input ID and preserve the description hash.
The ingest command rejects missing, extra, duplicate, stale, or malformed-source
results before the relevance gate.

## Token-efficient full-pipeline modes

The full pipeline splits this work:

- **Before approval**, read only the JSONL created by
  `python -m scripts.run_full_pipeline screening-input --run-id <id>`.
  Extract and persist only salary and mandatory minimum/maximum experience
  screening facts. Report each frozen version through the Decision Run seam,
  but do not populate education, qualifications, skills, or verbose review
  JSON for rejected inventory.
- **After approval**, fully enrich only the posting-version IDs in the
  immutable `process-approved` artifact. This is the ordinary supported-field
  extraction above, scoped to approved jobs rather than the global backlog.

In either full-pipeline mode, the coordinator may shard reasoning across up to
three workers by disjoint frozen posting-version IDs. Each worker writes a
separate structured result artifact and does not call
`report_job_enrichment` or update shared stage state. The coordinator validates
and persists each shard, then serially reports the combined terminal outcomes
through the Decision Run seam. Never pass a SQLAlchemy session or ORM object
to a worker.

Keep evidence in the database/audit artifact. Agent-facing batch reports are
counts, failed IDs, and the artifact path—not repeated evidence fields.
