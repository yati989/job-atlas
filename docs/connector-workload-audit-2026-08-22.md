# Active connector workload audit — 2026-08-22

Scope: all 20 headless sources in `ACTIVE_CONNECTORS` plus the active headed
Indeed source: 184 configured query instances in total. This is a static
workload/control-flow audit; it deliberately does not launch a broad live
scrape.

## Audit criteria

- Every listing loop has a fixed result/page bound or a finite response.
- Per-card detail hydration cannot combine a large cap with serial requests.
- Per-card hydration cannot exceed 12,000 candidates across a source's full
  search/location matrix, even when individual instances use worker pools.
- Source-wide request pacing has a bounded theoretical start-time budget.
- Network and browser operations use finite request/navigation timeouts.
- Slow failed attempts are not automatically doubled by the runner retry.
- An explicit sync window reaches both native source filters and the exact
  central recency gate.

The executable guard is `python -m scripts.audit_connector_workloads`. Every
pipeline entrypoint runs the same guard before making a network request.

## Recency-proportional depth policy

Only connectors explicitly verified to return newest-first results scale a
single primary cap with the requested native window:

`effective cap = ceil(60-day cap × native query days / 60)`

The native day count is rounded upward before scaling so the board-side query
cannot hide a posting accepted by the exact timestamp gate. Working Nomads
scales its 1,000-row Elasticsearch `size` after making `pub_date desc` the
primary sort. HN Hiring scales its 20-page Algolia `search_by_date` ceiling.
All other active connectors retain their existing static depth; merely having
a posted date or a date filter does not qualify a connector.

## Results

| Source | Instances | Primary bound | Detail strategy | Result |
|---|---:|---|---|---|
| Cutshort | 1 | 1,000-result safety cap; verified role/activity/creation-date filters | Complete API rows | Pass; 15.4% exact-window retention |
| Wellfound | 12 | 1,000 results / advertised pages | Complete SSR payload; shared 2-request coordinator and 30s cooldown circuit | Pass |
| LinkedIn | 12 | 1,000 results/query | Exact-gate-selective details; one source-wide 1.2s listing/detail coordinator | Original depth restored; 429 collision fixed |
| ZipRecruiter | 12 | 5 pages / 5,000 results | Complete listing payload; finite browser timeouts | Pass |
| Himalayas | 6 | 30 pages | Complete API rows | Pass |
| We Work Remotely | 1 | Finite six-search/RSS responses | Bounded detail pool | Pass |
| HN Hiring | 1 | 20-page/60-day baseline, recency-scaled | No detail requests | Pass after page guard |
| AI Jobs | 12 | 2 pages/query | Shared bounded detail pool/cache | Pass |
| BuiltIn | 12 | Advertised pages / 1,000-page safety guard | Shared bounded listing/detail pools and cache | Original depth restored |
| Shine | 6 | 300-result relevance-drift cursor | Complete API rows | Pass |
| Talent.com | 12 | 300-result relevance-drift cursor | Shared 24-request semaphore/cache | Pass |
| Working Nomads | 6 | 1,000-row/60-day baseline, recency-scaled | Complete API rows; primary date sort | Pass |
| DailyRemote | 12 | 300-result relevance-drift cursor | 2 detail workers/instance | Pass |
| Instahyre | 12 | 100 results/query | 4 detail workers/instance | Pass |
| eFinancialCareers | 1 | 300 results × six internal terms | Complete API rows; six search workers | Pass |
| TimesJobs | 12 | 15,000 results/query | Gate-selective, 4-worker detail hydration | Original depth restored; hydration redesigned |
| Naukri | 12 | 1,000 results/query; 100 rows/page | Complete API rows; repeated-page stop | Pass |
| Glassdoor | 12 | 1,000 results/query; 100 rows/page | Complete BFF rows; cursor/repeated-page stops | Pass |
| IIMJobs | 6 | Server `hasMore` exhaustion | 12 detail workers/instance | Original depth restored |
| Foundit | 12 | 5,000 results/query | Role/seniority/recency-selective, 4-worker details | Original depth restored; hydration redesigned |
| Indeed | 12 | 1,000-result relevance-drift ceiling | Four-request detail batches; one connector attempt | Pass, monitored |

## Defects found and corrected

1. TimesJobs had a serial detail loop across its large raw supply.
2. Foundit had the same multiplication pattern at a larger 5,000-result cap.
3. LinkedIn's 1,000-result cap remains retained by explicit user decision and
   visible in the live progress dashboard. Its former independent listing and
   detail clocks could start together and trigger paired HTTP 429s. Both now
   share one source-wide 1.2-second coordinator, and detail selection uses the
   exact sync cutoff instead of the rounded native window. A focused live run
   on 2026-08-22 fell from 379.8s with recurring 429s to 192.1s with no 429s,
   while fetching 965 jobs through the same 1,000-position ceiling.
4. HN Hiring now has a recency-scaled newest-first page ceiling. IIMJobs and
   BuiltIn retain their previous server/advertised-depth behavior.
5. The runner retried a failed connector even when its first attempt had
   already consumed more than five minutes.
6. `gate_and_upsert(cutoff_at=...)` passed the explicit cutoff only to date
   metrics; `filter_relevant()` silently used the configured default.
7. Full-pipeline ingestion did not accept `--since`, while the later decision
   stage did, producing two different windows.
8. The standalone headed-source entrypoint bypassed the shared workload,
   progress, retry, completion-email, and explicit sync-window controls.

By explicit user decision, the audit's temporary static cap reductions were
reverted for every connector without verified newest-first sorting. The
selective/parallel detail changes remain; only HN Hiring and Working Nomads
scale depth with the requested recency window.

The repository default is 60 days, but the current environment resolves
`RECENCY_WINDOW_DAYS` to 15 days. An explicit `--since` now overrides both:
native integer-day filters round upward to avoid false exclusions, while the
central gate enforces the exact timestamp.
