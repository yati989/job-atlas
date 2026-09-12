# Use a private profile-driven guided runtime

Status: accepted

The public product uses an accepted, immutable `SearchProfile` to drive source
selection and central relevance, stores run state in a separate private SQLite
database, retains kept/rejected/Needs review outcomes, and freezes downstream
posting-version/company scopes. This replaces the owner-specific assumption of
a code-defined India/data-role policy backed by the private PostgreSQL and
Google approval workflow.

The initial public beta is intentionally India-only. Accepted profiles use
`countries: [IN]`; professions, Indian cities, work arrangements, experience,
and source choices remain configurable. Multi-country support is deferred until
each advertised geography has verified source capability and relevance
evidence, rather than presenting a syntactically accepted country as supported.

SQLite keeps native setup portable across Windows, macOS, and Linux, while
schema versions, pre-upgrade backups, and structural checks protect local
history. Judgment-heavy Phase A, resume, profile-link, outreach, and mock
interview work remains agent-owned through skills; Python surfaces validate,
persist, budget, and export rather than replacing that reasoning.

## 2026-09-12 amendment: packaged database initialization

The installed public package initializes a private SQLite database and all
required tables on first workflow use. `job-atlas init` exposes the same
operation as an explicit setup and health check. This stores searches, jobs,
companies, enrichment, selections, and dashboard history without requiring a
separate database server.

Advanced installations may instead set `JOB_SEARCH_AGENT_DATABASE_URL` to an
existing PostgreSQL database. The same initialization interface creates its
tables; server provisioning remains outside the Python package.
