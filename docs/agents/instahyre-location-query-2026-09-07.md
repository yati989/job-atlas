# Instahyre native location-query evidence (2026-09-07)

The anonymous `GET https://www.instahyre.com/api/v1/job_search` endpoint
accepts a city name in `jobLocations`.

With `skills=data scientist` and the connector's ordinary base parameters,
`jobLocations=Pune` returned HTTP 200, 20 listings on the first page, and
`total_count: 389`. The returned locations included `Pune` and mixed-city
listings containing Pune. The response's `meta.next` retained
`jobLocations=Pune`.

The same request with `jobLocations=DefinitelyNotARealIndianCity` returned an
empty `objects` list. This native-query comparison establishes that the API
applies the city parameter instead of silently ignoring it.

Connector contract:

- `location_mode="city"` requires `location` and sends its stripped value as
  `jobLocations`.
- `location_mode="india"` sends no `jobLocations` parameter, so it is an
  India-wide source query rather than a remote-only query.
- Legacy `bangalore` and `remote` modes retain their existing API values.

This is source-request evidence only. The shared planner must supply the
profile's chosen city or India-wide scope; it is intentionally outside this
connector-only correction.

## Focused live verification

`PYTHONPATH=$PWD python -m
scripts.verify_source instahyre --max-instances 1 --max-jobs 20 --spec
'app.collectors.html.instahyre:InstahyreConnector:{"search":"data scientist","location_mode":"city","location":"Pune","max_results":20}'`

The changed Pune instance fetched 20 jobs and made all locations available;
the scorecard recorded 100% India-location evidence and 100% description
completeness. Its verdict was `FAIL: relevance 50.0% < 60%`, with 10 of 20
jobs dropped by location. `verify_source` uses the legacy personal Bengaluru
policy, not the accepted public Pune profile. It is not evidence that Instahyre ignored Pune;
the request URL in the run included `jobLocations=Pune`.
