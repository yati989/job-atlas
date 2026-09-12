# EFinancialCareers location-query evidence — 2026-09-07

## Scope

This change replaces the connector's hardcoded Bengaluru query scope with
city, India-wide, and India-remote modes. It preserves the existing aggregate
`search_terms` connector shape: one connector instance resolves and fetches
all of its terms, rather than creating one instance per term.

## Official routes and native API state

Read-only requests to the server-rendered official routes returned HTTP 200:

| Route | Native search scope found in page state |
| --- | --- |
| `/jobs/data-scientist/in-pune` | `locationPrecision=City`, `location=Pune, Maharashtra, India`, `latitude=18.5246`, `longitude=73.87862`, `countryCode2=IN` |
| `/jobs/data-scientist/in-india` | `locationPrecision=Country`, `location=India`, `latitude=20.59368`, `longitude=78.96288`, `countryCode2=IN` |
| `/jobs/remote/data-scientist/in-india` | Same India country scope plus `filters.workArrangementType=REMOTE` |

City resolution fetches that official SEO route during collection, rejects a
redirected route, and reads the source-provided scope from its embedded API
request. Constructor validation performs no network call.

## Constructor contract

`EFinancialCareersConnector(search_terms=None, ..., *, location_mode="bengaluru", location=None)` supports:

| `location_mode` | Native scope |
| --- | --- |
| `bengaluru` | Existing Bengaluru coordinates and city filter (default) |
| `city` | Required city name, resolved from its official SEO route during fetch |
| `india` | Official India country coordinates and filter |
| `remote` | Official India country scope plus `filters.workArrangementType=REMOTE` |

## Changed-Pune scorecard

The fetch-only specification below ran one aggregate connector instance with
one search term:

```bash
PYTHONPATH=$PWD python \
  -m scripts.verify_source efinancialcareers --max-instances 1 --max-jobs 100 \
  --spec 'app.collectors.html.efinancialcareers:EFinancialCareersConnector:{"search_terms":["data scientist"],"location_mode":"city","location":"Pune","max_results_per_term":100}'
```

It requested the official Pune resolver route and then the API with the
resolved Pune coordinates. The connector fetched 8 jobs, all with India-marked
locations, parseable fresh dates, descriptions, companies, salary, employment
type, and apply URLs. The scorecard verdict was `FAIL: relevance 0.0% < 60%`
because `verify_source` uses the legacy personal policy, which dropped four
rows on role and four on location. It does not evaluate the public Pune profile.

## Offline verification

The focused connector test was red before implementation because it accepted
neither `location_mode` nor `location`. It now covers deferred Pune resolution,
the India and remote filters, and city validation without any constructor
network access.
