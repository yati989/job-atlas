"""Built In connector.

Live audit 2026-08-13 established the current official India surfaces:

* ``/jobs`` with explicit ``city=Bengaluru&state=Karnataka&country=IND``
  is the reliable Bengaluru search. The pretty
  ``/jobs/as/india/bangalore`` route was not trusted because its visible
  Bangalore heading resolved to Dehradun in the embedded criteria.
* ``/jobs/remote`` with ``country=IND&allLocations=true`` is the native
  remote-India surface and sets ``criteria.remotePreferences`` to ``"2"``.
* ``daysSinceUpdated`` is a genuine native filter and receives the global
  ``RECENCY_WINDOW_DAYS`` value directly. The site exposes no explicit sort
  control and promoted blocks make the visible date order non-monotonic.

Every query returns at most 25 rows. ``pageSize=26`` and larger return zero;
other guessed size parameters are ignored. Pagination links were verified by
zero-overlap adjacent pages, a populated advertised final page, and an empty
page beyond it. The default therefore exhausts the advertised page range
(bounded by a 1,000-page safety guardrail) instead of truncating at 300.

Cards contain title, company, work mode, location, relative age, seniority,
salary when available, and a useful description. Details add exact date,
employment type, full description, applicant countries, and one-or-many
``jobLocation`` entries. Listing pages and details use bounded source-wide
pools; details are cached across the 12 role/location query instances.
"""

import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from math import ceil
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob

BUILTIN_SEARCH_URL = "https://builtin.com/jobs"
BUILTIN_REMOTE_URL = "https://builtin.com/jobs/remote"
BUILTIN_BASE_URL = "https://builtin.com"
USER_AGENT = "Mozilla/5.0 (compatible; job-atlas/0.1)"

PAGE_SIZE = 25
MAX_PAGE_GUARDRAIL = 1_000
LISTING_WORKERS = 8
DETAIL_WORKERS = 12

RELATIVE_AGE_RE = re.compile(
    r"(?:(\d+)|an?)\s+(minute|hour|day|week|month)s?\s+ago",
    re.IGNORECASE,
)
_AGE_SECONDS = {
    "minute": 60,
    "hour": 60 * 60,
    "day": 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
    "month": 30 * 24 * 60 * 60,
}
_COMPENSATION_LABEL_RE = re.compile(
    r"\b(?:salary|compensation|base pay|pay range|budget|ctc)\b",
    re.IGNORECASE,
)
_COMPENSATION_AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?|INR|\$|£|€)\s*\d|\d[\d,.]*\s*(?:LPA|lakhs?|[KM])\b",
    re.IGNORECASE,
)


def _parse_relative_age(text: str | None) -> datetime | None:
    if not text:
        return None
    lowered = text.lower()
    now = datetime.now(timezone.utc)
    if "yesterday" in lowered:
        return now - timedelta(days=1)
    if "today" in lowered or "just now" in lowered:
        return now
    match = RELATIVE_AGE_RE.search(lowered)
    if not match:
        return None
    quantity = int(match.group(1) or 1)
    return now - timedelta(seconds=quantity * _AGE_SECONDS[match.group(2).lower()])


def _parse_exact_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _detail_salary(description: str | None) -> str | None:
    """Keep an explicit compensation passage when the listing card omits it."""
    lines = [line.strip() for line in (description or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not _COMPENSATION_LABEL_RE.search(line):
            continue
        passage: list[str] = []
        for candidate in lines[index:index + 3]:
            passage.append(candidate)
            joined = " ".join(passage)
            if _COMPENSATION_AMOUNT_RE.search(joined):
                return joined[:255]
    return None


class BuiltInConnector(BaseConnector):
    source_name = "builtin"

    _listing_executor = ThreadPoolExecutor(
        max_workers=LISTING_WORKERS,
        thread_name_prefix="builtin-listing",
    )
    _detail_executor = ThreadPoolExecutor(
        max_workers=DETAIL_WORKERS,
        thread_name_prefix="builtin-detail",
    )
    _detail_lock = threading.Lock()
    _detail_cache: dict[str, str] = {}
    _detail_inflight: dict[str, threading.Event] = {}

    def __init__(
        self,
        search: str,
        max_results: int | None = None,
        days_since_updated: int = RECENCY_WINDOW_DAYS,
        location_mode: str = "bengaluru",
        fetch_descriptions: bool = True,
        *,
        location: str | None = None,
    ):
        if location_mode not in {"bengaluru", "remote_india", "city", "india"}:
            raise ValueError(
                "BuiltIn location_mode must be bengaluru, remote_india, city, or india"
            )
        if location_mode == "city" and not (location and location.strip()):
            raise ValueError("city mode requires location")
        if location_mode != "city" and location is not None:
            raise ValueError("location is only valid for city mode")
        self.location = location.strip() if location else None
        self.search = search
        # Explicit callers (for example the broad smoke script) may request a
        # small sample. Registry instances default to genuine exhaustion.
        self.max_results = max_results if max_results and max_results > 0 else None
        self.days_since_updated = days_since_updated
        self.location_mode = location_mode
        self.fetch_descriptions = fetch_descriptions

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self.make_pooled_client(headers={"User-Agent": USER_AGENT})
        return self._client

    def _search_url(self) -> str:
        if self.location_mode == "remote_india":
            return BUILTIN_REMOTE_URL
        return BUILTIN_SEARCH_URL

    def _search_params(self, page: int) -> dict[str, str | int]:
        params: dict[str, str | int] = {
            "search": self.search,
            "page": page,
            "country": "IND",
            "daysSinceUpdated": self.days_since_updated,
        }
        if self.location_mode == "bengaluru":
            params.update({"city": "Bengaluru", "state": "Karnataka"})
        elif self.location_mode == "city":
            # Verified 2026-09-07: city=Pune&country=IND resolves Pune in
            # jobBoardInit criteria without a state parameter. Unknown cities
            # can still return remote/promoted cards; the profile gate owns fit.
            params["city"] = self.location
        else:
            params["allLocations"] = "true"
        return params

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_page(self, page: int) -> str:
        response = self.client.get(self._search_url(), params=self._search_params(page))
        response.raise_for_status()
        return response.text

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_detail(self, url: str) -> str:
        response = self.client.get(url)
        response.raise_for_status()
        return response.text

    def _fetch_detail_cached(self, url: str) -> str:
        """Fetch a detail once across concurrent role/location instances."""
        cls = type(self)
        while True:
            with cls._detail_lock:
                cached = cls._detail_cache.get(url)
                if cached is not None:
                    return cached
                event = cls._detail_inflight.get(url)
                if event is None:
                    event = threading.Event()
                    cls._detail_inflight[url] = event
                    owner = True
                else:
                    owner = False

            if owner:
                try:
                    html = self._fetch_detail(url)
                    with cls._detail_lock:
                        cls._detail_cache[url] = html
                    return html
                finally:
                    with cls._detail_lock:
                        cls._detail_inflight.pop(url, None)
                        event.set()

            event.wait()

    @staticmethod
    def _last_page(html: str) -> int:
        soup = BeautifulSoup(html, "lxml")
        pages = [1]
        for anchor in soup.select('a[href*="page="]'):
            values = parse_qs(urlparse(anchor.get("href", "")).query).get("page", [])
            if values and values[0].isdigit():
                pages.append(int(values[0]))
        return min(max(pages), MAX_PAGE_GUARDRAIL)

    def _fetch_listing_pages(self) -> list[str]:
        first = self._fetch_page(1)
        last_page = self._last_page(first)
        if self.max_results is not None:
            last_page = min(last_page, max(1, ceil(self.max_results / PAGE_SIZE)))
        futures = {
            page: self._listing_executor.submit(self._fetch_page, page)
            for page in range(2, last_page + 1)
        }
        return [first, *(futures[page].result() for page in range(2, last_page + 1))]

    @staticmethod
    def _icon_value(card, icon_class: str) -> str | None:
        icon = card.select_one(f"i.{icon_class}")
        if not icon:
            return None
        icon_row = icon.find_parent("div")
        if not icon_row or not icon_row.parent:
            return None
        value = icon_row.parent.select_one("span")
        return value.get_text(strip=True) if value else None

    def _extract_location(self, card) -> str | None:
        return self._icon_value(card, "fa-location-dot")

    def _extract_remote_badge(self, card) -> str | None:
        return self._icon_value(card, "fa-house-building")

    def _extract_seniority(self, card) -> str | None:
        return self._icon_value(card, "fa-trophy")

    def _extract_salary(self, card) -> str | None:
        return self._icon_value(card, "fa-sack-dollar")

    @staticmethod
    def _extract_card_description(card) -> str | None:
        description = card.select_one("div.fs-sm.fw-regular.mb-md.text-gray-04")
        return description.get_text(" ", strip=True) if description else None

    @staticmethod
    def _extract_card_posted_at(card) -> datetime | None:
        icon = card.select_one("i.fa-clock")
        row = icon.find_parent("span") if icon else None
        return _parse_relative_age(row.get_text(" ", strip=True) if row else None)

    @staticmethod
    def _parse_detail(html: str) -> dict:
        """Return a schema.org JobPosting node, or an empty dict."""
        soup = BeautifulSoup(html, "lxml")
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
            except ValueError:
                continue
            if isinstance(data, list):
                nodes = data
            elif isinstance(data, dict) and data.get("@graph"):
                nodes = data["@graph"]
            else:
                nodes = [data]
            for node in nodes:
                if isinstance(node, dict) and node.get("@type") == "JobPosting":
                    return node
        return {}

    @staticmethod
    def _location_parts(posting: dict) -> list[str]:
        raw_locations = posting.get("jobLocation") or []
        if isinstance(raw_locations, dict):
            raw_locations = [raw_locations]
        locations = []
        for location in raw_locations:
            if not isinstance(location, dict):
                continue
            address = location.get("address")
            if not isinstance(address, dict):
                continue
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            text = ", ".join(str(part) for part in parts if part)
            if text and text not in locations:
                locations.append(text)
        return locations

    @staticmethod
    def _applicant_countries(posting: dict) -> list[str]:
        requirements = posting.get("applicantLocationRequirements") or []
        if isinstance(requirements, dict):
            requirements = [requirements]
        countries = []
        for requirement in requirements:
            if isinstance(requirement, dict) and requirement.get("name"):
                name = str(requirement["name"])
                if name not in countries:
                    countries.append(name)
        return countries

    @staticmethod
    def _prioritize_india(values: list[str]) -> list[str]:
        return sorted(
            values,
            key=lambda value: (
                not any(marker in value.lower() for marker in ("ind", "india", "bengaluru", "bangalore")),
                value,
            ),
        )

    def fetch(self) -> list[NormalizedJob]:
        cards_by_key: dict[str, dict] = {}
        for html in self._fetch_listing_pages():
            soup = BeautifulSoup(html, "lxml")
            for card in soup.select('div[data-id="job-card"]'):
                title_el = card.select_one('a[data-id="job-card-title"]')
                if not title_el:
                    continue
                company_el = card.select_one('a[data-id="company-title"]')
                job_id = title_el.get("data-builtin-track-job-id")
                relative_url = title_el.get("href")
                key = str(job_id or relative_url)
                if key in cards_by_key:
                    continue
                job_url = f"{BUILTIN_BASE_URL}{relative_url}" if relative_url else None
                remote_badge = self._extract_remote_badge(card)
                cards_by_key[key] = {
                    "job_id": job_id,
                    "job_url": job_url,
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else None,
                    "location": self._extract_location(card),
                    "remote_badge": remote_badge,
                    "seniority": self._extract_seniority(card),
                    "salary": self._extract_salary(card),
                    "description": self._extract_card_description(card),
                    "posted_at": self._extract_card_posted_at(card),
                }
                if self.max_results is not None and len(cards_by_key) >= self.max_results:
                    break
            if self.max_results is not None and len(cards_by_key) >= self.max_results:
                break

        cards = list(cards_by_key.values())
        detail_futures: dict[str, Future[str]] = {}
        if self.fetch_descriptions:
            for card in cards:
                job_url = card["job_url"]
                if job_url and job_url not in detail_futures:
                    detail_futures[job_url] = self._detail_executor.submit(
                        self._fetch_detail_cached,
                        job_url,
                    )

        normalized = []
        for card in cards:
            job_url = card["job_url"]
            posting = {}
            if job_url in detail_futures:
                try:
                    posting = self._parse_detail(detail_futures[job_url].result())
                except Exception:
                    posting = {}

            description_raw = card["description"]
            desc_html = posting.get("description")
            if desc_html:
                description_raw = BeautifulSoup(desc_html, "lxml").get_text(
                    separator="\n",
                    strip=True,
                )

            locations = self._prioritize_india(self._location_parts(posting))
            applicant_countries = self._prioritize_india(
                self._applicant_countries(posting)
            )
            location_raw = locations[0] if locations else card["location"]

            job_location_type = posting.get("jobLocationType")
            is_remote = self.location_mode == "remote_india"
            if job_location_type == "TELECOMMUTE":
                is_remote = True
            elif job_location_type and self.location_mode != "remote_india":
                is_remote = False
            elif self.location_mode != "remote_india" and card["remote_badge"]:
                is_remote = "remote" in card["remote_badge"].lower()

            if is_remote:
                scope_parts = applicant_countries or locations
                remote_scope = ", ".join(scope_parts) if scope_parts else card["remote_badge"]
            else:
                remote_scope = card["remote_badge"]

            company = card["company"] or "Unknown"
            organization = posting.get("hiringOrganization")
            if company == "Unknown" and isinstance(organization, dict):
                company = organization.get("name") or company

            raw_payload = {
                "job_id": card["job_id"],
                "url": job_url,
                "search": self.search,
                "location_mode": self.location_mode,
                "remote_badge": card["remote_badge"],
                "applicant_countries": applicant_countries,
                "detail_locations": locations,
            }
            if posting.get("industry"):
                raw_payload["industry_scraped"] = posting["industry"]
            if posting.get("jobBenefits"):
                raw_payload["company_benefits_scraped"] = posting["jobBenefits"]

            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=str(card["job_id"] or job_url),
                    title=card["title"][:255],
                    company_name_raw=company[:255],
                    location_raw=location_raw[:255] if location_raw else None,
                    is_remote=is_remote,
                    remote_scope=remote_scope[:255] if remote_scope else None,
                    employment_type=(
                        str(posting["employmentType"])[:255]
                        if posting.get("employmentType")
                        else None
                    ),
                    seniority=card["seniority"][:255] if card["seniority"] else None,
                    description_raw=description_raw,
                    salary_raw=(
                        card["salary"][:255]
                        if card["salary"]
                        else _detail_salary(description_raw)
                    ),
                    apply_url=job_url,
                    job_url=job_url,
                    posted_at=(
                        _parse_exact_date(posting.get("datePosted"))
                        or card["posted_at"]
                    ),
                    raw_payload=raw_payload,
                )
            )

        return normalized
