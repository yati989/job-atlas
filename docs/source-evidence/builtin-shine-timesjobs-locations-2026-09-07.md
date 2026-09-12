# Native location evidence: Built In, Shine, TimesJobs

Read-only probes preceded request changes. Only changed implementations were
run through source scorecards; no old collector was rerun for comparison.

## Built In

`https://builtin.com/jobs?search=software+engineer&city=Pune&country=IND`
returned HTTP 200, Pune cards, and `Builtin.jobBoardInit` criteria containing
`city: Pune`, `state: null`, `country: IND`. Thus a state is not required.
A nonsense-city request also echoed its city but returned broader/remote cards:
criteria echo alone is not proof of strict filtering. The profile gate remains
authoritative. Country-wide uses `/jobs` with `country=IND&allLocations=true`;
remote uses `/jobs/remote`.

Changed-Pune scorecard: 25 data-analyst rows fetched. The legacy scorecard
reported 36% relevance and 14 location drops because it uses the personal
Bengaluru policy, not an accepted Pune profile.

## Shine

`https://www.shine.com/api/v2/search/simple/` with
`q=data analyst jobs in Pune&loc=Pune` returned HTTP 200 and 5,723 matches.
Nonsense city fell back to the same broad 114,014-match inventory as an empty
location. `data analyst remote jobs` also returned the broad inventory;
`work from home` keywords still included onsite listings. The new remote mode
therefore deliberately uses the broad India feed, preserving each listing's
actual arrangement evidence. Native city support uses `loc` and city wording.

## TimesJobs

POST `https://tjapi.timesjobs.com/search/api/v1/search/jobs/list` with
`keyword=data analyst`, `location=Pune`, `page=1`, `size=5` returned HTTP 200,
5,273 matches, and Pune listings. A nonsense city returned zero. An empty
location returned a broad 119,460-match inventory; the shared gate applies
India eligibility. Existing remote mode uses the native `Remote` value.
A nonsense-role control (`keyword=zzzxqvnorole`, `location=Pune`) returned
zero jobs, confirming that the API also applies the role input even though
real-query results are broader than exact title matches.

Source-only `verify_source --max-instances 1 --spec ...` runs also exercised
the changed Shine and TimesJobs Pune constructors. Their legacy scorecards
are not public-profile relevance judgments.

After passing the accepted Pune profile to TimesJobs' detail screen, a new
bounded live check fetched 25 listings: four passed that profile's title and
location policy, and all four had descriptions. The other 21 were rejected
by the public profile. This resolves the hidden personal-policy hydration
restriction; it does not claim exact-title filtering by the source.
