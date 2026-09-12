"""
Foundit connector — turned out to need NO browser at all (L1 on the
escalation ladder: plain httpx, no proxy, no headless/headed Playwright).

Earlier version assumed browser automation was required (cards are
click-driven, no plain href) and used a headed+proxied Playwright session
just to click cards and intercept the resulting `/middleware/jobdetail/{id}`
network request. Re-tested from scratch under the standard ladder
(escalate only as far as actually needed):

- L1 (plain httpx GET of the search page) rendered only CSS class names
  (`.cardContainer{...}` in a `<style>` block) — real cards are injected
  client-side, so the *page* genuinely needs JS. But the page's own
  internal API calls don't: a plain httpx GET of the known
  `/middleware/jobdetail/{id}` detail endpoint returned clean 200 JSON with
  no browser/session at all (confirmed with a bogus ID — it 200s with an
  internal 404 payload, not an HTTP-level block).
- L2 (headless Playwright, no proxy, network-interception) hit a hard
  "Access Denied" wall — headless is fingerprinted and blocked outright,
  0 cards, 0 XHR captured.
- L4b (headed Playwright, `headless=False`, still no proxy) rendered fine
  (15 cards, real title) and its network log revealed the actual listing
  endpoint the SPA calls: `GET /middleware/jobsearch?sort=1&limit=15&query=
  {q}&start={offset}` — paginated with a plain `start=` offset, same
  numeric job `id`s as the click-driven cards.
- Escalating back down: that `jobsearch` endpoint (and the already-known
  `jobdetail/{id}` endpoint for full description text) both returned clean
  200 JSON via a **plain httpx GET** with just a normal browser
  `User-Agent` and a `Referer` pointing at the matching search URL — no
  cookies, no session, no browser process at all. So the actual fix is L1,
  just not the search *page* itself — its underlying JSON APIs, which carry
  no bot-detection of their own (Access Denied is applied at the page/HTML
  layer, not the API layer). This removes headed+proxy+patchright entirely
  for this source, and with it the "rate-limits after 1-2 loads" flakiness,
  which was specific to proxy contention, not the site itself — plain httpx
  hit both endpoints repeatedly with no throttling observed.

Field notes from the live payloads:
- Listing rows expose `freshness`/`createdAt` as epoch milliseconds, which
  are normalized into `posted_at`. The advertised `jobFreshness=60` native
  filter was verified against unfiltered and nonsense controls on
  2026-08-12 and is applied before pagination.
- `minimumSalary`/`maximumSalary` are near-universally zeroed out
  (`absoluteValue: 0`) with a separate `hideSalary` flag — salary_raw is
  only populated when a non-zero value is actually present.
- `applyUrl` frequently points off-site (e.g. straight to a LinkedIn
  posting) when Foundit is aggregating/syndicating a listing
  (`jobSource: "SCRAPPING"`) — used as apply_url, while job_url always
  stays Foundit's own `jdUrl` detail page (stable, always resolvable).
- `minimumExperience`/`maximumExperience` (years) are real recruiter-set
  fields on this site, unlike LinkedIn's self-reported seniority buckets —
  captured into raw_payload only, never fabricated into `seniority` (no
  years-of-experience filter is applied, per project convention).

Re-verified live 2026-07-24 (issue #51): `is_remote` previously did plain
text-matching on `locations` only. Live `jobsearch`/`jobdetail` payloads
carry a distinct structured `jobTypes` array (e.g. `["Work From Home"]`,
`["Permanent Job"]`) separate from `locations` — confirmed by querying for
remote-flavored terms and diffing: remote-labeled results consistently
carry `"Work From Home"` in `jobTypes`, on-site ones don't. Now used as the
primary `is_remote` signal, layered over the existing location-text
fallback.

Query matrix recheck (2026-08-12): canonical role terms are paired with
`bangalore` and `remote` suffixes. Bare terms returned role-relevant but
location-noisy results; `bangalore` returned Bengaluru-tagged jobs and
`remote` returned Remote/India-Remote jobs with strong gate relevance.
`india remote` was inconsistent or empty across terms and is not used.

Depth policy: fetch to real API exhaustion, bounded by a 5,000-result raw
safety ceiling. `limit=1000` is requested for speed, but Foundit currently
caps the effective page at 100 real jobs and injects non-job rows into
`data`; pagination therefore follows `meta.paging.cursors.next` rather than
the raw array length. Relevance-drift stopping is deliberately not used
because the site's ordering is not trusted; the central deterministic gate
filters the complete fetched supply.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob
from app.pipeline.relevance import (
    RelevancePolicy,
    evaluate_relevance,
    recency_ok,
    role_ok,
    seniority_ok,
)

FOUNDIT_SEARCH_PAGE_URL = "https://www.foundit.in/srp/results?query={query}"
FOUNDIT_JOBSEARCH_URL = (
    "https://www.foundit.in/middleware/jobsearch?sort=1&limit={limit}"
    "&start={start}&query={query}&jobFreshness={freshness_days}"
)
FOUNDIT_JOBDETAIL_URL = "https://www.foundit.in/middleware/jobdetail/{job_id}"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_MAX_FIELD_LEN = 255
FOUNDIT_MAX_RESULTS = 500
FOUNDIT_PAGE_SIZE = 1000
FOUNDIT_DETAIL_WORKERS = 4


def _truncate(value: str | None, limit: int = _MAX_FIELD_LEN) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


def _epoch_ms_to_datetime(value) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _location_text(value) -> str | None:
    if isinstance(value, list):
        locations = [
            str(location.get("city", "")).strip()
            for location in value
            if isinstance(location, dict) and location.get("city")
        ]
        return ", ".join(locations) or None
    return value if isinstance(value, str) else None


def _is_remote(job_types: list | None, location: str | None) -> bool:
    return bool(
        any("work from home" in str(job_type).lower() for job_type in job_types or [])
        or "remote" in (location or "").lower()
    )


def _has_location_or_remote_evidence(job_types: list | None, location: str | None) -> bool:
    return bool(location) or _is_remote(job_types, location)


class FounditConnector(BaseConnector):
    source_name = "foundit"

    def __init__(
        self,
        search: str,
        max_results: int = FOUNDIT_MAX_RESULTS,
        page_size: int = FOUNDIT_PAGE_SIZE,
        freshness_days: int = RECENCY_WINDOW_DAYS,
        fetch_descriptions: bool = True,
        detail_workers: int = FOUNDIT_DETAIL_WORKERS,
        *,
        relevance_policy: RelevancePolicy | None = None,
    ):
        self.search = search
        self.max_results = max_results
        self.page_size = page_size
        self.freshness_days = freshness_days
        self.fetch_descriptions = fetch_descriptions
        self.relevance_policy = relevance_policy
        if detail_workers < 1:
            raise ValueError("detail_workers must be at least 1")
        self.detail_workers = detail_workers

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _get_json(self, url: str, referer: str) -> dict:
        resp = self.client.get(
            url, headers={"Accept": "application/json", "Referer": referer, "User-Agent": _USER_AGENT}
        )
        resp.raise_for_status()
        return resp.json()

    def _list_jobs(self, referer: str) -> list[dict]:
        query = quote(self.search)
        items: list[dict] = []
        start = 0
        while len(items) < self.max_results:
            url = FOUNDIT_JOBSEARCH_URL.format(
                limit=self.page_size,
                start=start,
                query=query,
                freshness_days=self.freshness_days,
            )
            try:
                payload = self._get_json(url, referer)
            except Exception:
                break
            response = payload.get("jobSearchResponse") or {}
            data = response.get("data") or []
            if not data:
                break

            # Paging metadata counts jobs, while `data` also contains
            # advertisements and other injected non-job rows.
            jobs = [item for item in data if item.get("id") or item.get("jobId")]
            remaining = self.max_results - len(items)
            items.extend(jobs[:remaining])
            if len(items) >= self.max_results:
                break

            paging = (response.get("meta") or {}).get("paging") or {}
            total = paging.get("total")
            if isinstance(total, int) and len(items) >= total:
                break

            next_cursor = (paging.get("cursors") or {}).get("next")
            try:
                next_start = int(next_cursor)
            except (TypeError, ValueError):
                next_start = start + len(jobs)
            if not jobs or next_start <= start:
                break
            start = next_start
        return items

    def _fetch_detail(self, job_id: str, referer: str) -> dict | None:
        try:
            payload = self._get_json(FOUNDIT_JOBDETAIL_URL.format(job_id=job_id), referer)
        except Exception:
            return None
        detail = payload.get("jobDetailResponse")
        return detail if isinstance(detail, dict) else None

    def _scrape(self) -> list[dict]:
        referer = FOUNDIT_SEARCH_PAGE_URL.format(query=quote(self.search))
        results: list[dict] = []

        listing = self._list_jobs(referer)

        detail_candidates = [
            item for item in listing if self._listing_needs_detail(item)
        ]
        details: dict[str, dict | None] = {}
        if self.fetch_descriptions and detail_candidates:
            job_ids = [
                str(item.get("id") or item.get("jobId"))
                for item in detail_candidates
            ]
            with ThreadPoolExecutor(max_workers=self.detail_workers) as pool:
                fetched = pool.map(
                    lambda job_id: self._fetch_detail(job_id, referer), job_ids
                )
                details = dict(zip(job_ids, fetched))

        for item in listing:
            job_id = str(item.get("id") or item.get("jobId") or "")
            if not job_id:
                continue

            detail = details.get(job_id)
            source = detail if detail else item

            jd_url = source.get("jdUrl")
            title = source.get("title")
            if not jd_url or not title:
                continue

            company = source.get("companyName")
            location = _location_text(source.get("locations"))

            employment_types = source.get("employmentTypes") or []
            employment_type = employment_types[0] if employment_types else None
            job_types = source.get("jobTypes") or []

            min_salary = (source.get("minimumSalary") or {}).get("absoluteValue") or 0
            max_salary = (source.get("maximumSalary") or {}).get("absoluteValue") or 0
            currency = source.get("currencyCode") or "INR"
            salary_raw = None
            if min_salary or max_salary:
                salary_raw = f"{currency} {min_salary}-{max_salary}"

            results.append(
                {
                    "job_id": job_id,
                    "jd_url": jd_url,
                    "title": title,
                    "company": company,
                    "location": location,
                    "employment_type": employment_type,
                    "job_types": job_types,
                    "description": source.get("description"),
                    "salary_raw": salary_raw,
                    "apply_url": source.get("applyUrl"),
                    "posted_epoch_ms": (
                        source.get("freshness")
                        or source.get("createdAt")
                        or item.get("freshness")
                        or item.get("createdAt")
                    ),
                    "min_experience_years": (source.get("minimumExperience") or {}).get("years"),
                    "max_experience_years": (source.get("maximumExperience") or {}).get("years"),
                    "raw": source,
                }
            )

        return results

    def _listing_needs_detail(self, item: dict) -> bool:
        """Use card fields to skip detail requests that cannot be relevant.

        With a profile policy, cards with absent location or experience
        evidence remain reviewable and are hydrated so detail data can resolve
        them.
        """
        candidate = NormalizedJob(
            source=self.source_name,
            external_job_id=str(item.get("id") or item.get("jobId") or "candidate"),
            title=str(item.get("title") or ""),
            company_name_raw=str(item.get("companyName") or "Unknown"),
            location_raw=_location_text(item.get("locations")),
            is_remote=_is_remote(
                item.get("jobTypes") or [], _location_text(item.get("locations"))
            ),
            remote_scope=_location_text(item.get("locations")),
            posted_at=_epoch_ms_to_datetime(
                item.get("freshness") or item.get("createdAt")
            ),
            raw_payload=item,
        )
        if self.relevance_policy is not None:
            decision = evaluate_relevance(candidate, self.relevance_policy)
            # Incomplete listing cards must not prevent detail hydration:
            # detail text can resolve an otherwise reviewable location or
            # experience requirement. Confirmed profile rejections are safe
            # to skip before spending the source-detail request.
            if decision.outcome in {"kept", "needs_review"}:
                return True
            return (
                decision.axis == "location"
                and not _has_location_or_remote_evidence(
                    item.get("jobTypes") or [], candidate.location_raw
                )
            )
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.freshness_days)
        return (
            role_ok(candidate)
            and seniority_ok(candidate)
            and recency_ok(candidate, cutoff_at=cutoff)
        )

    def fetch(self) -> list[NormalizedJob]:
        raw_jobs = self._scrape()

        normalized: list[NormalizedJob] = []
        for item in raw_jobs:
            job_id = item.get("job_id")
            jd_url = item.get("jd_url")
            title = item.get("title")
            if not job_id or not jd_url or not title:
                continue

            job_url = f"https://www.foundit.in{jd_url}" if jd_url.startswith("/") else jd_url
            apply_url = item.get("apply_url") or job_url
            location = item.get("location")
            job_types = item.get("job_types") or []

            # Dedicated field (jobTypes, e.g. "Work From Home") takes
            # priority, location-text is fallback (issue #51).
            is_remote = _is_remote(job_types, location)

            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=job_id,
                    title=_truncate(title) or title[:_MAX_FIELD_LEN],
                    company_name_raw=_truncate(item.get("company")) or "Unknown",
                    location_raw=_truncate(location),
                    is_remote=is_remote,
                    remote_scope=_truncate(location),
                    employment_type=_truncate(item.get("employment_type")),
                    seniority=None,  # Foundit exposes years-of-experience, not a seniority bucket — never fabricated
                    description_raw=item.get("description"),
                    salary_raw=_truncate(item.get("salary_raw")),
                    apply_url=apply_url,
                    job_url=job_url,
                    posted_at=_epoch_ms_to_datetime(item.get("posted_epoch_ms")),
                    raw_payload=item,
                )
            )


        return normalized
