# ADR-0013: Company profile enrichment precedes contact search

## Status

Accepted, 2026-08-15.

## Context

`find-contacts` previously mixed company research (canonical domain,
employer/staffing verification, and hiring-function selection) with billed
people search. Company facts and pain points also lived inside `enrich-jobs`.
This made contact batches long and interpreted the same evidence at multiple
seams.

## Decision

Create one `enrich-companies` skill that owns:

- company facts and confidence-labeled pain points;
- identity-confirmed, MX-verified canonical domain status;
- `company_type` (`employer` or `staffing`);
- persisted `contact_search_groups` (`data_ai`, `credit_risk`, both, or `[]`).

`enrich-jobs` owns job rows only. `find-contacts` consumes the stored company
profile and never resolves domains or reclassifies companies/functions inline.
The contact queue admits only rows whose `contact_search_groups` is non-null.

The persistence seam is
`app.contacts.company_profile.record_contact_company_profile`, which validates
the domain-status invariant and closed classification vocabularies before
writing all contact prerequisites together.

## Schema change

Fresh databases receive the fields through SQLAlchemy `create_all`. Existing
Postgres databases require the manual migration:

```sql
ALTER TABLE companies
  ADD COLUMN IF NOT EXISTS contact_search_groups JSON,
  ADD COLUMN IF NOT EXISTS contact_profile_enriched_at TIMESTAMPTZ;
```

`NULL` search groups mean not yet classified; `[]` means classification
completed with no supported target hiring function.

## Consequences

- Contact batches start directly at the code-generated people-search plan.
- Domain/type/function evidence is reviewed once and reused.
- Existing companies are temporarily absent from the contact queue until
  `enrich-companies` profiles them; this is intentional rather than a legacy
  fallback that silently recreates the old long workflow.
- New job evidence can make a profile stale; `enrich-companies` compares its
  profile timestamp with the latest relevant posting and refreshes it.
