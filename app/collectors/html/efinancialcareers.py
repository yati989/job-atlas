"""eFinancialCareers connector using the official site's search API.

Live audit on 2026-08-14 verified the canonical India route at
``/jobs/<role>/in-india`` and the Bengaluru route at
``/jobs/<role>/in-bengaluru%2C-karnataka%2C-india``. Their server-rendered
Angular state exposes the exact API request. A bare ``countryCode2=IN`` only
prioritizes India and spills into global inventory; adding the site's own
location name, precision, and coordinates makes the location filter exact.

The official OpenAPI schema documents ``startDate``. It was verified against
a nonsense value and now applies the global freshness window server-side.
The API accepts large pages, returns full descriptions, exact dates, and all
supported fields in one response, so no detail requests are needed. Six
canonical Bengaluru searches run through a bounded pool. Each query uses the
shared relevance-drift policy and its 300-result safety ceiling.

The site's native India+Remote filter is reproduced from its own page state
(``filters.workArrangementType=REMOTE``). It remains distinct from India-wide
and city queries, so callers can request a remote-only aggregate search.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
import re
from urllib.parse import parse_qs, urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.collectors.depth import NATIVE_SEARCH_MAX_RESULTS, relevance_drift_cursor
from app.config.categories import RECENCY_WINDOW_DAYS, SEARCH_TERMS, is_on_slice
from app.config.geography import city_aliases
from app.models.schemas import NormalizedJob


SEARCH_API_URL = "https://job-search-ui.efinancialcareers.com/v3/efc/jobs/search"
SITE_ORIGIN = "https://www.efinancialcareers.com"
PAGE_SIZE = NATIVE_SEARCH_MAX_RESULTS
SEARCH_WORKERS = 3
MAX_FIELD_LEN = 255

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

BENGALURU = {
    "locationPrecision": "City",
    "latitude": 12.96289,
    "longitude": 77.57754,
    "location": "Bengaluru, Karnataka, India",
    "countryCode2": "IN",
    "radius": 50,
    "radiusUnit": "mi",
}

INDIA = {
    "locationPrecision": "Country",
    "latitude": 20.59368,
    "longitude": 78.96288,
    "location": "India",
    "countryCode2": "IN",
    "radius": 50,
    "radiusUnit": "mi",
}

_LOCATION_MODES = {"bengaluru", "city", "india", "remote"}
_SEO_API_QUERY_RE = re.compile(
    r"https://job-search-ui\.efinancialcareers\.com/v\d+/efc/jobs/search\?"
    r"([^\"'<>]+)"
)


def _url_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise ValueError("location must contain a city name")
    return slug


def _normalized_city(value: str) -> str:
    return " ".join(value.casefold().split())


def _truncate(value: str | None, limit: int = MAX_FIELD_LEN) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


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


class EFinancialCareersConnector(BaseConnector):
    source_name = "efinancialcareers"

    def __init__(
        self,
        search_terms: list[str] | None = None,
        max_results_per_term: int = NATIVE_SEARCH_MAX_RESULTS,
        freshness_days: int = RECENCY_WINDOW_DAYS,
        search_workers: int = SEARCH_WORKERS,
        *,
        location_mode: str = "bengaluru",
        location: str | None = None,
    ):
        if location_mode not in _LOCATION_MODES:
            raise ValueError(
                "location_mode must be 'bengaluru', 'city', 'india', or 'remote'"
            )
        if location_mode == "city" and not (location and location.strip()):
            raise ValueError("location is required when location_mode is 'city'")
        self.search_terms = list(search_terms or SEARCH_TERMS)
        self.max_results_per_term = max_results_per_term
        self.freshness_days = freshness_days
        self.search_workers = search_workers
        self.location_mode = location_mode
        self.location = location.strip() if location_mode == "city" else None
        self._resolved_city_scope: dict[str, str | int | float] | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self.make_pooled_client(headers=HEADERS)
        return self._client

    def _resolve_city_scope(self) -> dict[str, str | int | float]:
        """Read the official SEO route's own API location scope at fetch time."""
        if self._resolved_city_scope is not None:
            return self._resolved_city_scope

        route = (
            f"{SITE_ORIGIN}/jobs/{_url_slug(self.search_terms[0])}"
            f"/in-{_url_slug(self.location or '')}"
        )
        response = self.client.get(route, timeout=60)
        response.raise_for_status()
        expected_path = urlparse(route).path.rstrip("/")
        actual_path = urlparse(str(response.url)).path.rstrip("/")
        if actual_path != expected_path:
            raise RuntimeError(
                "eFinancialCareers redirected away from city route "
                f"{expected_path!r} to {actual_path!r}"
            )
        match = _SEO_API_QUERY_RE.search(response.text)
        if match is None:
            raise RuntimeError(
                "eFinancialCareers city page omitted its native search query"
            )
        query = parse_qs(unescape("".join(match.group(1).split())))
        try:
            scope = {
                "locationPrecision": query["locationPrecision"][0],
                "latitude": float(query["latitude"][0]),
                "longitude": float(query["longitude"][0]),
                "location": query["location"][0],
                "countryCode2": query["countryCode2"][0],
                "radius": int(query["radius"][0]),
                "radiusUnit": query["radiusUnit"][0],
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "eFinancialCareers city page omitted a native location field"
            ) from exc
        if scope["locationPrecision"] != "City" or scope["countryCode2"] != "IN":
            raise RuntimeError(
                "eFinancialCareers city route did not resolve to an Indian city"
            )
        resolved_city = str(scope["location"]).split(",", 1)[0].strip()
        accepted_cities = {
            _normalized_city(alias) for alias in city_aliases(self.location or "")
        }
        if _normalized_city(resolved_city) not in accepted_cities:
            raise RuntimeError(
                "eFinancialCareers resolved city "
                f"{resolved_city!r} does not match requested city {self.location!r}"
            )
        self._resolved_city_scope = scope
        return scope

    def _location_scope(self) -> dict[str, str | int | float]:
        if self.location_mode == "city":
            return self._resolve_city_scope()
        if self.location_mode in {"india", "remote"}:
            return INDIA
        return BENGALURU

    def _params(self, term: str) -> dict[str, str | int | float]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.freshness_days)
        params: dict[str, str | int | float] = {
            "q": term,
            **self._location_scope(),
            "page": 1,
            "pageSize": self.max_results_per_term,
            "culture": "en",
            "includeRemote": "false",
            "includeUnspecifiedSalary": "true",
            "startDate": cutoff.date().isoformat(),
        }
        if self.location_mode == "remote":
            params["filters.workArrangementType"] = "REMOTE"
        return params

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_search(self, term: str) -> dict:
        response = self.client.get(
            SEARCH_API_URL,
            params=self._params(term),
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def _fetch_term(self, term: str) -> list[tuple[str, dict]]:
        payload = self._fetch_search(term)
        cursor = relevance_drift_cursor(max_results=self.max_results_per_term)
        rows: list[tuple[str, dict]] = []
        for item in payload.get("data") or []:
            title = str(item.get("title") or "").strip()
            if not title or not cursor.should_continue():
                break
            cursor.record_job(is_on_slice(title, term))
            rows.append((term, item))
        return rows

    def fetch(self) -> list[NormalizedJob]:
        # Resolve before starting term workers so one profile city produces one
        # authoritative native scope lookup, not competing resolver requests.
        if self.location_mode == "city":
            self._resolve_city_scope()
        workers = min(max(1, self.search_workers), len(self.search_terms))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="efc-search") as pool:
            term_rows = list(pool.map(self._fetch_term, self.search_terms))

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.freshness_days)
        normalized: list[NormalizedJob] = []
        seen_ids: set[str] = set()
        for rows in term_rows:
            for term, item in rows:
                job_id = str(item.get("jobId") or item.get("id") or "")
                title = str(item.get("title") or "").strip()
                if not job_id or not title or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                posted_at = _parse_posted(item.get("postedDate"))
                if posted_at is not None and posted_at < cutoff:
                    continue

                job_location = item.get("jobLocation") or {}
                location = job_location.get("displayName")
                work_arrangement = item.get("workArrangementType")
                is_remote = str(work_arrangement or "").lower() == "remote"
                company = item.get("companyName") or item.get("clientBrandName")
                details_path = item.get("detailsPageUrl") or ""
                if details_path.startswith("http"):
                    job_url = details_path
                elif details_path:
                    job_url = f"{SITE_ORIGIN}{details_path}"
                else:
                    job_url = None

                raw_payload = dict(item)
                raw_payload["matched_search_term"] = term
                normalized.append(
                    NormalizedJob(
                        source=self.source_name,
                        external_job_id=job_id,
                        title=_truncate(title) or title[:MAX_FIELD_LEN],
                        company_name_raw=_truncate(company) or "Unknown",
                        location_raw=_truncate(location),
                        is_remote=is_remote,
                        remote_scope=_truncate(location, 500) if is_remote else None,
                        employment_type=_truncate(
                            item.get("employmentType") or item.get("positionType")
                        ),
                        seniority=None,
                        description_raw=(
                            item.get("description")
                            or item.get("jobSummary")
                            or item.get("summary")
                        ),
                        salary_raw=_truncate(item.get("salary")),
                        apply_url=job_url,
                        job_url=job_url,
                        posted_at=posted_at,
                        raw_payload=raw_payload,
                    )
                )

        return normalized
