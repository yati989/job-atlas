# Wellfound location-query evidence — 2026-09-07

## Scope

This evidence covers only the Wellfound connector's new city and India-wide
location modes. It does not change role scope or activate registry instances.

## Public route check

Read-only requests against Wellfound returned these outcomes:

| Requested route | Final route | Result evidence |
| --- | --- | --- |
| `/role/l/data-scientist/pune` | unchanged, HTTP 200 | Embedded job records listed `locationNames: ["Pune"]`. |
| `/role/l/data-scientist/india` | unchanged, HTTP 200 | Embedded listings included Indian cities such as Bangalore Urban, Chennai, Gurgaon, Mumbai, and Pune. |
| `/role/l/data-scientist/zzzxq-not-a-city` | `/role/data-scientist`, HTTP 200 | Broad fallback included foreign locations such as Redwood City, Tysons, London, New York, and United States. |

This validates the native `role/l/<role>/<city-slug>` route for Pune and
shows why a city-mode redirect must be rejected rather than treated as a
successful local search. `Bengaluru` and `Bangalore` remain aliases for the
existing verified `bangalore-urban` slug.

## Constructor contract

`WellfoundConnector(search, location_mode, ..., *, location=None)` supports:

| `location_mode` | Native route |
| --- | --- |
| `bengaluru` | `/role/l/<role>/bangalore-urban` (legacy) |
| `remote_india` | `/role/r/<role>` (legacy global remote route) |
| `city` | `/role/l/<role>/<normalized-city-slug>`; a nonblank `location` is required |
| `india` | `/role/l/<role>/india` |

For `city`, a response whose final path differs from the requested city route
raises an error. That prevents Wellfound's known broad-feed fallback from
being stored as a city query.

## Changed-Pune scorecard

The fetch-only command below ran one direct Pune instance:

```bash
PYTHONPATH=$PWD python \
  -m scripts.verify_source wellfound --max-instances 1 --max-jobs 100 \
  --spec 'app.collectors.html.wellfound:WellfoundConnector:{"search":"data scientist","location_mode":"city","location":"Pune"}'
```

It fetched 6 jobs from one HTTP request with the request coordinator closed
and no cooldown. All six had India-marked locations, dates, descriptions,
companies, employment types, and apply URLs. The scorecard verdict was
`FAIL: relevance 33.3% < 60%` (one role and three location gate drops).
That verdict reflects the legacy personal policy used by `verify_source`, not
the accepted public Pune profile. It does not invalidate the native route check.

## Offline verification

`tests/test_wellfound_connector.py` first failed because city mode was
unsupported. It now checks Pune, India-wide, the Bengaluru alias, the required
city value, and the redirect-to-broad-feed safeguard.
