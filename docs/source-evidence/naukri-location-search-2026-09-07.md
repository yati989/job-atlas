# Naukri native location-query evidence

Observed 2026-09-07 against Naukri's anonymous
`https://www.naukri.com/jobapi/v3/search` endpoint. Each request used the
connector's existing frontend-compatible headers, `keyword=data scientist`,
`jobAge=30`, `pageNo=1`, and `noOfResults=100`.

| Native `location` parameter | `seoKey` | API result | Interpretation |
| --- | --- | --- | --- |
| `Pune` | `data-scientist-jobs-in-pune` | HTTP 200; `noOfJobs=2123`; first results included Pune | Naukri accepts an arbitrary Indian city string as a native location query. |
| `india` | `data-scientist-jobs-in-india` | HTTP 200; `noOfJobs=11111`; first results included Bengaluru | Naukri accepts India-wide search independently from remote search. |
| `zznotacity` | `data-scientist-jobs-in-zznotacity` | HTTP 200; `noOfJobs=11971`; first results included Bengaluru | An unrecognized city silently falls back to a broad feed; callers must pass a validated city from the accepted search profile. |

The counts are live snapshots and will change. The implementation therefore
supports `location_mode="city", location="Pune"` and
`location_mode="india"`, while retaining the legacy `bengaluru` and `remote`
modes. This evidence does not establish that every possible city spelling is
accepted.

## Post-change source check

The following read-only source verification ran after the connector change:

```sh
PYTHONPATH=$PWD python -m \
  scripts.verify_source naukri --max-instances 1 \
  --spec 'app.collectors.html.naukri:NaukriConnector:{"search":"data scientist","location_mode":"city","location":"Pune","max_results":100}'
```

It received HTTP 200 with the expected native `location=Pune` and
`seoKey=data-scientist-jobs-in-pune` parameters, then fetched 100 rows. The
scorecard's acceptance verdict was `FAIL` at 42.0% relevance because the
current default relevance policy dropped 57 of those 100 rows on its location
axis. This confirms the connector's native city request; it does not validate
the separate profile/planning work required to make a Pune profile accept Pune
listings end to end.
