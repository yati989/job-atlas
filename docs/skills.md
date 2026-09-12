# Project skills

Job Atlas includes 17 focused skills. They cover the user journey and
the repository operations needed to maintain its job-board sources. Generic
engineering, writing, and personal-productivity skill packs are intentionally
excluded.

## Job seeker workflows

| Skill | Use it for |
|---|---|
| [`full-pipeline`](../.claude/skills/full-pipeline/SKILL.md) | Plan and run a reviewed search from job collection through screening, optional enrichment and tailoring, profile-link discovery, dashboard storage, and Excel export. |
| [`mock-interview`](../.claude/skills/mock-interview/SKILL.md) | Practise a concept-first interview grounded in a role, job description, resume, or project. |
| [`tailor-resumes`](../.claude/skills/tailor-resumes/SKILL.md) | Produce an ATS-safe resume and an honest match report for selected jobs. |

## Job and company research

| Skill | Use it for |
|---|---|
| [`enrich-jobs`](../.claude/skills/enrich-jobs/SKILL.md) | Extract experience, education, qualifications, and skills from saved job descriptions. |
| [`enrich-companies`](../.claude/skills/enrich-companies/SKILL.md) | Research company identity, facts, pain points, and optional market evidence. |
| [`enrich-company-domains`](../.claude/skills/enrich-company-domains/SKILL.md) | Compatibility name for company-domain requests; it routes to `enrich-companies`. |
| [`test-extraction`](../.claude/skills/test-extraction/SKILL.md) | Check extraction quality on a small sample before trusting a larger run. |
| [`find-profile-links`](../.claude/skills/find-profile-links/SKILL.md) | Find relevant LinkedIn profile links for an approved job selection, without collecting email addresses. |

## Company discovery and outreach

| Skill | Use it for |
|---|---|
| [`find-prospects`](../.claude/skills/find-prospects/SKILL.md) | Find and qualify companies worth approaching. |
| [`find-contacts`](../.claude/skills/find-contacts/SKILL.md) | Find relevant people and possible email addresses for an explicit batch. |
| [`draft-outreach`](../.claude/skills/draft-outreach/SKILL.md) | Prepare reviewable Gmail drafts. It requires an explicit request and never sends messages. |

## Source and pipeline maintenance

| Skill | Use it for |
|---|---|
| [`daily-pipeline`](../.claude/skills/daily-pipeline/SKILL.md) | Run the legacy daily ingest and enrichment workflow with a dated report. |
| [`add-connector`](../.claude/skills/add-connector/SKILL.md) | Scaffold a connector using the repository's established pattern. |
| [`triage-board`](../.claude/skills/triage-board/SKILL.md) | Determine the cheapest reliable collection mechanism for a new board. |
| [`onboard-source`](../.claude/skills/onboard-source/SKILL.md) | Take one source through verified filters, field capture, registration, and acceptance checks. |
| [`verify-connector`](../.claude/skills/verify-connector/SKILL.md) | Verify one connector with a fetch-only smoke test, pipeline run, and idempotency check. |
| [`update-tracking-sheet`](../.claude/skills/update-tracking-sheet/SKILL.md) | Update the authoritative board-tracking data after a source changes. |

## Why there are three hidden agent folders

The folders are adapters for different coding-agent runtimes:

- `.claude/skills/` holds the one canonical copy of every project skill.
- `.agents/skills/` contains symlinks to those skills for Codex-compatible
  discovery. It does not duplicate their contents.
- `.codex/` configures the repository's small correction worker for parallel,
  non-overlapping fixes.

The skill directories stay flat because agent runtimes discover skills by
looking for `<skills-folder>/<skill-name>/SKILL.md`. This catalog supplies the
human-friendly grouping without breaking discovery.
