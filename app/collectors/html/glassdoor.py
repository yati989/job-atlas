"""Browser-free Glassdoor connector backed by the site's anonymous search BFF.

Live discovery on 2026-08-12 found that the initial HTML is Cloudflare-
blocked to plain HTTP, but the search page's own JSON endpoint is not. A
cookie-free POST to ``/job-search-next/bff/jobSearchResultsQuery`` returns
listing cards, full description text, and cursor metadata.

The BFF honors at most 100 rows per request (101 or more returns an empty
result), so the connector requests 100 and follows the server cursors until
actual exhaustion or the 1,000-row safety ceiling. It deliberately does not
use relevance-drift stopping: Glassdoor defaults to relevance ordering and
the complete cursor supply is cheap enough to pass through the central,
deterministic relevance gate.

The UI's "Last month" preset was observed as ``fromAge=30``. That observation
does not establish a server-side maximum: the connector passes the global
recency value directly. The remote pass also sends the exact frontend filter
``remoteWorkType=1``. No LLM is used here or by the relevance gate.
"""

from datetime import datetime, timedelta, timezone

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob

GLASSDOOR_SEARCH_API = (
    "https://www.glassdoor.co.in/job-search-next/bff/jobSearchResultsQuery"
)
GLASSDOOR_LOCATION_RESOLVER_API = (
    "https://www.glassdoor.co.in/findPopularLocationAjax.htm"
)
GLASSDOOR_PAGE_SIZE = 100  # Live BFF returns an empty result for 101+.
GLASSDOOR_MAX_RESULTS = 500
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_MAX_FIELD_LEN = 255

LOCATION_SCOPES = {
    "remote_india": {
        "loc_slug": "india",
        "loc_code": "N115",
        "location_id": 115,
        "location_type": "COUNTRY",
        "remote": True,
    },
    "bengaluru": {
        "loc_slug": "bengaluru-india",
        "loc_code": "C2940587",
        "location_id": 2940587,
        "location_type": "CITY",
        "remote": False,
    },
    "india": {
        "loc_slug": "india",
        "loc_code": "N115",
        "location_id": 115,
        "location_type": "COUNTRY",
        "remote": False,
    },
}


def _truncate(value: str | None, limit: int = _MAX_FIELD_LEN) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


def _age_days_to_datetime(value) -> datetime | None:
    if not isinstance(value, (int, float)) or value < 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=value)


def _description_text(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        text = "\n".join(str(part).strip() for part in value if str(part).strip())
        return text or None
    return None


def _normalized_location_name(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _salary_text(header: dict) -> str | None:
    pay = header.get("payPeriodAdjustedPay")
    if pay in (None, "", {}):
        return None
    if not isinstance(pay, dict):
        return _truncate(str(pay))

    numeric_values = [
        value for value in pay.values() if isinstance(value, (int, float)) and value > 0
    ]
    if not numeric_values:
        return None
    low, high = min(numeric_values), max(numeric_values)
    def format_amount(value: int | float) -> str:
        return f"{value:,.2f}".rstrip("0").rstrip(".")

    amount = (
        format_amount(low)
        if low == high
        else f"{format_amount(low)}-{format_amount(high)}"
    )
    currency = header.get("payCurrency") or ""
    period = header.get("payPeriod") or ""
    return _truncate(" ".join(part for part in (currency, amount, period) if part))


class GlassdoorConnector(BaseConnector):
    source_name = "glassdoor"

    def __init__(
        self,
        search: str,
        location_mode: str,
        max_results: int = GLASSDOOR_MAX_RESULTS,
        page_size: int = GLASSDOOR_PAGE_SIZE,
        date_filter_days: int = RECENCY_WINDOW_DAYS,
        *,
        location: str | None = None,
    ):
        if location_mode not in {*LOCATION_SCOPES, "city"}:
            raise ValueError(
                "location_mode must be 'bengaluru', 'remote_india', 'city', or 'india'"
            )
        normalized_location = location.strip() if location else None
        if location_mode == "city" and not normalized_location:
            raise ValueError("location is required when location_mode is 'city'")
        if location_mode != "city" and normalized_location:
            raise ValueError("location is supported only when location_mode is 'city'")
        self.search = search
        self.location_mode = location_mode
        self.location = normalized_location
        self.max_results = max_results
        self.page_size = page_size
        self.date_filter_days = date_filter_days
        # Glassdoor rejects httpx's transport fingerprint with a 403 while
        # accepting the same cookie-free payload through requests. Session
        # keeps the cursor walk connection-pooled.
        self._session = requests.Session()
        self._resolved_city_scope: dict | None = None

    def _resolve_city_scope(self) -> dict:
        """Resolve a requested Indian city to Glassdoor's native location ID.

        Location IDs are supplied by Glassdoor rather than guessed. A resolver
        failure intentionally aborts this city pass: using the country ID would
        silently broaden an invalid city request to all of India.
        """
        if self._resolved_city_scope is not None:
            return self._resolved_city_scope

        response = self._session.get(
            GLASSDOOR_LOCATION_RESOLVER_API,
            params={"maxLocationsToReturn": 10, "term": self.location},
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            timeout=45,
        )
        # The location endpoint can require the short-lived Cloudflare cookie
        # that Glassdoor issues to its search BFF. Prime that cookie with one
        # discarded India result, then resolve the requested city again. The
        # actual city search below always uses the resolved city ID; this
        # primer is never treated as a city-search result.
        if getattr(response, "status_code", None) == 403:
            self._prime_location_resolver_session()
            response = self._session.get(
                GLASSDOOR_LOCATION_RESOLVER_API,
                params={"maxLocationsToReturn": 10, "term": self.location},
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
                timeout=45,
            )
        response.raise_for_status()
        candidates = response.json()
        requested = _normalized_location_name(self.location)
        if not isinstance(candidates, list):
            candidates = []

        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("locationType") != "C":
                continue
            location_name = str(
                candidate.get("locationName")
                or candidate.get("label")
                or candidate.get("longName")
                or ""
            )
            parts = [part.strip() for part in location_name.split(",") if part.strip()]
            city_label = parts[0] if parts else ""
            if city_label.endswith(")") and " (" in city_label:
                city_label = city_label.rsplit(" (", 1)[0]
            country_label = parts[-1] if parts else ""
            if country_label.endswith(")") and " (" in country_label:
                country_label = country_label.rsplit(" (", 1)[1][:-1]
            city_name = _normalized_location_name(candidate.get("city") or city_label)
            country_name = _normalized_location_name(
                candidate.get("countryName") or country_label
            )
            if city_name != requested or country_name != "india":
                continue
            try:
                location_id = int(candidate["locationId"])
            except (KeyError, TypeError, ValueError):
                continue
            city_slug = self.location.lower().replace(" ", "-")
            self._resolved_city_scope = {
                "loc_slug": city_slug,
                "loc_code": f"C{location_id}",
                "location_id": location_id,
                "location_type": "CITY",
                "remote": False,
            }
            return self._resolved_city_scope

        raise ValueError(f"Glassdoor could not resolve Indian city {self.location!r}")

    def _prime_location_resolver_session(self) -> None:
        """Acquire the BFF session cookie needed for location autocomplete."""
        scope = LOCATION_SCOPES["india"]
        keyword = self.search.strip()
        keyword_slug = keyword.lower().replace(" ", "-")
        loc_slug = scope["loc_slug"]
        keyword_start = len(loc_slug) + 1
        keyword_end = keyword_start + len(keyword_slug)
        parameter = f"IL.0,{len(loc_slug)}_I{scope['loc_code']}_KO{keyword_start},{keyword_end}"
        query_string = f"fromAge={self.date_filter_days}&countryRedirect=true"
        search_url = (
            f"https://www.glassdoor.co.in/Job/{loc_slug}-{keyword_slug}-jobs-SRCH_"
            f"{parameter}.htm?{query_string}"
        )
        self._post_json(
            {
                "excludeJobListingIds": [],
                "filterParams": [
                    {"filterKey": "fromAge", "values": str(self.date_filter_days)}
                ],
                "includeIndeedJobAttributes": True,
                "keyword": keyword,
                "locationId": scope["location_id"],
                "locationType": scope["location_type"],
                "numJobsToShow": 1,
                "originalPageUrl": search_url,
                "pageCursor": None,
                "pageNumber": 1,
                "pageType": "SERP",
                "parameterUrlInput": parameter,
                "queryString": query_string,
                "seoFriendlyUrlInput": f"{loc_slug}-{keyword_slug}-jobs",
                "seoUrl": True,
            },
            search_url,
        )

    def _location_scope(self) -> dict:
        if self.location_mode == "city":
            return self._resolve_city_scope()
        return LOCATION_SCOPES[self.location_mode]

    def _search_context(self) -> tuple[str, dict]:
        scope = self._location_scope()
        keyword = self.search.strip()
        keyword_slug = keyword.lower().replace(" ", "-")
        loc_slug = scope["loc_slug"]
        keyword_start = len(loc_slug) + 1
        keyword_end = keyword_start + len(keyword_slug)
        parameter = (
            f"IL.0,{len(loc_slug)}_I{scope['loc_code']}_"
            f"KO{keyword_start},{keyword_end}"
        )
        seo_input = f"{loc_slug}-{keyword_slug}-jobs"

        query_parts: list[str] = []
        filter_params: list[dict[str, str]] = []
        if scope["remote"]:
            query_parts.append("remoteWorkType=1")
            filter_params.append({"filterKey": "remoteWorkType", "values": "1"})
        query_parts.append(f"fromAge={self.date_filter_days}")
        filter_params.append(
            {"filterKey": "fromAge", "values": str(self.date_filter_days)}
        )
        query_parts.append("countryRedirect=true")
        query_string = "&".join(query_parts)
        search_url = (
            f"https://www.glassdoor.co.in/Job/{seo_input}-SRCH_{parameter}.htm"
            f"?{query_string}"
        )

        return search_url, {
            "excludeJobListingIds": [],
            "filterParams": filter_params,
            "includeIndeedJobAttributes": True,
            "keyword": keyword,
            "locationId": scope["location_id"],
            "locationType": scope["location_type"],
            "numJobsToShow": self.page_size,
            "originalPageUrl": search_url,
            "pageType": "SERP",
            "parameterUrlInput": parameter,
            "queryString": query_string,
            "seoFriendlyUrlInput": seo_input,
            "seoUrl": True,
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _post_json(self, payload: dict, referer: str) -> dict:
        response = self._session.post(
            GLASSDOOR_SEARCH_API,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://www.glassdoor.co.in",
                "Referer": referer,
                "User-Agent": _USER_AGENT,
            },
            timeout=45,
        )
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {}

    def _list_jobs(self) -> list[dict]:
        referer, base_payload = self._search_context()
        results: list[dict] = []
        seen_ids: set[str] = set()
        page_number = 1
        page_cursor = None

        while len(results) < self.max_results:
            payload = {
                **base_payload,
                "pageCursor": page_cursor,
                "pageNumber": page_number,
            }
            try:
                body = self._post_json(payload, referer)
            except Exception:
                break
            listing_data = ((body.get("data") or {}).get("jobListings") or {})
            jobs = listing_data.get("jobListings") or []
            if not isinstance(jobs, list) or not jobs:
                break

            new_count = 0
            for item in jobs:
                job = ((item.get("jobview") or {}).get("job") or {})
                job_id = str(job.get("listingId") or "")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                results.append(item)
                new_count += 1
                if len(results) >= self.max_results:
                    break

            if len(results) >= self.max_results or len(jobs) < self.page_size or new_count == 0:
                break

            next_page = page_number + 1
            cursors = listing_data.get("paginationCursors") or []
            next_cursor = next(
                (
                    item.get("cursor")
                    for item in cursors
                    if item.get("pageNumber") == next_page
                ),
                None,
            )
            if not next_cursor:
                break
            page_number = next_page
            page_cursor = next_cursor

        return results

    def fetch(self) -> list[NormalizedJob]:
        normalized: list[NormalizedJob] = []
        for item in self._list_jobs():
            jobview = item.get("jobview") or {}
            header = jobview.get("header") or {}
            job = jobview.get("job") or {}
            overview = jobview.get("overview") or {}

            job_id = str(job.get("listingId") or "")
            title = header.get("jobTitleText") or job.get("jobTitleText")
            job_url = header.get("seoJobLink")
            if not job_id or not title or not job_url:
                continue

            employer = header.get("employer") or {}
            company = (
                header.get("employerNameFromSearch")
                or employer.get("shortName")
                or overview.get("shortName")
                or "Unknown"
            )
            location = header.get("locationName")
            is_remote = self.location_mode == "remote_india" or "remote" in (
                location or ""
            ).lower()

            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=job_id,
                    title=_truncate(title) or str(title)[:_MAX_FIELD_LEN],
                    company_name_raw=_truncate(company) or "Unknown",
                    location_raw=_truncate(location),
                    is_remote=is_remote,
                    remote_scope=_truncate(location),
                    employment_type=None,
                    seniority=None,
                    description_raw=_description_text(job.get("descriptionFragmentsText")),
                    salary_raw=_salary_text(header),
                    apply_url=job_url,
                    job_url=job_url,
                    posted_at=_age_days_to_datetime(header.get("ageInDays")),
                    raw_payload=item,
                )
            )

        return normalized
