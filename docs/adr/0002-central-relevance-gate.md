# 0002: Central three-axis relevance gate for job ingestion

## Status

Accepted (2026-07-18)

## Context

An export of the ingested jobs table (1,296 rows) revealed a high leak rate
against the candidate's actual target (Bangalore-based, data-science /
analytics / credit-risk, IC-to-manager):

- **530 jobs (41%)** had titles naming no target role — they passed only
  because a keyword appeared somewhere in the *description* (e.g. "Senior
  Software Engineer" whose JD mentions a data-science team).
- **376 onsite jobs** were outside Bangalore (Mumbai, Delhi, Gurgaon, …).
- **265 remote jobs** were pinned to foreign geographies (US/UK/EU/LATAM).

Root cause: the pipeline enforced only **one** filter axis, and weakly. The
runner-level safety net matched keywords across title *and* description, and
there was **no central location or recency filter at all** — those were left to
each connector's own (unreliable, unverified) native search. So every new
connector silently re-introduced the same leaks, and prior fixes were
per-connector whack-a-mole with no measurement to catch regressions.

## Decision

Introduce a single **relevance gate** applied uniformly to every connector's
output in the runner, before storage, enforcing three independent axes. Jobs
that fail are **dropped at ingest** (never stored); the runner logs per-axis
drop counts for visibility.

**Role axis** — require a target-role keyword in the *title* (not description),
plus a negative title exclude-list for obviously-unrelated roles. Target role
families: data science, analytics, credit risk, AI/ML engineering, quant /
decision science, and data engineering / MLOps.

**Location axis** — onsite and hybrid jobs must be in Bangalore; remote jobs are
kept unless they name a specifically foreign-only region.

**Recency axis** — drop jobs older than 6 months; keep jobs with no known date.

**Seniority** — drop internships/trainees and senior-executive titles (Head /
VP / Director / Chief+); keep the IC-through-manager band.

Existing leaked rows are hard-deleted; a standing audit script measures the
leak rate after each run so regressions surface automatically.

## Trade-offs and alternatives

- **Precision over recall, deliberately** — the candidate's complaint was
  false positives (irrelevant jobs shown), so the gate is tuned to exclude.
  *But* for the specific ambiguous case of a remote job with no location signal
  at all (bare "Remote", empty), we chose **recall**: keep it, dropping only
  *explicitly* foreign remote. Rationale: bare-remote is often genuinely global
  and pinning it to "foreign" would be guessing.
- **Title-only role match** rejects genuine roles whose title is vague but
  whose description is on-point. Accepted: description-matching is what produced
  the 41% leak, and a vague title is a weak signal to surface to the candidate.
- **Bangalore-only onsite** (not "India onsite") drops real, senior India roles
  in other metros. Accepted per the candidate's explicit scope; revisit if they
  relocate or widen.
- **Central gate vs. per-connector** — centralizing is the whole point: it's the
  only way a *new* connector can't re-introduce leaks, and the only place a
  single audit can measure the leak rate. The cost is that a connector can no
  longer opt into source-specific relevance logic, which none needed anyway.
- **Drop-at-ingest vs. store-and-flag** — flagging would preserve a full audit
  trail; dropping keeps the table lean and matches the decision to delete
  existing leaks. Visibility is preserved via per-run drop-count logging and the
  standing audit re-checking stored rows for gate bugs.

## Consequences

- Relevance vocabulary is defined in `CONTEXT.md` (relevant job, in-scope role,
  India-eligible, onsite/hybrid, recency window, seniority band, leak).
- The three-axis definition lives in one config + one gate module, reused by
  the runner; the existing per-connector `_india_eligible` marker logic
  (e.g. `app/collectors/api/remotive.py`) is generalised into the shared
  location classifier rather than duplicated.
- The "only 33 sources" symptom (connectors silently yielding zero) is a
  *separate* root cause (connector failures / headed sources not run); this ADR
  adds per-connector yield visibility but defers the actual connector repairs.

## Amendment: agent-enriched LinkedIn workplace evidence (2026-08-29)

LinkedIn's logged-out search cards expose a geographic label but do not expose
the posting's workplace type. For its `remote_india` instances, every row that
passes role and seniority therefore hydrates the complete job
description before the location axis. The full-pipeline agent reads a frozen
JSONL artifact and returns one structured result per description: remote
India/unspecified, remote foreign-only, Bengaluru workplace, onsite outside
Bengaluru, or unclear. No external LLM API is called.

This does not move the location decision out of the central gate. The agent
supplies auditable workplace evidence and normalized fields; the unchanged
central predicate remains the keep/drop authority. Regex phrase matching was
removed after real remote-first wording produced false negatives. The frozen
description hash prevents stale or mismatched agent results from reaching the
gate, and incomplete enrichment coverage blocks ingestion instead of silently
falling back to listing geography.

Description evidence is attempted first. The same frozen handoff may request
the ordinary full job enrichment, allowing experience, education,
qualifications, skills, salary, and mandatory-experience facts to be extracted
from that one description read and persisted during ingest for later reuse.

When the frozen packet permits official-company fallback, rows that remain
`unclear` are grouped by company for one agent-owned official-careers pass
before results are frozen. When it disables that fallback, no company/ATS
search occurs and the description judgment is final. The canonical company
domain is resolved before inspecting its official careers site or first-party
ATS. A broad title/role-family match is sufficient; an exact title or
requisition match is not required. A first-party posting may replace `unclear`
only when that role-family match is high confidence. The result records the canonical official
posting URL, matched title, match confidence, and shortest exact workplace
evidence. Company-wide remote policy, another opening at the same employer,
aggregator text, and search snippets cannot classify the job. Missing,
blocked, weakly matched, or genuinely location-silent/ambiguous official
postings remain `unclear` and are rejected. A high-confidence matching
official posting that supplies a concrete location but no
remote/hybrid/onsite/flexible-work qualifier is treated as non-remote:
Bengaluru/Bangalore maps to `bengaluru_workplace`, and every other concrete
location maps to `onsite_outside_bengaluru`. Explicit remote evidence takes
precedence. The returned result carries the concrete location in
`required_location`.

The official-careers result also carries the domain resolution status and MX
host used to validate the identity-confirmed canonical domain. Ingest saves a
validated domain on the company before relevance filtering, so a useful
company-level resolution is not discarded merely because the associated job
fails the location gate. This narrow write does not complete the remaining
company/contact profile. It is idempotent and preserves a different existing
canonical domain as a reported conflict rather than overwriting it.

For the LinkedIn `remote_india` instance, only a low-confidence `unclear`
judgment whose trimmed, case-folded raw listing location is exactly `India` is
tagged `Remote — India`. All other unclear rows require the official-careers
pass above. If still unresolved, a raw Bengaluru/Bangalore listing remains
eligible as Bengaluru; other unresolved locations fail. Negative judgments fail at every
confidence level: mandatory onsite/hybrid work outside Bengaluru, or an
explicitly foreign-only remote scope. Bengaluru
workplace judgments still pass as Bengaluru without being labeled remote.
This exception is scoped by `query_location_mode`; it does not alter any other
source or the central gate order.

## Amendment: LinkedIn location pass is description-only (2026-08-30)

The official-company fallback described above is retired from the active
LinkedIn location pipeline. After the title-based role and seniority checks,
the agent reads each remaining LinkedIn description once and that judgment is
final. It does not resolve a canonical domain or search an employer careers
site/ATS for location evidence.

Description judgments of `remote_india` and `remote_unspecified` pass as
remote. `bengaluru_workplace` passes as Bengaluru. A low-confidence `unclear`
judgment passes as Remote — India only when the raw listing location is exactly
`India`; an unclear raw Bengaluru/Bangalore listing passes as Bengaluru. All
other unclear rows and all negative location judgments fail. Historical
official-posting evidence remains readable, but new runs cannot enable that
fallback through the full-pipeline command.

## Amendment: recency removed from relevance (2026-08-29)

Posting timestamps remain stored and `--since` may still drive a source's
native collection window, but the central relevance gate no longer rejects a
job because its parsed `posted_at` precedes that timestamp. Every job actually
returned by a connector is judged only on role, seniority, and location.
Existing `drop_recency` telemetry remains present with a value of zero for
dashboard and report compatibility.

## Amendment: public source window remains separate from relevance (2026-09-11)

The public profile defaults to a 14-day source search window; the user may
request any positive whole-day value up to 30. The pipeline passes that window
to sources where supported to focus collection and bound work.

The source window does not become a post-collection relevance gate. If a
source returns a posting older than the requested window, the pipeline retains
it when it passes role, job-type, experience, and location checks and records its actual
date. A posting without a usable date remains eligible with unknown age.

## Amendment: public seniority gate replaced by narrow job-type defaults (2026-09-12)

The guided public workflow no longer rejects an otherwise eligible job because
it is entry-level, individual-contributor, managerial, director, or executive.
Legacy `seniority` values remain readable in saved beta profiles and job data,
but do not control public eligibility.

Internships and part-time jobs are excluded by default using the structured
employment type, declared internship level, or an explicit title phrase. Each
category has a separate profile opt-in and becomes eligible only when the user
explicitly requests it. The reviewed pre-fetch plan records both choices and a
plain-language summary before collection can start.
