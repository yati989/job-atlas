---
name: daily-pipeline
description: Run the full daily job_agent pipeline from fetch through gate, upsert, and enrichment, with a dated report under logs/daily_runs/YYYY-MM-DD and source-access failures shown explicitly.
---

# Daily Pipeline

Manually-triggered end-to-end run: ingestion (fetch/gate/upsert) followed
by an enrichment pass, with everything logged to a dated directory so any
day's run can be inspected later without re-deriving it from a raw
terminal scrollback.

Two phases. Phase 1 is fully scripted. Phase 2 is agent reasoning (per
this project's enrichment design — see CLAUDE.md's "Enrichment" section:
no billed external LLM call, the agent extracts fields itself) and cannot
be scripted end-to-end, so you drive it directly.

## Phase 1 — Ingestion (fetch -> gate -> upsert)

1. Before running, capture a baseline for the diff at the end:
   ```
   python -m scripts.query_db --sql "SELECT source, count(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC"
   python -m scripts.query_db --sql "SELECT count(*) FROM jobs"
   ```

2. Run the daily pipeline script. Default runs both tiers (headless +
   headed — attended, opens a visible browser for Indeed). Use
   `--headless-only` if you don't want
   the headed/visible-browser tier this run:
   ```
   python -m scripts.run_daily_pipeline
   python -m scripts.run_daily_pipeline --headless-only
   ```
   This writes:
   - `logs/daily_runs/<date>/pipeline.log` — the full raw log (every
     connector's INFO/WARNING/ERROR output, same detail level as running
     it directly).
   - `logs/daily_runs/<date>/report.md` — a per-source fetched/kept/status
     table, plus a "Challenges detected" section pulled from WARNING/ERROR
     lines matching known patterns (network failures, bot-detection challenge
     page, rate-limiting, retry exhaustion, network-level blocks, the
     known upsert JSON-serialization bug class). This is genuinely useful
     signal, not raw log noise — read it before the raw log.

   This step also runs `scripts/dedup_jobs.py` (cross-source duplicate
   detection — the same posting on two different boards has no shared
   external_job_id, so it flags high-confidence title+company matches via
   `Job.duplicate_of_job_id` rather than deleting anything) and appends its
   counts to the report's "Duplicate detection" section. Medium-confidence
   matches are never auto-flagged — they're written to
   `logs/dedup_runs/<date>/review_candidates.csv` for manual review.

3. Run it in the background if it's going to take a while (the headed
   tier especially — treat "started" and "still running" as normal for a
   long time; don't assume a hang without checking process liveness, see
   CLAUDE.md's Windows gotchas if this is on Windows).

4. Once it completes, read `report.md` for that date. For each
   "Challenges detected" entry, use judgment on whether it's worth a
   deeper look now (a new bug/regression) or is already a known, accepted
   condition (e.g. a source with documented flakiness) — don't treat every
   entry as an action item, some are just visibility into expected
   noise. If something looks like a genuine new regression, you can
   investigate the same way as any other connector issue (live re-test,
   check the connector's own docstring for prior findings) — this skill
   doesn't prescribe that investigation, it just makes sure you see the
   signal instead of it being buried in the raw log.

## Phase 2 — Enrichment (agent reasoning, not scripted)

5. Scope enrichment to jobs this run actually touched, not the whole
   historical backlog — the pipeline updates `last_seen_at` on every
   upsert, so:
   ```sql
   SELECT id, title, description_raw FROM jobs
   WHERE last_seen_at >= '<run start timestamp, from step 2's log>'
     AND enrichment_status = 'pending' AND description_raw IS NOT NULL
   ORDER BY id LIMIT 40;
   ```
   Mark any `description_raw IS NULL` rows in that same cohort as
   `enrichment_status = 'no_description'` first (same as the `enrich-jobs`
   skill) so they don't get re-selected.

6. Run `enrich-jobs` for job-level fields and skills, then run
   `enrich-companies` for every distinct company represented in the same
   cohort. The latter owns company facts/pain points, domain resolution,
   employer/staffing type, and contact search groups. Invoke both skills for
   their mechanics rather than duplicating them here. The only difference
   from standalone runs is the fixed daily cohort scope and that results get
   appended to this run's report.

7. While enriching, note anything that made extraction genuinely hard —
   this is the "enrichment challenges" the report should carry, distinct
   from the fetch-side "Challenges detected" section:
   - Descriptions that were truncated, garbled encoding, or boilerplate-only
     (e.g. the "Tasks:"/"Skills/Tech-stack:" normalized format some
     sources emit, which is easy but different from prose JDs).
   - Titles/postings ambiguous enough that a field was left null rather
     than guessed (per the skill's own "null rather than guess" rule) —
     worth naming the pattern if it recurs (e.g. "many postings state a
     level code like L1-L5 with no plain years-of-experience number").
   - Any source whose postings were disproportionately `no_description`.

8. Append a real "## Enrichment" section to
   `logs/daily_runs/<date>/report.md`, replacing the Phase 1 placeholder,
   with: jobs enriched, jobs marked `no_description`, jobs still pending,
   companies profiled, domain hit rate, employer/staffing and search-group
   splits, and a short "Enrichment challenges" list from step 7.

## Reporting back

Give the user a short summary: total upserted, total errors, any
challenges worth their attention, jobs enriched vs. still pending, and the
path to `logs/daily_runs/<date>/report.md` for the full detail.
