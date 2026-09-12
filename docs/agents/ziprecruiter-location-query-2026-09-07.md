# ZipRecruiter India native location-query evidence (2026-09-07)

ZipRecruiter India's server-rendered search accepts an arbitrary city value in
the `l` query parameter when requested from a fresh anonymous headless Chrome
context using the connector's browser user agent.

`https://www.ziprecruiter.in/jobs/search?q=data%20scientist&l=Pune&page=1&per_page=1000`
rendered 1,000 `li.job-listing` elements. The first five location labels were
Pune locations, including `Pune,Maharashtra,IN,411014` and `Pune District, MH,
IN`. The same query with `l=DefinitelyNotARealIndianCity` rendered zero job
listings. A default browser context initially received Cloudflare's `Just a
moment...` challenge; the connector's explicit user-agent context passed it.

Connector contract:

- `location_mode="city"` requires `location` and sends its stripped value as
  `l`.
- `location_mode="india"` sends `l=India` without the native `remote=full`
  facet, so it is an India-wide query rather than a remote-only query.
- Legacy `bengaluru` and `remote_india` modes remain intact; only
  `remote_india` sends `remote=full`.

## Focused live verification

`PYTHONPATH=$PWD python -m
scripts.verify_source ziprecruiter --max-instances 1 --max-jobs 20 --spec
'app.collectors.browser.zip_recruiter:ZipRecruiterConnector:{"search":"data scientist","location_mode":"city","location":"Pune","max_results":20,"max_pages":1}'`

The changed Pune instance fetched 20 jobs. It had 100% India-location,
description, posting-date, company, and apply-URL completeness, and every
dated job was within the scorecard freshness window. Its verdict was
`FAIL: relevance 0.0% < 60%; location-dropped 20/20 by the gate` because the
scorecard uses the legacy personal Bengaluru policy rather than the accepted
public Pune profile. The one native page still requests `per_page=1000`;
the connector caps normalized output at the requested 20 results.
