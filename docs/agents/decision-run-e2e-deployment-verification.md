# Decision Run E2E deployment verification

`tests/test_attended_pipeline_e2e.py` exercises the internal workflow with
SQLite and an in-memory mailbox. It never sends mail. The literal live gate
below is **unexecuted** and must be recorded separately after deployment. A
non-sensitive predecessor-run record follows the reusable procedure; it is
useful evidence but does not satisfy the current one-draft and mandatory
completion-email requirements.

Use one run ID throughout. Before starting, apply the reviewed Decision Run SQL
migrations manually and run `python -m scripts.verify_dashboard_queries`
against production.

1. Start and reconcile delivery history:
   `python -m scripts.run_full_pipeline start --since <offset-aware-time>`;
   save the printed `<run-id>`, then run
   `python -m scripts.reconcile_outreach --run-id <run-id>`.
2. Ingest that exact window:
   `python -m scripts.run_full_pipeline ingest --run-id <run-id>`.
3. Invoke the **enrich-jobs skill** for only the printed canonical
   posting-version IDs and report every terminal outcome through its Decision
   Run seam. Then run
   `python -m scripts.run_full_pipeline screen-decision --run-id <run-id>`.
4. Invoke the **enrich-companies skill** only for the frozen company manifest:
   report Phase A, then Glassdoor and AmbitionBox Phase B observations for
   every company. Then run
   `python -m scripts.run_full_pipeline finalize-decision --run-id <run-id>`
   followed by
   `python -m scripts.run_full_pipeline prepare-decision --run-id <run-id> --output <approval.xlsx>`.
5. Confirm the dashboard’s run-scoped job/company funnels, exact drop and
   ungroupable drilldowns/links, stage attempts/counts/heartbeats, and approval
   waiting time. Approve the workbook with
   `python -m scripts.run_full_pipeline approve --run-id <run-id> --selection '<rules>' --workbook <approval.xlsx>`,
   then run `python -m scripts.run_full_pipeline process-approved --run-id <run-id>`.
6. Interrupt one approved downstream skill stage, resume the *same* run ID and
   frozen manifest, and record both attempt numbers and unchanged counts.
   Invoke **tailor-resumes**, **find-contacts**, and **draft-outreach** using
   only the IDs printed for that approved scope. Push the complete frozen
   run-scoped draft manifest with
   `python -m app.outreach.cli push --run-id <run-id>`; verify every prepared
   contact has a Gmail Draft, the Gmail manifest is terminal, and no outreach
   message was sent. Draft posting does not wait for calibration or delivery
   reconciliation.
7. Build the final report with
   `python -m scripts.run_full_pipeline final-report --run-id <run-id> --output <final.xlsx>`.
   Record the run ID, migration revision, exact window, every stage
   attempt/count/heartbeat, job/company funnel totals and drilldown links,
   selected IDs, resume artifact paths, Gmail Draft IDs, and final workbook
   path in the deployment record.

SQLite cannot prove PostgreSQL migration compatibility, authenticated Gmail
behavior, or attended agent judgment, so this checklist is not satisfied by
the offline test.

## Historical predecessor-run evidence: 2026-08-25

This run predates the mandatory completion-email requirement. It exercised the
complete durable stage chain and posted six Gmail Drafts for the approved
contacts, but its final workbook was not self-emailed. It must not be cited as
a passing execution of the current deployment gate.

- Decision Run: `20260825T154355Z-fee4`, state `completed`, telemetry
  `decision_funnels_v1`.
- Immutable posting window: `2026-08-24T20:44:10.768000+05:30` through
  `2026-08-25T21:13:55.899348+05:30` (`Asia/Kolkata`). Collection was one
  attended LinkedIn Data Science/Bengaluru connector instance.
- Schema gate: all reviewed 2026-08-25 Decision Run SQL migrations under
  `docs/migrations/` were present in the live schema. This repository has no
  migration-version table, so there is no synthetic revision identifier to
  record.
- Ingestion/job funnel: 200 fetched and collection-unique; relevance retained
  111 and dropped 89 (`recency=36`, `role=50`, `seniority=3`); canonical job
  dedup retained 110 and dropped one duplicate; enrichment completed 110/110;
  screening produced 98 eligible and 12 hard rejections
  (`minimum_experience_8_or_more=11`, `maximum_experience_2_or_less=1`).
- Grouped job counts were Group 2 = 18 and Group 4 = 79, with 13 ungrouped
  rows (12 hard rejections plus one eligible job belonging to the ungroupable
  company). Public-link drilldowns were spot-checked for eligible posting
  version `25737` and rejected/ungrouped posting version `25742`.
- Company funnel: 24 companies entered both phases. Phase A completed 24/24;
  Phase B terminated 24/24 as 23 advanced plus one recorded
  `glassdoor_source_error`. Terminal grouping was Group 2 = 15, Group 4 = 8,
  ungroupable = 1.
- Approval preserved the same run ID through three durable attempts:
  attempt 1 interrupted with `1` pending; attempt 2 waited for approval with
  the same `1` pending; attempt 3 completed `1/1`. Recorded human-wait time was
  3,568 seconds. The frozen scope was company `1180`, posting version `25764`,
  selection kind `fresh_outreach`; the selected counts remained one company
  and one job downstream.
- Resume tailoring completed `1/1` for job `9039`, posting version `25764`;
  artifact directory:
  `<project-root>/output/tailored_resumes/job_<id>`.
- Contact enrichment completed `1/1` (terminal outcome `exhausted`). Draft
  preparation completed `6/6`, and Gmail Draft posting completed `6/6`.
  Aggregate delivery evidence showed six Gmail Draft IDs and zero Gmail
  message IDs, zero `sent_at` values, zero sent/delivered/replied/bounced
  states, and zero messages marked sent. No outreach was sent.
- Final report completed `1/1`. The eight-sheet workbook at
  `<project-root>/outputs/final-<run-id>.xlsx`
  opened as a valid `.xlsx`, and its worksheet XML contained no spreadsheet
  error tokens. The report was deliberately not self-emailed in this historic
  run because that egress had not been authorized then; mandatory completion
  self-email is covered by the later implementation and offline tests.
- `python -m scripts.verify_dashboard_queries` passed against the live
  PostgreSQL database after completion, including every Decision Run stage,
  funnel, and drilldown read model. The verifier passed again on 2026-08-26
  after the progress read model was shared with completion-email rendering.
