# Reuse contact search with a links-only public-pipeline exit

Status: accepted

The guided public pipeline reuses the established contact search plan,
provider transport and billing ledger, relevance gates, and agent judgement;
it persists only profile links and stops before email resolution. Building a
parallel profile-discovery engine was rejected because it would duplicate
query policy, retries, spending controls, and relevance behavior that already
have production evidence.

The separately invoked `draft-outreach` skill may continue through the
existing contact email resolution and persistence workflow, prepare messages
in the established fixed format, and push them to Gmail Drafts. It is never a
guided-pipeline stage and neither path sends mail automatically. This explicit
standalone capability supersedes issue #101's earlier user-supplied-address-only
drafting restriction while preserving its no-email boundary inside the full
pipeline.
