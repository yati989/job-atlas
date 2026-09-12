# 0004: Relevance-drift search model, superseding ADR-0003

## Status

Accepted (2026-07-25) — supersedes ADR-0003.

## Context

ADR-0003 committed to a **combined OR-query** structure: every
`SEARCH_TERMS` title OR-combined into one query string, run twice per
source (Bangalore-scoped + remote-scoped), replacing one-query-per-term
searching. Live implementation during issue #55 killed that premise before
it reached a second connector — the combined query is **per-site
unreliable and often lossy**, not just occasionally awkward:

- **TalentCom**: the quoted `"a" OR "b"` syntax was not interpreted as
  boolean — `OR` matched the literal English word, returning junk. Only a
  bare space-joined term list worked.
- **BuiltIn**: rejected combined queries entirely — its search box appears
  to require every token to match (AND semantics), so a multi-concept
  combined string matched nothing (0 results) where the same terms
  individually returned real results.
- **Naukri**: a combined slug's total-count pool *shrank* 4–5× versus a
  single term — the site's own matching narrows on repeated/OR'd tokens,
  so combining **loses** reach rather than gaining efficiency.

Per-site boolean-query support turned out to be too inconsistent to build
a single combined-query pattern on top of, and the failure modes weren't
uniform enough to special-case away. The 700-result-per-query depth cap
ADR-0003 introduced (for the "one query now covers what 13 used to"
premise) also stopped making sense once the premise itself was dropped.

Separately, the epic's user (grilled 2026-07-25, see
`tell-me-what-s-the-noble-eclipse.md` in the planning history) also
tightened two other parameters while redesigning: the recency window
(180 → 60 days) and the search-term vocabulary itself (13 terms → a leaner
6, with the dropped 7 kept in the *gate*'s broader vocabulary so ride-along
titles are still accepted when surfaced, just not searched for).

## Decision

**Abandon the combined-OR-query structure.** Go back to one query per
search term (not one combined query), but with three changes that make
per-term searching cheap enough to run broadly instead of narrowly:

**1. Six search terms × two location passes (12 queries per searchable
source, not 13 × N).** `SEARCH_TERMS` (`app/config/categories.py`) is cut
to `data scientist, data analyst, data engineer, machine learning
engineer, ai engineer, credit risk`. The two location passes are
Bangalore (onsite/hybrid) and India-eligible remote — `app/collectors/
search_query.py`'s `LOCATION_MODE_BANGALORE`/`LOCATION_MODE_REMOTE`
constants name the two canonical passes conceptually; each connector's own
migration ticket maps its existing ad-hoc mode spellings onto whichever of
the two the site actually supports, or drops a third broad/`None` pass
where it isn't one of the two. Where a site can't implement a location
filter server-side at all, the standing rule is stop-and-flag for manual
sign-off rather than guessing. `combined_search_query()`/
`combined_search_terms()` in `search_query.py` are now dead code for this
purpose (kept, unused) — nothing calls them for query construction anymore
since no connector builds a combined string.

**2. Recency window tightened to 60 days**, applied on both axes:
`RECENCY_WINDOW_DAYS = 60` in the central gate (`app/pipeline/
relevance.py`) and each connector's own fetch-side date filter
(`date_filter_days`/`freshness_days`/equivalent constructor param).

**3. Stream-oriented relevance-drift stopping, replacing the fixed
700-result cap for native-search connectors.** Since per-term searching
comes back, the old one-query-per-term justification for staying narrow
(no central gate to backstop precision) still doesn't apply — ADR-0002's
gate remains the actual precision backstop, so a query can still fetch
deep rather than narrow. But depth is now driven by the query's own
result *ordering* signal instead of a flat cap:

- `app/config/categories.py` adds `TERM_TO_FAMILY` (maps each of the 6
  search terms to the `CATEGORY_KEYWORDS` family it owns) and
  `is_on_slice(title, search_term)` — true if `title` matches any keyword
  in the searched term's owning family. A dual-role title is on-slice for
  every family it touches; there is no single-best-family tiebreak
  (locked decision 7).
- `app/collectors/depth.py` adds `RelevanceDriftCursor` /
  `relevance_drift_cursor()`, separate from the existing tier1/2/3 depth
  cursors. A connector feeds each fetched job's on-slice bool via
  `cursor.record_job(is_on_slice(title, term))` in fetch order. The cursor
  arms after the first full rolling window of 10 jobs (`DRIFT_WINDOW_SIZE`,
  no extra floor — decision 9), then stops once fewer than 5 of the
  trailing 10 (`DRIFT_ON_SLICE_THRESHOLD = 0.5`) are on-slice (decision 8)
  — a proxy for "this query's relevant supply is exhausted," exploiting
  relevance-ranked ordering where the board provides it.
- A hard ceiling of 300 results per query (`NATIVE_SEARCH_MAX_RESULTS`)
  always applies too — backstop for deep uniform pools, and the primary
  cap for boards that can't relevance-sort at all (fixed-cap fallback,
  same as before drift was added).
- Before wiring a connector onto the drift cursor, its board must be
  live-verified to relevance-sort (or a real query's on-slice share
  checked to empirically stay high across several pages even with no
  confirmed sort mechanism — e.g. TalentCom, WorkingNomads). Boards that
  don't relevance-sort (confirmed by a collapsing on-slice share at/before
  the first arm point) stay on the fixed 300 cap instead — verify live,
  never assume from a site's marketing copy about "relevance" sorting.

**This applies only to native-search connectors.** Broad/no-search
connectors (RSS feeds, forum threads, tag/category APIs with no per-term
query — Remotive, Himalayas, RemoteOK, WeWorkRemotely, HNHiring,
WorkAtAStartup, AIJobsNet, NoDesk, JustRemote, Remote100K, Internshala,
OuterJoin, Wayoh, DynamiteJobs, Crossover, DailyRemote, Wellfound,
RemoteCo, DataJobs, SkipTheDrive) keep their existing fetch-all behavior
and are never put on the drift cursor or the 300/query cap — a broad
connector's single pass alone must cover what 12 narrow queries do for a
searchable source, so it keeps its own separate, larger `max_results`/
`max_pages` ceiling (`MAX_RESULTS_PER_QUERY = 700` in `depth.py`, or a
tier1/2/3 cursor using that same ceiling), audited and raised where a
connector's own cap had drifted below its live-verified real supply
(issue #65). The native-search 300 budget and the broad-connector 700
budget are two independent constants in `depth.py`, never repurposed into
each other.

## Consequences

- Every native-search connector needs its own migration pass (tickets
  #60–#64, all part of parent epic #47) to move off its old
  one-query-per-`SEARCH_TERMS`-entry-or-combined-query pattern onto 6
  terms × 2 locations + the drift cursor. This was executed sequentially,
  one connector at a time, live-verified before wiring — no big-bang
  migration, per the epic's standing rule.
- Some connectors, once live-re-verified under this model, turned out not
  to be worth keeping active at all: PowerToFly (confirmed not
  relevance-sorted, 9% relevance even on the fixed-cap fallback) and GARP
  (keyword search confirmed dead on re-check — a real term and a nonsense
  term returned byte-identical results) were deactivated rather than
  force-fit onto a model that can't help a source with no real filter.
  This is a direct consequence of the model requiring live-verified
  relevance-sort or on-slice stability before trusting a source's depth —
  sources that fail that check are surfaced, not silently accepted.
- The `CATEGORY_KEYWORDS` partition was **not** refactored into strict
  mutual exclusivity (locked decision 6 originally proposed this); in
  practice `is_on_slice`/`classify_title` operate on the existing
  `CATEGORY_KEYWORDS` structure as-is, since `TERM_TO_FAMILY` only needs
  each of the 6 *search terms* mapped to one owning family, not every
  keyword in the gate vocabulary to be unambiguous. `all_keywords()` (the
  gate's own broader vocabulary) is unchanged by this ADR.
- Depends on ADR-0002's gate remaining the real precision backstop, same
  dependency ADR-0003 already carried: broadening what a query fetches
  only stays safe as long as the gate's role/location axes catch what the
  query's own relevance-sort/drift-stop lets through.
- ADR-0003's two-query combined structure is fully abandoned — no
  connector uses `combined_search_query()`/`combined_search_terms()` for
  actual query construction. The 700-per-query cap ADR-0003 introduced for
  that structure now applies only to broad/board connectors (repurposed,
  not the same meaning as originally written).

## Cross-references

- ADR-0002 (`docs/adr/0002-central-relevance-gate.md`) — the central
  relevance gate this decision, like ADR-0003 before it, assumes is doing
  the precision work that narrow per-term searching used to do.
- ADR-0003 (`docs/adr/0003-two-query-search-structure.md`) — **Superseded
  by this ADR.** Its combined-OR-query premise was live-tested and
  rejected (see Context above) before any connector's query construction
  was migrated onto it.
- Issue #47 (parent epic) — search-coverage redesign, tickets #60–#67.
- Issue #53 — ADR-0003's own ticket (expand-only: added
  `search_query.py` + raised depth caps, no connector migration — that
  scope is now superseded by this ADR's model).
- Issues #55–#58 — original ADR-0003 migration tickets, restructured
  around this ADR's model once the combined-query premise was dropped.
- Issues #60–#64 — this ADR's actual per-connector migration tickets
  (native-search connectors onto 6-term/2-location/drift-cursor wiring).
- Issue #65 — broad/board connector cap audit (decision 10b: separate,
  larger ceilings, never brought down to the native-search budget).
