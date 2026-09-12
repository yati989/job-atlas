# TimesJobs profile-policy detail hydration — 2026-09-07

## Scope

TimesJobs already has live-verified city, India-wide, and remote collection
queries. This change concerns only the decision to fetch a listing's detail
after collection; it does not alter retries, pagination, location requests,
or source activation.

## Problem

The connector's private compatibility gate used the older default role and
location policy before fetching descriptions. A valid Pune job for a
non-default profile role could therefore be rejected before the description
and structured experience evidence were available. In the source scorecard
this manifested as very low description completeness despite successful city
collection.

## Contract

`TimesJobsConnector(..., *, relevance_policy: RelevancePolicy | None = None)`
keeps the old `first_failing_axis` prefilter when no policy is supplied. When
the guided run supplies its frozen `RelevancePolicy`, the connector calls
`evaluate_relevance` for the listing candidate and fetches detail for outcomes
of `kept` or `needs_review`. A policy result of `needs_review` includes unknown
experience requirements, so missing evidence does not prevent the request
that can supply it. Rejected candidates continue to skip detail hydration.

## Offline verification

The focused regression uses an `Operations Analyst` listing in Pune, a role
outside the legacy default gate but accepted by a supplied Pune policy. The
test was red before the new keyword-only argument existed and now proves that
the detail request occurs and its description is retained. Existing no-policy
detail-prefilter coverage remains in the same test module.
