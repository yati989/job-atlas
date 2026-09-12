"""Talent.com connector: one anonymous browser bootstrap, then direct HTTP.

Talent.com's first search page is server-rendered, while later pages require
anonymous cookies initialized by its Next.js application.  We establish that
session once per process, close Chromium, and reuse the exported cookies for
all search/location connector instances.  Listings and JobPosting JSON-LD
details are parsed from ordinary HTTP responses; there is no DOM scraping
after bootstrap and no account login.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from app.collectors.base import BaseConnector
from app.collectors.depth import NATIVE_SEARCH_MAX_RESULTS, relevance_drift_cursor
from app.config.categories import RECENCY_WINDOW_DAYS, is_on_slice
from app.models.schemas import NormalizedJob

logger = logging.getLogger(__name__)

TALENT_BASE_URL = "https://in.talent.com"
TALENT_SEARCH_URL = f"{TALENT_BASE_URL}/jobs"
TALENT_BOOTSTRAP_URL = (
    f"{TALENT_SEARCH_URL}?k=data&date={RECENCY_WINDOW_DAYS}&l=India&p=1"
)
LOCATION_VALUES = {
    "bengaluru": "Bengaluru",
    "remote": "india",
    "india": "India",
}
TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_STALE = object()


@dataclass(frozen=True)
class _SessionMaterial:
    user_agent: str
    cookies: dict[str, str]


_SESSION_MATERIAL: _SessionMaterial | None = None
_SESSION_LOCK = threading.Lock()
_DETAIL_SEMAPHORE = threading.BoundedSemaphore(24)
_DETAIL_CACHE: dict[str, dict | object] = {}
_DETAIL_IN_FLIGHT: dict[str, threading.Event] = {}
_DETAIL_CACHE_LOCK = threading.Lock()

_RELATIVE_DATE_RE = re.compile(
    r"(\d+)\+?\s*(hour|day|week|month)s?\s*ago", re.IGNORECASE
)


def _bootstrap_session() -> _SessionMaterial:
    """Create Talent.com's anonymous cookies once, without logging in."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(TALENT_BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("[data-new-id]", timeout=20_000)
            user_agent = page.evaluate("navigator.userAgent")
            cookies = {
                cookie["name"]: cookie["value"]
                for cookie in context.cookies()
                if cookie.get("name") and cookie.get("value")
            }
        finally:
            browser.close()
    if not cookies:
        raise RuntimeError("Talent.com anonymous session bootstrap returned no cookies")
    return _SessionMaterial(user_agent=user_agent, cookies=cookies)


def _get_session_material() -> _SessionMaterial:
    global _SESSION_MATERIAL
    if _SESSION_MATERIAL is None:
        with _SESSION_LOCK:
            if _SESSION_MATERIAL is None:
                _SESSION_MATERIAL = _bootstrap_session()
    return _SESSION_MATERIAL


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_relative_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    match = _RELATIVE_DATE_RE.search(value)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * {
        "hour": 3600,
        "day": 86400,
        "week": 604800,
        "month": 2592000,
    }[unit]
    return datetime.fromtimestamp(time.time() - seconds, tz=timezone.utc)


def _jobposting_nodes(value: Any):
    if isinstance(value, dict):
        node_type = value.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "JobPosting" in types:
            yield value
        for child in value.values():
            yield from _jobposting_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _jobposting_nodes(child)


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else None


class TalentComConnector(BaseConnector):
    source_name = "talentcom"

    def __init__(
        self,
        search: str,
        max_results: int = NATIVE_SEARCH_MAX_RESULTS,
        location_mode: str = "bengaluru",
        date_filter_days: int = RECENCY_WINDOW_DAYS,
        fetch_descriptions: bool = True,
        detail_workers: int = 4,
        *,
        location: str | None = None,
    ):
        if location_mode not in {*LOCATION_VALUES, "city"}:
            raise ValueError(f"Unsupported Talent.com location_mode: {location_mode!r}")
        normalized_location = location.strip() if location else None
        if location_mode == "city" and not normalized_location:
            raise ValueError("location is required when location_mode is 'city'")
        if location_mode != "city" and normalized_location:
            raise ValueError("location is supported only when location_mode is 'city'")
        if detail_workers < 1:
            raise ValueError("detail_workers must be at least 1")
        self.search = search
        self.max_results = max_results
        self.location_mode = location_mode
        self.location = normalized_location
        self.date_filter_days = date_filter_days
        self.fetch_descriptions = fetch_descriptions
        self.detail_workers = detail_workers

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            session = _get_session_material()
            self._client = self.make_pooled_client(
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "User-Agent": session.user_agent,
                    "Referer": f"{TALENT_BASE_URL}/",
                },
                timeout=45.0,
            )
            self._client.cookies.update(session.cookies)
        return self._client

    def _search_url(self, page: int) -> str:
        location = (
            self.location
            if self.location_mode == "city"
            else LOCATION_VALUES[self.location_mode]
        )
        params = {
            "k": self.search,
            "date": self.date_filter_days,
            "l": location,
            "p": page,
            "showSignInModal": "true",
        }
        if self.location_mode == "remote":
            params["workplace"] = "remote"
        return f"{TALENT_SEARCH_URL}?{urlencode(params)}"

    def _request(self, url: str, *, allowed_statuses: set[int] | None = None) -> httpx.Response:
        allowed_statuses = allowed_statuses or set()
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.get(url, follow_redirects=True)
                if response.status_code in allowed_statuses:
                    return response
                response.raise_for_status()
                return response
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in TRANSIENT_STATUS_CODES and not isinstance(exc, httpx.RequestError):
                    break
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"Talent.com request failed after retries: {url} ({last_error})") from last_error

    @staticmethod
    def _parse_listing_page(html_text: str) -> list[dict]:
        soup = BeautifulSoup(html_text, "lxml")
        listings: list[dict] = []
        for card in soup.select('article[class*="JobCard_card"]'):
            holder = card.find_parent(attrs={"data-new-id": True})
            title_el = card.select_one('h2[class*="JobCard_title"]')
            company_el = card.select_one('span[class*="JobCard_company"]')
            location_el = card.select_one('span[class*="JobCard_location"]')
            job_id = holder.get("data-new-id") if holder else None
            title = title_el.get_text(" ", strip=True) if title_el else None
            if not job_id or not title:
                raise RuntimeError("Talent.com listing schema changed: job ID/title missing")
            listings.append(
                {
                    "job_id": str(job_id),
                    "title": title,
                    "company": company_el.get_text(" ", strip=True) if company_el else None,
                    "location": location_el.get_text(" ", strip=True) if location_el else None,
                }
            )
        return listings

    def _fetch_listings(self) -> list[dict]:
        results: list[dict] = []
        seen_ids: set[str] = set()
        cursor = relevance_drift_cursor(max_results=self.max_results)
        page = 1
        while cursor.should_continue():
            page_items = self._parse_listing_page(self._request(self._search_url(page)).text)
            if not page_items:
                break
            page_had_new = False
            drifted = False
            for item in page_items:
                if item["job_id"] in seen_ids:
                    continue
                seen_ids.add(item["job_id"])
                page_had_new = True
                cursor.record_job(is_on_slice(item["title"], self.search))
                results.append(item)
                if not cursor.should_continue():
                    drifted = True
                    break
            if not page_had_new or drifted:
                break
            page += 1
        return results

    @staticmethod
    def _parse_detail(html_text: str) -> dict:
        soup = BeautifulSoup(html_text, "lxml")
        node = None
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(script.get_text())
            except (json.JSONDecodeError, TypeError):
                continue
            node = next(_jobposting_nodes(payload), None)
            if node:
                break
        if not node:
            heading_text = soup.find(
                string=lambda value: isinstance(value, str)
                and value.strip().lower() == "job description"
            )
            description = None
            if heading_text and heading_text.parent and heading_text.parent.parent:
                container = heading_text.parent.parent
                description_el = container.find("div", recursive=False)
                if description_el:
                    description = description_el.get_text("\n", strip=True) or None
            if not description:
                return {}
            page_text = soup.get_text(" ", strip=True)
            apply_url = None
            for anchor in soup.find_all("a", href=True):
                if "apply" in anchor.get_text(" ", strip=True).lower():
                    apply_url = urljoin(TALENT_BASE_URL, anchor["href"])
                    break
            return {
                "description_raw": description,
                "posted_at": _parse_relative_datetime(page_text),
                "employment_type": None,
                "company": None,
                "location": None,
                "title": None,
                "salary_raw": None,
                "apply_url": apply_url,
                "jobposting": None,
            }

        description = node.get("description")
        if isinstance(description, str):
            description = BeautifulSoup(description, "lxml").get_text("\n", strip=True) or None

        address = node.get("jobLocation")
        if isinstance(address, dict):
            address = address.get("address")
        location_parts: list[str] = []
        if isinstance(address, dict):
            for key in ("addressLocality", "addressRegion", "addressCountry"):
                value = address.get(key)
                if isinstance(value, str) and value.strip() and value.strip() not in location_parts:
                    location_parts.append(value.strip())
        elif isinstance(address, str) and address.strip():
            location_parts.append(address.strip())

        employment = node.get("employmentType")
        if isinstance(employment, list):
            employment = "/".join(str(value) for value in employment if value)
        if not isinstance(employment, str):
            employment = None

        apply_url = None
        for anchor in soup.find_all("a", href=True):
            if "apply" in anchor.get_text(" ", strip=True).lower():
                apply_url = urljoin(TALENT_BASE_URL, anchor["href"])
                break

        salary = node.get("baseSalary")
        salary_raw = json.dumps(salary, ensure_ascii=False) if isinstance(salary, dict) else salary
        return {
            "description_raw": description,
            "posted_at": _parse_datetime(node.get("datePosted")),
            "employment_type": employment,
            "company": _name(node.get("hiringOrganization")),
            "location": ", ".join(location_parts) or None,
            "title": _name(node.get("title")),
            "salary_raw": str(salary_raw) if salary_raw else None,
            "apply_url": apply_url,
            "jobposting": {key: value for key, value in node.items() if key != "description"},
        }

    def _fetch_detail(self, job_id: str) -> dict | object:
        url = f"{TALENT_BASE_URL}/view?id={job_id}"
        with _DETAIL_SEMAPHORE:
            response = self._request(url, allowed_statuses={404, 410})
        if response.status_code in {404, 410}:
            return _STALE
        detail = self._parse_detail(response.text)
        if not detail:
            logger.warning("Talent.com detail page lacks JobPosting JSON-LD: %s", url)
        return detail

    def _fetch_detail_cached(self, job_id: str) -> dict | object:
        with _DETAIL_CACHE_LOCK:
            if job_id in _DETAIL_CACHE:
                return _DETAIL_CACHE[job_id]
            event = _DETAIL_IN_FLIGHT.get(job_id)
            if event is None:
                event = threading.Event()
                _DETAIL_IN_FLIGHT[job_id] = event
                owner = True
            else:
                owner = False

        if not owner:
            event.wait()
            with _DETAIL_CACHE_LOCK:
                return _DETAIL_CACHE[job_id]

        try:
            detail = self._fetch_detail(job_id)
        except Exception:
            with _DETAIL_CACHE_LOCK:
                _DETAIL_IN_FLIGHT.pop(job_id, None)
                event.set()
            raise
        with _DETAIL_CACHE_LOCK:
            _DETAIL_CACHE[job_id] = detail
            _DETAIL_IN_FLIGHT.pop(job_id, None)
            event.set()
        return detail

    def _enrich_details(self, listings: list[dict]) -> list[tuple[dict, dict]]:
        if not self.fetch_descriptions or not listings:
            return [(item, {}) for item in listings]
        details: list[dict | object | None] = [None] * len(listings)
        with ThreadPoolExecutor(max_workers=self.detail_workers) as pool:
            futures = {
                pool.submit(self._fetch_detail_cached, item["job_id"]): index
                for index, item in enumerate(listings)
            }
            for future in as_completed(futures):
                details[futures[future]] = future.result()
        return [
            (item, detail if isinstance(detail, dict) else {})
            for item, detail in zip(listings, details)
            if detail is not _STALE
        ]

    def fetch(self) -> list[NormalizedJob]:
        normalized: list[NormalizedJob] = []
        for item, detail in self._enrich_details(self._fetch_listings()):
            location = item.get("location") or detail.get("location")
            company = item.get("company") or detail.get("company") or "Unknown"
            job_url = f"{TALENT_BASE_URL}/view?id={item['job_id']}"
            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=item["job_id"],
                    title=(item.get("title") or detail.get("title"))[:255],
                    company_name_raw=company[:255],
                    location_raw=location[:255] if location else None,
                    # Talent.com's native remote facet may still display a
                    # physical city on the card. The verified query mode is
                    # therefore stronger evidence than the location label.
                    is_remote=(
                        self.location_mode == "remote"
                        or "remote" in (location or "").lower()
                    ),
                    remote_scope=location[:255] if location else None,
                    employment_type=(detail.get("employment_type") or None),
                    seniority=None,
                    description_raw=detail.get("description_raw"),
                    salary_raw=(detail.get("salary_raw") or None),
                    apply_url=detail.get("apply_url") or job_url,
                    job_url=job_url,
                    posted_at=detail.get("posted_at"),
                    raw_payload={"listing": item, "jobposting": detail.get("jobposting")},
                )
            )
        return normalized
