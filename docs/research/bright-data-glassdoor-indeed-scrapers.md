# Bright Data Glassdoor and Indeed scraper fit

Research date: 2026-08-18

## Question

Can Bright Data's pre-built Glassdoor and Indeed scrapers replace the fragile
visible-browser collection used by the company-market-profile feature?

The feature needs:

- a reliable company identity;
- a company-level work-life-balance (WLB) signal where that source is meant to
  provide one;
- an India-specific salary for a ranked target role (or a clearly labelled
  fallback role);
- for the narrowed Glassdoor row, only Glassdoor's overall rating and estimated
  salary are required.

## Bottom line

| Source | Verdict | What Bright Data can supply | Important gap |
|---|---|---|---|
| Glassdoor | **Yes for the narrowed Glassdoor row** | The job endpoint exposes `company_id`, `company_name`, `company_url_overview`, `company_rating`, `job_title`, `job_location`, `pay_range_glassdoor_est`, `pay_median_glassdoor`, `pay_range_currency`, and `pay_type`. Its discovery metadata also exposes `keyword`, `location`, and `country`. | This is a salary estimate attached to a job listing, not a scrape of Glassdoor's company salary page. Coverage therefore depends on a relevant job/estimate existing. |
| Glassdoor | **Also technically supports WLB, if ever needed** | The company-overview endpoint contains `ratings_overall` and `ratings_work_life_balance`, plus strong company identifiers and profile metadata. | The fields are nullable. The current project decision does not require Glassdoor WLB. |
| Indeed | **Partial; not a drop-in replacement for the planned Indeed row** | The company endpoint exposes `company_id`, name/URL, `overall rating`, and a `work_happiness` array. The job endpoint exposes location/country, `salary_formatted`, `company_rating`, and `company_link`. | Bright Data's published pre-built endpoints do not expose an explicit Indeed WLB field or a role-level salary-page aggregate. `work_happiness` is a different metric, while `salary_formatted` is compensation on a job posting. |

Accordingly:

1. Use Bright Data's **Glassdoor job-listing endpoint** for the project's final
   Glassdoor contract: overall rating plus Glassdoor-estimated median/range.
2. Do not call `pay_range_Employer_est` a Glassdoor estimate; it is the
   employer-provided range. Prefer `pay_median_glassdoor` and retain its
   currency, pay period, job title, and job location as evidence.
3. Keep the Indeed implementation unresolved unless the product requirement is
   relaxed to “work happiness + currently advertised salary.” The catalog does
   not establish that the pre-built Indeed endpoints can return the required
   WLB score and company salary-page estimate.

Bright Data's public product pages list separate company, job, and review
scrapers, and state that the managed layer performs rendering, proxy rotation,
and CAPTCHA solving. That removes our local visible-browser/CAPTCHA burden, but
does not change the semantic limitations of each endpoint.
([Glassdoor product](https://brightdata.com/products/web-scraper/glassdoor),
[Indeed product](https://brightdata.com/products/web-scraper/indeed))

## Evidence method

The field lists below were retrieved on 2026-08-18 from Bright Data's official,
authenticated metadata endpoint for each public dataset ID. Bright Data
documents that `GET /datasets/{dataset_id}/metadata` returns the available
fields, types, and descriptions. No paid scrape was triggered in this research.
([metadata API documentation](https://docs.brightdata.com/api-reference/marketplace-dataset-api/get-dataset-metadata))

The relevant dataset IDs are exposed in Bright Data's own product examples:

| Product | Dataset ID |
|---|---|
| Glassdoor companies overview | `gd_l7j0bx501ockwldaqf` |
| Glassdoor job listings | `gd_lpfbbndm1xnopbrcr0` |
| Glassdoor company reviews | `gd_l7j1po0921hbu0ri1z` |
| Indeed companies | `gd_l7qekxkv2i7ve6hx1s` |
| Indeed job listings | `gd_l4dx9j9sscpvs7no2` |

Sources: [Glassdoor product examples](https://brightdata.com/products/web-scraper/glassdoor),
[Indeed product examples](https://brightdata.com/products/web-scraper/indeed).

## Glassdoor

### 1. Companies overview

**Kind:** company-level overview and aggregate ratings, not salary records.

**Documented collect input:** an array of objects containing a Glassdoor
company-overview `url`, for example:

```json
[
  {
    "url": "https://www.glassdoor.com/Overview/Working-at-Bright-Data-EI_IE2267280.11,22.htm"
  }
]
```

The exact input is shown on the
[Glassdoor product page](https://brightdata.com/products/web-scraper/glassdoor).
The metadata marks only `url` as required.

**Exact output keys currently exposed by official metadata:**

```text
id
company
ratings_overall
details_size
details_founded
details_type
country_code
company_type
url_jobs
url_overview
url_reviews
benefits_url
details_headquarters
region
details_industry
details_revenue
details_website
interviews_url
photos_url
ratings_career_opportunities
ratings_ceo_approval
ratings_ceo_approval_count
ratings_compensation_benefits
ratings_cutlure_values
diversity_inclusion_score
diversity_inclusion_count
ratings_senior_management
ratings_work_life_balance
ratings_business_outlook
ratings_recommend_to_friend
ratings_rated_ceo
competitors
salaries_url
salaries_count
career_opportunities_distribution
url_faq
interview_difficulty
interviews_count
benefits_count
jobs_count
photos
photos_count
faq
reviews_count
interviews_experience
url
industry
additional_information
stock_symbol
```

Source: authenticated metadata for
[`gd_l7j0bx501ockwldaqf`](https://api.brightdata.com/datasets/gd_l7j0bx501ockwldaqf/metadata),
retrieved using Bright Data's documented metadata API.

This endpoint directly satisfies company identity, overall rating, and WLB.
It does **not** return salary values: `salaries_url` and `salaries_count` are
only a link and count.

Note the schema's typo: the key is `ratings_cutlure_values`, although its
display name may be shown as “ratings_culture_values.” Code must consume the
actual key returned by the API.

### 2. Job listings

**Kind:** individual Glassdoor job listing enriched with employer ratings and
salary estimates.

**Documented collect input:** an array of objects containing a Glassdoor job
listing `url`. The product catalog also offers discovery by job keyword and by
company URL. The official metadata records these discovery keys:

```text
keyword
location
country
```

Bright Data's public page does not enumerate the accepted `country` values, so
`IN`/India and the desired city string should be validated with one trial
before a bulk run.

**Exact output keys currently exposed by official metadata:**

```text
url
company_url_overview
company_name
company_rating
job_title
job_location
job_overview
company_headquarters
company_founded_year
company_industry
company_revenue
company_size
company_type
company_sector
company_website
percentage_that_recommend_company_to_a friend
percentage_that_approve_of_ceo
company_ceo
company_career_opportunities_rating
company_comp_and_benefits_rating
company_culture_and_values_rating
company_senior_management_rating
company_work/life_balance_rating
reviews_by_same_job_pros
reviews_by_same_job_cons
company_benefits_rating
company_benefits_employer_summary
employee_benefit_reviews
job_posting_id
company_id
job_application_link
pay_range_glassdoor_est
pay_median_glassdoor
pay_range_Employer_est
pay_range_employer_est
pay_median_employer
pay_range_currency
pay_type
discovery_input
```

Source: authenticated metadata for
[`gd_lpfbbndm1xnopbrcr0`](https://api.brightdata.com/datasets/gd_lpfbbndm1xnopbrcr0/metadata),
retrieved using Bright Data's documented metadata API.

This is the strongest fit for the current Glassdoor decision. A defensible
saved evidence record should include at least:

```text
company_id
company_name
company_url_overview
company_rating
job_posting_id
job_title
job_location
pay_median_glassdoor
pay_range_glassdoor_est
pay_range_currency
pay_type
url
```

The product page describes the endpoint as a job scraper, and separately says
Glassdoor company pages can provide employee reviews, overall ratings, salary
ranges, and company information. The metadata is more precise: actual salary
numbers live on the job endpoint, while the overview endpoint contains only a
salary URL/count.
([Glassdoor product](https://brightdata.com/products/web-scraper/glassdoor))

### 3. Company reviews

**Kind:** one row per employee review, not an employer aggregate and not a
salary endpoint.

**Documented collect input:** a reviews-page `url` with optional `days`, for
example `{"url": ".../Reviews/...", "days": 10}`.

**Exact output keys currently exposed by official metadata:**

```text
overview_id
review_id
review_url
rating_date
pros
cons
count_helpful
count_unhelpful
employee_job_end_year
employee_length
employee_responses
employee_status
employee_type
flag_covid
flag_featured
flags_business_outlook
flags_ceo_approval
flags_recommend_frend
ratinf_compensation_benefits
rating_culture_values
rating_diversity_inclusion
rating_overall
rating_senior_leadersheep
rating_work_life
summary
company_name
review_advice
url
career_opportunities_rating
employee_location
employee_job_title
advice_to_management
glassdoor_employee_id
days
overview_url
original_url
review_pros
review_cons
rating_compensation_benefits
rating_senior_leadership
rating_career_opportunities
glassdoor_employer_id
```

Source: authenticated metadata for
[`gd_l7j1po0921hbu0ri1z`](https://api.brightdata.com/datasets/gd_l7j1po0921hbu0ri1z/metadata),
retrieved using Bright Data's documented metadata API.

This endpoint could support a separately designed India- or role-specific
review aggregation, because it has employee location/title and `rating_work_life`.
That would be a different feature and is unnecessary for V1. Several exact
field names contain typos, so they must not be silently corrected in parser
code.

## Indeed

### 1. Companies

**Kind:** company profile, aggregate work-happiness signals, and links/counts
for other sections.

**Documented collect input:** an array of objects containing an Indeed company
profile `url`, for example:

```json
[
  {"url": "https://www.indeed.com/cmp/Allstate-Insurance"}
]
```

The catalog additionally advertises variants for a company list, search by
company name, and discovery by industry/location **in the US**. It does not
publish an India-specific company discovery example.

**Exact output keys currently exposed by official metadata:**

```text
name
description
url
work_happiness
jobs_categories
website
industry
company_size
revenue
logo
headquarters
country_code
details
related_companies
benefits
salaries
reviews
company_id
reviews_count
reviews_url
salaries_count
salaries_url
jobs_count
q&a_count
Interviews_count
photos_count
jobs_url
q&a_url
Interviews_url
photos_url
overall rating
```

Source: authenticated metadata for
[`gd_l7qekxkv2i7ve6hx1s`](https://api.brightdata.com/datasets/gd_l7qekxkv2i7ve6hx1s/metadata),
retrieved using Bright Data's documented metadata API.

Important semantic details:

- The exact overall-rating key contains a space: `overall rating`.
- `work_happiness` is an array of Indeed wellbeing factors. Bright Data's own
  sample shows titles such as Happiness, Achievement, Support, Energy,
  Appreciation, Purpose, Compensation, and Learning. It does not show an
  explicit work-life-balance item.
- `salaries` contains only a count and link; `salaries_count` and
  `salaries_url` are also only navigation metadata. This endpoint does not
  provide a role salary value.

Sources: [Indeed product sample](https://brightdata.com/products/web-scraper/indeed)
and the authenticated metadata above.

### 2. Job listings

**Kind:** individual Indeed job posting, not the Indeed company salary page.

**Documented collect input:** an array of objects containing an Indeed job URL.
The catalog also offers keyword/location discovery and discovery by company
URL. The official metadata records these discovery keys:

```text
country
domain
keyword_search
location
```

For India, the discovery request should be constrained to the India domain,
country, and location, then the returned `country`, `location`, and
`company_link` must be checked. Bright Data does not enumerate accepted country
or domain values on the public product page, so a one-company live contract
test is still required.

**Exact output keys currently exposed by official metadata:**

```text
jobid
company_name
date_posted_parsed
job_title
description_text
benefits
qualifications
job_type
location
salary_formatted
company_rating
company_reviews_count
country
date_posted
description
region
company_link
company_website
domain
apply_link
srcname
url
is_expired
discovery_input
job_location
job_description_formatted
logo_url
shift_schedule
```

Source: authenticated metadata for
[`gd_l4dx9j9sscpvs7no2`](https://api.brightdata.com/datasets/gd_l4dx9j9sscpvs7no2/metadata),
retrieved using Bright Data's documented metadata API.

`salary_formatted` is described as a formatted pay range from a job posting.
Bright Data explicitly says salary ranges are collected “when shown.” It is
therefore usable as a sparse advertised-salary proxy, not as a deterministic
company/role salary estimate.
([Indeed Jobs product](https://brightdata.com/products/web-scraper/indeed/job))

Identity is weaker than Glassdoor's job schema because the Indeed job row has
no `company_id`; it has `company_name` and `company_link`. A robust integration
would canonicalize `company_link`, then join it to the Indeed companies
endpoint to obtain `company_id` rather than matching on name alone.

## Salary dataset is not a hidden salary-page scraper

Bright Data markets a separate “Salary Dataset,” but its currently listed
Indeed and Glassdoor products are job-listing datasets. The page says salary
data can contain pay range, median, currency, and pay type; those are the same
job-level concepts exposed by the Glassdoor job schema. The page does not list
a dedicated Glassdoor or Indeed company-salary-page scraper.
([Salary Dataset](https://brightdata.com/products/datasets/salary))

This matters because purchasing/filtering the dataset may improve bulk access
and freshness, but does not by itself change the record grain from job listing
to company-role salary aggregation.

## Request and delivery modes

Bright Data documents three request modes:

- synchronous: one request/response, best for individual URLs;
- asynchronous: trigger a job and retrieve later, for thousands of URLs;
- discovery: find records by keyword/category when URLs are not already known.

Delivery options are API download, webhook, cloud storage (S3, GCS, Azure, or
Snowflake), and streaming. The individual product pages also list Google
Pub/Sub and SFTP. The general scraper API supports JSON, NDJSON, and CSV; the
product delivery pages also advertise JSON Lines and optional gzip compression.
([Scrapers overview](https://docs.brightdata.com/datasets/scrapers/overview),
[Glassdoor delivery details](https://brightdata.com/products/web-scraper/glassdoor),
[Indeed delivery details](https://brightdata.com/products/web-scraper/indeed))

Bright Data's synchronous API accepts `dataset_id`, optional
`custom_output_fields`, `include_errors`, and `format`; the JSON body contains
the input array. This lets the implementation request only the small evidence
contract above instead of every field.
([synchronous API](https://docs.brightdata.com/api-reference/scrapers/synchronous-requests))

The product pages advertise bulk inputs up to 5,000 URLs, scheduled/batch
collection, unlimited concurrency, and payment only for successfully delivered
records. These are vendor capabilities, not a measured latency or coverage SLA
for our 4,000-company set.

## Limitations and acceptance test needed before implementation

1. **No field implies complete coverage.** Many rating and salary fields are
   nullable; neither product page promises that every company or job has them.
2. **India must be verified empirically.** The schemas include country,
   location, currency, and domain controls, but the public pages do not specify
   the exact accepted India values. Test `IN`/India and `indeed.co.in`/the
   currently supported India domain rather than assuming them.
3. **Role and seniority still belong to our code.** Bright Data returns titles;
   it does not implement the project's ranked role fallback or normalize
   seniority/experience. The existing deterministic ranking and seniority
   guard must remain.
4. **Salary grain matters.** Glassdoor's `pay_median_glassdoor` is explicitly a
   Glassdoor estimate attached to a job. Indeed's `salary_formatted` is a job
   posting range. Neither is documented as the aggregate on a company salary
   page.
5. **Company matching must use source identifiers.** Prefer Glassdoor
   `company_id` + overview URL. For Indeed, resolve/canonicalize `company_link`
   through the companies endpoint to get `company_id`.
6. **Exact key names are not clean.** Several fields contain spaces, slashes,
   inconsistent capitalization, and misspellings. A narrow adapter should map
   source keys to our internal model and retain the raw payload for evidence.
7. **Do not run 4,000 companies first.** Before committing to this route, run a
   paid but small contract check (for example, the same three known companies)
   and confirm: correct company identity, India location, INR currency/pay
   period, target-role match, non-null rating, and whether the returned salary
   is Glassdoor-estimated or employer-provided.
