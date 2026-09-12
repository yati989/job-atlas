"""Shine India connector using the site's anonymous search API.

Live audit 2026-08-13 established these source contracts:

* Shine is an India-native board. Its official ``/job-search/<slug>`` pages
  are backed by ``/api/v2/search/simple/`` on the same origin.
* ``q`` is a relevance-ranked search, while ``loc=Bangalore`` genuinely
  restricts inventory to Bengaluru. Nonsense role queries return zero.
* The native ``emp_type=4`` (Work from home) filter is real but currently
  destroys role precision: every tested canonical role had zero relevant
  rows in its first page. It is therefore not registered as a role+remote
  query.
* ``sort=1`` is the visible Freshness sort, but it also destroys role
  relevance (the role acts only as a weak ranking hint). We retain the
  default relevance order, stop on the shared relevance-drift rule, and
  locally apply ``RECENCY_WINDOW_DAYS`` to exact ``jPDate`` values.
* ``perpage=50`` is the largest effective batch. Requests for 100 through
  1,000 still return 50. API pages 1-6 were distinct in a bounded live probe.

Each API row already contains the full description, company, locations,
salary when published, and exact posted date, so no detail requests are
needed. Missing/ambiguous fields remain unknown rather than guessed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.collectors.depth import NATIVE_SEARCH_MAX_RESULTS, relevance_drift_cursor
from app.config.categories import RECENCY_WINDOW_DAYS, is_on_slice
from app.models.schemas import NormalizedJob


SHINE_API_URL = "https://www.shine.com/api/v2/search/simple/"
SHINE_SEARCH_ROOT = "https://www.shine.com/job-search/"
PAGE_SIZE = 50

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": SHINE_SEARCH_ROOT,
}

# Shine tokenizes the short form as the common substring "ai", producing
# unrelated inventory. The full official term is strongly role-relevant.
ROLE_QUERY_OVERRIDES = {"ai engineer": "artificial intelligence engineer"}

EMPLOYMENT_TYPES = {
    1: "Regular",
    2: "Contractual",
    3: "Internship",
    4: "Work from home",
}


def _parse_posted(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class ShineConnector(BaseConnector):
    source_name = "shine"

    def __init__(
        self,
        search: str,
        max_results: int = NATIVE_SEARCH_MAX_RESULTS,
        location_mode: str = "india",
        freshness_days: int = RECENCY_WINDOW_DAYS,
        *,
        location: str | None = None,
    ):
        if location_mode not in {"india", "bangalore", "city", "remote"}:
            raise ValueError("Shine location_mode must be india, bangalore, city, or remote")
        if location_mode == "city" and not (location and location.strip()):
            raise ValueError("city mode requires location")
        if location_mode != "city" and location is not None:
            raise ValueError("location is only valid for city mode")
        self.location = location.strip() if location else None
        self.search = search
        self.max_results = max_results
        self.location_mode = location_mode
        self.freshness_days = freshness_days

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self.make_pooled_client(headers=HEADERS)
        return self._client

    def _role_query(self) -> str:
        return ROLE_QUERY_OVERRIDES.get(self.search.lower(), self.search)

    def _params(self, page: int) -> dict[str, str | int]:
        role = self._role_query()
        query = f"{role} jobs"
        params: dict[str, str | int] = {
            "q": query,
            "page": page,
            "perpage": PAGE_SIZE,
        }
        if self.location_mode == "bangalore":
            params["q"] = f"{query} in bangalore"
            params["loc"] = "Bangalore"
        elif self.location_mode == "city":
            params["q"] = f"{query} in {self.location}"
            params["loc"] = self.location
        # Live 2026-09-07: Pune changes results; unknown cities and the word
        # "remote" can fall back to broad results. Remote mode deliberately
        # uses the India feed and preserves listing-level work arrangement.
        return params

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_page(self, page: int) -> dict:
        response = self.client.get(SHINE_API_URL, params=self._params(page))
        response.raise_for_status()
        return response.json()

    def fetch(self) -> list[NormalizedJob]:
        normalized: list[NormalizedJob] = []
        seen_ids: set[str] = set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.freshness_days)
        cursor = relevance_drift_cursor(max_results=self.max_results)

        page = 1
        while cursor.should_continue():
            try:
                payload = self._fetch_page(page)
            except Exception:
                # Preserve earlier successful pages if the source begins
                # throttling a later request.
                break

            results = payload.get("results") or []
            if not results:
                break

            new_ids_this_page = 0
            for item in results:
                job_id = str(item.get("id") or "")
                title = str(item.get("jJT") or "").replace("_", " ").strip()
                if not job_id or not title or job_id in seen_ids:
                    continue

                seen_ids.add(job_id)
                new_ids_this_page += 1
                cursor.record_job(is_on_slice(title, self.search))

                posted_at = _parse_posted(item.get("jPDate"))
                if posted_at is not None and posted_at < cutoff:
                    if not cursor.should_continue():
                        break
                    continue

                locations = item.get("jLoc") or []
                if isinstance(locations, str):
                    locations = [locations]
                location_raw = ", ".join(str(value) for value in locations if value)

                salary_raw = item.get("jSal")
                if salary_raw and str(salary_raw).strip().lower() == "[salary hidden]":
                    salary_raw = None

                description_raw = None
                if item.get("jJD"):
                    description_raw = BeautifulSoup(
                        str(item["jJD"]),
                        "lxml",
                    ).get_text("\n", strip=True)

                employment_code = item.get("jEType")
                try:
                    employment_code = int(employment_code)
                except (TypeError, ValueError):
                    employment_code = None
                employment_type = EMPLOYMENT_TYPES.get(employment_code)

                title_says_remote = "remote" in title.lower()
                is_remote = True if employment_code == 4 or title_says_remote else None
                remote_scope = "Work from home" if employment_code == 4 else None

                job_slug = item.get("jSlug")
                job_url = f"https://www.shine.com/jobs/{job_slug}" if job_slug else None

                raw_payload = {
                    "job_url_slug": job_slug,
                    "search_query": self._params(page)["q"],
                    "location_mode": self.location_mode,
                    "employment_code": employment_code,
                    "work_mode_code": item.get("jWM"),
                }
                if item.get("jExp"):
                    raw_payload["experience_scraped"] = item["jExp"]
                if item.get("jInd"):
                    raw_payload["industry_scraped"] = item["jInd"]
                skills_blob = item.get("jKwd") or item.get("jKwds")
                if skills_blob:
                    raw_payload["skills_scraped"] = [
                        value.strip()
                        for value in str(skills_blob).split(",")
                        if value.strip()
                    ]

                normalized.append(
                    NormalizedJob(
                        source=self.source_name,
                        external_job_id=job_id,
                        title=title[:500],
                        company_name_raw=str(item.get("jCName") or "Unknown")[:255],
                        location_raw=location_raw[:255] if location_raw else None,
                        is_remote=is_remote,
                        remote_scope=remote_scope,
                        employment_type=employment_type,
                        seniority=None,
                        description_raw=description_raw,
                        salary_raw=str(salary_raw)[:255] if salary_raw else None,
                        apply_url=job_url,
                        job_url=job_url,
                        posted_at=posted_at,
                        raw_payload=raw_payload,
                    )
                )

                if not cursor.should_continue():
                    break

            if new_ids_this_page == 0 or not payload.get("next"):
                break
            page += 1

        return normalized
