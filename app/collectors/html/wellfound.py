"""Browser-free Wellfound connector using server-rendered Next.js data.

Wellfound has no India-specific domain; its official India surface is the
global site's ``/location/india`` family. Live checks on 2026-08-13 showed
that plain HTTP now returns complete ``__NEXT_DATA__`` with an Apollo cache,
including full descriptions, exact posting timestamps, company references,
locations, remote configuration, experience, job type, and compensation.

The SEO endpoint fixes its own page size (``perPage`` reports 20 and ignores
``perPage``, ``pageSize``, and ``limit`` URL overrides). Genuine ``?page=N``
pagination is therefore fetched concurrently through the reported page count,
up to the source's 1,000-job cap. No relevance-drift stop is used.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import threading
import time
from typing import Callable
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
import requests
from app.collectors.base import BaseConnector, PartialFetchError
from app.collectors.depth import DepthCursor
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob

WELLFOUND_BASE_URL = "https://wellfound.com"
WELLFOUND_MAX_RESULTS = 500
WELLFOUND_PAGE_WORKERS = 2
WELLFOUND_SOURCE_MAX_REQUESTS = 1
WELLFOUND_MIN_REQUEST_INTERVAL_S = 1.0
WELLFOUND_MAX_REQUEST_ATTEMPTS = 3
WELLFOUND_MAX_SOURCE_WAIT_S = 30.0

ROLE_SLUGS = {
    "data scientist": "data-scientist",
    "data analyst": "data-analyst",
    "data engineer": "data-engineer",
    "machine learning engineer": "machine-learning-engineer",
    "ai engineer": "ai-engineer",
    # `credit-risk` redirects to the broad location page exactly like a
    # nonsense role. `risk-analyst` is a genuine role route.
    "credit risk": "risk-analyst",
}

LOCATION_SLUGS = {
    "bengaluru": "bangalore-urban",
}
_CITY_SLUG_ALIASES = {
    "bangalore": "bangalore-urban",
    "bengaluru": "bangalore-urban",
}
LOCATION_MODES = {"bengaluru", "remote_india", "city", "india"}

_REMOTE_SCOPE_LABELS = {
    "REMOTE": "Remote",
    "ONSITE_OR_REMOTE": "Onsite or remote",
    "ONSITE": "Onsite",
}
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
}


class WellfoundCircuitOpen(RuntimeError):
    """The process-wide Wellfound wait budget was exhausted for this run."""


class WellfoundRequestCoordinator:
    """Coordinate every Wellfound instance in one ingestion process.

    Request starts are paced across connector instances, concurrent HTTP calls
    are bounded, and a 429 extends one shared cooldown.  Cooldown extensions
    consume a bounded process-run budget; a server delay beyond that budget
    opens the circuit instead of stalling unrelated ingestion work.
    """

    def __init__(
        self,
        *,
        max_in_flight: int = WELLFOUND_SOURCE_MAX_REQUESTS,
        min_interval_s: float = WELLFOUND_MIN_REQUEST_INTERVAL_S,
        max_attempts: int = WELLFOUND_MAX_REQUEST_ATTEMPTS,
        max_source_wait_s: float = WELLFOUND_MAX_SOURCE_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._semaphore = threading.BoundedSemaphore(max_in_flight)
        self._max_in_flight_limit = max_in_flight
        self._min_interval_s = max(0.0, min_interval_s)
        self._max_attempts = max(1, max_attempts)
        self._max_source_wait_s = max(0.0, max_source_wait_s)
        self._clock = clock
        self._sleeper = sleeper
        self._utcnow = utcnow
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._cooldown_committed_s = 0.0
        self._circuit_error: str | None = None
        self._http_requests_started = 0
        self._in_flight = 0
        self._max_in_flight_observed = 0
        self._cooldown_events = 0

    @staticmethod
    def retry_after_seconds(value: str | None, *, now: datetime) -> float | None:
        """Parse Retry-After delta-seconds or an RFC HTTP date."""
        if not value:
            return None
        try:
            seconds = float(value.strip())
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - now.astimezone(timezone.utc)).total_seconds())
        return max(0.0, seconds)

    def _wait_for_turn(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                if self._circuit_error is not None:
                    if now < self._cooldown_until:
                        raise WellfoundCircuitOpen(self._circuit_error)
                    # The source cooldown elapsed while unrelated connectors
                    # continued.  Permit one paced half-open probe instead of
                    # poisoning every runner-level retry for the process.
                    self._circuit_error = None
                    self._cooldown_committed_s = 0.0
                delay = max(self._next_request_at, self._cooldown_until) - now
                if delay <= 0:
                    self._next_request_at = now + self._min_interval_s
                    return
            self._sleeper(delay)

    def _register_cooldown(self, delay_s: float) -> None:
        with self._lock:
            self._cooldown_events += 1
            now = self._clock()
            proposed_until = now + max(0.0, delay_s)
            extension = max(
                0.0, proposed_until - max(self._cooldown_until, now)
            )
            proposed_total = self._cooldown_committed_s + extension
            if proposed_total > self._max_source_wait_s:
                self._cooldown_until = max(self._cooldown_until, proposed_until)
                self._circuit_error = (
                    "Wellfound cooldown budget exhausted; requests stopped for this run"
                )
                raise WellfoundCircuitOpen(self._circuit_error)
            self._cooldown_until = max(self._cooldown_until, proposed_until)
            self._cooldown_committed_s = proposed_total

    def snapshot(self) -> dict[str, int | float | bool]:
        """Return query-free request-policy metrics for source verification."""
        with self._lock:
            return {
                "http_requests_started": self._http_requests_started,
                "max_in_flight_limit": self._max_in_flight_limit,
                "max_in_flight_observed": self._max_in_flight_observed,
                "cooldown_events": self._cooldown_events,
                "cooldown_committed_s": round(self._cooldown_committed_s, 3),
                "circuit_open": self._circuit_error is not None,
            }

    def request(self, send: Callable[[], requests.Response]) -> requests.Response:
        last_response: requests.Response | None = None
        for attempt in range(1, self._max_attempts + 1):
            with self._semaphore:
                self._wait_for_turn()
                with self._lock:
                    self._http_requests_started += 1
                    self._in_flight += 1
                    self._max_in_flight_observed = max(
                        self._max_in_flight_observed, self._in_flight
                    )
                try:
                    response = send()
                finally:
                    with self._lock:
                        self._in_flight -= 1
            if response.status_code != 429:
                return response

            last_response = response
            retry_after = self.retry_after_seconds(
                response.headers.get("Retry-After"), now=self._utcnow()
            )
            fallback = min(8.0, float(2**attempt))
            self._register_cooldown(retry_after if retry_after is not None else fallback)

        assert last_response is not None
        last_response.raise_for_status()
        raise AssertionError("unreachable")


_WELLFOUND_REQUEST_COORDINATOR = WellfoundRequestCoordinator()


def _refs(value: list[dict] | None) -> list[str]:
    return [item["__ref"] for item in value or [] if item.get("__ref")]


def _joined(values: list[str | None] | None) -> str | None:
    clean = list(
        dict.fromkeys(
            value.strip() for value in values or [] if value and value.strip()
        )
    )
    return ", ".join(clean) or None


class _PartialScrapeError(RuntimeError):
    def __init__(self, items: list[dict], cause: Exception):
        super().__init__(f"Wellfound stopped after {len(items)} completed jobs: {cause}")
        self.items = items
        self.cause = cause


class WellfoundConnector(BaseConnector):
    source_name = "wellfound"

    def __init__(
        self,
        search: str,
        location_mode: str,
        max_results: int = WELLFOUND_MAX_RESULTS,
        page_workers: int = WELLFOUND_PAGE_WORKERS,
        recency_window_days: int = RECENCY_WINDOW_DAYS,
        request_coordinator: WellfoundRequestCoordinator | None = None,
        *,
        location: str | None = None,
    ):
        if search not in ROLE_SLUGS:
            raise ValueError(f"Unsupported Wellfound search term: {search!r}")
        if location_mode not in LOCATION_MODES:
            raise ValueError(
                "location_mode must be 'bengaluru', 'remote_india', 'city', or 'india'"
            )
        if location_mode == "city" and not (location and location.strip()):
            raise ValueError("location is required when location_mode is 'city'")
        self.search = search
        self.location_mode = location_mode
        self.location = location.strip() if location_mode == "city" else None
        self.location_slug = (
            self._city_slug(self.location) if self.location is not None else None
        )
        self.max_results = min(max_results, WELLFOUND_MAX_RESULTS)
        self.page_workers = page_workers
        self.recency_window_days = recency_window_days
        self.request_coordinator = (
            request_coordinator or _WELLFOUND_REQUEST_COORDINATOR
        )

    @staticmethod
    def _city_slug(location: str) -> str:
        normalized = location.casefold()
        if normalized in _CITY_SLUG_ALIASES:
            return _CITY_SLUG_ALIASES[normalized]
        slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
        if not slug:
            raise ValueError("location must contain a city name")
        return slug

    def _build_url(self, page: int = 1) -> str:
        role = ROLE_SLUGS[self.search]
        if self.location_mode == "remote_india":
            # Wellfound's genuine remote role route is global. The central
            # location gate retains India-eligible/ambiguous remote jobs and
            # rejects explicitly foreign-only jobs.
            base = f"{WELLFOUND_BASE_URL}/role/r/{role}"
        else:
            location = (
                self.location_slug
                if self.location_mode == "city"
                else "india"
                if self.location_mode == "india"
                else LOCATION_SLUGS[self.location_mode]
            )
            base = f"{WELLFOUND_BASE_URL}/role/l/{role}/{location}"
        return base if page == 1 else f"{base}?{urlencode({'page': page})}"

    def request_observability(self) -> dict[str, int | float | bool]:
        """Query-free shared-coordinator metrics for source-scoped QA."""
        return self.request_coordinator.snapshot()

    def _fetch_page(self, page: int) -> tuple[list[dict], int]:
        response = self.request_coordinator.request(
            lambda: requests.get(
                self._build_url(page), headers=_HEADERS, timeout=30
            )
        )
        response.raise_for_status()
        if self.location_mode == "city":
            expected_path = urlparse(self._build_url(page)).path.rstrip("/")
            actual_path = urlparse(str(response.url)).path.rstrip("/")
            if actual_path != expected_path:
                raise RuntimeError(
                    "Wellfound redirected away from city route "
                    f"{expected_path!r} to {actual_path!r}"
                )

        node = BeautifulSoup(response.text, "html.parser").select_one(
            "#__NEXT_DATA__"
        )
        if node is None or not node.string:
            raise RuntimeError("Wellfound response omitted __NEXT_DATA__")

        payload = json.loads(node.string)
        data = payload["props"]["pageProps"]["apolloState"]["data"]
        talent = data["ROOT_QUERY"]["talent"]
        result = next(
            value
            for key, value in talent.items()
            if "seoLandingPageJobSearchResults" in key
        )
        page_count = max(1, int(result.get("pageCount") or 1))

        jobs: list[dict] = []
        for startup_ref in _refs(result.get("startups")):
            startup = data.get(startup_ref) or {}
            for job_ref in _refs(startup.get("highlightedJobListings")):
                job = data.get(job_ref)
                if job:
                    jobs.append({**job, "company": startup.get("name")})
        return jobs, page_count

    def _merge_batches(self, batches: list[list[dict]]) -> list[dict]:
        seen: set[str] = set()
        results: list[dict] = []
        for batch in batches:
            for item in batch:
                job_id = str(item.get("id") or "")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                if self.location_mode == "remote_india" and not item.get("remote"):
                    continue
                results.append(item)
                if len(results) >= self.max_results:
                    return results
        return results

    def _scrape(self) -> list[dict]:
        first_page, page_count = self._fetch_page(1)
        cursor = DepthCursor(max_pages=page_count, max_results=self.max_results)
        cursor.record_page(len(first_page))
        batches_by_page: dict[int, list[dict]] = {1: first_page}
        next_page = 2
        terminal_error: Exception | None = None

        while next_page <= page_count and cursor.should_continue():
            wave: list[int] = []
            while (
                next_page <= page_count
                and len(wave) < self.page_workers
                and cursor.pages_fetched + len(wave) < cursor.max_pages
            ):
                wave.append(next_page)
                next_page += 1

            with ThreadPoolExecutor(max_workers=self.page_workers) as executor:
                futures = {
                    executor.submit(self._fetch_page, page): page for page in wave
                }
                for future in as_completed(futures):
                    page = futures[future]
                    try:
                        batch, _ = future.result()
                    except Exception as exc:
                        terminal_error = terminal_error or exc
                    else:
                        batches_by_page[page] = batch

            for page in sorted(wave):
                if page in batches_by_page:
                    cursor.record_page(len(batches_by_page[page]))
            if terminal_error is not None:
                break

        results = self._merge_batches(
            [batches_by_page[page] for page in sorted(batches_by_page)]
        )
        if terminal_error is not None:
            raise _PartialScrapeError(results, terminal_error)
        return results

    @staticmethod
    def _normalize(item: dict) -> NormalizedJob:
        job_id = str(item["id"])
        slug = item.get("slug") or "job"
        job_url = f"{WELLFOUND_BASE_URL}/jobs/{job_id}-{slug}"
        location = _joined(item.get("locationNames"))
        accepted_remote = _joined(item.get("acceptedRemoteLocationNames"))
        remote_config = item.get("remoteConfig") or {}
        remote_label = _REMOTE_SCOPE_LABELS.get(remote_config.get("kind"))
        remote_scope = _joined([remote_label, accepted_remote])
        timestamp = item.get("liveStartAt")
        posted_at = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if isinstance(timestamp, (int, float))
            else None
        )
        compensation = (item.get("compensation") or "").strip() or None

        return NormalizedJob(
            source="wellfound",
            external_job_id=job_id,
            title=item.get("title") or item.get("primaryRoleTitle") or "Unknown",
            company_name_raw=item.get("company") or "Unknown",
            location_raw=location,
            is_remote=bool(item.get("remote")),
            remote_scope=remote_scope,
            employment_type=item.get("jobType"),
            seniority=None,
            description_raw=item.get("description"),
            salary_raw=compensation,
            apply_url=job_url,
            job_url=job_url,
            posted_at=posted_at,
            raw_payload=item,
        )

    def fetch(self) -> list[NormalizedJob]:
        try:
            items = self._scrape()
        except _PartialScrapeError as exc:
            jobs = [self._normalize(item) for item in exc.items]
            raise PartialFetchError(str(exc), jobs, exc.cause) from exc.cause
        return [self._normalize(item) for item in items]
