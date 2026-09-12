---
name: verify-connector
description: Run the standard end-to-end verification loop for a job_agent connector — fetch-only smoke test, full pipeline run, idempotency re-run
---

# Verify Connector

The standard "is this connector actually done" check used throughout
job_agent. Run all three steps — don't stop after step 1.

## Steps

1. **Fetch-only smoke test** (no DB writes):
   `python -m scripts.smoke_test_connectors`
   Confirm the target connector returns a non-trivial job count and the
   fields look sane (title/company/location/URL all populated, no
   obvious garbage).

2. **Full pipeline run** (writes to Postgres):
   `python -m app.pipeline.run_all` (headless/no-login connectors) or
   `python -m scripts.run_headed_sources` (headed/bot-blocked/login-gated
   connectors) — whichever list the connector is registered in.
   Confirm `Upserted=N Errors=0` (or errors are pre-existing/unrelated to
   this connector).

3. **Re-run for idempotency**: run the same pipeline command again.
   Query the DB (`SELECT count(*) FROM jobs WHERE source='<name>'`)
   before and after — confirm no duplicate explosion. A small drift
   (few jobs up or down) between runs is expected and fine, since live
   listings rotate; a large jump (e.g. doubling) means dedup key
   (`source`, `external_job_id`) isn't working and needs fixing before
   this is considered done.

4. Report: fetch count, upsert count, error count, idempotency result.
   If everything passes, hand off to `update-tracking-sheet`.
