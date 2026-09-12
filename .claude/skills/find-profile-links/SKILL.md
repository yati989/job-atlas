---
name: find-profile-links
description: Discover relevant LinkedIn profile links for a confirmed full-pipeline scope with a durable provider-call budget and no email collection.
---

# Links-only profile discovery

This is the only people-search skill the public `full-pipeline` may invoke. It
stores LinkedIn profile URLs and minimal professional relevance evidence. It
does not discover, derive, verify, store, log, or export email addresses and
does not create outreach drafts.

This is a links-only mode of the existing `find-contacts` machinery, not a
second people-search workflow. Before running it, read and follow
`../find-contacts/SKILL.md` through candidate judgement, with the differences
below.

Start from an immutable `PublicSelectionScope` and completed Phase A company
identity/function evidence. Load the selected companies with
`contexts_for_public_scope`, then reuse the existing:

- `app.contacts.search_plan.plan_for_company` query plan and tier ordering;
- `app.contacts.brightdata_query` / `search_source.bright_data_profiles`
  provider path, billed-call ledger, retry rules, and caps;
- profile relevance gates and agent judgement rules from `find-contacts`.

Public Phase A function groups are not restricted to the owner's historical
`data_ai`/`credit_risk` categories. A validated profession slug receives the
same established tier ordering with profession-specific seed titles. A
canonical company website may help identity matching, but an MX record is not
a prerequisite and must not be queried in this links-only workflow.

Review the selected company/function denominator and the code-generated query
plan before provider calls:

```bash
job-atlas profile-plan --scope-id <scope-id>
job-atlas authorize-profile-search --run-id <run-id> \
  --scope-id <scope-id> --maximum-calls <approved-ceiling> --confirm
```

Run only exact queries printed in that frozen plan, through the existing
provider command with the authorization attached:

```bash
python -m app.contacts.brightdata_query search \
  --authorization-id <authorization-id> --private-home <private-home> \
  --company-id <id> --group <group> --tier <tier> --attempt <1|2> "<query>"
```

The command reserves the global call slot durably before provider I/O, then
uses the existing per-tier ledger and retrying provider transport. Successful,
failed, and interrupted/ambiguous reservations all remain visible across
resume; an already recorded query is never issued twice. Do not repair
missing company identity through an unapproved paid call, improvise extra
queries, or bypass either budget ledger.

After judgement, call `app.contacts.agentic_batch.record_profile_links` with
the same `CompanyContext` and `JudgedContact` objects. This replaces only
`find-contacts` step 4: never call `attach_email` or `record_company` in this
mode. The persisted evidence is limited to canonical LinkedIn URL, name,
headline, tier, search group, and tier rationale; raw snippets and email-like
text are not stored.

Keep every relevant canonical `/in/` URL found within the approved plan, even
more than five for one company, but return fewer when company/title evidence
is insufficient. Never pad results. Resume from the existing provider ledger
and repeat-safe link rows; stop with partial status when the established call
cap is exhausted.
