# LinkedIn location-query evidence — 2026-09-07

## Scope

This evidence covers the anonymous LinkedIn India jobs connector's new
constructor modes only. It does not activate any new registry instances.

## Public-source check

Before changing the connector, the public endpoint returned HTTP 200 for:

```text
https://in.linkedin.com/jobs/search?keywords=data%20scientist&location=Pune&f_TPR=r2592000
```

The connector's existing card parser found 60 cards. The first five listing
locations were Pune Division or Pune District, Maharashtra, India. A control
request with `location=zzzxq-not-a-place` returned one parsed card located in
Port-au-Prince, Haiti; its job ID did not overlap the Pune result set. This
shows that LinkedIn's `location` parameter changes the visible inventory,
though the anonymous surface remains fuzzy and a malformed location does not
guarantee an empty result.

## Constructor contract

`LinkedInConnector(search, location_mode, ..., *, location=None)` supports:

| `location_mode` | Native query |
| --- | --- |
| `bengaluru` | `keywords=<search>&location=Bengaluru` (legacy) |
| `remote_india` | `keywords=remote <search>&location=India` (legacy) |
| `city` | `keywords=<search>&location=<location>`; a nonblank `location` is required |
| `india` | `keywords=<search>&location=India`, without adding `remote` |

The location string is keyword-only so existing positional constructor calls
retain their behavior.

## Verification

Focused regression test: `tests/test_linkedin_connector.py`.

The changed Pune specification was run fetch-only with:

```bash
PYTHONPATH=$PWD python \
  -m scripts.verify_source linkedin --max-instances 1 --max-jobs 100 \
  --spec 'app.collectors.html.linkedin:LinkedInConnector:{"search":"data scientist","location_mode":"city","location":"Pune"}'
```

It fetched 200 listings (the scorecard sampled 100) from the initial page and
continuations through `start=190`, all with HTTP 200. The sample had 100%
India-marked locations and 100% parseable, fresh posting dates. The scorecard
verdict was `FAIL`: the legacy personal policy used by `verify_source` dropped
92/100 rows on location and then did not hydrate descriptions. This is not the
accepted public Pune profile. Public profile-to-storage integration is tested
separately in `tests/test_india_search_locations.py` and documented in ADR-0019.
