# Search tactics — domain filter, query shape, budget, exec_fallback

## Filter on the regional subdomain, not the root domain

**Measured in the #75 trial, and the single biggest factor in whether a
search returns anything usable:**

| query | domain filter | usable profiles |
|---|---|---|
| `"Head of Data" OR "VP of Data"` + company | `linkedin.com` | **0** — all job ads |
| `"at <Company>" "Data Scientist" OR …` | `linkedin.com` | **0** — all job ads |
| `"Head of Data Science" "<Company>" India` | **`in.linkedin.com`** | **~6 real profiles** |

The root domain is dominated by `/jobs/` pages: title terms match job
*postings* more strongly than profiles, so the results fill with "N jobs in
X" aggregator pages. The regional subdomain returns actual `/in/` profiles.

**Use `in.linkedin.com/in`.** It biases toward India and restricts results to
the profile path, which is where the user wants contacts. Fall back to the root domain only if a company has no
India presence at all — and expect job-ad noise when you do.

## Query shape: put the company *inside* the quoted title phrase

This is the single biggest recall lever found in the trial. Two shapes, same
tier, same company, wildly different yield:

| shape | example | result |
|---|---|---|
| separate quotes | `"Data Scientist" "Optum"` | job ads; **0 IC profiles** |
| **one phrase** | `"Data Scientist at Optum"` | **3 IC profiles**, quota filled |

Search for the string **as a headline is actually written**:
`"<Title> at <Company>"`. LinkedIn headlines read "Lead Data Scientist @
Optum", "Senior Data Scientist at Optum(UHG)", "Data Scientist - Optum" — so
the phrase form matches people, while two separate quoted terms match job
postings that happen to contain both.

OR together 2-3 seniority variants of the same phrase:

```
"Data Scientist at X" OR "Senior Data Scientist at X" OR "Lead Data Scientist at X"
"Technical Recruiter at X" OR "Talent Acquisition at X"
```

**This matters most for the `ic` and `talent_acquisition` tiers**, which are
otherwise the hardest to fill — their titles are exactly the words job ads
use, so the separate-quotes shape drowns in listings. Measured: Optum went
from 0 to 3 ICs and 0 to 6 TA contacts purely by switching shape; TCS 0 -> 2;
Zensar 0 -> 1. Head-tier searches survive the separate-quotes shape because
titles like "Head of AI" rarely appear in job ads, which is why the problem
hid until the lower tiers were worked.

**Caveat, confirmed later (2026-08):** an identical OR-of-quoted-phrases
query re-run on Capgemini/HCLTech came back empty the first time, then
returned real profiles on an unchanged re-run — search results aren't fully
deterministic. Don't conclude a query shape is broken from one thin result;
re-run before rewriting guidance based on it. The finding above (company
*inside* the phrase, not as a separate quoted term) is still the real,
reproducible effect — it's specifically the "declare a shape broken from a
single noisy sample" mistake to avoid, not the phrase-shape finding itself.

## Drop the quotes and the OR — one plain title per query, every tier

**Confirmed 2026-08-02, first real Bright Data batch.** The quoted-OR shape
badly under-performs, most severely on the leadership tiers:

| company | tier | quoted-OR query | plain-title query |
|---|---|---|---|
| Zensar | head | `"Head of Data Science at Zensar" OR "Head of Data at Zensar" OR ...` → **0** | `Head of Data Science at Zensar site:in.linkedin.com` → **CTO/Head of Data and Analytics + Global Delivery Head for Analytics (600+ team)** |
| Cotiviti | head | same shape → **0** | `Head of Data Science at Cotiviti site:in.linkedin.com` → **2 R&D Directors** |
| Zensar | hiring_manager | `"Data Science Manager at Zensar" OR ...` → **0** | `Data Science Manager at Zensar site:in.linkedin.com` → **Enterprise Analytics Practice Manager** |
| Cotiviti | talent_acquisition | *never run* (missed) | `Technical Recruiter at Cotiviti site:in.linkedin.com` → **3/3 quota in one call** |
| Weave | ic | `"Senior Data Scientist at Weave" OR ...` → **1** | `Machine Learning Engineer at Weave site:in.linkedin.com` → **filled 3/3** |

Real titles vary far more than any quoted list can predict — "Global Delivery
Head for Analytics", "Director - R&D", "Enterprise Analytics Practice
Manager" are all genuine function-owner titles no exact phrase would guess.
An unquoted single-title query lets Google's relevance ranking surface those
variants; quoting suppresses exactly that ranking signal.

Batch outcome: **40 of 46 quota slots filled**, most tiers on a single call.

## Two seeds per tier, and why the second one must be different

Each tier carries exactly two seed titles in `ladder.py`. Query 1 uses
`seeds[0]`, query 2 uses `seeds[1]`, and there is no query 3 — so a second
seed that merely rephrases the first wastes the only top-up call available.
This is why "Head of Data" was dropped (near-duplicate of "Head of Data
Science") in favour of "Director of Data", and why `hiring_manager`'s second
seed is "Analytics Manager" rather than "Machine Learning Manager":
"Analytics Manager" is the broader net that found four real managers at
Cotiviti in one call.

Never spend call 2 on **page 2** of the same query. Page 2 is the
low-relevance tail of a query that already underperformed; a different title
reaches a structurally different set of people. The transport enforces this —
`_bright_data_search` fetches exactly one page.

## The noise cost, and what discriminates

Unquoted queries pull in more off-target hits. A "Weave" search surfaced
**DataWeave**, **Bridgeweave Ltd.**, **Weave Design**, **I-WEAVE SOLUTIONS**
and **Powerweave** — five unrelated companies — plus generic Engineering
Managers not running any data function.

The reliable discriminator is the **structured `Location · Title · Company`
field** Bright Data returns. Every false positive caught in the trial was
caught by it: Supriya Kumari's read `· Bridgeweave Ltd.`, AbhiLash AG's read
`· Veltris`. Where it disagrees with the free-text headline, trust it. Where
it's absent, an explicit "at &lt;Company&gt;" in the title is the next-best
signal — a company name appearing loose in the snippet body is the weakest
and routinely denotes a *past* employer or a client.

## Spend the budget — but the budget is now 2 calls per tier

Up to 2 calls per tier x 4 tiers = **8 per company** (12 for a dual-group
company, which gets extra calls for `head`/`hiring_manager` only; 2 for a
staffing firm). Under-spending is the other half of a thin batch — a first
trial pass that ran 1-2 searches per company produced 1 contact per company,
where a proper 4-search pass on one company produced 12.

But the cap is a **ceiling, not a target**. Stop the moment a tier's quota
fills: `ic` and `talent_acquisition` both hit 100% in the 2026-08-02 batch
largely on one call each, and spending the second call anyway is pure waste.
Every call is billed and logged to `logs/brightdata_calls.jsonl`;
`batch_report(results, started_at=...)` prints spend against the cap.

Two failure modes to avoid, both seen live:
- **Never running a tier at all.** Cotiviti's `talent_acquisition` read 0/3
  in the first pass purely because the query was never issued — that reads
  identically to "no recruiters exist" in the report, but one call later it
  was 3/3.
- **Stopping at one call on a short tier.** Zensar's `head` was declared
  empty after a single failed query shape; the right second call found two
  real function owners.

**This isn't a hypothetical — it happened across a full multi-batch run,**
not just a single company. A 50-company pilot (2026-08-03) started at 3.1
calls/company with 100-120% quota fill on its first two batches, then
drifted down to 2.0 calls/company with quota fill as low as 8% by the fifth
batch — same skill, same companies-are-real-employers profile, no change in
the underlying search quality. The cause was pace, not scarcity: later
batches ran one seed per tier and skipped the seed-2 top-up on tiers that
came back short, and in a couple of cases skipped a tier's query entirely
(Binance got one call total across four tiers; Highbrow Technologies got
one). The resulting `partial` statuses were **indistinguishable in the
report from genuine scarcity** — nothing about the report itself flagged
under-spend as the cause, which is exactly why this needs a habit, not just
a documented risk. **If a batch's calls/company is well under the tier
count × 2, that is a signal to go back and finish the tiers before trusting
any `partial` verdict, not a sign the search naturally got harder.**

## exec_fallback is conditional on two things, not one

Search it only if:

1. `head` *and* `hiring_manager` both came back empty, **and**
2. the company is plausibly small enough that a founder / C-level is a
   reachable contact — **your judgement**, not a stored field
   (`employee_count_range` is populated for only ~6% of companies and in
   inconsistent formats, so it can't carry this).

**Condition 2 is not optional, and the #75 trial is why.** Empty head and
hiring_manager tiers correlate with *search difficulty*, not company size —
so condition 1 alone fires hardest at exactly the giant enterprises where
this tier is useless. In the trial it would have stored the CEO of a
600,000-person IT services firm as a contact: technically compliant with the
rule, worthless in practice, and the precise opposite of what this tier is
for. If both tiers are empty at a large company, that company is `partial`.
Leave it.
