---
name: draft-outreach
description: Explicit standalone outreach workflow for one recipient or a user-supplied batch, including contact/email discovery when needed, truthful drafting, persistence, retries, and Gmail Draft creation; never sends.
---

> **Invocation boundary:** This skill runs only when the user explicitly
> invokes `draft-outreach`. `full-pipeline` must never invoke, resume, or count
> it as a stage. This skill may use its complete contact discovery, email
> resolution/verification, retry, persistence, draft-format, and Gmail Draft
> capabilities. It never calls Gmail `messages.send`.

## Single or batch input

Accept one target directly, a pasted list/CSV table, or a `.csv`, `.json`, or
`.xlsx` file. Validate all rows through `app.outreach.batch_input` before any
provider call. Supported columns are `recipient_email` (aliases `email` and
`to_email`), `company_name`/`company`, `to_name`/`name`, `recipient_title`/
`title`, `company_id`, `prospect_id`, `matched_job_id`/`job_id`, and
`resume_path`.

An address supplied in a row goes directly to pairing/drafting. When it is
blank but company identity exists, invoke the existing `find-contacts` skill;
do not implement or call a second discovery engine. Run its normal
code-generated search plan, provider ledger/caps, agent judgement,
`attach_email` resolution/retries, and `record_company` persistence before
continuing here with the resolved recipients.
A row with neither an address nor company identity is invalid. Show the exact
validated target count, targets requiring discovery, expected provider work,
and Gmail Draft count before a batch. The input list is the immutable batch
scope: do not silently add companies or recipients.

Run the existing steps below once per resolved target, using the established
draft format and repeat-safe persistence. Push only the resulting scoped
drafts to Gmail Drafts; do not use an unscoped historical `push --all` when
unrelated pending drafts exist. Report every target as drafted, pushed,
skipped, unresolved, or failed, retaining retry-safe state.

# Draft Outreach

Takes **a company (or prospect) and an email address**, and produces one cold
email asking about available opportunities — pushed to **Gmail Drafts**, never
sent by this system. Reuses the same no-API-bill contract as `enrich-jobs` and
`tailor-resumes`: the email's *judgement* (which resume to attach, what the
hook is, how the ask is phrased) is the agent's own reasoning in-session;
everything mechanical (pairing, persistence, Gmail) is `app/outreach/`.

The workflow uses Gmail Drafts as its review surface and handles known contacts
and researched prospects through the same evidence rules. This file is the
operational sequence: read the bundled `references/` document before that
step's first use in a run, not necessarily every time.

## The hard integrity rule (same as tailor-resumes, read first)

The email may only assert things that are true: the candidate's real
experience (from the confirmed private resume master at
`~/.job-atlas/resume-master.yaml`, or `RESUME_MASTER_PATH`), the recipient's real title, a
real job at the company, or a **hook** with a verifiable first-party source
stored alongside it. **Never** flatter, never claim familiarity that doesn't
exist, never assert anything about the company that isn't in an enrichment
field or a sourced hook. See `references/hooks.md` for why this is a hard
rule, not a style preference.

## Steps

### Approved decision-run mode

Use `python -m app.outreach.cli pair --run-id <id> --contact-id <id>` for
pairing. When persisting a draft, include `--decision-run-id` and
`--posting-version-id`. The CLI rejects a job/version outside the approved
manifest and dual-writes the immutable delivery ledger. Do not use
`push --all` for this mode: `push --run-id <id>` pushes every prepared contact in
the frozen approved-run manifest immediately.

Before preparing any copy, make an explicit `ChosenPairing` for **every**
frozen contact, then call
`freeze_draft_preparation_manifest(session, run_id, chosen_pairings=choices)`.
For a tailored choice, supply the exact approved `(job_id, posting_version_id)`
from the returned candidate/evidence set. For a master choice, supply
`(None, None)` only when the pairing policy returns master. The seam rejects
missing, partial, out-of-scope, or automatic fallback choices. Use the returned
`posting_version_id` and `job_id`, never a current queue or an independently chosen job. On a copy refusal or rendering failure, report
that exact frozen contact as `DraftResult(FAILED, reason, disposition)` (or
`DROPPED` for an approved exclusion); do not leave it silently pending.
For Gmail, use `push --run-id <id>` for the complete frozen manifest. It does
not wait for a calibration send or delivery-pattern reconciliation. It creates
Gmail Drafts only—never `messages.send`.

After the coordinator freezes the complete explicit pairing manifest, it may
shard copy reasoning by disjoint contact IDs. Workers write separate subject
and body files and return evidence; they do not persist `OutreachDraft` rows or
report stage progress. The coordinator serially validates/persists every draft,
then performs one run-scoped Gmail push. Gmail posting and the final self-report
remain serial because their intent/receipt reconciliation is the safety seam.

### 1. Resolve the recipient and decide the resume

```bash
python -m app.outreach.cli pair --company-id 1 --recipient-tier ic --recipient-title "Senior Data Scientist"
```

Prints the pairing decision: `resume_kind` (`tailored`|`master`), the
`reason`, and — when tailored — the `candidates` job-id pool the actual
`--matched-job-id` must come from. **Read
[references/tier-shapes.md](references/tier-shapes.md) before your first
draft** — it has the pairing rule's rationale and the five recipient shapes.

If you don't have a `company_id`/`prospect_id` yet, resolve the name first:
`companies` by exact/normalized name, then `prospects` (same normalization,
`app/companies/naming.py`) if no company matches. If neither table has it,
say so — this skill does not create new company or prospect rows.

**No candidate job → master, always.** Do not pick "the closest job anyway."
A resume tailored to the wrong function is worse than the plain master — see
`references/tier-shapes.md` for the measured reason.

### 2. Tailored path only: reuse the resume

```bash
python -m scripts.tailor_resume --requirements --compact --run-id <run-id> --job-id <matched-job-id>
```

In an approved full-pipeline run, `/tailor-resumes` has already rendered the
exact approved posting version. Reuse that `tailored_resumes` row and PDF;
never repeat tailoring inside draft generation. The compact job brief is also
the source for the pain-points line, so do not reopen the full JD.

**Master path**: nothing to render here — `app.outreach.cli draft` renders
(and caches) the plain master automatically when `--resume-kind master` is
given with no `--resume-path`.

### 3. Write the email

**Follow [references/email-template.md](references/email-template.md)
exactly — it is a fixed format, not a style default.** Every tailored
email uses the same skeleton: keyword-dense subject, a soft team-level
opening ask with the specific job as one example, ONE flowing sentence of
relevant experience (no bullets, no per-company breakdown), one JD-derived
pain-points line, fixed resume/sign-off close. No labeled sections, no
bullets, minimal-to-no bold — that was v1's shape and the user rejected it
as "too structured, more resume" than email. Only the content inside each
slot changes. A master draft uses the complete TA master fallback documented
there. Do not restructure either shape or write freely instead.

Every factual claim must trace to: the resume master, the compact brief for the
job just matched (for the pain-points line), a company
enrichment field, or a sourced hook.

For `resume_kind=master`, use the fixed **TA master fallback** in the template
doc. Treat the recipient as Talent Acquisition for copy shape only: use the
generic opportunities opening, omit the unavailable job link and JD-only
pain-points paragraph, and continue without asking for wording. Do not change
the contact's stored tier, the pairing reason, or the master-resume decision.
Tier variation for tailored drafts remains defined by
`references/tier-shapes.md`.

**Hook research is off by default.** Only research a hook when the user
explicitly requests hooks for this run. If requested and you find a genuine, current,
first-party fact (company newsroom, official blog, the recipient's own
post) — use it, and capture its URL and the verbatim text. If you did not
find one within budget, write the evidence-bound version. **Never invent
one to fill the slot.** Read
[references/hooks.md](references/hooks.md) before your first hook — it has
the sourcing rule, the fabrication evidence behind it, and the search
budget.

Write `subject` and `body` to files (avoids shell-quoting a long body).

### 4. Persist

```bash
python -m app.outreach.cli draft --to-email "x@example.com" --company-id 1 \
    --to-name "..." --recipient-title "..." --recipient-tier ic \
    --resume-kind tailored --pairing-reason recipient_function_match \
    --tailored-resume-id <id> --resume-path <pdf path> --matched-job-id <id> \
    --subject-file subj.txt --body-file body.txt \
    [--hook-url "..." --hook-quote "..."]
```

Refuses (exit 2) on: a hook with only one of URL/quote, a `resume_kind`/
`pairing_reason` mismatch, a `tailored_resume_id` that doesn't exist, or an
email that can't be resolved to a recipient at all. Fix and retry — never
work around a refusal by picking different flags to make it pass.

**Re-running for the same `--to-email` updates the existing draft, never
duplicates one.** One live draft per recipient (ADR-0010).

If the row comes back `collision_risk=True` (ADR-0012), it is still pushed
like any other draft — the flag is informational, surfaced in the
full-pipeline run workbook so the candidate can double-check the person
before sending, not a reason to stop here.

### 5. Push and review

```bash
python -m app.outreach.cli push --to-email "x@example.com"   # one
python -m app.outreach.cli push --all                     # everything drafted, including collision-risk rows (ADR-0012)
```

Requires a cached Gmail token. If it exits `GMAIL NOT CONFIGURED`, tell the
user to run `job-atlas auth setup --authorize-gmail`. This is a one-time setup
step, not something to retry automatically.

**This skill never sends.** Pushing creates a Gmail Draft; the user reads,
edits, and sends it themselves. Report the Gmail draft id and tell the user
to check their Drafts folder.

### 6. Report

```bash
python -m app.outreach.cli report
```

Status counts plus the collision-risk list (pushed, flagged for review —
ADR-0012). Give the user both.

## The standalone first-batch trial gate

This gate does not apply to an approved full-pipeline run, whose approval
authorizes drafts for the complete frozen contact manifest. For a standalone
invocation, **the first invocation is a go/no-go, not a normal run.** Draft one
recipient, push it, and stop — have the user read it in their actual Gmail
Drafts folder before drafting a batch. Inline tailoring (step 2) makes a
realistic batch **5–10 recipients**, not more — a careful tailoring pass
takes real reasoning time per recipient, and pretending otherwise produces
rushed, generic resumes.

## What this skill does not do

- **It does not send.** Sending is the user's own act, from their own Gmail.
- **It does not verify email deliverability.** Every stored contact address
  is `unverified` (ADR-0005's addendum dropped SMTP verification) — a wrong
  guess bounces harmlessly, which is a different risk from a name
  collision (delivers successfully to the wrong human, never bounces —
  see `collision_risk` flagging, ADR-0012).
- **It does not create Company or Prospect rows.** If the input company
  isn't in either table, that's `find-prospects`' or the ingestion
  pipeline's job, not this one.
- **It does not sync Gmail.** That's `python -m scripts.sync_outreach`,
  meant to run on a schedule (cron), not from inside this skill.
