"""Instahyre connector using its anonymous listing and public-detail APIs.

The listing endpoint is ordinary JSON. Job pages also call
``/api/v1/employer_public_jobs/<id>`` anonymously; that endpoint supplies the
description, company, locations, active state, and experience range without a
browser. It does not expose the page's JSON-LD ``datePosted`` or general
``employmentType`` values, so those fields remain unknown rather than being
fabricated.

The listing API's real ``skills=`` and ``jobLocations=`` parameters are used
by the registry's canonical search-term/location matrix. On 2026-08-12,
``skills=data scientist`` raised broad-mode relevance from 4% to 66%; adding
``jobLocations=Bangalore`` raised it to 100% (100/100 with zero gate drops).
On 2026-09-07, ``jobLocations=Pune`` returned Pune listings while a nonexistent
city returned none, confirming that arbitrary city values reach the native API.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import random
import threading
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob

BASE_URL = "https://www.instahyre.com"
SEARCH_API_URL = f"{BASE_URL}/api/v1/job_search"
DETAIL_API_URL = f"{BASE_URL}/api/v1/employer_public_jobs"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
LOCATION_VALUES = {
    "bangalore": "Bangalore",
    "remote": "Work From Home",
    None: None,
}
LOCATION_MODES = frozenset((*LOCATION_VALUES, "city", "india"))
TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_LISTING_LOCK = threading.Lock()
_DETAIL_SEMAPHORE = threading.BoundedSemaphore(24)

logger = logging.getLogger(__name__)


def _location_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return ", ".join(parts) or None
    return None


class InstahyreConnector(BaseConnector):
    source_name = "instahyre"

    def __init__(
        self,
        search: str = "",
        max_results: int = 50,
        location_mode: str | None = None,
        date_filter_days: int = RECENCY_WINDOW_DAYS,
        fetch_descriptions: bool = True,
        detail_workers: int = 4,
        *,
        location: str | None = None,
    ):
        if location_mode not in LOCATION_MODES:
            raise ValueError(f"unsupported Instahyre location_mode: {location_mode!r}")
        if location_mode == "city" and not (isinstance(location, str) and location.strip()):
            raise ValueError("Instahyre city location_mode requires a location")
        if detail_workers < 1:
            raise ValueError("detail_workers must be at least 1")
        self.search = search
        self.max_results = max_results
        self.location_mode = location_mode
        self.location = location.strip() if isinstance(location, str) else None
        self.date_filter_days = date_filter_days
        self.fetch_descriptions = fetch_descriptions
        self.detail_workers = detail_workers

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self.make_pooled_client(
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=30.0,
            )
        return self._client

    def _fetch_listings(self) -> list[dict]:
        # Registry instances run in the global parallel pool. Keep this
        # rate-limited listing stream source-serial so the three modes do not
        # push one another into Instahyre's known anonymous 429 depth wall.
        with _LISTING_LOCK:
            return self._fetch_listings_unlocked()

    def _request_listing_page(self, params: dict[str, Any]):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.get(SEARCH_API_URL, params=params)
                if response.status_code == 429:
                    return response
                response.raise_for_status()
                return response
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in TRANSIENT_STATUS_CODES and not isinstance(exc, httpx.RequestError):
                    raise
                if attempt < 2:
                    time.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    def _fetch_listings_unlocked(self) -> list[dict]:
        results: list[dict] = []
        offset = 0
        while len(results) < self.max_results:
            params: dict[str, Any] = {
                "company_size": 0,
                "isLandingPage": "true",
                "job_type": 0,
                "offset": offset,
                "source": "opportunities",
            }
            if self.search:
                params["skills"] = self.search
            location = (
                self.location
                if self.location_mode == "city"
                else LOCATION_VALUES.get(self.location_mode)
            )
            if location:
                params["jobLocations"] = location

            response = self._request_listing_page(params)
            if response.status_code == 429:
                break
            response.raise_for_status()
            payload = response.json()
            objects = payload.get("objects", [])
            if not objects:
                break
            results.extend(objects)
            offset += len(objects)
            if not payload.get("meta", {}).get("next"):
                break
            time.sleep(random.uniform(0.3, 0.8))
        return results[: self.max_results]

    def _fetch_detail(self, job_id: int | str) -> dict:
        url = f"{DETAIL_API_URL}/{job_id}"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with _DETAIL_SEMAPHORE:
                    response = self.client.get(url)
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else {}
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in TRANSIENT_STATUS_CODES and not isinstance(exc, httpx.RequestError):
                    break
                if attempt < 2:
                    time.sleep(2**attempt)
        logger.warning(
            "Instahyre public detail request failed; keeping listing without detail: %s (%s)",
            url,
            last_error,
        )
        return {}

    def fetch(self) -> list[NormalizedJob]:
        listings = self._fetch_listings()
        detail_ids = [
            item["id"]
            for item in listings
            if self.fetch_descriptions and item.get("id") is not None
        ]
        with ThreadPoolExecutor(max_workers=self.detail_workers) as pool:
            details = dict(zip(detail_ids, pool.map(self._fetch_detail, detail_ids)))

        normalized: list[NormalizedJob] = []
        for item in listings:
            job_id = item.get("id")
            url = item.get("public_url")
            if job_id is None or not url:
                continue

            detail = details.get(job_id, {})
            location = _location_text(detail.get("locations")) or _location_text(item.get("locations"))
            company = (
                detail.get("hiring_company_name")
                or (item.get("employer") or {}).get("company_name")
                or "Unknown"
            )
            title = detail.get("candidate_title") or item.get("title") or "Unknown"
            description = detail.get("description")
            if isinstance(description, str) and description.strip():
                description = BeautifulSoup(description, "html.parser").get_text("\n", strip=True) or None
            else:
                description = None

            experience = None
            minimum = detail.get("workex_min")
            maximum = detail.get("workex_max")
            if minimum is not None or maximum is not None:
                experience = f"{minimum if minimum is not None else ''}-{maximum if maximum is not None else ''} Years"

            raw_payload = {"listing": item, "public_detail": detail or None}
            if experience:
                raw_payload["experience_years_scraped"] = experience

            location_lower = (location or "").lower()
            is_remote = "work from home" in location_lower or "remote" in location_lower
            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=str(job_id),
                    title=title[:255],
                    company_name_raw=company[:255],
                    location_raw=location[:255] if location else None,
                    is_remote=is_remote,
                    remote_scope=location[:255] if is_remote and location else None,
                    employment_type="INTERNSHIP" if detail.get("is_internship") is True else None,
                    seniority=None,
                    description_raw=description,
                    salary_raw=None,
                    apply_url=url,
                    job_url=url,
                    posted_at=None,
                    raw_payload=raw_payload,
                )
            )
        return normalized
