# Talent.com native location-query evidence (2026-09-07)

Talent.com's India search accepts a city value in the `l` query parameter.

The anonymous `GET https://in.talent.com/jobs` page with
`k=data scientist&date=60&l=Pune&p=1&showSignInModal=true` returned HTTP 200
and 20 parsed listings. Its first five listings all displayed `Pune, India`.
A query with the same parameters except
`l=DefinitelyNotARealIndianCity` returned 20 mixed-India listings, including
Pune, Bengaluru, Mumbai, and Hyderabad. Talent.com therefore falls back from
an unrecognised city, but applies the recognised `Pune` city value.

Connector contract:

- `location_mode="city"` requires `location` and sends its stripped value as
  `l`.
- `location_mode="india"` sends `l=India`; it has no `workplace=remote`
  facet, so it is an India-wide source query rather than remote-only.
- Legacy `bengaluru` and `remote` modes retain their existing values and the
  remote-only `workplace=remote` facet.

## Focused live verification

`PYTHONPATH=$PWD python -m
scripts.verify_source talentcom --max-instances 1 --max-jobs 20 --spec
'app.collectors.html.talentcom:TalentComConnector:{"search":"data scientist","location_mode":"city","location":"Pune","max_results":20}'`

The changed Pune instance fetched 20 jobs. It had 100% India-location,
description, posting-date, company, and apply-URL completeness, and every
dated job was within the scorecard's freshness window. Its verdict was
`FAIL: relevance 0.0% < 60%; location-dropped 20/20 by the gate` because the
scorecard uses the legacy personal Bengaluru policy rather than the accepted
public Pune profile. The verified request URL included `l=Pune`.
