# Glassdoor native location-query evidence

Observed on 2026-09-07 against Glassdoor India's public search BFF and its
location autocomplete endpoint.

## Native city resolution

Glassdoor's job-search BFF requires numeric location IDs rather than an
arbitrary city string. Its public autocomplete request is:

```text
GET https://www.glassdoor.co.in/findPopularLocationAjax.htm
    ?maxLocationsToReturn=10&term=Pune
```

The first direct autocomplete request returned Cloudflare HTTP 403. A
one-result, discarded BFF request for the existing India country scope set the
same session's short-lived cookies. Retrying autocomplete then returned:

```json
{
  "label": "Pune (India)",
  "countryName": "India",
  "locationId": 2856202,
  "locationType": "C"
}
```

The city connector uses `locationId=2856202`, `locationType=CITY`, and native
URL parameter `IC2856202`. It accepts only an exact requested city whose
resolver result identifies India. Resolver failure raises an error; it never
falls back to an India-wide job query for a requested city.

`location_mode="india"` uses the pre-existing India country ID `115` with no
`remoteWorkType` filter. `location_mode="remote_india"` retains that same
country scope plus Glassdoor's native `remoteWorkType=1` filter. The legacy
`bengaluru` mode is unchanged.

## Post-change source check

The following read-only source check used the new city constructor:

```sh
PYTHONPATH=$PWD python -m \
  scripts.verify_source glassdoor --max-instances 1 \
  --spec 'app.collectors.html.glassdoor:GlassdoorConnector:{"search":"data scientist","location_mode":"city","location":"Pune","max_results":100}'
```

It completed the resolver recovery, sent the Pune-native BFF payload, and
fetched 100 jobs. `verify_source` reported `0.0%` relevance because it applies
the repository's default legacy relevance policy rather than a Pune profile;
94 rows first failed that policy's location axis. That scorecard is evidence
that the source request works, but cannot assess a Pune user's end-to-end
acceptance until it is run with the accepted Pune profile.
