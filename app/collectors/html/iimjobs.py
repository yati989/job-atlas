"""IIMJobs connector using the board's native India search APIs.

Live-verified 2026-08-14:

* ``GET https://gladiator.iimjobs.com/job/search`` accepts the canonical
  free-text role in ``query``.  Unlike guessed ``/k/{slug}-jobs`` routes, it
  returns results for all six configured search terms; a nonsense query
  returns zero.
* ``loc=3,132`` is the real native Bangalore-or-Remote filter (IDs exposed by
  the site's own result data).  A nonsense location ID returns zero.  Other
  plausible parameter names were proven ignored. City IDs are resolved from
  the board's unfiltered result data at fetch time, never guessed or fetched
  by the constructor.
* ``size=1000`` is honored and exhausted every configured role/location
  inventory observed live (the largest was 160 rows); pagination remains as
  a defensive fallback when ``hasMore`` is true.
* ``GET /job/detail?jobcode=...`` returns the hydrated detail payload over
  plain HTTP.  ``introText`` is the description for ordinary and branded
  jobs, replacing the former four-second serial Playwright visit per job.

The listing order contains premium pins and is not date-monotonic, so the
connector exhausts the cheap native inventory and applies the exact local
recency cutoff.  No relevance-drift stop is used.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import re

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob


SEARCH_URL = "https://gladiator.iimjobs.com/job/search"
DETAIL_URL = "https://gladiator.iimjobs.com/job/detail"
SEARCH_PAGE_SIZE = 1000
TARGET_LOCATION_IDS = "3,132"  # Bangalore, Remote
REMOTE_LOCATION_ID = "132"
DETAIL_WORKERS = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,*/*",
    "version": "2",
}

SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
CITY_NAME_ALIASES = {"bengaluru": "bangalore", "gurugram": "gurgaon"}


def _slugify(text: str) -> str:
    return SLUG_STRIP_RE.sub("-", text.lower()).strip("-") or "job"


def _normalize_city_name(text: str) -> str:
    normalized = SLUG_STRIP_RE.sub("", text.lower())
    return CITY_NAME_ALIASES.get(normalized, normalized)


class IIMJobsConnector(BaseConnector):
    source_name = "iimjobs"

    def __init__(
        self,
        search: str = "data scientist",
        fetch_descriptions: bool = True,
        freshness_days: int = RECENCY_WINDOW_DAYS,
        detail_workers: int = DETAIL_WORKERS,
        *,
        location_mode: str | None = None,
        location: str | None = None,
    ):
        if location_mode not in {None, "city", "india", "remote"}:
            raise ValueError("location_mode must be 'city', 'india', or 'remote'")
        normalized_location = location.strip() if location else None
        if location_mode == "city" and not normalized_location:
            raise ValueError("location is required when location_mode is 'city'")
        if location_mode != "city" and normalized_location:
            raise ValueError("location is supported only when location_mode is 'city'")
        self.search = search
        self.fetch_descriptions = fetch_descriptions
        self.freshness_days = freshness_days
        self.detail_workers = detail_workers
        self.location_mode = location_mode
        self.location = normalized_location

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self.make_pooled_client(headers=HEADERS)
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_search_page(self, page: int, location_ids: str | None = TARGET_LOCATION_IDS) -> dict:
        params: dict[str, str | int] = {
            "query": self.search,
            "page": page,
            "size": SEARCH_PAGE_SIZE,
        }
        if location_ids is not None:
            params["loc"] = location_ids
        response = self.client.get(
            SEARCH_URL,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    def _resolve_location_ids(self) -> str | None:
        """Resolve a requested city from IIMJobs' live native ID evidence."""
        if self.location_mode is None:
            return TARGET_LOCATION_IDS
        if self.location_mode == "remote":
            return REMOTE_LOCATION_ID
        if self.location_mode == "india":
            return None

        response = self.client.get(
            SEARCH_URL,
            params={"query": self.search, "page": 0, "size": SEARCH_PAGE_SIZE},
        )
        response.raise_for_status()
        target = _normalize_city_name(self.location or "")
        for item in response.json().get("data") or []:
            for candidate in item.get("locations") or []:
                name = candidate.get("name")
                location_id = candidate.get("id")
                if (
                    isinstance(name, str)
                    and location_id is not None
                    and _normalize_city_name(name) == target
                ):
                    return str(location_id)
        raise ValueError(
            f"IIMJobs did not expose a native location ID for requested city {self.location!r}"
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
    def _fetch_detail(self, job_id: int | str) -> dict:
        response = self.client.get(DETAIL_URL, params={"jobcode": job_id})
        response.raise_for_status()
        data = response.json().get("data") or {}
        description_html = data.get("introText")
        description = (
            BeautifulSoup(str(description_html), "lxml").get_text("\n", strip=True)
            if description_html
            else None
        )
        return {"description_raw": description}

    def _fetch_details(self, job_ids: list[int | str]) -> dict[str, dict]:
        if not self.fetch_descriptions or not job_ids:
            return {}

        details: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.detail_workers)) as executor:
            futures = {executor.submit(self._fetch_detail, job_id): str(job_id) for job_id in job_ids}
            for future in as_completed(futures):
                job_id = futures[future]
                try:
                    details[job_id] = future.result()
                except Exception:
                    details[job_id] = {}
        return details

    def fetch(self) -> list[NormalizedJob]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.freshness_days)
        location_ids = self._resolve_location_ids()
        raw_jobs: list[dict] = []
        seen_ids: set[str] = set()
        page = 0

        while True:
            try:
                payload = self._fetch_search_page(page, location_ids)
            except Exception:
                break

            items = payload.get("data") or []
            if not items:
                break
            for item in items:
                job_id = item.get("id")
                if job_id is None or str(job_id) in seen_ids:
                    continue
                seen_ids.add(str(job_id))
                raw_jobs.append(item)

            if not payload.get("hasMore", False):
                break
            page += 1

        eligible_jobs: list[dict] = []
        for item in raw_jobs:
            created_ms = item.get("createdTimeMs")
            posted_at = (
                datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                if isinstance(created_ms, (int, float))
                else None
            )
            if posted_at is not None and posted_at < cutoff:
                continue
            item["_posted_at"] = posted_at
            eligible_jobs.append(item)

        details = self._fetch_details([item["id"] for item in eligible_jobs])
        normalized: list[NormalizedJob] = []

        for item in eligible_jobs:
            job_id = item.get("id")
            raw_title = (item.get("title") or "").strip()
            if job_id is None or not raw_title:
                continue

            company = ((item.get("companyData") or {}).get("companyName") or "").strip()
            title = raw_title
            if company and title.startswith(f"{company} - "):
                title = title[len(company) + 3 :].strip()
            elif not company:
                parts = title.split(" - ", 1)
                if len(parts) > 1:
                    company, title = parts[0].strip(), parts[1].strip()
            company = company or "Unknown"

            job_url = item.get("jobDetailUrl") or f"https://www.iimjobs.com/j/{_slugify(title)}-{job_id}"
            locations = [
                loc.get("name")
                for loc in (item.get("locations") or [])
                if loc.get("name")
            ]
            location = ", ".join(locations) if locations else None
            location_lower = (location or "").lower()

            raw_payload: dict = {"url": job_url}
            exp_min, exp_max = item.get("min"), item.get("max")
            if exp_min is not None or exp_max is not None:
                raw_payload["experience_scraped"] = f"{exp_min}-{exp_max} yrs"
            tags = [tag.get("name") for tag in (item.get("tags") or []) if tag.get("name")]
            if tags:
                raw_payload["skills_scraped"] = tags

            salary_raw = None
            if not item.get("hideSal") and (item.get("minSal") or item.get("maxSal")):
                lo, hi = item.get("minSal"), item.get("maxSal")
                salary_raw = f"{lo}-{hi}" if lo and hi and lo != hi else str(lo or hi)

            detail = details.get(str(job_id), {})
            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=str(job_id),
                    title=title[:500],
                    company_name_raw=company[:255],
                    location_raw=location[:255] if location else None,
                    is_remote=bool(item.get("workFromHome")) or "remote" in location_lower,
                    remote_scope=None,
                    employment_type=None,
                    seniority=None,
                    description_raw=detail.get("description_raw"),
                    salary_raw=salary_raw,
                    apply_url=job_url,
                    job_url=job_url,
                    posted_at=item.get("_posted_at"),
                    raw_payload=raw_payload,
                )
            )

        return normalized
