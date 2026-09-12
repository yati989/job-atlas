# IIMJobs native location-query evidence (2026-09-07)

IIMJobs applies native location IDs through the `loc` parameter on
`GET https://gladiator.iimjobs.com/job/search`. A live unfiltered
`query=data scientist&page=0&size=1000` response exposed location objects in
its own result data, including `{id: 7, name: "Pune"}`, `{id: 3, name:
"Bangalore"}`, and `{id: 132, name: "Remote"}`. The endpoint requires a
nonempty `query`: the initial resolver attempt using `query=` returned HTTP
404, so the resolver uses the connector's real search term instead.

The subsequent native `query=data scientist&loc=7` request returned HTTP 200
and 29 fresh Pune-scoped results. This establishes both the live Pune ID and
that the source applies it.

Connector contract:

- The legacy default retains `loc=3,132` (Bangalore plus Remote).
- `location_mode="city"` requires `location`; at fetch time it discovers the
  exact city ID from live, unfiltered result-location objects for the current
  search term, then passes that ID in `loc`.
- `location_mode="india"` omits `loc`, producing an India-wide source query.
- `location_mode="remote"` sends the verified Remote ID `132`.

The source has no separate public location-catalog endpoint: `/locations`,
`/location/search`, `/job/data/location`, and `/job/data/locations` all
returned 404. A city absent from the current search response cannot be given
a native ID safely, so the connector raises a clear error rather than guessing
one.

## Focused live verification

`PYTHONPATH=$PWD python -m
scripts.verify_source iimjobs --max-instances 1 --max-jobs 20 --spec
'app.collectors.html.iimjobs:IIMJobsConnector:{"search":"data scientist","location_mode":"city","location":"Pune"}'`

The corrected Pune instance first called the unfiltered search to resolve ID
`7`, then called the filtered `loc=7` search. It collected 29 jobs and the
scorecard assessed 20: 100% India-location, description, date, company, and
apply-URL completeness, and 100% freshness among dated jobs. Its verdict was
`FAIL: relevance 25.0% < 60%; location-dropped 15/20 by the gate`, reflecting
the legacy personal Bengaluru policy used by `verify_source`, not the
accepted public Pune profile.
