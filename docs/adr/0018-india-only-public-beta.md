# Keep the public beta India-only

Status: accepted

The public beta accepts exactly `countries: [IN]`. Users may still configure
professions, Indian cities, remote/hybrid/onsite arrangements, experience
bands, and source choices. Profiles containing another country code fail
validation with a clear message before a plan is accepted or collection starts.

This boundary reflects the source routes and location evidence currently
verified by the repository. Accepting arbitrary two-letter codes would imply a
level of source coverage and relevance accuracy the product has not established.
Multi-country support can be added later as an explicit product change, backed
by verified source capabilities, examples, and relevance tests for each new
geography.
