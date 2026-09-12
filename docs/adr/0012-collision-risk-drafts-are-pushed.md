# Collision-risk drafts are pushed and flagged, not blocked

Status: accepted

Amends the collision-risk block introduced alongside ADR-0009: previously,
a contact with `name_collision_risk = True` (the `find-contacts` search
found more than one LinkedIn profile with that exact name at that company)
had its draft written but forced to `status=blocked_collision_risk`, and
`push`/`push_drafts.py` refused it outright — no override.

## Why this changed

The full-pipeline orchestration (issue #82) drafts and pushes for every
contact found in a run, with no per-recipient approval gate — that was a
deliberate, explicit decision (issue #82's grilling, "draft for everyone").
Under that design, a hard block has no path forward: the row sits
permanently unpushed with no step in the pipeline that would ever revisit
it. The candidate would only discover it by separately running
`app.outreach.cli report` and reading the blocked list — a report they
have to think to go check, for a population (26 of 829 contacts) they may
not know is being silently excluded from an otherwise-automatic pipeline.

## Decision

`collision_risk` is stored on every draft (unchanged) but no longer gates
`push_draft`/`push_drafts.py` — a collision-risk draft is created and
pushed exactly like any other. The full-pipeline run workbook (#87)
surfaces `collision_risk` as a column on the Contacts sheet so the
candidate sees it flagged before choosing to send.

`STATUS_BLOCKED_COLLISION_RISK` is kept as a historical status value only
— no row is assigned it going forward. `push_draft` still checks for it
defensively (any pre-ADR-0012 row that still carries it must be
re-drafted via `save_draft` to migrate to `drafted` before it can push),
but this is not expected to fire against fresh data — no rows currently
in the table carry that status.

## Consequences

- The risk ADR-0009's block existed to prevent — a same-named stranger
  receiving the candidate's resume and cold pitch — is now possible. This
  is accepted deliberately (issue #82), not an oversight: the review point
  moved from "before the draft can be pushed" to "before the candidate
  hits send in Gmail," which is where a human is already looking at the
  message regardless.
- `app.outreach.cli report`'s "blocked (collision risk)" section is now a
  "collision-risk (pushed, flagged)" section — informational, not an
  action-needed list.
- The standalone `/draft-outreach` skill's behavior changes too, since the
  block lived in shared persistence code (`app/outreach/drafts.py`,
  `app/outreach/gmail.py`), not in the full-pipeline orchestration layer.
  There is no separate "pipeline mode" vs "manual mode" — one policy,
  documented here.
