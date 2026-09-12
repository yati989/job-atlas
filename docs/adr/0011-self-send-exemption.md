# Self-addressed run reports are exempt from the never-send rule

Status: accepted

Amends [ADR-0009](0009-gmail-drafts-as-the-send-surface.md), which states
"the system itself never sends" for the outreach pipeline. That rule exists
to stop a cold email — under the candidate's own name, to a real stranger,
at a *guessed* address — going out unreviewed.

The full-pipeline orchestration (issue #82) needs to deliver its run
workbook (contacts found, drafts created, jobs tailored) to the candidate
so it's readable away from the terminal. This is a different act: the
recipient is the candidate themselves, known and fixed, not guessed and not
a third party. It doesn't touch the risk ADR-0009 exists to prevent.

## Decision

`app.outreach.gmail.send_self_report` is permitted to call
`messages().send()` directly, exempt from the drafts-only rule — but only
under a guard designed to make it structurally incapable of becoming a
second outreach-send path:

- It takes **no recipient argument at all**. The only address it can ever
  send to is `app.config.settings.SELF_EMAIL`.
- It is a distinct function, not a mode of `push_draft`/`create_draft` — a
  caller cannot reach the send path through the drafting API by passing an
  unusual argument.

Everything else in ADR-0009 stands unchanged: outreach to contacts and
prospects is drafted only, reviewed and sent by the candidate by hand.

## Consequences

- No new OAuth scope. `gmail.compose` (already required by ADR-0009) covers
  `messages.send`, not just draft creation.
- If a future stage ever needs to email a third party, it must go through
  the drafted-review flow (`push_draft`), never through
  `send_self_report` or a widened version of it. Adding a `to_email`
  parameter to `send_self_report` would defeat the guard and must not be
  done — a genuinely new send target needs a genuinely new, equally
  guarded decision, not a parameter on this one.
