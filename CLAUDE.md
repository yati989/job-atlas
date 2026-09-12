# CLAUDE.md

Maintainer guide for Job Agent. Read `AGENTS.md` first for operating rules and
`CONTEXT.md` for domain terminology. Architectural rationale lives in
`docs/adr/`.

## Product boundary

Job Atlas is a local-first guided pipeline for collecting job postings,
applying one accepted `SearchProfile`, retaining relevance outcomes,
deduplicating canonical jobs, building evidence, selecting an immutable scope,
and optionally tailoring resumes or finding relevant LinkedIn profile links.
Public profiles, results, and credentials use an independent private data root;
SQLite is the default public store.

The accepted profile defines profession, search/relevance vocabulary,
geography, arrangements, seniority, experience, hard rejects, collection
window, and exact sources. Connector capability still limits supported native
queries. The full pipeline never discovers person emails or creates outreach.
Explicit standalone `draft-outreach` may reuse the existing complete
contact/email and Gmail Draft machinery and never sends automatically.

## Architecture

```text
app/
├── collectors/       Source adapters implementing BaseConnector
├── pipeline/         Collection, deduplication, gating, progress, persistence
├── decision_runs/    Immutable run manifests and human approval state
├── jobs/             Job screening and evidence
├── companies/        Company identity and market evidence
├── contacts/         Public-search contact research and email resolution
├── prospects/        Companies discovered without a scraped job
├── resume/           Resume schema, tailoring plans, rendering
├── outreach/         Draft persistence and Gmail adapters
├── reporting/        Decision and final workbooks
├── dashboard/        Streamlit read models and UI
├── models/           Pydantic and SQLAlchemy schemas
└── config/           Search policy and environment settings
```

Runtime source registries are authoritative:

- `app/pipeline/registry.py` contains unattended and headless-capable source
  instances.
- `scripts/run_headed_sources.py` contains attended sources that require a
  visible browser.

Inactive connectors are not retained as commented imports or dead modules in
the main tree. Use Git history when investigating an old implementation.

## Ingestion contract

Every connector subclasses `BaseConnector` and returns
`list[NormalizedJob]`. That schema is the only boundary between source-specific
collection and the shared pipeline.

```text
connector fetch
  → NormalizedJob
  → within-run collection deduplication
  → profile-driven hard-reject / role / seniority / experience / location gate
  → retained kept / rejected / Needs review outcomes
  → kept companies/jobs/posting versions
  → cross-source job deduplication
  → company deduplication
```

Connectors own transport, native query syntax, pagination, retries, and raw
field extraction. They do not own relevance policy. The shared gate in
`app/pipeline/relevance.py` applies the frozen profile in a stable order and
returns `kept`, `rejected`, or `needs_review`. Unknown evidence must not be
silently converted into a confirmed fact.

Posting timestamps are stored and can be used to bound collection, but are not
a relevance-gate axis. Source identity is `(source, external_job_id)`. Material
changes to a posting create immutable `JobPostingVersion` rows.

### LinkedIn location exception

The active LinkedIn connector uses the logged-out jobs surface. Because that
surface does not reliably expose workplace type, hydrated descriptions are
classified for location before the location gate, after role and seniority have
already removed irrelevant jobs.

For a LinkedIn `remote_india` query, an `unclear` result whose raw listing
location is exactly `India` is accepted as remote at low or medium confidence.
Bengaluru/Bangalore remains a Bengaluru result. Confirmed foreign-only or
onsite-outside-Bengaluru jobs are rejected. There is no company-career-page
fallback in this stage.

## Connector mechanisms

Choose the cheapest verified mechanism that works:

1. JSON API or feed.
2. Static HTML/internal HTTP endpoint.
3. Headless browser for genuinely client-rendered content.
4. Attended browser when a visible session is required.
5. Saved login session only when the source is genuinely account-gated.

Do not infer support for a query, location, date, sort, or pagination parameter
from its name. Compare a real query with a nonsense query and inspect returned
content. For a connector change, run only its focused offline tests and
`python -m scripts.verify_source <source> --max-instances 1`.

## Agent-driven modules

The judgment-heavy modules run through repository skills, not their Python CLI
seams:

- `enrich-jobs`
- `enrich-companies`
- `find-prospects`
- `find-contacts`
- `find-profile-links`
- `tailor-resumes`
- `mock-interview`
- `draft-outreach`

The CLIs under `app/*/cli.py` and `scripts/` provide validated persistence,
provider access, billing limits, and report generation. They are not alternate
entrypoints for agent judgment.

The public guided workflow is coordinated by `full-pipeline`. It freezes the
effective profile/source plan, records durable progress, builds reusable Phase
A evidence, freezes an all/manual/filtered selection scope, and offers
tailoring and links-only profile discovery independently. Provider work needs
a reviewed exact query plan and global call ceiling. Google Sheets and Gmail
are not pipeline prerequisites. The public beta is India-only: accepted search
profiles must use `countries: [IN]`, while Indian cities and work arrangements
remain configurable.

## Important invariants

- The central relevance gate is the only relevance authority.
- Role and seniority are checked before LinkedIn description-location work.
- Collection deduplication is separate from cross-source canonical job
  deduplication.
- Agent result ingestion rejects missing, extra, duplicate, stale, or malformed
  rows before persistence.
- A selection scope can be frozen only after every selected posting version has
  version-bound Phase A evidence and every selected company has completed its
  Phase A identity/function classification.
- Company profiling precedes contact search.
- Full-pipeline profile discovery reuses contact-search mechanics but persists
  only canonical LinkedIn URLs and minimal professional evidence; it never
  resolves person emails.
- Standalone contact/outreach work must not fabricate people, titles, profiles,
  domains, emails, resume facts, or message claims.
- Tailored resumes must remain truthful and traceable to the master resume.
- Explicit standalone outreach is stored and pushed as Gmail drafts; it is
  never auto-sent and is not a full-pipeline stage.
- Approval scope is immutable and downstream work cannot broaden it.
- Terminal progress is recorded only after all required work finishes.

## Database

`app/models/orm.py` is the schema source of truth. Public guided execution uses
the private SQLite path managed by `app/workflows/public_database.py`; it backs
up existing data before upgrades and refuses to mark a structurally incomplete
schema current. `create_all()` is fresh-schema setup, not an existing-column
migration. Legacy PostgreSQL deployments apply reviewed SQL from
`docs/migrations/`.

## Commands

```bash
# Setup
python -m scripts.init_db

# Collection
python -m app.pipeline.run_all --headless-only
python -m app.pipeline.run_all
python -m app.pipeline.run_all --source linkedin

# Verification
python -m pytest tests/
python -m scripts.verify_source <source> --max-instances 1
python -m scripts.verify_dashboard_queries

# Dashboard
scripts/web-dashboard

# Inspection
python -m scripts.query_db companies --where "enrichment_status='done'"
python -m scripts.query_db --sql "SELECT source, count(*) FROM jobs GROUP BY 1"
```

Use the repository virtual environment (`.venv/bin/python`) in this workspace.
Do not run the broad live connector smoke test for a one-source change.

## Tests

The offline suite uses in-memory SQLite and provider fakes. Connector tests are
fixture-driven. Tectonic-dependent rendering tests skip when Tectonic is not on
`PATH`. Live connector behavior is verified separately and only at the scope
authorized by the user.

## Public-release hygiene

Keep `.env`, OAuth tokens, browser profiles, resumes, contact batches, logs,
generated workbooks, and other user data out of version control. Before a
public release, audit both the current tree and Git history for secrets and
personal data.
