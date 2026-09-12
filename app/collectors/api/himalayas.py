"""
Himalayas connector.

Current implementation (live-verified 2026-08-14) uses the official filtered
``/jobs/api/search`` endpoint. Each instance sends one canonical role as
``q``, ``country=IN``, and ``sort=relevant``. The country filter includes both
India-restricted and worldwide-friendly jobs; a separate worldwide pass would
duplicate eligible inventory. Pagination is 1-based via ``page`` and follows
a 30-page defensive ceiling, with the response's
``offset``/``limit``/``totalCount`` as the normal inventory exhaustion signal.

Himalayas exposes a free public JSON API for remote jobs.
The notes below describe the historical broad browse endpoint.

Historical browse-feed checks from 2026-07-14:
- The page size is server-capped at 20 regardless of `limit` (a limit=100
  request returns 20). Pagination via `offset` still works; `totalCount`
  is present. So volume comes from more pages, not bigger pages.
- The browse endpoint had no working search param (`search=` returned the identical
  default feed — same failure mode as Remotive's).
- `locationRestrictions` is a list of full country names; empty means
  unrestricted. India-eligibility must use EXACT country matching — a
  substring test wrongly matches "British Indian Ocean Territory".
- `pubDate`/`expiryDate` are Unix epoch seconds.
- Salary fields exist (minSalary/maxSalary/currency/salaryPeriod) and were
  previously unmapped.

Historical browse-feed depth policy (issue #15): Tier 1 — the feed's own
`pubDate` ordering is
newest-first (confirmed live: paging forward strictly decreases dates, and
the pre-existing `stop_paging` logic below already relied on that fact to
stop early once a page's jobs age out of the freshness window). This is the
same "verified stop-toward-cutoff" behavior `tier1_cursor()` formalizes, so
pagination now goes through the shared cursor instead of the ad hoc
`stop_paging` flag. `max_pages` stays a caller override (old default 10);
the shared `TIER1_PAGE_CEILING` (30) is only used when not explicitly set.
"""
from datetime import datetime, timedelta, timezone

from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob

HIMALAYAS_URL = "https://himalayas.app/jobs/api/search"
PAGE_SIZE = 20
COUNTRY = "IN"


def _salary_raw(item: dict) -> str | None:
    lo, hi = item.get("minSalary"), item.get("maxSalary")
    if lo is None and hi is None:
        return None
    currency = item.get("currency") or ""
    period = item.get("salaryPeriod") or ""
    if lo is not None and hi is not None:
        core = f"{lo}-{hi}"
    else:
        core = str(lo if lo is not None else hi)
    return f"{core} {currency} {period}".strip()[:255]


class HimalayasConnector(BaseConnector):
    source_name = "himalayas"

    def __init__(
        self,
        search: str = "data scientist",
        country: str = COUNTRY,
        max_pages: int = 30,
        max_age_days: int | None = RECENCY_WINDOW_DAYS,
        # Backward-compat: old registry entries passed limit=; the server
        # caps at 20 anyway so it's accepted and ignored.
        limit: int | None = None,
    ):
        self.search = search
        self.country = country
        self.max_pages = max_pages
        self.max_age_days = max_age_days

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_page(self, page: int) -> dict:
        params = {
            "q": self.search,
            "country": self.country,
            "sort": "relevant",
            "page": page,
        }
        resp = self.client.get(HIMALAYAS_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> list[NormalizedJob]:
        cutoff = None
        if self.max_age_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)

        normalized: list[NormalizedJob] = []
        seen_ids: set[str] = set()

        page = 1
        pages_fetched = 0
        while pages_fetched < self.max_pages:
            payload = self._fetch_page(page)
            page += 1
            pages_fetched += 1
            jobs_raw = payload.get("jobs", [])
            if not jobs_raw:
                break

            page_dates: list[datetime | None] = []
            for item in jobs_raw:
                posted_at = None
                raw_epoch = item.get("pubDate")
                if isinstance(raw_epoch, (int, float)):
                    posted_at = datetime.fromtimestamp(raw_epoch, tz=timezone.utc)
                page_dates.append(posted_at)

            for item, posted_at in zip(jobs_raw, page_dates):
                guid = item.get("guid") or item.get("id")
                if guid is None or str(guid) in seen_ids:
                    continue
                seen_ids.add(str(guid))

                title = (item.get("title") or "").strip()
                if posted_at is not None:
                    # Relevance order is not date-monotonic, so recency is a
                    # per-row filter and never a pagination stop signal.
                    if cutoff is not None and posted_at < cutoff:
                        continue

                locations = item.get("locationRestrictions") or []

                location_str = (", ".join(locations) if locations else "Worldwide")[:255]
                if not locations:
                    remote_scope = "Worldwide"
                elif "India" in locations:
                    remote_scope = "India"
                else:
                    remote_scope = location_str

                seniority_raw = item.get("seniority")
                if isinstance(seniority_raw, list):
                    seniority_raw = (", ".join(str(s) for s in seniority_raw) or None)
                if seniority_raw:
                    seniority_raw = seniority_raw[:255]

                employment_type_raw = item.get("employmentType")
                if isinstance(employment_type_raw, list):
                    employment_type_raw = (", ".join(str(s) for s in employment_type_raw) or None)
                if employment_type_raw:
                    employment_type_raw = employment_type_raw[:255]

                company = item.get("companyName") or "Unknown"

                normalized.append(
                    NormalizedJob(
                        source=self.source_name,
                        external_job_id=str(guid),
                        title=title,
                        company_name_raw=company.strip()[:255],
                        location_raw=location_str,
                        is_remote=True,
                        remote_scope=remote_scope,
                        employment_type=employment_type_raw,
                        seniority=seniority_raw,
                        description_raw=item.get("description"),
                        salary_raw=_salary_raw(item),
                        apply_url=item.get("applicationLink"),
                        job_url=item.get("applicationLink"),
                        posted_at=posted_at,
                        raw_payload=item,
                    )
                )

            offset = payload.get("offset")
            limit = payload.get("limit")
            total = payload.get("totalCount")
            if (
                isinstance(offset, int)
                and isinstance(limit, int)
                and isinstance(total, int)
                and offset + limit >= total
            ):
                break

        return normalized
