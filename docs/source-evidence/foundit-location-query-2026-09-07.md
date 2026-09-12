# Foundit combined role-and-location query evidence

Observed on 2026-09-07 against Foundit's public
`/middleware/jobsearch` API with the existing query-string contract and
`jobFreshness=60`. Foundit does not require a separate location ID or filter:
the native `query` value combines the role and location terms.

| Native query | API total | First-result location evidence | Result |
| --- | ---: | --- | --- |
| `software engineer Pune` | 2 | Both listings: `Pune, India` | City suffix is honored. |
| `software engineer India` | 26 | Hyderabad, Bengaluru, Kolkata, Delhi, Chennai, and India | India suffix is an India-wide pass. |
| `software engineer remote` | 26 | `Remote`, `India, Remote`, and `Bengaluru, India, Remote` | Remote suffix is distinct from the India pass. |
| `software engineer zznotacity` | 0 | None | A nonsense location does not fall back to a broad feed. |

The existing `FounditConnector(search=...)` contract is therefore retained.
The registry can safely create the required strings as `"{term} {city}"`,
`"{term} India"`, and `"{term} remote"`; no connector change is needed.

## Post-change source check

```sh
PYTHONPATH=$PWD python -m \
  scripts.verify_source foundit --max-instances 1 \
  --spec 'app.collectors.html.foundit:FounditConnector:{"search":"software engineer India","max_results":100}'
```

The source fetched 26 India-wide listings. `verify_source` reported a failed
scorecard because its default legacy relevance policy rejected all 26 on the
role axis and did not hydrate descriptions after that gate. It is valid source
query evidence, but cannot measure a software-engineer India profile's final
relevance or description completeness.

## Profile-aware detail hydration

Foundit's listing cards are screened before detail requests. With an accepted
public profile, the connector now uses that profile's `RelevancePolicy` at
this pre-detail seam: cards classified `kept` or `needs_review` are hydrated,
while confirmed role, seniority, location, or hard-reject mismatches are not.
The `needs_review` path is deliberate because a card may omit location or
experience evidence that its detail payload supplies. Without a supplied
policy, the connector keeps its legacy private role/seniority/recency screen.
