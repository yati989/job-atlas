"""LinkedIn India anonymous HTML connector.

The official India surface, ``in.linkedin.com/jobs/search``, serves an initial
60-card HTML document with stable job IDs, title, company, location, canonical
URL, and exact posting date. The separate anonymous continuation endpoint,
``/jobs-guest/jobs/api/seeMoreJobPostings/search``, returns ten positions from
the genuine zero-based ``start`` offset. The browser happens to request every
25 positions, but live checks proved that doing so skips jobs; complete
collection continues at offsets 60, 70, 80, and so on.

The connector collects up to 1,000 raw jobs per canonical role/location query.
Four listing workers overlap response latency. Listing and detail traffic use
one source-wide 1.2-second request-start coordinator across all 12 query
instances. A 2026-08-22 trace showed that the former independent 2.5-second
listing and 1.2-second detail clocks periodically started together and both
received HTTP 429; one clock removes those bursts while retaining a lower
combined request rate. Any 429 applies its cooldown to both request types. No
``geoId``, cookies, CSRF token, browser, proxy, or account is needed.

The connector returns listing fields first. The full-pipeline agent's
LinkedIn-only pre-gate enrichment checkpoint hydrates remote-query
role/seniority survivors and reasons over their complete descriptions.
The connector itself never classifies workplace policy and never calls an LLM
API. After the gate, the runner reuses complete details already in Postgres and asks
``hydrate_relevant_jobs()`` to fetch only remaining new or incomplete postings.
Hydration uses the small anonymous guest-detail fragment, which was
live-verified to contain the same description and four job-criteria fields as
the canonical page at roughly one-seventh the response size. Four detail
workers overlap latency; request starts remain source-wide paced at 1.2
seconds. Successful details are cached and in-flight IDs coordinated across
all 12 instances. Each detail response has a 45-second absolute wall-clock
deadline, and hydration abandons only the unfinished details after 60 seconds
without any completion so one slow response cannot stall the pipeline.

``keywords``, ``location``, and ``f_TPR`` (posted-time) materially change
inventory. Remote passes use a ``remote <role>`` keyword query with
``location=India`` because live runs showed that LinkedIn's ``f_WT=2`` filter
did not reliably return remote jobs. City passes use LinkedIn's ``location``
parameter with the requested Indian city, while India-wide passes use
``location=India`` without the ``remote`` keyword. Search is fuzzy, not strict, and
``sortBy=DD`` was silently ignored, so collection does not use a
relevance-drift stop. The query word ``remote`` is discovery input, not
workplace evidence.
"""

import asyncio
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob
from app.pipeline.relevance import first_failing_axis

LINKEDIN_INDIA_SEARCH_URL = "https://in.linkedin.com/jobs/search"
LINKEDIN_INDIA_CONTINUATION_URL = (
    "https://in.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
LINKEDIN_INDIA_DETAIL_URL = (
    "https://in.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
)
LINKEDIN_INITIAL_RESULT_COUNT = 60
LINKEDIN_CONTINUATION_RESULT_COUNT = 10
LINKEDIN_MAX_RESULTS = 200
LINKEDIN_POSITION_CEILING = 200
LINKEDIN_LISTING_WORKERS = 4
LINKEDIN_DETAIL_WORKERS = 4
LINKEDIN_LISTING_INTERVAL_SECONDS = 2.5
LINKEDIN_DETAIL_INTERVAL_SECONDS = 1.2
LINKEDIN_LISTING_MAX_ATTEMPTS = 6
LINKEDIN_DETAIL_MAX_ATTEMPTS = 3
LINKEDIN_FINAL_DETAIL_RETRY_DELAY_SECONDS = 10.0
LINKEDIN_DETAIL_RESPONSE_DEADLINE_SECONDS = 45.0
LINKEDIN_DETAIL_BATCH_NO_PROGRESS_SECONDS = 60.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_JOB_ID_RE = re.compile(r"urn:li:jobPosting:(\d+)")
logger = logging.getLogger("pipeline")


def _text(element) -> str | None:
    if element is None:
        return None
    value = element.get_text(" ", strip=True)
    return value or None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_listings(html: str, max_results: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    seen_ids: set[str] = set()
    for card in soup.select("div.base-search-card"):
        urn = card.get("data-entity-urn") or ""
        match = _JOB_ID_RE.search(urn)
        link = card.select_one("a.base-card__full-link")
        title = _text(card.select_one("h3.base-search-card__title"))
        href = link.get("href") if link else None
        if not match or not title or not href:
            continue
        job_id = match.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        date = card.select_one("time")
        rows.append(
            {
                "job_id": job_id,
                "title": title,
                "href": href,
                "company": _text(card.select_one("h4.base-search-card__subtitle")),
                "location": _text(card.select_one("span.job-search-card__location")),
                "posted_at_text": date.get("datetime") if date else None,
            }
        )
        if len(rows) >= max_results:
            break
    return rows


def _parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    detail: dict[str, str | None] = {
        "description_raw": _text(
            soup.select_one("div.show-more-less-html__markup")
            or soup.select_one("section.description div.description__text")
        ),
        "seniority": None,
        "employment_type": None,
        "industry_scraped": None,
        "job_function": None,
    }
    fields = {
        "seniority level": "seniority",
        "employment type": "employment_type",
        "industries": "industry_scraped",
        "job function": "job_function",
    }
    for row in soup.select("li.description__job-criteria-item"):
        label = _text(row.select_one("h3.description__job-criteria-subheader"))
        value = _text(row.select_one("span.description__job-criteria-text"))
        key = fields.get((label or "").lower())
        if key:
            detail[key] = value
    return detail


def _retry_after_seconds(response: httpx.Response, default: float) -> float:
    try:
        return float(response.headers.get("retry-after", default))
    except ValueError:
        return default


class LinkedInConnector(BaseConnector):
    source_name = "linkedin"
    reuse_stored_details = True

    _request_rate_lock = threading.Lock()
    _last_request_started = 0.0
    _request_cooldown_until = 0.0

    _detail_cache_lock = threading.Condition()
    _detail_cache: dict[str, dict] = {}
    _detail_inflight: set[str] = set()

    def __init__(
        self,
        search: str,
        location_mode: str,
        max_results: int = LINKEDIN_MAX_RESULTS,
        date_filter_days: int = RECENCY_WINDOW_DAYS,
        fetch_descriptions: bool = True,
        listing_workers: int = LINKEDIN_LISTING_WORKERS,
        detail_workers: int = LINKEDIN_DETAIL_WORKERS,
        listing_interval_seconds: float = LINKEDIN_LISTING_INTERVAL_SECONDS,
        detail_interval_seconds: float = LINKEDIN_DETAIL_INTERVAL_SECONDS,
        final_detail_retry_delay_seconds: float = (
            LINKEDIN_FINAL_DETAIL_RETRY_DELAY_SECONDS
        ),
        detail_response_deadline_seconds: float = (
            LINKEDIN_DETAIL_RESPONSE_DEADLINE_SECONDS
        ),
        detail_batch_no_progress_seconds: float = (
            LINKEDIN_DETAIL_BATCH_NO_PROGRESS_SECONDS
        ),
        transport: httpx.BaseTransport | None = None,
        *,
        location: str | None = None,
    ):
        if location_mode not in {"bengaluru", "remote_india", "city", "india"}:
            raise ValueError(
                "location_mode must be 'bengaluru', 'remote_india', 'city', or 'india'"
            )
        if location_mode == "city" and not (location and location.strip()):
            raise ValueError("location is required when location_mode is 'city'")
        self.search = search
        self.location_mode = location_mode
        self.location = location.strip() if location_mode == "city" else None
        self.max_results = min(max_results, LINKEDIN_MAX_RESULTS)
        self.date_filter_days = date_filter_days
        self.fetch_descriptions = fetch_descriptions
        self.listing_workers = listing_workers
        self.detail_workers = detail_workers
        self.listing_interval_seconds = listing_interval_seconds
        self.detail_interval_seconds = detail_interval_seconds
        # These used to control independent clocks. The faster of the two is
        # safe only when it is the sole source-wide clock: the previous pair
        # averaged 1.23 starts/s and could start simultaneously; this clock
        # averages at most 0.83 starts/s with the production defaults.
        self.request_interval_seconds = min(
            listing_interval_seconds, detail_interval_seconds
        )
        self.exact_recency_cutoff: datetime | None = None
        self.final_detail_retry_delay_seconds = final_detail_retry_delay_seconds
        self.detail_response_deadline_seconds = detail_response_deadline_seconds
        self.detail_batch_no_progress_seconds = detail_batch_no_progress_seconds
        self.transport = transport

    def _search_params(self) -> dict[str, str | int]:
        keywords = (
            f"remote {self.search}"
            if self.location_mode == "remote_india"
            else self.search
        )
        location = (
            "Bengaluru"
            if self.location_mode == "bengaluru"
            else self.location if self.location_mode == "city" else "India"
        )
        params: dict[str, str | int] = {
            "keywords": keywords,
            "location": location,
            "f_TPR": f"r{self.date_filter_days * 86400}",
        }
        return params

    def _build_search_url(self) -> str:
        return f"{LINKEDIN_INDIA_SEARCH_URL}?{urlencode(self._search_params())}"

    def _listing_client(self) -> httpx.Client:
        return httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"},
            follow_redirects=True,
            timeout=45,
            transport=self.transport,
        )

    def _detail_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-IN,en;q=0.9",
            },
            follow_redirects=True,
            timeout=45,
            transport=self.transport,
        )

    @classmethod
    def _wait_for_slot(
        cls,
        rate_lock: threading.Lock,
        last_started_name: str,
        cooldown_name: str,
        interval_seconds: float,
    ) -> None:
        with rate_lock:
            now = time.monotonic()
            target = max(
                getattr(cls, last_started_name) + interval_seconds,
                getattr(cls, cooldown_name),
            )
            if target > now:
                time.sleep(target - now)
            setattr(cls, last_started_name, time.monotonic())

    @classmethod
    def _apply_cooldown(
        cls,
        rate_lock: threading.Lock,
        cooldown_name: str,
        seconds: float,
    ) -> None:
        with rate_lock:
            setattr(
                cls,
                cooldown_name,
                max(getattr(cls, cooldown_name), time.monotonic() + seconds),
            )

    def _fetch_initial(self, client: httpx.Client) -> list[dict]:
        response = client.get(self._build_search_url())
        response.raise_for_status()
        return _parse_listings(
            response.text, min(self.max_results, LINKEDIN_INITIAL_RESULT_COUNT)
        )

    def _fetch_continuation(self, client: httpx.Client, start: int) -> list[dict]:
        for attempt in range(LINKEDIN_LISTING_MAX_ATTEMPTS):
            self._wait_for_slot(
                self._request_rate_lock,
                "_last_request_started",
                "_request_cooldown_until",
                self.request_interval_seconds,
            )
            response = client.get(
                LINKEDIN_INDIA_CONTINUATION_URL,
                params={**self._search_params(), "start": start},
            )
            if response.status_code == 200:
                return _parse_listings(response.text, LINKEDIN_CONTINUATION_RESULT_COUNT)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
            if response.status_code == 429:
                self._apply_cooldown(
                    self._request_rate_lock,
                    "_request_cooldown_until",
                    _retry_after_seconds(response, 10.0),
                )
            if attempt + 1 < LINKEDIN_LISTING_MAX_ATTEMPTS:
                time.sleep(min(2 ** (attempt + 1), 10))
        raise RuntimeError(f"LinkedIn continuation offset {start} failed after retries")

    def _request_detail(self, client: httpx.Client, job_id: str) -> dict:
        for attempt in range(LINKEDIN_DETAIL_MAX_ATTEMPTS):
            self._wait_for_slot(
                self._request_rate_lock,
                "_last_request_started",
                "_request_cooldown_until",
                self.request_interval_seconds,
            )
            response = client.get(LINKEDIN_INDIA_DETAIL_URL.format(job_id=job_id))
            if response.status_code == 200:
                return _parse_detail(response.text)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
            if response.status_code == 429:
                self._apply_cooldown(
                    self._request_rate_lock,
                    "_request_cooldown_until",
                    _retry_after_seconds(response, 2.0),
                )
            if attempt + 1 < LINKEDIN_DETAIL_MAX_ATTEMPTS:
                time.sleep(min(2 ** (attempt + 1), 10))
        return {}

    def _detail_for_job(self, client: httpx.Client, job_id: str) -> dict:
        with self._detail_cache_lock:
            while job_id in self._detail_inflight:
                self._detail_cache_lock.wait()
            cached = self._detail_cache.get(job_id)
            if cached is not None:
                return cached
            self._detail_inflight.add(job_id)

        try:
            detail = self._request_detail(client, job_id)
        finally:
            with self._detail_cache_lock:
                self._detail_inflight.discard(job_id)
                if "detail" in locals() and detail:
                    self._detail_cache[job_id] = detail
                self._detail_cache_lock.notify_all()
        return detail

    async def _wait_for_async_detail_slot(self) -> None:
        """Share the existing source-wide request clock without blocking asyncio."""
        connector_type = type(self)
        while True:
            with self._request_rate_lock:
                now = time.monotonic()
                target = max(
                    getattr(connector_type, "_last_request_started")
                    + self.request_interval_seconds,
                    getattr(connector_type, "_request_cooldown_until"),
                )
                if target <= now:
                    setattr(connector_type, "_last_request_started", now)
                    return
                delay = target - now
            await asyncio.sleep(delay)

    async def _request_detail_async(
        self, client: httpx.AsyncClient, job_id: str
    ) -> dict:
        for attempt in range(LINKEDIN_DETAIL_MAX_ATTEMPTS):
            await self._wait_for_async_detail_slot()
            try:
                async with asyncio.timeout(
                    self.detail_response_deadline_seconds
                ):
                    response = await client.get(
                        LINKEDIN_INDIA_DETAIL_URL.format(job_id=job_id)
                    )
            except TimeoutError:
                logger.warning(
                    "LinkedIn detail %s exceeded %.1fs wall-clock deadline",
                    job_id,
                    self.detail_response_deadline_seconds,
                )
                return {}

            if response.status_code == 200:
                return _parse_detail(response.text)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
            if response.status_code == 429:
                self._apply_cooldown(
                    self._request_rate_lock,
                    "_request_cooldown_until",
                    _retry_after_seconds(response, 2.0),
                )
            if attempt + 1 < LINKEDIN_DETAIL_MAX_ATTEMPTS:
                await asyncio.sleep(min(2 ** (attempt + 1), 10))
        return {}

    async def _detail_for_job_async(
        self, client: httpx.AsyncClient, job_id: str
    ) -> dict:
        claimed = False
        while not claimed:
            with self._detail_cache_lock:
                cached = self._detail_cache.get(job_id)
                if cached is not None:
                    return cached
                if job_id not in self._detail_inflight:
                    self._detail_inflight.add(job_id)
                    claimed = True
            if not claimed:
                await asyncio.sleep(0.05)

        try:
            detail = await self._request_detail_async(client, job_id)
        finally:
            with self._detail_cache_lock:
                self._detail_inflight.discard(job_id)
                if "detail" in locals() and detail:
                    self._detail_cache[job_id] = detail
                self._detail_cache_lock.notify_all()
        return detail

    async def _hydrate_detail_round(
        self,
        client: httpx.AsyncClient,
        jobs: list[NormalizedJob],
    ) -> list[tuple[NormalizedJob, dict]]:
        semaphore = asyncio.Semaphore(self.detail_workers)

        async def load(job: NormalizedJob) -> dict:
            async with semaphore:
                return await self._detail_for_job_async(
                    client, job.external_job_id
                )

        remaining = {
            asyncio.create_task(load(job)): job
            for job in jobs
        }
        completed: list[tuple[NormalizedJob, dict]] = []
        while remaining:
            done, pending = await asyncio.wait(
                remaining,
                timeout=self.detail_batch_no_progress_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                logger.warning(
                    "LinkedIn detail hydration made no progress for %.1fs; "
                    "marking %s remaining detail(s) failed",
                    self.detail_batch_no_progress_seconds,
                    len(pending),
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                break

            for task in done:
                job = remaining.pop(task)
                try:
                    detail = task.result()
                except Exception as exc:
                    logger.warning(
                        "LinkedIn detail %s failed: %s",
                        job.external_job_id,
                        exc,
                    )
                    detail = {}
                completed.append((job, detail))
        return completed

    def _normalize(self, row: dict) -> NormalizedJob:
        location = row.get("location")
        location_lower = (location or "").lower()
        href = row["href"]
        return NormalizedJob(
            source=self.source_name,
            external_job_id=row["job_id"],
            title=row["title"][:255],
            company_name_raw=(row.get("company") or "Unknown")[:255],
            location_raw=location[:255] if location else None,
            is_remote=("remote" in location_lower or "work from home" in location_lower),
            remote_scope=location,
            employment_type=row.get("employment_type"),
            seniority=row.get("seniority"),
            description_raw=row.get("description_raw"),
            salary_raw=None,
            apply_url=href,
            job_url=href,
            posted_at=_parse_datetime(row.get("posted_at_text")),
            raw_payload={
                **row,
                "query_location_mode": self.location_mode,
                "query_search": self.search,
            },
        )

    def _should_fetch_detail(self, row: dict) -> bool:
        return first_failing_axis(
            self._normalize(row), cutoff_at=self.exact_recency_cutoff
        ) is None

    def hydrate_relevant_jobs(
        self, jobs: list[NormalizedJob]
    ) -> dict[str, int]:
        """Hydrate centrally gated jobs; the runner excludes stored-complete IDs."""
        if not self.fetch_descriptions or not jobs:
            return {"eligible": len(jobs), "hydrated": 0, "failed": 0}

        pending = [job for job in jobs if not job.description_raw]
        hydrated = 0
        failed: list[NormalizedJob] = []

        def apply_detail(job: NormalizedJob, detail: dict) -> bool:
            if not detail.get("description_raw"):
                return False
            job.description_raw = detail.get("description_raw")
            job.seniority = detail.get("seniority") or job.seniority
            job.employment_type = (
                detail.get("employment_type") or job.employment_type
            )
            job.raw_payload.update(detail)
            return True

        async def hydrate() -> None:
            nonlocal hydrated, failed
            async with self._detail_client() as detail_client:
                for job, detail in await self._hydrate_detail_round(
                    detail_client, pending
                ):
                    if apply_detail(job, detail):
                        hydrated += 1
                    else:
                        failed.append(job)

                if failed and self.final_detail_retry_delay_seconds > 0:
                    await asyncio.sleep(self.final_detail_retry_delay_seconds)
                    retry_jobs = failed
                    failed = []
                    for job, detail in await self._hydrate_detail_round(
                        detail_client, retry_jobs
                    ):
                        if apply_detail(job, detail):
                            hydrated += 1
                        else:
                            failed.append(job)

        asyncio.run(hydrate())

        return {
            "eligible": len(jobs),
            "hydrated": hydrated,
            "failed": len(pending) - hydrated,
        }

    def fetch(self) -> list[NormalizedJob]:
        rows: list[dict] = []
        seen_ids: set[str] = set()

        with (
            self._listing_client() as listing_client,
            ThreadPoolExecutor(max_workers=self.listing_workers) as listing_pool,
        ):
            def accept(new_rows: list[dict]) -> None:
                for row in new_rows:
                    if len(rows) >= self.max_results:
                        return
                    if row["job_id"] in seen_ids:
                        continue
                    seen_ids.add(row["job_id"])
                    rows.append(row)

            accept(self._fetch_initial(listing_client))
            offsets = list(
                range(
                    LINKEDIN_INITIAL_RESULT_COUNT,
                    min(self.max_results, LINKEDIN_POSITION_CEILING),
                    LINKEDIN_CONTINUATION_RESULT_COUNT,
                )
            )
            for offset_index in range(0, len(offsets), self.listing_workers):
                starts = offsets[offset_index : offset_index + self.listing_workers]
                futures = {
                    listing_pool.submit(
                        self._fetch_continuation, listing_client, start
                    ): start
                    for start in starts
                }
                page_results: list[tuple[int, list[dict]]] = []
                for future in as_completed(futures):
                    page_results.append((futures[future], future.result()))
                batch_rows = [
                    row
                    for _start, page_rows in sorted(page_results)
                    for row in page_rows
                ]
                before = len(rows)
                accept(batch_rows)
                if len(rows) == before:
                    break

        return [self._normalize(row) for row in rows]
