"""Working Nomads connector using the job board's public Elasticsearch API.

Live-verified 2026-08-14 from the site's own jobs-page request:

* The captured UI ``query_string`` across title, description, and company is
  genuine but far too broad for ingestion (10% title relevance in the first
  Data Scientist scorecard). Restricting the same exact quoted query to the
  native ``title`` field returned the same focused inventory as
  ``match_phrase``. A nonsense role returns zero.
* The official India-selection request filters ``locations`` to ``Anywhere``,
  ``APAC``, ``Asia``, or ``India``. The filter is genuine: a nonsense location
  returns zero, versus 107 advertised Data Scientist matches for the official
  set and 602 without a location filter.
* ``size=100`` is not always exhaustive (Data Scientist advertised 107 and AI
  Engineer 280). ``size=1000`` is accepted and exhausts every canonical role
  inventory observed live, so each role remains one request with no paging.
* A ``pub_date`` range filter is parsed (a future cutoff returns zero). The
  exact shared freshness cutoff is sent server-side and enforced locally too.
  ``pub_date desc`` is the primary result sort, so the one-request size scales
  from its 1,000-row/60-day baseline with the requested native window.
* API rows include full descriptions and all supported fields; no detail
  requests are necessary. The six connector instances are non-browser work
  and therefore run concurrently through ``app.pipeline.runner.fetch_all``.

Outside pipeline window orchestration, the response's tracked total is checked
against the returned hit count and unexpected truncation fails loudly. During
a pipeline run, truncation at the declared newest-first recency-scaled cap is
intentional.
"""

from datetime import datetime, timedelta, timezone

from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob


WORKINGNOMADS_API_URL = "https://www.workingnomads.com/jobsapi/_search"
SEARCH_SIZE = 1000
ELIGIBLE_LOCATIONS = ("Anywhere", "APAC", "Asia", "India")
SOURCE_FIELDS = (
    "company",
    "company_slug",
    "category_name",
    "locations",
    "location_base",
    "salary_range",
    "salary_range_short",
    "number_of_applicants",
    "instructions",
    "id",
    "external_id",
    "slug",
    "title",
    "description",
    "pub_date",
    "tags",
    "source",
    "apply_option",
    "apply_email",
    "apply_email_subject",
    "apply_url",
    "premium",
    "expired",
    "use_ats",
    "position_type",
    "annual_salary_usd",
    "experience_level",
)
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.workingnomads.com",
    "Referer": "https://www.workingnomads.com/jobs?location=anywhere,india",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}


def _tracked_total(data: dict) -> int | None:
    total = (data.get("hits") or {}).get("total")
    if isinstance(total, int):
        return total
    if isinstance(total, dict) and isinstance(total.get("value"), int):
        return total["value"]
    return None


def _parse_posted_at(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class WorkingNomadsConnector(BaseConnector):
    source_name = "workingnomads"
    # The Elasticsearch endpoint accepts pub_date as the primary descending
    # sort. This makes a window-proportional one-request size safe to apply.
    verified_newest_first = True
    recency_scaled_cap_attribute = "size"

    def __init__(
        self,
        search: str,
        size: int = SEARCH_SIZE,
        max_age_days: int | None = RECENCY_WINDOW_DAYS,
    ):
        self.search = search
        self.size = size
        self.max_age_days = max_age_days

    @property
    def client(self):
        if self._client is None:
            self._client = self.make_pooled_client(headers=HEADERS)
        return self._client

    def _build_query(self, cutoff: datetime | None) -> dict:
        escaped_search = self.search.replace("\\", "\\\\").replace('"', '\\"')
        filters: list[dict] = [
            {"terms": {"locations": list(ELIGIBLE_LOCATIONS)}}
        ]
        if cutoff is not None:
            filters.append({"range": {"pub_date": {"gte": cutoff.isoformat()}}})

        return {
            "track_total_hits": True,
            "from": 0,
            "size": self.size,
            "_source": list(SOURCE_FIELDS),
            "sort": [
                {"pub_date": {"order": "desc"}},
                {"premium": {"order": "desc"}},
                {"_score": {"order": "desc"}},
            ],
            "query": {
                "bool": {
                    "must": {
                        "query_string": {
                            "query": f'"{escaped_search}"',
                            "fields": ["title"],
                        }
                    },
                    "filter": filters,
                }
            },
            "min_score": 2,
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch(self, cutoff: datetime | None) -> dict:
        response = self.client.post(WORKINGNOMADS_API_URL, json=self._build_query(cutoff))
        response.raise_for_status()
        return response.json()

    def fetch(self) -> list[NormalizedJob]:
        cutoff = None
        if self.max_age_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)

        data = self._fetch(cutoff)
        hits = (data.get("hits") or {}).get("hits") or []
        total = _tracked_total(data)
        if (
            total is not None
            and total > len(hits)
            and not getattr(self, "_recency_cap_intentionally_scaled", False)
        ):
            raise RuntimeError(
                f"Working Nomads returned {len(hits)} of {total} tracked hits; "
                f"increase size above {self.size} or add verified pagination"
            )

        normalized: list[NormalizedJob] = []
        seen_ids: set[str] = set()
        for hit in hits:
            job = hit.get("_source") or {}
            job_id = job.get("id")
            slug = job.get("slug")
            if job_id is None or not slug or str(job_id) in seen_ids:
                continue
            seen_ids.add(str(job_id))
            if job.get("expired"):
                continue

            posted_at = _parse_posted_at(job.get("pub_date"))
            if cutoff is not None and posted_at is not None and posted_at < cutoff:
                continue

            locations = [str(location) for location in (job.get("locations") or [])]
            location_str = ", ".join(locations) or None
            if "Anywhere" in locations:
                remote_scope = "Worldwide"
            elif "India" in locations:
                remote_scope = "India"
            elif "APAC" in locations:
                remote_scope = "APAC"
            elif "Asia" in locations:
                remote_scope = "Asia"
            else:
                remote_scope = location_str

            job_url = f"https://www.workingnomads.com/jobs/{slug}"
            salary_raw = job.get("salary_range") or (
                str(job.get("annual_salary_usd"))
                if job.get("annual_salary_usd")
                else None
            )
            employment_type = job.get("position_type") or None
            seniority = job.get("experience_level") or None

            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=str(job_id),
                    title=(job.get("title") or "Unknown")[:500],
                    company_name_raw=(job.get("company") or "Unknown")[:255],
                    location_raw=location_str and location_str[:255],
                    is_remote=True,
                    remote_scope=remote_scope and remote_scope[:255],
                    employment_type=(str(employment_type)[:255] if employment_type else None),
                    seniority=(str(seniority)[:255] if seniority else None),
                    description_raw=job.get("description"),
                    salary_raw=salary_raw and str(salary_raw)[:255],
                    apply_url=job.get("apply_url") or job_url,
                    job_url=job_url,
                    posted_at=posted_at,
                    raw_payload=job,
                )
            )

        return normalized
