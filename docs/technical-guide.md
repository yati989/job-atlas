# Technical guide

[← Back to Job Atlas](../README.md)

Setup, configuration, command reference, source requirements, and contributor details.

Job Atlas is a local-first guided pipeline for configurable professions,
Indian cities, work arrangements, experience bands, and source choices. The
public beta accepts only the `IN` country code. A reviewed
YAML/JSON profile—or an agent-generated proposal from natural language—drives
the same validated plan, central relevance policy, persistence, and resume
workflow.

Public profile discovery inside the full pipeline is LinkedIn-links-only. The
pipeline never discovers email addresses or creates outreach. An explicitly
invoked `draft-outreach` workflow can separately process one recipient or a
user-supplied pasted/CSV/Excel batch, perform its own contact/email discovery
when needed, and create Gmail Drafts. It never sends messages.

## What it does

```text
Reviewed private SearchProfile + exact source plan
    ↓
Fetch selected sources → normalize into NormalizedJob
    ↓
Collection deduplication
    ↓
Hard rejects → role → internship/part-time policy → experience → location
    ↓
Private database: all run outcomes + kept jobs + immutable posting versions
    ↓
Phase A evidence → optional Phase B over an all-eligible research scope
    ↓
Final all/manual/filter scope → truthful resumes and/or profile links
```

Rejected and ambiguous observations retain decision evidence without entering
canonical job storage. Source failures and retries remain visible per run.

## Active sources

The runtime registries are the source of truth:

- `app/pipeline/registry.py` — unattended and headless-capable sources
- `scripts/run_headed_sources.py` — attended sources requiring a visible browser

| Source | Runtime | Notes |
|---|---|---|
| Built In | HTTP | Indian city, India-wide, and remote searches |
| Cutshort | Authenticated HTTP | Requires a one-time saved candidate session |
| eFinancialCareers | HTTP | Finance and risk-oriented source |
| Foundit | HTTP | Native role/location search |
| Glassdoor | HTTP | Anonymous search endpoint |
| Hacker News Hiring | API/feed | Broad monthly hiring thread |
| Himalayas | API | Remote-job search |
| IIMJobs | HTTP | India-focused roles |
| Indeed | Attended browser | Visible browser session required |
| Instahyre | HTTP | India-focused role/location search |
| LinkedIn | HTTP | Logged-out jobs surface; descriptions support remote classification |
| Naukri | HTTP | Anonymous search endpoint |
| Shine | HTTP | Native city search; remote evidence screened from the broad feed |
| Talent.com | HTTP + headless browser | Role/location search |
| TimesJobs | HTTP | Role/location search |
| Wellfound | HTTP | Startup roles |
| We Work Remotely | Feed/search | Remote-job source |
| Working Nomads | API | Remote-job search |
| ZipRecruiter India | Headless browser | Browser-rendered India listings |

Inactive and superseded connectors are intentionally not part of the runtime
surface. Their history remains available through Git.

## Guided stages

1. Review the effective search profile, its 14-day default search period (or
   an explicitly requested 1–30 days), the older-job rejection policy, and
   the exact named source/workload preview.
2. Freeze the plan, collect, normalize, classify, deduplicate, and persist.
3. Run reusable job/company Phase A evidence.
4. Optionally approve paid Phase B research.
5. Confirm all eligible results, a manual subset, or validated saved filters.
6. Independently choose resume tailoring and/or budgeted LinkedIn profile links.
7. Export artifacts and resume safely after interruption.

Selection changes create immutable revisions; completed source queries and
provider calls are reused instead of silently repeated.

## Requirements

- Python 3.11+
- SQLite (included with Python) for private search history and dashboard data
- Optional PostgreSQL when an advanced user wants an external database server
- Chromium through Playwright only for browser-backed selected sources
- A visible desktop session for attended connectors such as Indeed
- Optional Bright Data credentials for approved Phase B/profile-link research
- Optional Google OAuth desktop-client credentials for creating Gmail Drafts
- Optional Tectonic and `pdftotext` for resume rendering and verification

## Quick start

Install the package on Windows, macOS, or Linux. The recommended tool install
keeps Job Atlas in its own managed environment:

```bash
uv tool install job-atlas
job-atlas setup
```

`pipx install job-atlas` and `pip install job-atlas` are supported alternatives.
Maintainers can use `pip install .` from a source checkout before the first
published release.

`setup` initializes the local database and installs the job-seeker skills in
`~/.agents/skills`. Restart the coding agent once after setup, then launch the
dashboard:

```bash
job-atlas-dashboard
```

Check the installation at any time:

```bash
job-atlas doctor
job-atlas skills status
```

Most sources work without a browser. When your reviewed source plan includes a
browser-backed source, install Chromium once with `job-atlas browser install`.
Attended sources such as Indeed also require a visible desktop session.

`setup` reports whether the optional Bright Data and Gmail integrations are
ready. Store those credentials outside the checkout only when you use those features:

```bash
job-atlas auth setup
job-atlas auth status
```

The setup prompt masks Bright Data API keys, copies the selected Google OAuth
client JSON, and can open Google's one-time Gmail consent flow. Credentials and
tokens default to `~/.job-atlas`. On macOS and Linux, Job Atlas sets private
directory and file modes; on Windows, the directory inherits the current
user profile's access controls. Existing
checkout `.env`, `credentials.json`, and `.gmail_token.json` files remain
supported for older installations. Status output reports presence and source
only; it never prints keys, client secrets, or OAuth tokens.

The first workflow command also performs this initialization automatically.
It creates `~/.job-atlas/data/job-atlas.sqlite3` and every table
needed for searches, jobs, companies, enrichment, selections, and dashboard
history. Existing databases are preserved and checked before use.

Advanced users may install the optional PostgreSQL adapter and point the same
package at an existing PostgreSQL database:

```powershell
pip install "job-atlas[postgres]"
$env:JOB_ATLAS_DATABASE_URL='postgresql+psycopg2://USER:PASSWORD@HOST:5432/DATABASE'
job-atlas init
```

The initializer reports the database adapter without printing the password.

Copy and edit the neutral example profile, then preview it before anything
runs:

```bash
job-atlas plan --config examples/search-profile.example.yaml
job-atlas accept-profile --config examples/search-profile.example.yaml --accept
job-atlas start-run --profile example-software-engineering --confirm
```

The start command prints a run ID. Collect or resume only its frozen sources:

```bash
job-atlas collect --run-id <run-id>
job-atlas status --run-id <run-id>
```

The guided agent uses the same private database for Phase A, selection, resume
reuse, profile links, and local Excel output. Reproducible command seams are:

```bash
job-atlas phase-a-context --run-id <run-id>
job-atlas apply-phase-a --run-id <run-id> --input phase-a.json
job-atlas selection-preview --run-id <run-id> --config filters.yaml
job-atlas freeze-selection --run-id <run-id> --mode all_eligible \
  --phase-b-research --confirm
job-atlas phase-b-plan --scope-id <research-scope-id> --source glassdoor
job-atlas authorize-phase-b --scope-id <research-scope-id> \
  --source glassdoor --maximum-calls <approved-ceiling> --confirm
job-atlas run-glassdoor-phase-b --run-id <run-id> \
  --authorization-id <authorization-id> \
  --reviewed-artifact reviewed-glassdoor-resolutions.json --confirm
job-atlas freeze-selection --run-id <run-id> --mode filtered \
  --config filters.yaml --confirm
job-atlas tailoring-context --scope-id <scope-id>
job-atlas export --run-id <run-id> --scope-id <scope-id> \
  --out output/run.xlsx
job-atlas apply-workbook-shortlist --run-id <run-id> \
  --scope-id <scope-id> --input output/run.xlsx --confirm
```

Keep the `context_fingerprint` returned by `phase-a-context` in the Phase A
result file. `apply-phase-a` rejects stale context instead of applying agent
results to changed job/company evidence.

`freeze-selection` is the confirmation boundary for the exact shortlist. Use
`--phase-b-research` only for the all-eligible, immutable Phase B research
denominator; it does not confirm the shortlist and cannot be used for resume
tailoring, profile discovery, or export.
It requires stored Phase A evidence for every selected posting version and a
completed Phase A identity/function classification for every selected company.
If either is incomplete, finish Phase A and retry; no partial shortlist is
silently confirmed.

The Glassdoor Phase B command expects an agent-reviewed identity decision for
every company in the authorized plan. The agent uses its built-in web search,
tries at most the full company name and one simpler core-brand query, and
records either a verified Glassdoor Overview employer ID or `unresolved`. The
command groups aliases that share an employer ID, reserves the resulting paid
calls, submits one Bright Data snapshot, and saves its snapshot ID before
polling. If the process stops, the same command resumes that snapshot instead
of paying for a replacement. A SERP API is not part of this workflow.

Schema upgrades are backup-first and additive. Existing supported public
SQLite versions migrate automatically; unknown or structurally unsafe drift
still stops rather than resetting data.

Indeed is an attended source. Its preview says `attended_browser` and
`desktop session`; add `--allow-attended` only in a visible desktop session.

## Dashboard

The Excel workbook is the portable result of a search. It includes the full
collected list with each job's decision, enriched eligible jobs, enriched
companies, the frozen selection, and any optional profile-link or resume
outputs. The dashboard is an optional local workspace for exploring the same
run in more detail.

The workbook's **Start here** sheet explains its review control. In
**Shortlist**, every frozen job has a `follow_up_decision` dropdown that starts
as `Approved`. Change unwanted jobs to `Declined`, save the file, and give it
back to the coding agent. `apply-workbook-shortlist` validates that all original
rows are still present and creates a new exact scope containing only approved
jobs. Resume tailoring and LinkedIn profile research can then use that returned
scope. The import does not start either optional stage automatically.

Run the dashboard with:

```bash
streamlit run app/dashboard/public_app.py
```

Then open <http://127.0.0.1:8501>. Choose a plain-language search in the
sidebar. Repeated attempts for the same search are represented by their latest
run, so internal run IDs and timestamps do not clutter the selector. Choose
**All searches** to see all unique eligible jobs and companies together and to
download one cumulative Excel workbook. That workbook also contains the full
collected observations and decisions for every included search.

For an individual search, the dashboard has four views:

- **Pipeline** shows source progress, pipeline steps, and relevance outcomes.
  Each step uses a plain-language name, a short explanation, and a readable
  result message instead of internal Phase A/Phase B names or raw data.
- **Job insights** summarizes sources, work modes, extracted skills, and hiring
  companies.
- **Explore jobs & companies** shows the enriched job and company records,
  including experience, education, qualifications, salary evidence, skills,
  company type, industry, description, pain points, and working links.
- **Other outputs** shows the frozen selection, LinkedIn profile links, and
  tailored resume records when those optional stages were run.

The final-selection filter turns on by default after a shortlist is frozen.
The other sidebar controls change only the visible dashboard rows; they never
alter the saved search or frozen selection. Optional Glassdoor, work-life
balance, and company salary filters keep blank values by default because those
fields are unavailable when paid company research is skipped. Use **Refresh
dashboard** while a pipeline is running to retrieve its latest durable state.
Job, application, company, and LinkedIn URLs are clickable in dashboard tables
and Excel workbooks.

## Full guided workflow

Invoke `$full-pipeline` to have the agent translate natural-language intent,
review configuration, and coordinate the optional evidence, selection, resume,
and profile-link stages around the portable CLI. The same CLI accepts files
directly for reproducible advanced use.

```text
$full-pipeline
```

The optional agent-driven modules are:

- `enrich-jobs`
- `enrich-companies`
- `find-profile-links` (links-only full-pipeline discovery)
- `find-contacts` (email-capable, standalone only)
- `tailor-resumes`
- `mock-interview` (adaptive interview practice using an optional role, job, resume, or topic)
- `draft-outreach` (separate single/batch Gmail Draft workflow)

Their Python CLIs are persistence and provider adapters, not substitutes for
the skill workflows.

## Configuration

### Choose a city, anywhere in India, or remote

Keep `countries: [IN]`. Cities and work arrangements are independent choices:

| Search | `cities` | `arrangements` |
|---|---|---|
| Jobs in Pune | `[Pune]` | `[onsite, hybrid]` |
| Jobs in Pune or Mumbai | `[Pune, Mumbai]` | `[onsite, hybrid]` |
| Jobs anywhere in India | `[]` | `[onsite, hybrid]` |
| Remote jobs eligible for India | `[]` | `[remote]` |
| Pune jobs **or** India-eligible remote jobs | `[Pune]` | `[onsite, hybrid, remote]` |
| Any arrangement anywhere in India | `[]` | `[onsite, hybrid, remote]` |

The plan includes each requested city and a separate remote route where needed.
Remote-only boards can contribute to a mixed city-or-remote search. Source-native
location support varies: some boards resolve city IDs, while broad feeds rely
on the shared relevance gate. An unresolved city or blocked source is reported;
listings with unclear location evidence remain reviewable.


| Concern | Current source of truth |
|---|---|
| Effective search/relevance policy | accepted private `SearchProfile` revision |
| Exact source plan | immutable `GuidedRunPlan` attached to the run |
| Runtime and coverage metadata | `app/pipeline/registry.py` |
| Environment and provider settings | private `~/.job-atlas/credentials.env`, with checkout `.env` fallback |
| Resume content | private `~/.job-atlas/resume-master.yaml` (override with `RESUME_MASTER_PATH`) |

Private profiles, results, backups, and local drafts default to
`~/.job-atlas`; override the root with `JOB_ATLAS_HOME`. The earlier
`JOB_SEARCH_AGENT_HOME` name remains a compatibility fallback.
Connector-specific URL syntax, pagination, normalization, and retry behavior
remain connector-owned.

## Architecture

```text
app/
├── collectors/       Source adapters implementing BaseConnector
├── pipeline/         Collection, normalization, gating, progress and upsert
├── decision_runs/    Immutable manifests, approval and durable stage state
├── jobs/             Job screening evidence
├── companies/        Company market evidence
├── contacts/         Legacy provider adapters reused by links-only discovery
├── resume/           Resume schema, tailoring plan and rendering
├── workflows/        Public profiles, plans, scopes, budgets, and local drafts
├── reporting/        Decision and final workbooks
├── dashboard/        Streamlit read models and interface
└── models/           Pydantic and SQLAlchemy schemas
```

Important design properties:

- **Central relevance gate:** connectors collect and normalize; one shared
  policy decides what is stored.
- **Idempotent ingestion:** source identity is `(source, external_job_id)`.
- **Immutable posting versions:** material posting changes create durable
  episodes for reproducible decisions.
- **Recoverable execution:** stages and connector attempts have historical
  progress and resumable manifests.
- **Human approval:** consequential downstream work uses an immutable approved
  scope and provider-call ceiling.
- **Data minimization:** profile discovery stores LinkedIn URLs and minimal
  professional evidence, never snippets or email addresses.

See `CLAUDE.md` for implementation guidance, `CONTEXT.md` for domain language,
and `docs/adr/` for architectural decisions.

## Testing

The offline unit suite uses in-memory SQLite and provider fakes:

```bash
python -m pytest tests/
```

For a change to one connector, run its focused tests and then its live
scorecard only:

```bash
python -m pytest tests/test_<source>_connector.py
python -m scripts.verify_source <source> --max-instances 1
```

Do not run the broad live connector smoke test for a one-source change.

## Adding a source

1. Choose the cheapest verified collection mechanism: API, feed, static HTML,
   headless browser, then attended browser.
2. Implement `BaseConnector.fetch()` and return `NormalizedJob` rows.
3. Register the connector in the appropriate runtime registry.
4. Add fixture-driven tests.
5. Run `python -m scripts.verify_source <source> --max-instances 1`.

Do not assume a source supports search, location, date, sorting, or pagination
parameters until those behaviors have been verified against the live source.

## Current limitations

- Some sources can change or block automated access without notice.
- Source coverage is not universal. The preview reports unsupported
  profession/geography combinations rather than substituting a board.
- Native city names and routes depend on the source. Some boards return broad
  or promoted results even for a city query; the profile gate checks listing
  evidence. Unknown country eligibility remains reviewable.
- The full workflow depends on a compatible coding-agent environment and
  repository skills.
- Paid Phase B/profile discovery needs separately configured provider access
  and explicit workload approval.

## Safety and responsible use

- Respect each source's terms, access controls, and rate limits.
- Do not bypass subscriptions, CAPTCHAs, or account restrictions.
- Keep browser sessions, API keys, resumes, profile evidence,
  and generated outputs out of version control.
- Review resume and profile-link evidence before acting on it.
- The pipeline does not collect contact email. Separately invoked outreach may
  discover or verify addresses and create Gmail Drafts, but never sends them.
