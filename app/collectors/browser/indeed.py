"""
Indeed connector — anonymous, bot-detection-gated (no login needed).

A plain HTTP fetch gets a flat "Blocked - Indeed.com" page from Akamai, even
headless via patchright/real Chrome. Headed Chrome through the proxy gets a
real result page reliably (confirmed via live testing) — no account
required, unlike LinkedIn/Naukri. See session_utils.py for the shared
headed+proxy pattern this reuses.

Domain: uses `in.indeed.com` (the India edition), not `www.indeed.com`.
Live testing found `www.indeed.com?l=Bengaluru...` silently ignores the `l=`
location param and returns US-located jobs regardless (same "param looks
like a filter but isn't" trap as Remotive's `search=`) -- the proxy's exit
IP, not the `l=` value, decided the results. `in.indeed.com` with the same
`l=` param reliably returned real Bengaluru-located cards across repeated
attempts.

Filter methodology:
- Location: `l=<city>` (e.g. `Bengaluru`) is a genuine, reliably-working
  location filter on `in.indeed.com` -- confirmed via repeated live loads
  showing `formattedLocation` values like "Bengaluru, Karnataka" for every
  result. By contrast, the broad/global `l=India` param reliably triggered
  a Cloudflare "Additional Verification Required" challenge across 4/4
  attempts (with backoff between them, ruling out simple proxy contention
  -- the identical `l=Bengaluru` query succeeded 3/3 times in the same
  window). This is the same class of "a param can be silently broken/
  blocked" trap flagged for LinkedIn's `&start=25` -- so
  `location_mode="remote_india"` (using `l=India`) is wired in but wrapped
  in the same retry-across-fresh-context pattern used for Foundit/
  ZipRecruiter/CareerJet, since it's an accepted-flaky param rather than a
  hard-broken one. `location_mode="bengaluru"` is the reliable default,
  and since Indeed tags plenty of Bengaluru-scoped results `remote: true`
  in the job-card data (see below), a city-scoped pass alone already
  captures a good share of remote-eligible-for-India postings without
  needing the flaky country-wide pass.
  `location_mode="city"` uses the requested Indian city in that same native
  `l` parameter. `location_mode="india"` uses `l=India` without the remote
  filter.
- Date: the observed "Last 14 days" UI selection is encoded as `fromage=14`.
  That does not establish a server-side maximum. The connector passes the
  global recency value directly as `fromage=<days>` and the central gate
  enforces the same cutoff locally.
- No experience-level filter is used as a stand-in for years of experience
  (standing project rule) -- Indeed's "job type" tags (Fresher/Full-time/
  Internship/etc, see below) are captured as scraped data but are not a
  years-of-experience signal.

Data source: NOT the visible DOM cards. The search-results page embeds a
full per-job JSON payload in `window.mosaic.providerData["mosaic-provider-
jobcards"]` (`metaData.mosaicProviderJobCardsModel.results`), which is
parsed directly out of the page's rendered HTML via a regex + `json.loads`
rather than querying DOM nodes card-by-card. This was found by grepping a
full page dump for a "19 days ago" string that never appeared in any
visible card's `inner_text()` -- it turned out to live only in this JSON
blob (`formattedRelativeTime` / `createDate` fields), confirming the DOM
alone doesn't carry a real date at all on this template. The same blob also
carries `jobkey` (the same id as `data-jk`), `company`, `formattedLocation`,
`extractedSalary` (min/max/type), `jobTypes` (list), and a `remoteLocation`
bool -- i.e. everything the old per-card DOM-selector pass was scraping,
plus real posted-date and salary that the DOM pass had no access to at all.
Falls back to zero results (rather than a partial/wrong scrape) if the blob
isn't found or fails to parse, since the DOM has already been shown to lack
this data.

Description/detail-page capture: `#jobDescriptionText` on the detail page
(`in.indeed.com/viewjob?jk=<id>`) holds the full description -- this part
still needs a real page visit; it isn't in the search-results JSON. The
detail page also renders a "Job details" panel (distinct `role="group"`
blocks with a stable `aria-label` -- "Pay", "Job type", and occasionally
others depending on posting) -- the same "missed criteria block" lesson as
LinkedIn's guest detail pages, scraped generically by `aria-label` rather
than pinned to hashed CSS class names (which change across Indeed's React
builds). Card-level `jobTypes`/`extractedSalary` are used as the primary
source for `employment_type`/`salary_raw` since they're always present when
the JSON blob parses; the detail-page "Job details" panel only fills gaps.
No explicit seniority-level field exists on Indeed (unlike LinkedIn's
criteria block) -- `seniority` stays unset; enrichment infers it
downstream, consistent with the project rule against fabricating a proxy
field.

Proxy re-check (issue #64, 2026-07-25): live-verified with the shared
proxy down entirely (`ERR_TIMED_OUT` on a plain connectivity check) that
`in.indeed.com` renders a genuine results page -- real `mosaic-provider-
jobcards` blob, 15 real "Data Scientist" results -- on a plain headed
patchright launch with no proxy at all. Now drops `launch_anonymous()`
for a direct headed launch, same as LinkedIn/Naukri/Monster/CareerBuilder.
`FLAKY_LOCATION_MODES`'s retry-across-fresh-context handling is kept for
`remote_india` (the mechanism is cheap insurance either way), but
live-checked 2026-07-25: `remote_india` returned a clean 15-result page on
the very first attempt with no proxy and no challenge -- consistent with
the original flakiness (a Cloudflare "Additional Verification Required"
challenge, 4/4 times) having been specific to the shared proxy's exit IP
reputation rather than an inherent Indeed-side behavior on this location
mode. Kept as a documented exception rather than removed, since one clean
run doesn't retire a challenge that was reproduced 4/4 times previously.

Depth policy (issue #64): no real pagination exists in this connector (a
single page load, `max_results`-truncated) -- `depth.relevance_drift_cursor()`
(issue #60) is applied as a post-hoc truncation over that one batch,
classifying each result's title via `app.config.categories.is_on_slice`.
`date_filter_days` defaults to the 60-day `RECENCY_WINDOW_DAYS` configured
intent; only the observed 14-day site value is sent.
"""
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from patchright.sync_api import sync_playwright

from app.collectors.base import (
    BaseConnector,
    NonRetryableFetchError,
    PartialFetchError,
)
from app.collectors.browser.indeed_auth import (
    launch_indeed_context,
    page_reports_logged_in,
)
from app.collectors.browser.session_utils import human_delay
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob
from app.pipeline.relevance import first_failing_axis

INDEED_SEARCH_URL = "https://in.indeed.com/jobs"
INDEED_DETAIL_URL = "https://in.indeed.com/viewjob?jk={job_id}"

LOCATION_VALUES = {
    "bengaluru": "Bengaluru",
    "remote_india": "India",
    "india": "India",
    None: None,
}

INDEED_MAX_RESULTS = 1_000
INDEED_POSITION_CEILING = 1_000
INDEED_START_STEP = 10
INDEED_LISTING_BATCH_SIZE = 4
INDEED_DETAIL_BATCH_SIZE = 4
INDEED_NO_NEW_PAGE_LIMIT = 2
INDEED_REMOTE_FILTER = "0kf:attr(DSQF7);"
INDEED_MANUAL_CHALLENGE_TIMEOUT_S = 900
INDEED_MANUAL_CHALLENGE_POLL_MS = 1_000
logger = logging.getLogger("pipeline")


class IndeedAuthenticationError(NonRetryableFetchError):
    """The saved Indeed profile is no longer authenticated."""


class IndeedChallengeError(NonRetryableFetchError):
    """Indeed presented an anti-bot verification page or response."""


class IndeedRateLimitError(NonRetryableFetchError):
    """Indeed returned HTTP 429; partial progress may still be retained."""


class _IndeedPartialScrapeError(NonRetryableFetchError):
    def __init__(self, items: list[dict], cause: Exception):
        super().__init__(f"Indeed stopped after {len(items)} completed jobs: {cause}")
        self.items = items
        self.cause = cause


_CHALLENGE_MARKERS = (
    "additional verification required",
    "just a moment",
    "verify you are human",
    "captcha",
    "cf-chl-",
)

# location_mode values that are known-flaky (see module docstring) and get
# the retry-across-fresh-context treatment already used for Foundit/
# ZipRecruiter/CareerJet, rather than a single fixed attempt.
FLAKY_LOCATION_MODES = {"remote_india"}

_JOBCARDS_BLOB_RE = re.compile(
    r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});\s*window',
    re.S,
)


def _extract_job_results(html: str) -> list[dict]:
    """Pull the per-job list out of the embedded jobcards JSON blob (see
    module docstring) -- this is the real data source, not the DOM."""
    m = _JOBCARDS_BLOB_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    return data.get("metaData", {}).get("mosaicProviderJobCardsModel", {}).get("results", []) or []


def _extract_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    detail: dict = {}
    description = soup.select_one("#jobDescriptionText")
    if description:
        detail["description_raw"] = description.get_text("\n", strip=True)

    job_types: list[str] = []
    for group in soup.select('div[role="group"][aria-label]'):
        label = group.get("aria-label")
        lines = [line.strip() for line in group.get_text("\n").splitlines() if line.strip()]
        values = [line for line in lines if line != label]
        if label == "Pay" and values:
            detail["salary_raw"] = values[0]
        elif label == "Job type":
            job_types.extend(values)
    if job_types:
        detail["job_type_tags"] = list(dict.fromkeys(job_types))
    return detail


def _browser_fetch_many(page, urls: list[str]) -> list[dict]:
    """Fetch a bounded same-origin batch inside the shared Chrome session."""
    return page.evaluate(
        """async urls => Promise.all(urls.map(async url => {
            try {
                const response = await fetch(url, {
                    credentials: 'include',
                    signal: AbortSignal.timeout(45000)
                });
                return {url, status: response.status, text: await response.text()};
            } catch (error) {
                return {url, status: 0, text: '', error: String(error)};
            }
        }))""",
        urls,
    )


class IndeedConnector(BaseConnector):
    source_name = "indeed"
    pipeline_priority = -100
    inter_instance_cooldown_s = 0.0
    _detail_cache: dict[str, dict] = {}

    def __init__(
        self,
        search: str,
        max_results: int = INDEED_MAX_RESULTS,
        location_mode: str | None = "bengaluru",
        date_filter_days: int = RECENCY_WINDOW_DAYS,
        fetch_descriptions: bool = True,
        max_attempts: int = 3,
        listing_batch_size: int = INDEED_LISTING_BATCH_SIZE,
        detail_batch_size: int = INDEED_DETAIL_BATCH_SIZE,
        manual_challenge_timeout_s: int = INDEED_MANUAL_CHALLENGE_TIMEOUT_S,
        *,
        location: str | None = None,
    ):
        if location_mode not in {None, "bengaluru", "remote_india", "city", "india"}:
            raise ValueError(
                "location_mode must be 'bengaluru', 'remote_india', 'city', 'india', or None"
            )
        if location_mode == "city" and not (location and location.strip()):
            raise ValueError("location is required when location_mode is 'city'")
        self.search = search
        self.max_results = min(max_results, INDEED_MAX_RESULTS)
        self.location_mode = location_mode
        self.location = location.strip() if location_mode == "city" else None
        self.date_filter_days = date_filter_days
        self.fetch_descriptions = fetch_descriptions
        self.max_attempts = max_attempts if location_mode in FLAKY_LOCATION_MODES else 1
        self.listing_batch_size = listing_batch_size
        self.detail_batch_size = detail_batch_size
        self.manual_challenge_timeout_s = manual_challenge_timeout_s
        self._batch_context = None
        self._batch_page = None
        self._batch_state = None
        self.filter_state = {
            "date_posted": {
                "configured_window_days": date_filter_days,
                "effective_window_days": date_filter_days,
                "selection": f"Last {date_filter_days} days",
            },
            "experience_years": {
                "support": "unsupported_in_observed_ui",
                "selection": None,
            },
        }

    @classmethod
    @contextmanager
    def batch_scope(cls, connectors):
        """Keep one persistent Chrome lifetime for a serial Indeed batch."""
        with sync_playwright() as playwright:
            context = launch_indeed_context(playwright, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            state = {"halt_error": None}
            for connector in connectors:
                connector._batch_context = context
                connector._batch_page = page
                connector._batch_state = state
            try:
                yield
            finally:
                for connector in connectors:
                    connector._batch_context = None
                    connector._batch_page = None
                    connector._batch_state = None
                context.close()

    def _raise_if_batch_halted(self) -> None:
        if self._batch_state and self._batch_state["halt_error"] is not None:
            raise self._batch_state["halt_error"]

    def _halt_batch(self, error: Exception) -> None:
        if self._batch_state is not None:
            self._batch_state["halt_error"] = error

    @staticmethod
    def _challenge_error(html: str, status: int | None = None):
        lowered = (html or "").lower()
        if status == 403 or any(marker in lowered for marker in _CHALLENGE_MARKERS):
            suffix = f" (HTTP {status})" if status else ""
            return IndeedChallengeError(
                "Indeed verification required; remaining Indeed instances stopped"
                f"{suffix}"
            )
        return None

    def _wait_for_manual_challenge(self, page, url: str | None = None) -> dict:
        """Wait for the user to clear a visible challenge; never interact with it."""
        if url is not None:
            page.goto(url, timeout=45_000, wait_until="domcontentloaded")
        page.bring_to_front()
        logger.warning(
            "Indeed CAPTCHA detected. Complete it manually in the visible browser; "
            "the shared session will remain open for up to %ss.",
            self.manual_challenge_timeout_s,
        )

        remaining_ms = self.manual_challenge_timeout_s * 1_000
        while remaining_ms > 0:
            poll_ms = min(INDEED_MANUAL_CHALLENGE_POLL_MS, remaining_ms)
            page.wait_for_timeout(poll_ms)
            remaining_ms -= poll_ms
            html = page.content()
            if self._challenge_error(html) is None:
                logger.info("Indeed CAPTCHA cleared manually; resuming collection")
                return {"url": page.url, "status": 200, "text": html}

        raise IndeedChallengeError(
            "Indeed CAPTCHA was not cleared before the manual-action timeout"
        )

    def _wait_for_manual_login(self, page, search_url: str) -> dict:
        """Keep the visible browser open until the user restores the session."""
        page.bring_to_front()
        logger.warning(
            "Indeed login required. Log in manually in the visible browser, then "
            "navigate to an Indeed jobs page. Waiting up to %ss.",
            self.manual_challenge_timeout_s,
        )

        remaining_ms = self.manual_challenge_timeout_s * 1_000
        while remaining_ms > 0:
            poll_ms = min(INDEED_MANUAL_CHALLENGE_POLL_MS, remaining_ms)
            page.wait_for_timeout(poll_ms)
            remaining_ms -= poll_ms
            if not page_reports_logged_in(page):
                continue

            response = page.goto(
                search_url, timeout=45_000, wait_until="domcontentloaded"
            )
            human_delay(3.0, 6.0)
            html = page.content()
            if page_reports_logged_in(page):
                logger.info("Indeed login confirmed; resuming collection")
                return {"response": response, "text": html}

        raise IndeedAuthenticationError(
            "Indeed login was not completed before the manual-action timeout"
        )

    @contextmanager
    def _browser_scope(self):
        if self._batch_context is not None and self._batch_page is not None:
            yield self._batch_context, self._batch_page
            return

        with sync_playwright() as playwright:
            context = launch_indeed_context(playwright, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                yield context, page
            finally:
                context.close()

    def _location_value(self) -> str | None:
        if self.location_mode == "city":
            return self.location
        return LOCATION_VALUES[self.location_mode]

    def _search_params(self, start: int | None = None) -> dict[str, str]:
        params = {"q": self.search}
        location = self._location_value()
        if location:
            params["l"] = location
        params["fromage"] = str(self.date_filter_days)
        if self.location_mode == "remote_india":
            params["sc"] = INDEED_REMOTE_FILTER
        if start is not None:
            params["start"] = str(start)
        return params

    def _search_url(self, start: int | None = None) -> str:
        return f"{INDEED_SEARCH_URL}?{urlencode(self._search_params(start))}"

    @staticmethod
    def _row_from_result(result: dict) -> dict | None:
        job_id = result.get("jobkey")
        title = result.get("displayTitle") or result.get("title")
        if not job_id or not title:
            return None

        salary = result.get("extractedSalary") or {}
        salary_raw = None
        if salary.get("min") is not None:
            salary_range = (
                f"{salary['min']}-{salary['max']}"
                if salary.get("max") and salary["max"] != salary["min"]
                else str(salary["min"])
            )
            salary_raw = f"{salary_range} {salary.get('type', '')}".strip()

        create_date_ms = result.get("createDate")
        posted_at = (
            datetime.fromtimestamp(create_date_ms / 1000, tz=timezone.utc)
            if create_date_ms
            else None
        )
        return {
            "job_id": job_id,
            "title": title,
            "company": result.get("company"),
            "location": result.get("formattedLocation"),
            "is_remote": bool(result.get("remoteLocation")),
            "job_types": result.get("jobTypes") or [],
            "salary_raw": salary_raw,
            "posted_at": posted_at.isoformat() if posted_at else None,
        }

    def _normalize(self, item: dict) -> NormalizedJob:
        job_id = item["job_id"]
        job_url = INDEED_DETAIL_URL.format(job_id=job_id)
        location = item.get("location")
        job_type_tags = item.get("job_type_tags") or item.get("job_types") or []
        employment_type = ", ".join(job_type_tags) if job_type_tags else None
        posted_at = datetime.fromisoformat(item["posted_at"]) if item.get("posted_at") else None
        return NormalizedJob(
            source=self.source_name,
            external_job_id=job_id,
            title=item["title"],
            company_name_raw=(item.get("company") or "Unknown")[:255],
            location_raw=(location[:255] if location else None),
            is_remote=bool(item.get("is_remote")) or "remote" in (location or "").lower(),
            remote_scope=(location[:255] if location else None),
            employment_type=(employment_type[:255] if employment_type else None),
            seniority=None,
            description_raw=item.get("description_raw"),
            salary_raw=(item["salary_raw"][:255] if item.get("salary_raw") else None),
            apply_url=job_url,
            job_url=job_url,
            posted_at=posted_at,
            raw_payload=item,
        )

    def _should_fetch_detail(self, item: dict) -> bool:
        return first_failing_axis(self._normalize(item)) is None

    def _scrape_once(self) -> list[dict]:
        results = []

        with sync_playwright() as p:
            # No proxy (issue #64) — headed patchright alone is enough, see
            # module docstring's "Proxy re-check" section.
            context = launch_indeed_context(p, headless=False)
            page = context.new_page()

            try:
                query = {"q": self.search}
                location = self._location_value()
                if location:
                    query["l"] = location
                query["fromage"] = str(self.date_filter_days)
                url = f"{INDEED_SEARCH_URL}?{urlencode(query)}"
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                human_delay(3.0, 6.0)

                job_results = _extract_job_results(page.content())
                if not job_results:
                    return []

                cursor = relevance_drift_cursor(max_results=self.max_results)
                for jr in job_results:
                    if not cursor.should_continue():
                        break

                    job_id = jr.get("jobkey")
                    title = jr.get("displayTitle") or jr.get("title")
                    if not job_id or not title:
                        continue
                    cursor.record_job(is_on_slice(title, self.search))

                    salary = jr.get("extractedSalary") or {}
                    salary_raw = None
                    if salary.get("min") is not None:
                        rng = (
                            f"{salary['min']}-{salary['max']}"
                            if salary.get("max") and salary["max"] != salary["min"]
                            else str(salary["min"])
                        )
                        salary_raw = f"{rng} {salary.get('type', '')}".strip()

                    create_date_ms = jr.get("createDate")
                    posted_at = (
                        datetime.fromtimestamp(create_date_ms / 1000, tz=timezone.utc)
                        if create_date_ms
                        else None
                    )

                    results.append(
                        {
                            "job_id": job_id,
                            "title": title,
                            "company": jr.get("company"),
                            "location": jr.get("formattedLocation"),
                            "is_remote": bool(jr.get("remoteLocation")),
                            "job_types": jr.get("jobTypes") or [],
                            "salary_raw": salary_raw,
                            "posted_at": posted_at.isoformat() if posted_at else None,
                        }
                    )
                    human_delay(0.2, 0.6)

                if self.fetch_descriptions:
                    for item in results:
                        job_id = item.get("job_id")
                        if not job_id:
                            continue
                        detail_page = context.new_page()
                        try:
                            detail_page.goto(
                                INDEED_DETAIL_URL.format(job_id=job_id),
                                timeout=30000,
                                wait_until="domcontentloaded",
                            )
                            human_delay(2.0, 3.5)

                            desc_el = detail_page.query_selector("#jobDescriptionText")
                            item["description_raw"] = desc_el.inner_text().strip() if desc_el else None

                            # "Job details" panel: role=group blocks with a stable
                            # aria-label ("Pay", "Job type", ...) -- only used to
                            # fill gaps the card-level JSON didn't already cover.
                            job_type_tags = list(item.get("job_types") or [])
                            for group in detail_page.query_selector_all('div[role="group"]'):
                                label = group.get_attribute("aria-label")
                                if not label:
                                    continue
                                text = group.inner_text().strip()
                                if label == "Pay" and not item.get("salary_raw"):
                                    value_lines = [ln for ln in text.splitlines() if ln and ln != "Pay"]
                                    if value_lines:
                                        item["salary_raw"] = value_lines[0]
                                elif label == "Job type" and not job_type_tags:
                                    value_lines = [ln for ln in text.splitlines() if ln and ln != "Job type"]
                                    job_type_tags.extend(value_lines)

                            if job_type_tags:
                                item["job_type_tags"] = job_type_tags
                        except Exception:
                            item.setdefault("description_raw", None)
                        finally:
                            detail_page.close()
            finally:
                context.close()

        return results

    def _scrape_full(self) -> list[dict]:
        results: list[dict] = []
        seen_ids: set[str] = set()

        with self._browser_scope() as (context, page):
            try:
                self._raise_if_batch_halted()
                url = self._search_url()
                initial_response = page.goto(
                    url, timeout=45_000, wait_until="domcontentloaded"
                )
                human_delay(3.0, 6.0)

                initial_html = page.content()
                challenge = self._challenge_error(initial_html)
                if challenge is not None:
                    recovered = self._wait_for_manual_challenge(page)
                    initial_html = recovered["text"]
                    initial_response = None
                if getattr(initial_response, "status", None) == 429:
                    raise IndeedRateLimitError("Indeed initial search returned HTTP 429")
                if not page_reports_logged_in(page):
                    recovered = self._wait_for_manual_login(page, url)
                    initial_html = recovered["text"]
                    initial_response = recovered["response"]
                    challenge = self._challenge_error(
                        initial_html, getattr(initial_response, "status", None)
                    )
                    if challenge is not None:
                        recovered = self._wait_for_manual_challenge(page)
                        initial_html = recovered["text"]
                        initial_response = None
                    if getattr(initial_response, "status", None) == 429:
                        raise IndeedRateLimitError(
                            "Indeed initial search returned HTTP 429 after login"
                        )
                initial_results = _extract_job_results(initial_html)
                if not initial_results:
                    return []

                def accept(page_results: list[dict]) -> int:
                    added = 0
                    for result in page_results:
                        if len(results) >= self.max_results:
                            break
                        row = self._row_from_result(result)
                        if not row or row["job_id"] in seen_ids:
                            continue
                        seen_ids.add(row["job_id"])
                        results.append(row)
                        added += 1
                    return added

                accept(initial_results)
                consecutive_no_new = 0
                stop = len(results) >= self.max_results
                offsets = list(
                    range(
                        INDEED_START_STEP,
                        min(self.max_results, INDEED_POSITION_CEILING),
                        INDEED_START_STEP,
                    )
                )
                for batch_start in range(0, len(offsets), self.listing_batch_size):
                    if stop:
                        break
                    starts = offsets[
                        batch_start : batch_start + self.listing_batch_size
                    ]
                    responses = _browser_fetch_many(
                        page, [self._search_url(start) for start in starts]
                    )
                    human_delay(1.0, 2.0)
                    for start, response in zip(starts, responses):
                        challenge = self._challenge_error(
                            response.get("text", ""), response.get("status")
                        )
                        if challenge is not None:
                            response = self._wait_for_manual_challenge(
                                page, response.get("url")
                            )
                        if response.get("status") == 429:
                            raise IndeedRateLimitError(
                                f"Indeed listing offset {start} returned HTTP 429"
                            )
                        if response.get("status") != 200:
                            raise RuntimeError(
                                f"Indeed listing offset {start} returned "
                                f"HTTP {response.get('status')}"
                            )
                        added = accept(
                            _extract_job_results(response.get("text", ""))
                        )
                        consecutive_no_new = (
                            consecutive_no_new + 1 if added == 0 else 0
                        )
                        if (
                            consecutive_no_new >= INDEED_NO_NEW_PAGE_LIMIT
                            or len(results) >= self.max_results
                        ):
                            stop = True
                            break

                if self.fetch_descriptions:
                    pending: list[dict] = []
                    for item in results:
                        if not self._should_fetch_detail(item):
                            continue
                        cached = self._detail_cache.get(item["job_id"])
                        if cached:
                            item.update(cached)
                        else:
                            pending.append(item)

                    for batch_start in range(
                        0, len(pending), self.detail_batch_size
                    ):
                        batch = pending[
                            batch_start : batch_start + self.detail_batch_size
                        ]
                        responses = _browser_fetch_many(
                            page,
                            [
                                INDEED_DETAIL_URL.format(job_id=item["job_id"])
                                for item in batch
                            ],
                        )
                        human_delay(1.0, 2.0)
                        for item, response in zip(batch, responses):
                            challenge = self._challenge_error(
                                response.get("text", ""), response.get("status")
                            )
                            if challenge is not None:
                                response = self._wait_for_manual_challenge(
                                    page, response.get("url")
                                )
                            if response.get("status") == 429:
                                raise IndeedRateLimitError(
                                    "Indeed detail request returned HTTP 429"
                                )
                            if response.get("status") != 200:
                                continue
                            detail = _extract_detail(response.get("text", ""))
                            if detail:
                                self._detail_cache[item["job_id"]] = detail
                                item.update(detail)
            except NonRetryableFetchError as exc:
                if not isinstance(exc, IndeedRateLimitError):
                    self._halt_batch(exc)
                if results:
                    raise _IndeedPartialScrapeError(results, exc) from exc
                raise

        return results

    def _scrape(self) -> list[dict]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                results = self._scrape_full()
                if results:
                    return results
            except NonRetryableFetchError:
                raise
            except Exception:
                if attempt == self.max_attempts:
                    raise
                continue
        return []

    def fetch(self) -> list[NormalizedJob]:
        try:
            items = self._scrape()
        except _IndeedPartialScrapeError as exc:
            jobs = [self._normalize(item) for item in exc.items]
            raise PartialFetchError(str(exc), jobs, exc.cause) from exc.cause
        return [self._normalize(item) for item in items]
