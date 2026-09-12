# AGENTS.md

Compact overlay for OpenCode sessions. For architecture depth read
`CLAUDE.md` (six modules, ADR layout, connector pattern, data model) and
`CONTEXT.md` (domain terminology). This file only carries what CLAUDE.md
gets wrong, misses, or an agent would otherwise guess wrong.

## Agent-driven modules — invoke the skill, never the script

Modules 2–6 (enrichment, prospect discovery, contact-finding, resume
tailoring, outreach drafting) run on agent reasoning via skills in
`.claude/skills/`; the CLI seams (`app/*/cli.py`,
`app/contacts/brightdata_query.py`) are persistence/billing-only and are
never the entrypoint for the work itself. Invoke the matching skill
(`enrich-jobs`, `enrich-companies`, `find-prospects`, `find-contacts`, `tailor-resumes`,
`draft-outreach`). End-to-end runs: `daily-pipeline` remains the legacy
ingest/enrich path; the public `full-pipeline` guides collection, Phase A,
selection, optional Phase B/tailoring, links-only profile discovery, and local
exports. It never invokes outreach. `draft-outreach` is explicit-only,
Gmail-Draft-only, and never sends.

Repo-specific skill content lives in `.claude/skills/`. `.agents/skills/`
contains only symlink entrypoints to those same skills so Codex-compatible
agents can discover them without maintaining a second copy. Keep both flat:
skill discovery expects each skill at the top level. The logical grouping lives
in `docs/skills.md`.

`.codex/` contains the small project-scoped correction-worker configuration.
It is agent tooling, not product data. Do not put skill content there or copy
skills between these folders.

## Parallel correction workers

Use the project-scoped `correction_worker` subagent for small, independent
correction tasks. When several corrections are supplied while prior work is
still running, spawn one worker per non-overlapping task, up to the available
thread limit, and keep the main agent focused on coordination and review.
Never let parallel workers edit the same files or dependent code paths; queue
overlapping work instead. Give each worker a concrete scope and expected
verification, then review its diff and test result before reporting completion.

## Tests (CLAUDE.md is stale here)

`python -m pytest tests/` — the suite covers contacts profile-gates, triage,
title classification, search plans, outreach pairing and drafts, Gmail push,
prospect deduplication, tailoring, persistence, MX email resolution, and the
active connector set (including fixture-driven Instahyre and Talent.com
tests). All run offline
against in-memory SQLite (no Postgres, no network); `test_render.py`'s
Tectonic-dependent tests auto-skip when Tectonic isn't on PATH.

## Commands (all via `python -m`)

```bash
python -m scripts.init_db                     # create tables (create_all only — see gotchas)
python -m scripts.smoke_test_connectors       # broad all-source smoke; run only when the user explicitly requests it
python -m app.pipeline.run_all                # both tiers (attended; headed tier opens visible browsers)
python -m app.pipeline.run_all --headless-only # unattended-safe
python -m app.pipeline.run_all --headless-only --tier=parallel   # non-browser connectors only
python -m app.pipeline.run_all --headless-only --tier=browser    # browser connectors only (+ --shard=1/3, --index-range=33:36)
python -m scripts.run_headed_sources          # headed tier only; visible desktop required
python -m scripts.run_daily_pipeline          # ingest + job dedup; writes logs/daily_runs/<date>/{pipeline.log,report.md}
python -m scripts.run_full_pipeline ingest|skills-dedup|report|record-stage  # staged full run, state in logs/daily_runs/
python -m scripts.run_company_market_profiles --ambitionbox-only --limit 50 # salary recovery (Levels.fyi parked)
python -m scripts.query_db companies --where "..."     # inspection (also --sql for raw queries)
python -m scripts.verify_source <source>       # per-source acceptance scorecard
python -m scripts.verify_dashboard_queries     # exercise every dashboard query against real Postgres
python -m scripts.tailor_resume --job-id N     # dump requirements / record a tailored render
python -m pytest tests/                        # offline unit suite (see above)
```

## Gotchas

- **No new observable behavior without explicit approval.** Do not change
  retry, cooldown, scheduling, pagination, output, workflow, or user-facing
  behavior merely as an implementation consequence. Present the proposed
  behavior and obtain the user's explicit approval first.
- **No migration tool** — `init_db.py` does `create_all` only and won't
  alter existing columns; a changed column needs a manual `ALTER TABLE` on
  the live Postgres.
- `.env` is auto-loaded by `app/config/settings.py` — don't shell-export
  vars. Indeed is the current attended connector and uses a dedicated local
  Chrome profile; active collection otherwise runs direct-IP.
- Public beta CI is the small offline Windows/macOS/Linux matrix in
  `.github/workflows/tests.yml`; there is no deployment/CD workflow. The repo
  still uses bare `requirements.txt` and plain pytest defaults.
- Browser connectors run serial within the browser tier; the
  `--tier`/`--shard` flags on `run_all` exist for sharding them.
- `tools/` (Tectonic binary), `logs/`, `contact_batches/`,
  `prospect_batches/` are gitignored.
- **Single-source change scope:** test and live-smoke only the source being
  changed (focused unit tests plus `verify_source <source> --max-instances 1`).
  Never run `smoke_test_connectors`, which fetches every connector, for a
  one-source change unless the user explicitly requests broad all-source
  validation. Apply the same source-scoping rule as connectors are changed
  one by one.
- **Low relevance requires a native-query check:** when a source fetches many
  jobs but keeps few, especially with role-heavy or location-heavy drops,
  live-test whether its search/API accepts real role and location parameters
  before accepting the broad-feed yield. When verified, drive it with the
  canonical `SEARCH_TERMS` and supported location filters; compare a real
  query with a nonsense query so silently ignored parameters are not trusted.
- **Activation decisions belong to the user:** never activate, deactivate,
  comment out, or remove a source based only on yield, quality, timing, or an
  agent recommendation. Present the evidence and wait for the user's explicit
  decision for that specific source before changing its active status.
- **Never rerun an old collector for comparison.** For every one-by-one source
  migration, use the latest completed prior pipeline logs/report as the old
  baseline (normally yesterday's run), and run only the new implementation
  live. Compare current results to that recorded baseline; do not launch the
  superseded browser/transport code again.
- Broad connector-suite changes, only when explicitly requested, may use:
  `smoke_test_connectors` → full `run_all` → a second `run_all` for
  idempotency → per-source scorecards.

## Layout pointers

- `app/reporting/` — full-pipeline workbook builder (not in CLAUDE.md's map).
- `scripts/` — CLI entrypoints; agent-driven modules' seams included.
- Connector registration and public capability metadata live in
  `app/pipeline/registry.py`; `scripts/run_headed_sources.py` owns its attended
  legacy entrypoint. Compatibility re-exports must not duplicate inventories.
- `docs/adr/` — every design decision's rationale; `docs/agents/` —
  repo-specific procedures such as low-yield debugging, source-query findings,
  deployment verification, and parallel contact batches.
- `docs/skills.md` — the maintained catalog of project skills and the purpose
  of `.claude/`, `.agents/`, and `.codex/`.

## Scraper development

For scraper work whose correctness depends on live website behavior, read
`CLAUDE.md` and inspect the live source before changing selectors, filters,
sorting, pagination, loading, or request semantics. Record durable source facts
in the connector or the relevant repository documentation, run focused offline
tests, and use `python -m scripts.verify_source <source> --max-instances 1` for
the changed source. Preserve the single-source and activation rules above.
