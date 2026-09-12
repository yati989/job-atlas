---
name: update-tracking-sheet
description: Update the authoritative job-boards tracking CSV after building, triaging, or changing the status of a source
---

# Update Tracking Sheet

The authoritative per-board status sheet is
`D:\job_hunt_agent\job_boards_updated.csv`
(columns: `source_name, original_priority, status, connector_file, notes`).
This is the single source of truth across sessions — always keep it
current, don't just mention a status change in chat.

## Steps

1. **Check for an Excel lock before editing.** This file has repeatedly
   hit `EPERM: operation not permitted` when Excel had it open. Run:
   `tasklist //FI "IMAGENAME eq EXCEL.EXE"` (Windows) — if it shows a
   running process, ask the user to close Excel before editing rather
   than forcing it.

2. **Update the row** for the source with one of the established status
   values: `Built`, `Built (flaky)`, `Dead`, `Blocked`, `Blocked (flaky)`,
   `Redundant`, `Not attempted`, `Low relevance`. Fill `connector_file`
   with the relative path, and `notes` with a one-line reason — especially
   for any non-`Built` terminal state, so a future session understands
   *why* without re-investigating (e.g. "Incapsula WAF, same tier as
   Jooble" or "paid subscription paywall, declined bypass").

3. **Never leave a row as "Untriaged"/"Unverified"/"Deferred (unresolved)"
   without a concrete plan** — per this project's working convention, every
   board should resolve to `Built` or a documented terminal decision.

4. After editing, update the memory file
   `reference_job_boards_sheet.md` (in the persistent memory store) if the
   overall counts/summary have materially changed, so future sessions don't
   need to re-read the full CSV to know the current state.
