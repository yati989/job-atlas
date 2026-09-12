"""Authenticated HTTP connector for Cutshort's India-native job inventory.

One attended Google login creates a Playwright storage-state file. Normal
collection then replays those cookies through pooled HTTP; no browser starts.
The internal API honors ``pageSize=1000`` and advances ``page`` by that size,
although its ``totalPages`` field is incorrectly based on an internal default
of five. We therefore calculate page count from ``total_count`` ourselves and
fetch all pages concurrently.

The authenticated endpoint genuinely filters on the candidate UI's
``roletype``, ``tags``, and ``creationDate``
parameters. A live
real-versus-nonsense check on 2026-08-22 returned 93 full-time data/ML jobs
for the configured tags and zero for a nonsense tag, versus 3,642 rows when
the parameters were omitted. The connector therefore uses Cutshort's native
role/activity filters instead of downloading the broad all-jobs inventory.
``creationDate`` follows the pipeline's rounded-up native day window; the
central gate still enforces the exact timestamp.

Each listing already includes the employer's original description in
``sanitizedComment``/``comment`` plus structured dates, location, remote,
employment, experience, skills, company, and salary fields. No detail request
or LLM judgement is needed. The native filter is only a fetch-side prefilter;
every result still passes the central deterministic relevance gate unchanged.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import math
from pathlib import Path
import threading

from bs4 import BeautifulSoup
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.settings import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob

CUTSHORT_API_URL = "https://cutshort.io/findjobs/q"
CUTSHORT_PAGE_SIZE = 1000
CUTSHORT_MAX_RESULTS = 1000
CUTSHORT_PAGE_WORKERS = 4
CUTSHORT_SOURCE_MAX_REQUESTS = 8
CUTSHORT_ROLE_TYPE = "full_time"
CUTSHORT_TAGS = (
    "data_science-data_analytics-ml_engineering-"
    "others_data_science_analytics-big_data"
)
CUTSHORT_SESSION_STATE_PATH = (
    Path(__file__).parents[1] / "browser" / ".sessions" / "cutshort_state.json"
)

_REQUEST_SEMAPHORE = threading.BoundedSemaphore(CUTSHORT_SOURCE_MAX_REQUESTS)
_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://cutshort.io/profile/all-jobs",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
}


def _text(value: str | None) -> str | None:
    if not value:
        return None
    return BeautifulSoup(value, "html.parser").get_text(separator="\n").strip() or None


def _truncate(value: str | None, limit: int = 255) -> str | None:
    return value.strip()[:limit] if value and value.strip() else None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _session_from_storage(path: Path) -> requests.Session:
    if not path.exists():
        raise RuntimeError(
            "Cutshort session is missing; run `python -m scripts.setup_cutshort_session`"
        )
    state = json.loads(path.read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers.update(_HEADERS)
    for cookie in state.get("cookies", []):
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    return session


class CutshortConnector(BaseConnector):
    source_name = "cutshort"

    def __init__(
        self,
        max_results: int = CUTSHORT_MAX_RESULTS,
        page_size: int = CUTSHORT_PAGE_SIZE,
        page_workers: int = CUTSHORT_PAGE_WORKERS,
        date_filter_days: int = RECENCY_WINDOW_DAYS,
        session_state_path: Path = CUTSHORT_SESSION_STATE_PATH,
    ):
        self.max_results = min(max_results, CUTSHORT_MAX_RESULTS)
        self.page_size = page_size
        self.page_workers = page_workers
        self.date_filter_days = date_filter_days
        self.session_state_path = session_state_path

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def _fetch_page(self, page: int) -> dict:
        session = _session_from_storage(self.session_state_path)
        with _REQUEST_SEMAPHORE:
            response = session.get(
                CUTSHORT_API_URL,
                params={
                    "page": page,
                    "pageSize": self.page_size,
                    "roletype": CUTSHORT_ROLE_TYPE,
                    "tags": CUTSHORT_TAGS,
                    "creationDate": f"{self.date_filter_days}-days",
                },
                timeout=90,
            )
        if response.status_code in (401, 403):
            raise RuntimeError(
                "Cutshort session expired; run `python -m scripts.setup_cutshort_session`"
            )
        response.raise_for_status()
        return response.json()

    def _scrape(self) -> list[dict]:
        first = self._fetch_page(1)
        total_count = int(first.get("total_count") or 0)
        page_count = min(
            math.ceil(total_count / self.page_size),
            math.ceil(self.max_results / self.page_size),
        )
        batches = [first.get("results") or []]
        if page_count > 1:
            with ThreadPoolExecutor(max_workers=self.page_workers) as executor:
                batches.extend(
                    payload.get("results") or []
                    for payload in executor.map(self._fetch_page, range(2, page_count + 1))
                )

        seen: set[str] = set()
        results: list[dict] = []
        for batch in batches:
            for item in batch:
                job_id = str(item.get("short_id") or item.get("_id") or "")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                results.append(item)
                if len(results) >= self.max_results:
                    return results
        return results

    @staticmethod
    def _normalize(item: dict) -> NormalizedJob | None:
        job_id = str(item.get("short_id") or item.get("_id") or "")
        title = item.get("headline")
        job_url = item.get("publicUrl") or item.get("public_url")
        if not job_id or not title or not job_url:
            return None

        location = item.get("locationsText")
        remote_type = item.get("remoteType") or ""
        is_remote = bool(item.get("remoteRole")) or bool(
            remote_type and remote_type != "remote_not_okay"
        )
        role_types = item.get("roleTypes")
        employment_type = (
            ", ".join(role_types) if isinstance(role_types, list) else role_types
        )
        description = _text(item.get("sanitizedComment") or item.get("comment"))

        return NormalizedJob(
            source="cutshort",
            external_job_id=job_id,
            title=title,
            company_name_raw=_truncate(item.get("company")) or "Unknown",
            location_raw=_truncate(location),
            is_remote=is_remote,
            remote_scope=_truncate(remote_type or location),
            employment_type=_truncate(employment_type),
            seniority=None,
            description_raw=description,
            salary_raw=_truncate(item.get("salaryRangeText")),
            apply_url=job_url,
            job_url=job_url,
            posted_at=_parse_datetime(item.get("creationDate")),
            # Preserve only JSON-native API data. The old connector inserted
            # parsed datetimes here, causing every retained upsert to fail.
            raw_payload=item,
        )

    def fetch(self) -> list[NormalizedJob]:
        return [job for item in self._scrape() if (job := self._normalize(item))]
