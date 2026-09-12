# Evidence system for company WLB and role-level total CTC

**Research date:** 2026-08-15
**Target use:** prioritising an India/Bengaluru or India-eligible remote job before contact discovery and outreach

## Executive recommendation

Treat both outputs as **job-company assessments**, not permanent facts about a
company. A Bengaluru data engineer and a US-hours support engineer at the same
company can have different pay bands and working conditions.

- WLB should be a conservative consensus of the narrowest published WLB
  ratings available, qualified by recent role/location review evidence and
  direct JD signals such as shifts or on-call. Do not let an official flexible
  work policy prove good lived WLB.
- Estimated total CTC should preserve components and basis. For prioritisation,
  define recurring annual TC as base + target cash bonus + annualised vesting
  equity. Exclude sign-on, relocation, benefits and unverifiable private-option
  paper value from the comparable number.
- Never turn a base-salary estimate into total CTC by inventing a bonus or stock
  percentage. Market-wide data is context only; it cannot prove what a named
  company will pay for the target JD.
- Both criteria should be tri-state or better (`meets`, `borderline`, `does_not_meet`,
  `unknown`) and confidence-gated. Unknown is not failure, but it must not
  silently satisfy a top-priority rule.
- Do not build bulk scrapers for review/pay sites. Glassdoor and Indeed expressly
  restrict automated agents; AmbitionBox blocked automated retrieval in this
  research environment; Levels.fyi provides documented structured/paid access.

## Definitions and scope

### Assessment key

The durable key should be:

```text
(job_id, company_id, employing_entity, role_family, seniority_band,
 compensation_geography, assessment_version)
```

`employing_entity` matters for staffing-company postings: an agency's reviews
and salaries do not establish the unnamed client's conditions, and client
evidence does not establish the agency's.

For a remote job, `compensation_geography` is the employer's stated pay zone.
“Remote” alone does not make a US salary comparable to an India-based offer.

### Comparable total CTC

Use this normalised measure for the configurable threshold:

```text
recurring_annual_tc = annual_base
                    + target_annual_cash_bonus
                    + annualised_expected_equity_vest
```

Store employer-stated Indian `CTC` separately when it includes PF, gratuity,
insurance, one-time joining bonus or other non-comparable components. Levels.fyi
defines displayed compensation as base, bonus and annualised equity and says it
does not include benefits; its stock figure divides a grant over the vesting
period ([total-comp explanation](https://www.levels.fyi/blog/what-is-total-compensation.html),
[data guide](https://www.levels.fyi/reports/archive/guides/Levels.fyi%20Compensation%20Data%20Guide.pdf)).

## What the candidate sources actually expose

### Source capability matrix

| Source | WLB evidence exposed | Pay evidence exposed | Important limitation |
|---|---|---|---|
| Official employer JD/careers page | Work mode, shifts, on-call, travel and sometimes working-hours policy | Employer-stated range and sometimes its basis/components | Employer-authored, not lived-experience evidence; a posted range may be base only or cover a broad level/geography |
| Glassdoor | 1-5 WLB category rating, overall rating, total review count; individual review date, title and location; title/location controls | India salary pages can show base, additional pay, total-pay range, submissions, experience filter and update date | Company WLB category is normally broad; visible title/location controls do not prove that the displayed category score is recalculated for the filtered subset |
| Indeed | 1-5 WLB category rating, overall rating/count; dated reviews with title/location; title, location, keyword and WLB-topic filters; browse pages list review counts by title/location | India Career Explorer labels its value **average base salary**, with report count/update date; some company/year figures use postings from the prior 36 months | Base salary is not total CTC; filtered review text does not establish a filtered numeric WLB score |
| AmbitionBox | Company-location and company-designation-location pages can expose WLB rating, overall review count/update date, distribution, WFH/hybrid, working-days and flexible-timing percentages | Company-title-location annual range, experience span, count, recent observations, experience breakdown; some pages expose fixed/variable or label an `AmbitionBox Estimate` | “Annual salary” does not consistently prove full recurring TC or equity; some fields are login-gated and access is automation-restricted/unclear |
| Levels.fyi | No WLB signal | Median total comp; P25/P75/P90; company, location, date, level/tag, years of experience; total/base/stock/bonus components and update date | Strongest structured TC source, but coverage is uneven outside large technology employers and not every submission is proof-verified |

### Glassdoor

Glassdoor asks employees for pros/cons and ratings of workplace factors,
including WLB. Its published scale defines 1 as very dissatisfied, 3 as “OK”
and 5 as very satisfied. An older award methodology required at least 20
approved reviews in the current measurement year, which is useful as a
minimum-sample precedent but is **not** a current promise about every company
page ([rating and historical methodology](https://www.glassdoor.com/about/press-release/glassdoor-announces-top-25-companies-worklife-balance-2012/)).

A current company page exposes category ratings plus dated reviews carrying
job title and location, as illustrated by
[MassMutual India](https://www.glassdoor.co.in/Reviews/MassMutual-India-Reviews-E4603076.htm).
Glassdoor also documents company search by workplace-factor ratings
([Company Explorer announcement](https://www.glassdoor.com/about/press-release/glassdoor-launches-advanced-filters-to-find-companies-highly-rated-for-work-life-balance-diversity-amp-inclusion-and-more/)).

Review data is voluntary self-report. Glassdoor says it uses automated and
human moderation, validated accounts and anti-manipulation rules
([trust process](https://www.glassdoor.com/about/trust/),
[review authenticity](https://www.glassdoor.com/about/trust/fighting-fake-reviews/)).
That raises trust relative to an unmoderated anecdote but does not remove
selection, team or recency bias.

Glassdoor's current terms prohibit automated agents from scraping, stripping
or mining the service without written permission
([Terms section 4.3](https://www.glassdoor.com/about/terms/)). A documented
Company API response includes `numberOfRatings`, `overallRating` and
`workLifeBalanceRating`, but requires an assigned partner ID/key and has its
own attribution/use terms
([Company API](https://www.glassdoor.com/developer/companiesApiActions.htm),
[API terms](https://www.glassdoor.com/crs/api/glassdoor-public-api-terms.pdf)).
Do not assume new partner access is available until Glassdoor confirms it.

### Indeed

Indeed company-review pages expose overall and WLB ratings, total review
count, page update date, dated review text, reviewer job title and location,
plus job-title, location, keyword and topic controls
([example company review page](https://www.indeed.com/cmp/Indeed/reviews),
[browse counts by title/location](https://www.indeed.com/cmp/Indeed/browse-reviews)).
The page does not establish that the headline WLB number is recomputed after
a title or location filter; use filtered reviews as qualitative evidence
unless a page explicitly labels a subgroup score.

Indeed's Bengaluru Data Scientist page exposes an **average base salary**,
salary-report count and update date. Its company comparison says the annual
figures are based on job postings from the prior 36 months
([Indeed India salary page](https://in.indeed.com/career/data-scientist/salaries/Bengaluru--Karnataka)).
It is a useful market/base-pay prior, not proof that a target employer's total
CTC clears the threshold.

Indeed's current terms explicitly prohibit bots, scrapers, AI and agentic AI
from accessing or data-mining the site without express written permission,
apart from conditional robots.txt crawling
([Terms, Site Rules](https://www.indeed.com/legal?hl=en_US)). Its documented
APIs cover employer/job submission and labour-market aggregates, not a
general company-review or company-salary retrieval API
([Job Sync API](https://docs.indeed.com/job-sync-api/),
[Hiring Lab API](https://docs.indeed.com/hiring-lab-api/)).

### AmbitionBox

AmbitionBox has the narrowest public India-specific WLB surface found. A
company + role + Bengaluru page can publish an explicit WLB category score,
review count, update date and workplace insights; see
[Cisco Software Engineer in Bengaluru](https://www.ambitionbox.com/reviews/cisco-reviews/software-engineer/bengaluru-location).
Company + location pages expose the same categories and reported work mode,
working days and flexibility, as in
[Schneider Electric Bengaluru](https://www.ambitionbox.com/reviews/schneider-electric-reviews/bengaluru-location).

Its salary pages can expose a company/title/location range, years-of-experience
span, source count, recent observation dates and experience slices, as in
[Accenture Data Scientist Bengaluru](https://www.ambitionbox.com/salaries/accenture-salaries/data-scientist/bengaluru-location).
Some rows are explicitly labelled as AmbitionBox estimates rather than
observations. Treat the published annual range as `basis=unspecified_salary`
unless fixed, variable, bonus and equity composition is shown.

AmbitionBox says content is employee/user contributed, allows anonymous
contributions, applies technological moderation and sometimes human/report
review, but also warns that moderation cannot catch every problem and users
should exercise judgment
([Community Guidelines](https://www.ambitionbox.com/legal/community-guidelines)).
Its robots policy denied direct automated retrieval in this research
environment, and no public review/salary API was identified. Until explicit
permission or a licensed API is obtained, use it only through human-attended
personal research and store facts/paraphrases rather than copied review text.

### Levels.fyi

Levels.fyi is the preferred inferred-total-comp source. Its Bengaluru Data
Scientist page exposes median total compensation, percentile ranges, last
update and rows with company/location/date/level/experience plus base, stock
and bonus
([Bengaluru page](https://www.levels.fyi/t/data-scientist/locations/greater-bengaluru)).
The data guide says submissions are continuously cleaned and normalised,
human-reviewed, and may be either self-entered or supported by an offer letter,
pay statement or tax form
([data guide](https://www.levels.fyi/reports/archive/guides/Levels.fyi%20Compensation%20Data%20Guide.pdf)).

Do not treat every row as verified. Levels.fyi's verified stream requires a
proof document, performs outlier/forgery and identity checks, and slightly
perturbs published values for anonymity
([verified salary policy](https://www.levels.fyi/verified)). Its terms still
describe salary data as approximate and varying in accuracy
([Terms](https://www.levels.fyi/about/terms.html)).

General HTML scraping is restricted by those terms. However, Levels.fyi now
documents attribution-required `.md` routes for LLMs and points to official
API/MCP/CLI access
([llms.txt](https://www.levels.fyi/llms.txt),
[API/MCP/CLI access](https://www.levels.fyi/api-access/)). Use only the
documented structured route for a bounded lookup, with attribution, or obtain
licensed API access; do not crawl the HTML site.

## Recommended WLB rating system

### 1. Select one numeric rating per platform

Use the narrowest explicitly labelled WLB aggregate from each platform:

1. same employing entity + role/designation + location;
2. same employing entity + location;
3. same employing entity + India;
4. company-wide global.

Do not average multiple scopes from the same platform. Do not call a global
score “Bengaluru WLB” merely because individual reviews were filtered to
Bengaluru.

For every selected value, store the explicit scope, displayed review count,
page update date and whether that count is total reviews or confirmed WLB
respondents. Most pages expose the former; do not overstate it as the latter.

### 2. Produce a robust consensus, not a synthetic precise model

When two or more eligible platform scores exist, use their median as
`wlb_consensus_1_to_5`. With one score, show the number but cap confidence at
low unless it is a role-location aggregate with a meaningful sample and recent
corroboration.

Recommended display labels:

| Consensus | Label |
|---:|---|
| 4.2-5.0 | excellent |
| 3.7-4.19 | good |
| 3.2-3.69 | mixed |
| 1.0-3.19 | hectic_risk |
| no defensible numeric evidence | unknown |

These boundaries are a **user policy**, not a platform fact. A good default for
the future priority criterion is `consensus >= 3.7`, medium/high confidence,
and no direct role-level red flag.

### 3. Use recent text and the JD as variance/red-flag evidence

Review up to ten most recent relevant records, prioritising same role family,
location and current employees. Extract only concrete signals:

- long or unpredictable hours;
- night/weekend/holiday work or on-call frequency;
- cross-time-zone schedule;
- deadline or client-project intensity;
- understaffing, manager/team dependence or recurring reorganisation;
- flexibility, leave and boundary-respecting management.

Set `hectic_risk=true` when the JD directly requires an adverse schedule, or
when at least two distinct recent relevant reviews repeat the same adverse
condition. Deduplicate cross-posted wording. Text does not add arbitrary
decimal points to the rating: it adds `hectic_risk` or `high_variance`. A
positive official careers statement can corroborate an explicit policy but
cannot cancel repeated employee evidence.

### 4. WLB confidence

| Confidence | Minimum evidence |
|---|---|
| high | Role+location aggregate with displayed `n >= 20`, plus corroboration from another platform or at least five recent relevant reviews, with no material contradiction |
| medium | Location/India aggregate with `n >= 20` plus one independent corroborating signal; or role+location `n=5-19` plus corroboration |
| low | Only a global aggregate, fewer than five relevant reviews, one platform, or material disagreement |
| unknown | No eligible aggregate and fewer than two concrete recent relevant reviews |

The `n >= 20` rule is a conservative product threshold informed by Glassdoor's
historical methodology, not a claim of statistical representativeness.

### 5. WLB freshness

- Individual review: fresh through 18 months, ageing at 19-36 months, stale
  after 36 months.
- Aggregate: page `updated` date records collection time, not the age
  distribution. Freshness remains `unknown/all_time` unless the platform
  states a measurement window; require recent textual corroboration for
  medium/high confidence.
- Cached assessment: refresh after 180 days, and immediately after a material
  RTO, acquisition, restructuring or layoff signal.

## Recommended total-CTC estimate

### 1. Evidence hierarchy

Use the highest available tier and retain lower tiers as corroboration:

1. **Direct:** exact target JD or employer recruiter material explicitly states
   annual total compensation and components for the applicable India pay zone.
2. **Strong inferred:** Levels.fyi exact employing entity + role family + level
   + India/Bengaluru, prioritising proof-verified and recent observations.
3. **Corroborating inferred:** Glassdoor exact company/title/location total pay,
   with base/additional split; AmbitionBox exact company/designation/location
   annual salary, preserving its unknown component coverage.
4. **Base-only corroboration:** Indeed exact company/title/location base pay or
   an employer range explicitly labelled base.
5. **Market context only:** other-company or role/location aggregates. These
   may help flag implausibility, but can never make the target company pass.

Never substitute a parent, client or staffing agency for the employing entity.
Never transfer a US/global remote band to an India-eligible remote job without
an explicit India pay-zone statement.

### 2. Normalise observations before combining

Every observation needs:

```text
source_url, observed_at, source_published_at, employing_entity,
role_title_raw, role_family, level_raw, seniority_band, years_experience,
location, pay_zone, currency, period, low, midpoint_or_median, high,
base, target_bonus, annualised_equity, other_recurring,
one_time_amount, component_coverage, verification_status, sample_count
```

- Convert to annual INR using a recorded FX rate/date only when the actual
  offer is denominated in another currency for an India worker.
- Exclude sign-on/relocation from recurring TC; retain it separately.
- Treat private-company option values as `uncertain_equity`, not cash-equivalent
  TC, unless a defensible realisable value is available.
- Do not combine `base_only` and `full_tc` observations into one distribution.
- Deduplicate likely copies with matching amount/title/location/date.

### 3. Aggregate only comparable observations

- `n >= 5` exact comparable observations: report median and P25-P75 if the
  source supplies percentiles or the raw licensed data supports them.
- `n = 3-4`: report observed range and median, without invented percentiles.
- `n = 1-2`: report anecdotal range and low confidence.
- Adjacent titles/one adjacent level: report a separate supporting range; do
  not silently pool it with exact matches.

Never emit a single unexplained number. The output should state its basis, for
example: `₹28L-₹36L recurring annual TC; medium confidence; 6 exact comparable
observations; base+target bonus+annualised public equity`.

### 4. Compare to the configurable threshold

Preserve uncertainty rather than forcing yes/no:

| Status | Rule |
|---|---|
| clearly_above | defensible lower bound is greater than the threshold |
| likely_above | central estimate is greater than the threshold, but the range straddles it |
| possible_above | only the upper bound clears the threshold |
| below | defensible upper bound is at or below the threshold |
| unknown | no comparable full-TC evidence |

Recommended automatic priority pass: `clearly_above` or `likely_above` with
medium/high confidence. Keep `possible_above` selectable in the UI, but do not
auto-message it as though ₹30L+ were established.

### 5. Salary confidence and freshness

| Confidence | Minimum evidence |
|---|---|
| high | Active exact JD explicitly states applicable recurring TC/components; inferred estimates do not reach high merely because one platform labels itself high-confidence |
| medium | At least five recent exact comparable full-TC observations, preferably across two sources or including multiple verified records, with matching level and pay zone |
| low | One to four observations, adjacent role/level, one source, incomplete components or ambiguous Indian `annual salary/CTC` |
| unknown | Only market-wide/base-only evidence or no comparable observations |

- Exact active-JD amount: refresh while the posting is active and expire with
  the posting; re-check after 90 days if it remains open.
- Individual compensation observation: fresh through 12 months, ageing at
  13-24 months, stale after 24 months.
- Cached company-role estimate: refresh after 90 days while it controls active
  outreach, otherwise after 180 days.

## Safe operating model

### Bounded research sequence per target JD

1. Parse the stored JD first: employer identity, work mode, location/pay zone,
   seniority, work-schedule signals and any pay wording.
2. Check the official employer careers page and other employer-authored pay or
   work-policy page, subject to its own terms/robots policy.
3. Query a documented Levels.fyi `.md` route or licensed API for the exact
   company/role/location; cite Levels.fyi as required.
4. For Glassdoor, Indeed and AmbitionBox, create a **manual evidence request**
   for the user: the narrow page to visit and the exact small set of values to
   record. Do not automate access without written/licensed permission.
5. Calculate the two assessments only from stored evidence records. Preserve
   `unknown` when evidence is insufficient.
6. Present evidence URLs, basis, confidence, freshness and contradictions
   before any contacts or messages are generated.

Bound each assessment to one target JD and at most ten recent relevant reviews
per platform. Do not retain full review bodies; store a short factual
paraphrase, scope metadata and source URL. Never bypass login, CAPTCHA,
rate-limit or robots controls.

### Failure behaviour

- Ambiguous company/client identity: both WLB and pay become `unknown`.
- Salary basis missing: store the observed range with
  `component_coverage=unknown`; it cannot independently prove total CTC.
- Conflicting WLB sources: keep the median, set `high_variance`, lower
  confidence and require human review before a top-priority pass.
- No reviews for a small company: do not infer good WLB from silence or from a
  remote policy.
- No company-specific pay: show market context separately, but total-CTC status
  remains `unknown`.
- Restricted access: emit a manual evidence request; do not fall back to a
  scraper or treat a search snippet as decisive evidence.

## Suggested persisted output

```text
WLB
  consensus_1_to_5
  label: excellent|good|mixed|hectic_risk|unknown
  criterion_status: meets|does_not_meet|review|unknown
  confidence: high|medium|low|unknown
  hectic_risk, high_variance
  evidence_scope, evidence_count, source_urls
  assessed_at, refresh_after

Salary
  recurring_tc_low_lpa, recurring_tc_mid_lpa, recurring_tc_high_lpa
  component_coverage: full_tc|base_plus_variable|base_only|unspecified
  threshold_lpa, threshold_status
  confidence: high|medium|low|unknown
  exact_observation_count, adjacent_observation_count, source_urls
  assessed_at, refresh_after
```

Keep the evidence rows separately from the derived assessment so that a
threshold change from ₹30L does not trigger new web research: recompute the
status from the stored range. Version the assessment policy so a later change
to WLB cut-offs, TC components or freshness rules is auditable.

## Decisions the product owner still needs to lock

1. Whether `likely_above` is enough for the salary criterion (recommended) or
   only `clearly_above` passes.
2. Whether the WLB pass threshold should default to 3.7 (recommended) or a
   stricter 4.0.
3. Whether one direct adverse JD schedule signal always vetoes WLB, even when
   company aggregates are strong (recommended: yes).
4. Whether restricted-site manual evidence is acceptable, or those platforms
   must remain absent until licensed access exists.
