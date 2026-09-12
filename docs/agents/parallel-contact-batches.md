# Running contact-finding in parallel batches of 50

The standing process for working the contact queue at volume. The unit of
work is a **50-company batch**, split across **5 sub-agents owning 10
companies each**, followed by one summary table reported to the user.

`/find-contacts` (`.claude/skills/find-contacts/SKILL.md`) remains the source
of truth for *how one company is worked*. This file only covers what changes
when five agents do it at once, and nothing here overrides the skill.

## Why partition by company, and never by tier

Every parallelism hazard in this pipeline disappears if — and only if —
**each sub-agent owns a disjoint set of already-profiled company IDs end to
end**: every people search, judging, and the `record_company` write. Domain,
company type, and contact groups are completed beforehand by
`enrich-companies`.

- **The search budget is a read-then-bill check.** `search_source.spent_for()`
  reads `logs/brightdata_calls.jsonl`, then decides whether to spend. Two
  agents working the same `(company_id, search_group, tier)` triple could both
  read "0 spent" and both bill it. Disjoint companies means that triple is
  never contended, so the `BudgetExceeded` guard stays sound.
- **Ledger appends are safe as-is.** Each entry is one short line written to a
  file opened `"a"`, so `O_APPEND` places it at EOF atomically; lines do not
  interleave. No locking needed.
- **DB writes touch disjoint rows.** `upsert_contact` dedups on
  `(company_id, profile_url)` and `record_company` updates one company row.
  Different companies never collide.
- **Evidence files must not collide.** `write_evidence` defaults to a
  second-granularity timestamp, so five agents finishing together can clash.
  Each sub-agent MUST pass an explicit `batch_name`, e.g.
  `write_evidence(results, batch_name="20260804-slice3")`.

Splitting by *tier* instead (one agent does all the `head` searches, another
all the `ic`) breaks all of this and additionally makes the harvest rules
impossible — those depend on one agent seeing everything a company's earlier
queries returned. Do not do it.

## The loop

1. **Take the batch.** `get_batch(session, limit=50)` off the prioritised
   queue, or an explicit ID list when re-working known companies. Explicit
   IDs must also have a completed company contact profile
   (`contact_search_groups IS NOT NULL`); do not make contact workers fill it
   inline.
2. **Slice it into 5 × 10** and dispatch one sub-agent per slice. Give each
   sub-agent its explicit company IDs, its `batch_name`, and an instruction to
   invoke the `find-contacts` skill and follow it exactly. Do not re-state the
   judging rules in the prompt — they live in the skill and its `references/`,
   and a paraphrase will drift from them.
3. **Each sub-agent works its 10 companies to completion**, including
   `record_company(..., strict=True)` and its own evidence file. It reports
   back per-company per-tier counts and anything it had to judge unusually.
4. **The parent aggregates and prints the summary table** (below). The parent
   does *not* re-judge or re-query — the sub-agent already spent the calls and
   made the calls that matter.
5. **Report to the user: the summary table only.** Not per-contact detail
   unless asked; the evidence files carry that, and the user reviews contacts
   in a browser against those.

### Reporting format

Three tables, in this order, every batch. Always built **from the DB**, never
from what the sub-agents reported back — if an agent miscounts or a write
silently fails, the report must show truth rather than intent.

#### Table 1 — Coverage gaps, worst first

**Sorted ascending by total, so the companies that need attention are at the
top.** A descending sort buries them under companies that are already fine and
need no reading at all; the whole point of the table is to find where
something went wrong.

```sql
SELECT c.name AS company, c.company_type AS type,
       count(*) FILTER (WHERE ct.seniority_tier='head') AS head,
       count(*) FILTER (WHERE ct.seniority_tier='hiring_manager') AS hm,
       count(*) FILTER (WHERE ct.seniority_tier='ic') AS ic,
       count(*) FILTER (WHERE ct.seniority_tier='talent_acquisition') AS ta,
       count(*) FILTER (WHERE ct.seniority_tier='exec_fallback') AS exec,
       count(ct.id) AS total,
       c.contact_enrichment_status AS status,
       CASE WHEN c.contact_enrichment_status='done' AND count(ct.id)=0
            THEN 'IMPOSSIBLE' ELSE '' END AS flag
FROM companies c LEFT JOIN contacts ct ON ct.company_id=c.id
WHERE c.id IN (<the 50 ids>)
GROUP BY c.name, c.company_type, c.contact_enrichment_status
ORDER BY total ASC, c.name
```

Rendered with three rules that the raw counts don't carry:

| Company | type | head | hm | ic | ta | exec | Total | Fetched→Kept | Status |
|---|---|---|---|---|---|---|---|---|---|
| **F...** | employer | 0/2 | 0/2 | 0/3 | 0/3 | 0 | **0** | ? → 0 | ⛔ `done` |
| Katbotz India | employer | 0/2 | 0/2 | 0/3 | 0/3 | 0 | **0** | ⚠️ 38 → 0 | partial |
| Metova Federal | staffing | — | — | — | 0/3 | 0 | **0** | 2 → 0 | partial |
| Highbrow Technologies | employer | 0/2 | 0/2 | 1/3 | 0/3 | 0 | **1** | 21 → 1 | partial |
| Ameotech | employer | 0/2 | 0/2 | 0/3 | 1/3 | 1 | **2** | 26 → 2 | partial |
| V3 Staffing | staffing | — | — | — | 3/3 | 0 | **3** | 9 → 3 | done |
| *31 companies at full quota — not listed* | | | | | | | | | |

1. **`filled/quota` cells, not raw counts.** A staffing firm with 3 TA
   contacts is *complete*; an employer with 3 contacts total is badly short.
   Raw counts make those look identical. Quotas are `head` 2, `hiring_manager`
   2, `ic` 3, `talent_acquisition` 3 for employers; `talent_acquisition` 3
   only for staffing (`ladder.QUOTAS` / `STAFFING_QUOTAS`). `exec_fallback` is
   conditional and has no standing quota, so it shows a bare count.
2. **`—` for tiers that don't apply** to that company type, so an empty cell
   never reads as a miss.
3. **`⛔` for impossible states** — `done` with 0 contacts, or `done` below
   quota. These are bookkeeping bugs, not scarcity. Five such rows exist from
   the pre-agentic July runs (#70 purged their contacts without resetting
   company status); any NEW one is a bug in this batch and must be chased.
4. **`Fetched→Kept`** — profile rows the company's billed calls handed the
   agent to judge, and how many survived, via
   `search_source.profiles_fetched_for(company_id)`. This is the column that
   separates the pipeline's two most confusable outcomes:

   | shape | meaning |
   |---|---|
   | `2 → 0` | genuine scarcity — barely anyone to find |
   | `38 → 0` | ⚠️ filtering failure — plenty found, all discarded |
   | `? → 0` | unknown; searched before 2026-08-04, treat as unverified |

   Flag any row with a high fetched count and near-zero kept. It is the only
   automatic signal for this class, because `partial` cannot express it (see
   below) and `partial` is terminal.

   Use `profile_results`, never the ledger's `results` field: `results` is the
   RAW SERP row count and is near-always 10 (a full Google page), so it says
   nothing about how many candidates were actually seen. Rows are counted with
   duplicates — one person surfacing in both a `head` and an `ic` search counts
   twice — which is correct for "how much did we look at and throw away", but
   is not a headcount of the company.

**Companies at full quota collapse to a count line.** The report should spend
its space where something is wrong.

#### Table 2 — Batch totals

| | |
|---|---|
| Contacts added | N (DB before → after) |
| Companies | X `done` · Y `partial` · Z `no_category_match` |
| Zero-contact companies | N (and whether coverage-confirmed) |
| Bright Data calls | spent / cap · per company |
| Evidence files | `contact_batches/batch-<name>-slice{1..5}.md` |

#### Table 3 — Per-tier quota fill

Straight from `batch_report`: `filled / quota` and a percentage per tier,
across the whole batch. This is the fastest read on whether one tier is
systematically failing (a `head` tier at 8% was the signal that exposed the
2026-08-03 under-spend).

#### Then: flags, then a suggested sample

Flags worth a line each: `UnderSearched` bypassed anywhere (should be zero);
any sub-agent reporting candidates that were structurally dropped but looked
real (a gate bug, not a judgement call — chase it); `BudgetExceeded` hits;
`name_collision_risk` count.

Because review is by sample, **suggest where to look rather than leaving it
random** — errors concentrate, so a targeted 20 beats a random 20:
`exec_fallback` rows (fires only when head+hiring_manager are empty, so it is
the most-stretched tier), any company whose single tier is unusually full
(a spike is what over-acceptance looks like), and the closest judgement calls
each sub-agent self-reports.

## What to carry into the report, beyond counts

Counts alone hide the failure modes this pipeline actually has:

- **A `partial` that is under-searched rather than scarce.** `strict=True`
  raises `UnderSearched` and blocks that write, so it should not reach the
  report — but if a sub-agent ever passes `strict=False`, say so explicitly
  and say why.
- **A gate bug found while judging.** Four were found and fixed on
  2026-08-03/04 by exactly this kind of manual reading. If a sub-agent
  reports "N candidates were structurally dropped but were obviously real",
  that is a code bug to file, not a judgement call to absorb quietly.
- **Zero contacts kept out of many profiles fetched.** `partial` cannot
  distinguish "found 8 of 10" from "found 0 of 10", and it is terminal — so a
  filtering failure freezes permanently as if it were scarcity. The
  `Fetched→Kept` column above is the automatic signal; flag any company with a
  high fetched count and near-zero kept, and treat its `partial` as unverified
  rather than final.

  The motivating case is Binance and Bjak, both recorded `partial` with zero
  contacts on 2026-08-03 and read as scarcity in that batch's report. Their
  ledger entries show 4 and 8 billed calls, every one returning a full page of
  raw Google results — but they predate `profile_results`, so **how many were
  actual candidate profiles is unrecoverable**, and `profiles_fetched_for`
  correctly reports `None` rather than guessing. What makes them suspect is
  not a measured ratio but the combination of company size, a full result page
  on every call, and the fact that they were searched through the pre-fix gate
  whose title-blindness separately wiped out every CrowdStrike profile. That
  ambiguity is exactly why the column exists from 2026-08-04 on.

## Cost, and why the batch size is what it is

A 50-company batch is roughly **250-400 billed Bright Data calls** worst case
(8 per single-group employer, 12 dual-group, 2 staffing) — less in practice,
since the harvest rules retire tiers without querying them. Bright Data is
funded and not the constraint.

The real constraint is **agent token spend**: judging is the LLM-reasoning
step, and it runs once per returned row. Five sub-agents each carry their own
context. Check `/usage` before dispatching a batch if the weekly limit is
close; a batch is not resumable mid-flight in a useful way, though completed
companies are durable in the DB and the ledger, so a re-run skips them
cheaply.

## Related

- `.claude/skills/find-contacts/SKILL.md` — how one company is worked.
- `references/judging-and-evidence.md` — the evidence bar and the documented
  "works here"/"right function" traps. Read before judging anything.
- `docs/adr/0005-*`, `docs/adr/0006-*` — why judgement (not heuristics) makes
  the accept/reject call, and what the structural gate may and may not decide.
