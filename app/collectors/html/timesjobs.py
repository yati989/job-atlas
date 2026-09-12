"""
TimesJobs connector (timesjobs.com).

Originally built as a Playwright DOM-scraper to work around two independent
quirks: the site's TLS cert fails standard verification (a real server
misconfiguration, confirmed via a plain httpx GET), and the search-results
page is a client-side-rendered Next.js app with no useful content in the
initial HTML.

Live network inspection (watching requests fired by the rendered page,
same lesson as Foundit's `/middleware/jobdetail` shortcut) found the actual
data source behind both the search page and the job-detail page: a plain
JSON API at `tjapi.timesjobs.com`, reachable directly with `httpx` (no
Playwright, no proxy, `verify=False` to route around the same bad cert):
- `POST /search/api/v1/search/jobs/list` -- paginated search results
  (`{"keyword", "location", "page", "size", ...}`), returns title, company,
  location, postDate, jobFunction, jobType, skills, jobId, jobDetailUrl.
- `GET /job-api/api/jobs/public/{jobId}` (plain numeric jobId works, no
  need to URL-decode the opaque token embedded in jobDetailUrl) -- full
  detail: HTML description, employmentType, jobType, industryNames,
  functionNames, minExperience/maxExperience, minCtc/maxCtc/ctcCurrency,
  postedAt (ISO), expiresAt, externalJobsLink (the real employer apply URL).
This eliminates the browser entirely -- faster and immune to the Next.js
rendering + cert issues that motivated the original Playwright approach.

Location filtering: `location="Bengaluru"` and `location="Remote"` are
genuine server-side geographic filters (verified live 2026-08-12). The
Bengaluru filter can return noisy titles even when the keyword is specific,
so the central relevance gate remains authoritative. `location="India"` is
a trap confirmed live: it silently breaks keyword matching (returns unrelated
Bajaj Finserv/branch manager listings for a "data scientist" query). The
registry therefore uses six canonical search terms across exactly two native
location modes: Bengaluru and Remote. There is no unscoped broad pass and no
keyword-suffix location fallback.

Date: `postedAt` (detail API, ISO datetime) is a genuine scraped date --
post-filtered to `date_filter_days`, which the runner derives from the active
sync window, never fabricated.

Experience: `minExperience`/`maxExperience` are real scraped years-of-
experience values (not a seniority-bucket proxy) -- captured into
raw_payload for the enrichment pipeline, never used here as a filter
(per standing project rule: don't approximate an experience filter).
No structured seniority field exists on this site, so `seniority` stays
None rather than being inferred from experience numbers.

Depth policy (issue #63): live-checked 2026-07-25 and rechecked 2026-08-12
-- NOT reliably relevance-sorted. A real "data scientist"/Bengaluru query's
on-slice share dropped early and kept falling, with off-role titles mixed
throughout. Each query retains its 15,000-row static ceiling; card fields
screen out clearly irrelevant rows before detail hydration, and the remaining
details are fetched with a bounded worker pool. The central gate remains
authoritative for the native-location result set and exact sync-window timestamp.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob
from app.pipeline.relevance import (
    RelevancePolicy,
    evaluate_relevance,
    first_failing_axis,
)

SEARCH_URL = "https://tjapi.timesjobs.com/search/api/v1/search/jobs/list"
DETAIL_URL = "https://tjapi.timesjobs.com/job-api/api/jobs/public/{job_id}"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "referer": "https://www.timesjobs.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
}

# Whitelist, not blacklist -- deliberately mirrors Remotive's
# INDIA_ELIGIBLE_MARKERS pattern. TimesJobs' `location` text for genuine
# India postings is a city/state name (often without the literal word
# "India" -- e.g. "Bengaluru", "Gurugram", "Other City(s) in Jharkhand"),
# so this needs real city/state coverage, not just the country name.
TIMESJOBS_MAX_RESULTS = 500
TIMESJOBS_PAGE_SIZE = 1000
TIMESJOBS_DETAIL_WORKERS = 4
LOCATION_VALUES = {
    "bengaluru": "Bengaluru",
    "remote": "Remote",
}


class TimesJobsConnector(BaseConnector):
    source_name = "timesjobs"

    def __init__(
        self,
        search: str,
        location_mode: str,
        max_results: int = TIMESJOBS_MAX_RESULTS,
        page_size: int = TIMESJOBS_PAGE_SIZE,
        date_filter_days: int = RECENCY_WINDOW_DAYS,
        fetch_descriptions: bool = True,
        detail_workers: int = TIMESJOBS_DETAIL_WORKERS,
        *,
        location: str | None = None,
        relevance_policy: RelevancePolicy | None = None,
    ):
        if location_mode not in {*LOCATION_VALUES, "city", "india"}:
            raise ValueError(f"Unsupported TimesJobs location mode: {location_mode!r}")
        if location_mode == "city" and not (location and location.strip()):
            raise ValueError("city mode requires location")
        if location_mode != "city" and location is not None:
            raise ValueError("location is only valid for city mode")
        self.location = location.strip() if location else None
        self.search = search
        self.location_mode = location_mode
        self.max_results = max_results
        self.page_size = page_size
        self.date_filter_days = date_filter_days
        self.fetch_descriptions = fetch_descriptions
        self.relevance_policy = relevance_policy
        if detail_workers < 1:
            raise ValueError("detail_workers must be at least 1")
        self.detail_workers = detail_workers
        # Site's TLS cert fails standard verification (server misconfig, not
        # a project bug) -- pre-build the pooled client with verify=False
        # rather than relying on BaseConnector.client's verify=True default.
        self._client = self.make_pooled_client(verify=False)

    def _search_page(self, client: httpx.Client, location: str, page: int) -> list[dict]:
        body = {
            "keyword": self.search,
            "location": location,
            "experience": "",
            "page": str(page),
            "size": str(self.page_size),
            "jobFunctions": [],
            "company": "",
            "industry": "",
            "functionAreaId": "",
            "jobFunction": "",
        }
        resp = client.post(SEARCH_URL, json=body, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json().get("jobs", [])

    def _collect(self, client: httpx.Client, location: str) -> list[dict]:
        collected: list[dict] = []
        page = 1
        while len(collected) < self.max_results:
            jobs = self._search_page(client, location, page)
            if not jobs:
                break
            collected.extend(jobs)
            page += 1
            if page > 20:  # hard safety cap against a runaway loop
                break
        return collected[: self.max_results]

    def _fetch_detail(self, client: httpx.Client, job_id: str) -> dict | None:
        try:
            resp = client.get(
                DETAIL_URL.format(job_id=job_id), headers=HEADERS, timeout=20
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _scrape(self) -> list[dict]:
        results: dict[str, dict] = {}
        client = self.client

        # Live verified 2026-09-07: Pune returns Pune rows, a nonsense city
        # returns none. Empty location is the broad feed, screened centrally
        # for India eligibility; it is not a remote-only query.
        location = self.location if self.location_mode == "city" else LOCATION_VALUES.get(self.location_mode, "")
        for item in self._collect(client, location):
            job_id = str(item.get("jobId") or "")
            if job_id:
                results.setdefault(job_id, item)

        merged = list(results.values())

        if self.fetch_descriptions:
            eligible = [item for item in merged if self._listing_passes_gate(item)]
            with ThreadPoolExecutor(max_workers=self.detail_workers) as pool:
                details = pool.map(
                    lambda item: self._fetch_detail(
                        client, str(item.get("jobId") or "")
                    ),
                    eligible,
                )
                for item, detail in zip(eligible, details):
                    if detail:
                        item["_detail"] = detail

        return merged

    @staticmethod
    def _parse_posted_at(value) -> datetime | None:
        if not value:
            return None
        try:
            posted_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)

    def _listing_passes_gate(self, item: dict) -> bool:
        """Avoid detail requests for cards the accepted policy will reject."""
        location = item.get("location")
        job_type = item.get("jobType")
        is_remote = bool(job_type and "remote" in str(job_type).lower()) or bool(
            location and "remote" in str(location).lower()
        )
        candidate = NormalizedJob(
            source=self.source_name,
            external_job_id=str(item.get("jobId") or "candidate"),
            title=str(item.get("title") or ""),
            company_name_raw=str(item.get("company") or "Unknown"),
            location_raw=location,
            is_remote=is_remote,
            remote_scope=job_type,
            posted_at=self._parse_posted_at(item.get("postDate")),
        )
        if self.relevance_policy is not None:
            decision = evaluate_relevance(candidate, self.relevance_policy)
            # Missing pre-detail experience evidence is intentionally
            # reviewable: detail extraction is what supplies the evidence for
            # the later centralized policy decision.
            return decision.outcome in {"kept", "needs_review"}
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.date_filter_days)
        return first_failing_axis(candidate, cutoff_at=cutoff) is None

    def fetch(self) -> list[NormalizedJob]:
        raw_jobs = self._scrape()

        cutoff = None
        if self.date_filter_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.date_filter_days)

        normalized: list[NormalizedJob] = []
        for item in raw_jobs:
            job_id = str(item.get("jobId") or "")
            job_url = item.get("jobDetailUrl")
            if not job_id or not job_url:
                continue

            detail = item.get("_detail") or {}

            posted_at = self._parse_posted_at(
                detail.get("postedAt") or item.get("postDate")
            )

            if cutoff is not None and posted_at is not None and posted_at < cutoff:
                continue

            description_raw = detail.get("description")

            employment_type = detail.get("employmentType")
            job_type = detail.get("jobType") or item.get("jobType")
            location_raw = item.get("location")
            is_remote = bool(job_type and "remote" in job_type.lower()) or bool(
                location_raw and "remote" in location_raw.lower()
            )

            salary_raw = None
            min_ctc = detail.get("minCtc")
            max_ctc = detail.get("maxCtc")
            if (
                not detail.get("hideCtcFromCandidate")
                and min_ctc not in (None, -1)
                and max_ctc not in (None, -1)
            ):
                currency = detail.get("ctcCurrency") or ""
                salary_raw = f"{min_ctc} - {max_ctc} {currency}".strip()

            apply_url = detail.get("externalJobsLink") or job_url

            company_name = (detail.get("companyName") or item.get("company") or "Unknown")[:255]

            raw_payload = dict(item)
            raw_payload.pop("_detail", None)
            if detail:
                raw_payload["min_experience_years"] = detail.get("minExperience")
                raw_payload["max_experience_years"] = detail.get("maxExperience")
                raw_payload["industry_scraped"] = ", ".join(detail.get("industryNames") or []) or None
                raw_payload["job_function_scraped"] = ", ".join(detail.get("functionNames") or []) or None
                raw_payload["skills_scraped"] = detail.get("skills")

            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=job_id,
                    title=item["title"],
                    company_name_raw=company_name,
                    location_raw=(location_raw or None) and location_raw[:255],
                    is_remote=is_remote,
                    remote_scope=(job_type or None) and job_type[:255],
                    employment_type=(employment_type or None) and employment_type[:255],
                    seniority=None,
                    description_raw=description_raw,
                    salary_raw=(salary_raw or None) and salary_raw[:255],
                    apply_url=apply_url[:2048] if apply_url else None,
                    job_url=job_url[:2048] if job_url else None,
                    posted_at=posted_at,
                    raw_payload=raw_payload,
                )
            )

        return normalized
