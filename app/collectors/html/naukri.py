"""Browser-free Naukri connector backed by the site's search JSON API.

Live discovery on 2026-08-12 established that ``jobAge`` is a genuine
server-side date filter, the API page ceiling is 100, and five pages return
500 unique rows during discovery. Anonymous requests have no cookie/auth dependency; Naukri's
frontend supplies an ``nkparam`` header by RSA-encrypting
``v0|<epoch-ms>|121_srp`` with the public key shipped in its own bundle.

The connector collects at most 1,000 native-search matches per query and does
not use relevance drift as a stop signal. The central deterministic relevance
gate handles the board's noisy ordering after collection.
"""

import base64
import time
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob

NAUKRI_SEARCH_API = "https://www.naukri.com/jobapi/v3/search"
NAUKRI_PAGE_SIZE = 100  # Live API rejects 101 and above.
NAUKRI_MAX_RESULTS = 1000

_PUBLIC_KEY_DER_B64 = (
    "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBALrlQ+djR0RjJwBF1xuisHmdFv334MIm"
    "K6LgzJhmLhN7B5yuEyaKoasgXQk3+OQglsOaBxEJ0j5PcTL3nbOvt80CAwEAAQ=="
)
_PUBLIC_KEY = serialization.load_der_public_key(base64.b64decode(_PUBLIC_KEY_DER_B64))
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAX_FIELD_LEN = 255


def _truncate(value: str | None, limit: int = _MAX_FIELD_LEN) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


def _epoch_ms_to_datetime(value) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _make_nkparam(now_ms: int | None = None) -> str:
    """Create the anonymous SRP header exactly as Naukri's frontend does."""
    timestamp = now_ms if now_ms is not None else int(time.time() * 1000)
    plaintext = f"v0|{timestamp}|121_srp".encode()
    encrypted = _PUBLIC_KEY.encrypt(plaintext, padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def _placeholder(job: dict, field_type: str) -> str | None:
    for item in job.get("placeholders") or []:
        if item.get("type") == field_type:
            return _truncate(item.get("label"))
    return None


class NaukriConnector(BaseConnector):
    source_name = "naukri"

    def __init__(
        self,
        search: str,
        location_mode: str,
        *,
        location: str | None = None,
        max_results: int = NAUKRI_MAX_RESULTS,
        date_filter_days: int = RECENCY_WINDOW_DAYS,
    ):
        if location_mode not in {"bengaluru", "remote", "city", "india"}:
            raise ValueError(
                "location_mode must be 'bengaluru', 'remote', 'city', or 'india'"
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
        self.date_filter_days = date_filter_days

    def _params(self, page_num: int) -> dict[str, str | int]:
        slug = self.search.strip().lower().replace(" ", "-")
        location = self.location if self.location_mode == "city" else self.location_mode
        return {
            "noOfResults": NAUKRI_PAGE_SIZE,
            "urlType": "search_by_key_loc",
            "searchType": "adv",
            "location": location,
            "keyword": self.search,
            "pageNo": page_num,
            "jobAge": self.date_filter_days,
            "seoKey": f"{slug}-jobs-in-{location.lower().replace(' ', '-')}",
            "src": "directSearch",
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _get_json(self, page_num: int) -> dict:
        # The live frontend's anonymous search request carries no Cookie
        # header. Naukri nevertheless sets response cookies; replaying those
        # on later pages silently shrinks continuation responses to 20 rows.
        # Keep connection pooling, but preserve the frontend's cookie-free
        # request shape so every page honors noOfResults=100.
        self.client.cookies.clear()
        response = self.client.get(
            NAUKRI_SEARCH_API,
            params=self._params(page_num),
            headers={
                "Accept": "application/json",
                "appid": "109",
                "clientid": "naukri",
                "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
                "nkparam": _make_nkparam(),
                "Referer": "https://www.naukri.com/",
                "systemid": "109",
                "User-Agent": _USER_AGENT,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _list_jobs(self) -> list[dict]:
        results: list[dict] = []
        seen_ids: set[str] = set()
        page_num = 0
        while len(results) < self.max_results:
            page_num += 1
            try:
                payload = self._get_json(page_num)
            except Exception:
                break
            jobs = payload.get("jobDetails") or []
            if not isinstance(jobs, list) or not jobs:
                break

            new_count = 0
            for job in jobs:
                job_id = str(job.get("jobId") or "")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                results.append(job)
                new_count += 1
                if len(results) >= self.max_results:
                    break

            if len(results) >= self.max_results:
                break
            total = payload.get("noOfJobs")
            if isinstance(total, int) and len(results) >= total:
                break
            if len(jobs) < NAUKRI_PAGE_SIZE or new_count == 0:
                break
        return results

    def fetch(self) -> list[NormalizedJob]:
        normalized: list[NormalizedJob] = []
        for item in self._list_jobs():
            job_id = str(item.get("jobId") or "")
            title = _truncate(item.get("title"))
            jd_url = item.get("jdURL")
            if not job_id or not title or not jd_url:
                continue

            job_url = (
                f"https://www.naukri.com{jd_url}" if jd_url.startswith("/") else jd_url
            )
            redirect_url = item.get("applyRedirectUrl")
            apply_url = redirect_url if isinstance(redirect_url, str) and redirect_url else job_url
            location = _placeholder(item, "location")
            salary = _placeholder(item, "salary")
            if salary and "not disclosed" in salary.lower():
                salary = None
            is_remote = "remote" in (location or "").lower()

            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=job_id,
                    title=title,
                    company_name_raw=_truncate(item.get("companyName")) or "Unknown",
                    location_raw=location,
                    is_remote=is_remote,
                    remote_scope=location if is_remote else None,
                    employment_type=None,
                    seniority=None,
                    description_raw=item.get("jobDescription"),
                    salary_raw=salary,
                    apply_url=apply_url,
                    job_url=job_url,
                    posted_at=_epoch_ms_to_datetime(item.get("createdDate")),
                    raw_payload=item,
                )
            )
        return normalized
