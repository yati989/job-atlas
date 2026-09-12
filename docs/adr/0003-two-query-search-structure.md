# 0003: Two-query-per-source search structure (Bangalore + remote), 700-result depth cap

## Status

Superseded (2026-07-25) by
[ADR-0004](0004-relevance-drift-search-model.md). Live implementation
during issue #55 found the combined-OR-query decision below to be
per-site unreliable and often lossy (TalentCom matched literal "OR",
BuiltIn's AND-semantics returned 0 results on a combined query, Naukri's
combined slug shrank its result pool 4–5×) — abandoned before any
connector's actual query construction was migrated onto it. ADR-0004
records the replacement model (6 search terms × 2 location passes,
relevance-drift stopping). Kept below for historical record; do not treat
the Decision section as current.

~~Accepted (2026-07-23)~~

## Context

`app/config/categories.py`'s `SEARCH_TERMS` holds 13 lean, high-precision
terms used to drive native-site searches, one query per term. Several
search-capable connectors also run a location-mode matrix on top of that
(e.g. onsite-Bangalore pass + remote pass), so a source like TalentCom ends
up running 13 x 2 = 26 separate query-instances per pipeline run. Each
instance pays a large fixed cost regardless of how few results it returns —
browser launch, stealth pacing on anti-bot connectors, per-instance
pagination overhead — so the marginal cost of narrow per-term searching is
high relative to what it buys.

That per-term narrowness was originally load-bearing: it kept native search
precise before a central relevance gate existed. It no longer is. ADR-0002
introduced a single central relevance gate (`app/pipeline/relevance.py`)
that every connector's output passes through regardless of how it was
fetched, enforcing role, location, recency, and seniority independently of
what the connector's own query happened to select for. Since the gate
already drops onsite-non-Bangalore and foreign-only-remote at ingest, a
connector's own query no longer needs to pre-narrow by exact role term or by
India-metro-other-than-Bangalore — over-fetching broadly and letting the
gate filter is now strictly fine, and searching India metros other than
Bangalore for onsite roles is pointless work (the gate would drop them
anyway).

## Decision

**Two canonical queries per search-capable source, not 13 x N.** Every
target job title from `SEARCH_TERMS` is OR-combined into a single query
string, run twice per source: once scoped to Bangalore (onsite/hybrid), once
scoped to remote. This replaces the one-query-per-`SEARCH_TERMS`-entry
pattern. The two location passes are named canonically
`LOCATION_MODE_BANGALORE` ("bangalore") and `LOCATION_MODE_REMOTE`
("remote") in the new `app/collectors/search_query.py` module — existing
connectors currently spell these several different ways
("bengaluru"/"bangalore", "remote"/"remote_india", "india" broad/unfiltered,
`None`); migration tickets (#54-#57) normalize each connector onto the two
canonical names, collapsing or dropping any third "broad India" pass per
that connector's own judgment call.

Per-site OR/boolean-query syntax is explicitly **not** decided here —
`app/collectors/search_query.py` provides only the generic combined-term
string (`combined_search_query()`, e.g. `'"data scientist" OR "data
analyst" OR ...'`, with a `quote=False` variant for sites that don't want
quoted phrases); each connector's actual per-site query construction is a
migration-ticket concern.

**Depth cap raised to a uniform 700 results per query.** Since one query now
covers what 13 narrow queries used to, each query needs to reach much
deeper. `app/collectors/depth.py`'s three tiers (verified date-filter /
dates-but-no-sort / no-date-signal) keep their existing shape and
stop-decision logic, but `max_results` is unified to a single
`MAX_RESULTS_PER_QUERY = 700` across all three tiers (was 30-page-ceiling/
uncapped-results for tier 1, 200 for tier 2, 50 for tier 3), with page
ceilings/caps raised alongside so a connector can actually reach 700 results
before its own page cap stops it first. 700-per-query was an explicit
settled decision (not independently re-derived here) — see prior discussion
referenced in the issue #53 spec.

## Consequences

- This ticket (issue #53) only *adds* `app/collectors/search_query.py` and
  raises the depth caps — it does not migrate any connector's actual query
  construction. Every connector keeps running its current one-query-per-term
  pattern until its own migration ticket lands, so this change is
  zero-behavior-change for every existing connector today (confirmed via
  `scripts/smoke_test_connectors.py` showing identical output before/after).
- Once migration tickets land, expected effect is far fewer query-instances
  per run (2 per source instead of up to 26), each fetching deeper (up to
  700 vs. as low as 50) and relying on the ADR-0002 gate — not the query
  itself — to keep precision.
- Depends on ADR-0002's gate remaining the actual precision backstop: if the
  gate's role/location axes were ever weakened, broadening every connector's
  query this much would leak much more than it does today.

## Cross-references

- ADR-0002 (`docs/adr/0002-central-relevance-gate.md`) — the central
  relevance gate this decision assumes is doing the precision work that
  narrow per-term searching used to do.
- Issue #47 (parent spec) — full search-restructuring epic.
- Issue #53 (this ticket) — expand-only: helper + raised caps, no connector
  migration.
- Issues #54-#57 — per-source migration tickets that will actually wire
  connectors onto the two-query structure.
