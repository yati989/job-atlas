# ADR 0014: Durable decision runs and outreach ledger

Decision and approval are durable, immutable artifacts keyed by a run ID.
Membership is based on `posted_at`, not observation time, and downstream work
reads the approved manifest rather than re-querying mutable job state. Posting
versions preserve application and screening history. Outreach delivery attempts
are a separate ledger so posting ambiguity, sends, replies, and late bounces can
be reconciled without rewriting drafted copy. Every prepared contact in an
approved run is pushed to Gmail Drafts immediately; calibration and learned
delivery patterns do not gate draft creation. Gmail remains draft-only except
for self reports.
