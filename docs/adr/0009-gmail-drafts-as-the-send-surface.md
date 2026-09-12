# Gmail Drafts is the review-and-send surface, via a direct API client

Status: accepted

Outreach emails are drafted by the agent but must be read, edited and sent by
a human. Rather than print drafts to a terminal or a file the candidate would
have to copy-paste, the pipeline **pushes each draft into Gmail Drafts** and
lets the real mail client be the review UI — where editing, attaching and
sending already work. The system itself **never sends**; a separate sync job
later observes that a draft left the Drafts folder and records it as sent.

## Considered options

**The claude.ai Gmail MCP connector.** Rejected. Setup is far cheaper (authorize
in connector settings, no Google Cloud project), and it can create drafts
perfectly well. But MCP tools are available to the *agent*, not to Python — so
the second half of the requirement is unbuildable: "mark it sent when I send
it" is a polling job that has to run on a schedule, without a Claude session
open. A mechanism that can do the push but structurally cannot do the sync
would have forced a second mechanism anyway.

**Terminal or file output, copy-paste to send.** Rejected. No place to attach
the tailored PDF, and no way to observe what was actually sent.

**Sending directly.** Never on the table. A cold email under the candidate's
own name, to a *guessed* address (every stored address is `unverified` —
ADR-0005's addendum dropped SMTP verification), is not something to automate.

## Consequences

- Requires a one-time Google Cloud OAuth client with `gmail.compose` and
  `gmail.readonly`, plus a locally cached token. This is real setup cost and a
  hard dependency for the pipeline's push half.
- The draft row stores `gmail_draft_id` and `gmail_thread_id`. Sync checks
  whether the draft still exists; if it is gone and a message on that thread is
  in `SENT`, the row flips to `sent`.
- **Sync pulls the sent body back into the table**, overwriting what was
  drafted. The candidate edits in Gmail, so the edited version is the real
  message — keeping our generated text would make the record a lie, and the
  record is what a future follow-up reads.
- Drafts for contacts flagged `name_collision_risk` were originally **never
  pushed** (25 of 817 stored contacts) — Gmail is a low-friction send
  surface by design, and putting such a message one click from sending
  seemed to contradict a warning the system itself generates. **Amended by
  [ADR-0012](0012-collision-risk-drafts-are-pushed.md)**: these are now
  pushed and flagged like any other draft, not blocked — see that ADR for
  why.
