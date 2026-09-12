# Indeed location-query evidence — 2026-09-07

## Scope

This records the connector-only location-mode change. It does not add or
activate registry instances.

## Constructor contract

`IndeedConnector(search, ..., location_mode="bengaluru", *, location=None)`
supports:

| `location_mode` | Native request |
| --- | --- |
| `bengaluru` | `q=<search>&l=Bengaluru` (legacy) |
| `remote_india` | `q=<search>&l=India&sc=0kf:attr(DSQF7);` (legacy) |
| `city` | `q=<search>&l=<location>`; a nonblank keyword-only `location` is required |
| `india` | `q=<search>&l=India`, without the remote filter |

The helper shared by the normal browser path and the retained UI fallback
builds the city value, so both paths use the same native location query.

## Live-source check and blocker

A read-only direct request to:

```text
https://in.indeed.com/jobs?q=data%20scientist&l=Pune&fromage=60
```

returned HTTP 403 with Indeed's `Security Check - Indeed.com` / `Additional
Verification Required` page. No job-card data was exposed in that response.

Indeed's documented verified collection mechanism is an attended Chrome
session. No visible-browser source scorecard was run because desktop
availability and an operator able to clear a challenge were not established;
opening it can wait up to 900 seconds for human interaction. This is an
access blocker, not evidence that `l=Pune` is unsupported.

## Offline verification

`tests/test_indeed_connector.py` contains a regression that asserts the Pune
city and India-wide URLs and confirms the retained UI fallback navigates to
the Pune URL. The test was red before the change because `location` was
unsupported and city mode had no validation, then passed after the change.
