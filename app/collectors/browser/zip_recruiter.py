"""ZipRecruiter India HTML connector.

The official India board is ``ziprecruiter.in`` and is materially different
from the US ``ziprecruiter.com`` site. Live discovery on 2026-08-12 confirmed
that ``/jobs/search`` renders server-side ``li.job-listing`` rows containing
title, company, location, description snippet, stable detail URL, and posted
day/month. A real query returns jobs while a nonsense query returns zero.

Plain HTTP receives a Cloudflare challenge. Fresh anonymous headless Chrome
contexts successfully return the listing HTML, so no visible browser and no
personal or persisted cookies are required.

Native pagination is ``page=N`` and its ``per_page`` parameter was live-tested
with 1,000 rows in one response; 5,000 rows in one response returns HTTP 500.
The connector therefore fetches five 1,000-row pages concurrently in isolated
contexts. The site advertised thousands of Bengaluru
ML-engineer results. There is
no native date-posted filter: ``d`` is the distance field, and visible dates
are not ordered newest-first. The connector requests up to 5,000 raw jobs and
applies no relevance-drift stop; the central deterministic
gate handles role, location, seniority, and recency after full collection.

The Bengaluru location parameter is genuine. ZipRecruiter India also accepts
``remote=full`` as its native remote-only filter; the tested data-scientist
remote searches currently return zero, but the six-query remote pass is kept
so future inventory is collected. No LLM or token-billed judgment is used.

On 2026-09-07, a fresh anonymous headless context returned 1,000 Pune jobs
for ``l=Pune`` and zero for a nonexistent city, confirming that arbitrary
Indian city values reach the native location filter.
"""

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from app.collectors.base import BaseConnector
from app.models.schemas import NormalizedJob

ZIPRECRUITER_INDIA_SEARCH_URL = "https://www.ziprecruiter.in/jobs/search"
ZIPRECRUITER_PAGE_SIZE = 1000
ZIPRECRUITER_MAX_RESULTS = 5000
ZIPRECRUITER_MAX_PAGES = ZIPRECRUITER_MAX_RESULTS // ZIPRECRUITER_PAGE_SIZE

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_JOB_ID_RE = re.compile(r"/jobs/(\d+)-")


def _truncate(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:max_len] if value else None


def _job_id(href: str | None) -> str | None:
    if not href:
        return None
    match = _JOB_ID_RE.search(href)
    return match.group(1) if match else None


def _parse_posted_at(value: str | None, now: datetime | None = None) -> datetime | None:
    if not value:
        return None
    current = now or datetime.now(timezone.utc)
    try:
        parsed = datetime.strptime(
            f"{value.strip()} {current.year}", "%d %b %Y"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    # The UI omits the year. Around New Year, December dates must resolve to
    # the previous year rather than a future posting.
    if parsed > current:
        parsed = parsed.replace(year=current.year - 1)
    return parsed


class ZipRecruiterConnector(BaseConnector):
    source_name = "ziprecruiter"

    def __init__(
        self,
        search: str,
        location_mode: str,
        max_results: int = ZIPRECRUITER_MAX_RESULTS,
        max_pages: int = ZIPRECRUITER_MAX_PAGES,
        max_attempts_per_page: int = 3,
        *,
        location: str | None = None,
    ):
        if location_mode not in {"bengaluru", "remote_india", "city", "india"}:
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
        self.max_pages = max_pages
        self.max_attempts_per_page = max_attempts_per_page

    def _build_url(self, page_number: int) -> str:
        location = self.location if self.location_mode == "city" else (
            "bengaluru" if self.location_mode == "bengaluru" else "India"
        )
        params: dict[str, str | int] = {
            "q": self.search,
            "l": location,
            "page": page_number,
            "per_page": ZIPRECRUITER_PAGE_SIZE,
        }
        if self.location_mode == "remote_india":
            params["remote"] = "full"
        return f"{ZIPRECRUITER_INDIA_SEARCH_URL}?{urlencode(params)}"

    @staticmethod
    async def _parse_page(page) -> list[dict]:
        # Extract every card in one browser round trip. Calling Playwright
        # locators field-by-field is tolerable for 20 rows but extremely slow
        # once per_page=1000 is enabled.
        cards = await page.locator("li.job-listing").evaluate_all(
            """
            elements => elements.map(card => {
                const titleLink = card.querySelector('a.jobList-title');
                const meta = Array.from(card.querySelectorAll('ul.jobList-introMeta li'))
                    .map(element => element.textContent.trim())
                    .filter(Boolean);
                return {
                    href: titleLink?.getAttribute('href') || null,
                    title: titleLink?.textContent.trim() || null,
                    company: meta[0] || null,
                    location: meta[1] || null,
                    description: card.querySelector('.jobList-description')?.textContent.trim() || null,
                    date_text: card.querySelector('.jobList-date')?.textContent.trim() || null,
                };
            })
            """
        )
        rows: list[dict] = []
        for card in cards:
            href = card["href"]
            job_id = _job_id(href)
            title = card["title"]
            if not job_id or not href or not title:
                continue
            rows.append(
                {
                    "job_id": job_id,
                    "href": href,
                    "title": title,
                    "company": card["company"],
                    "location": card["location"],
                    "description": card["description"],
                    "date_text": card["date_text"],
                }
            )
        return rows

    async def _fetch_page(self, browser, page_number: int) -> list[dict] | None:
        for _attempt in range(self.max_attempts_per_page):
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            try:
                await page.goto(
                    self._build_url(page_number),
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                try:
                    await page.wait_for_selector("li.job-listing", timeout=5_000)
                except Exception:
                    pass
                if "just a moment" in (await page.title()).lower():
                    continue
                return await self._parse_page(page)
            except Exception:
                continue
            finally:
                await context.close()
        return None

    async def _fetch_pages(self, browser) -> list[list[dict] | None]:
        return await asyncio.gather(
            *(self._fetch_page(browser, page_number) for page_number in range(1, self.max_pages + 1))
        )

    def _merge_pages(self, pages: list[list[dict] | None]) -> list[dict]:
        results: list[dict] = []
        seen_ids: set[str] = set()
        for page_items in pages:
            if not page_items:
                continue
            for item in page_items:
                job_id = item["job_id"]
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                results.append(item)
                if len(results) >= self.max_results:
                    return results
        return results

    async def _scrape_async(self) -> list[dict]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, channel="chrome")
            try:
                return self._merge_pages(await self._fetch_pages(browser))
            finally:
                await browser.close()

    def _scrape(self) -> list[dict]:
        return asyncio.run(self._scrape_async())

    def fetch(self) -> list[NormalizedJob]:
        normalized: list[NormalizedJob] = []
        for item in self._scrape():
            href = item["href"]
            job_url = href if href.startswith("http") else f"https://www.ziprecruiter.in{href}"
            location = item.get("location")
            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=item["job_id"],
                    title=_truncate(item["title"], 255) or item["title"][:255],
                    company_name_raw=_truncate(item.get("company"), 255) or "Unknown",
                    location_raw=_truncate(location, 255),
                    is_remote=self.location_mode == "remote_india" or "remote" in (location or "").lower(),
                    remote_scope=_truncate(location, 500),
                    employment_type=None,
                    seniority=None,
                    description_raw=item.get("description"),
                    salary_raw=None,
                    apply_url=job_url,
                    job_url=job_url,
                    posted_at=_parse_posted_at(item.get("date_text")),
                    raw_payload=item,
                )
            )
        return normalized
