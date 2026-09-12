"""Hacker News monthly "Who is hiring?" connector.

The official Algolia HN Search API needs no key. Monthly threads are
discovered through the shared recency window plus one month so that fresh job
comments on the preceding thread are not lost. Each thread's comments are then
searched with the exact shared cutoff. Only top-level comments are job posts;
nested replies are excluded by parent ID.

HN entries are free text. Their conventional first line is pipe-separated,
usually ``Company | Role | Location | ...``. Parsing remains deliberately
heuristic and unsupported fields remain null.
"""

import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS
from app.models.schemas import NormalizedJob

ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"

SEARCH_PAGE_SIZE = 1000
THREAD_BOUNDARY_BUFFER_DAYS = 31
THREAD_WORKERS = 3
MAX_SEARCH_PAGES = 20

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_TAG_RE = re.compile(r"<(?:p|br)\b[^>]*>", re.IGNORECASE)
_HIRING_THREAD_RE = re.compile(r"^Ask HN:\s*Who is hiring\?\s*\(", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
_INDIA_RE = re.compile(
    r"\b(india|bengaluru|bangalore|mumbai|delhi|hyderabad|pune|chennai)\b",
    re.IGNORECASE,
)
_LOCATION_HINT_RE = re.compile(
    r"\b(remote|onsite|on-site|hybrid|india|bengaluru|bangalore)\b",
    re.IGNORECASE,
)


def _strip_html(text: str) -> str:
    unescaped = html.unescape(text or "")
    with_breaks = _BREAK_TAG_RE.sub("\n", unescaped)
    return _TAG_RE.sub(" ", with_breaks).strip()


def _parse_first_line(first_line: str) -> tuple[str, str, str | None]:
    """Parse the common HN pipe convention into title/company/location."""
    segments = [segment.strip() for segment in first_line.split("|") if segment.strip()]
    if len(segments) >= 2 and len(segments[0]) <= 60:
        company = segments[0]
        # Posts do not consistently order role and location after company.
        # Preserve the remaining header so the title-only central role gate can
        # see roles without interpreting the prose description.
        title = " | ".join(segments[1:])[:200]
    else:
        company = "Unknown"
        title = first_line[:200]

    location_hint = next(
        (segment[:255] for segment in segments[1:] if _LOCATION_HINT_RE.search(segment)),
        None,
    )
    return title, company, location_hint


class HNHiringConnector(BaseConnector):
    source_name = "hn_hiring"
    # Algolia's search_by_date endpoint is explicitly newest-first. Scale its
    # 60-day page ceiling with the native sync window; unsorted sources do not
    # opt into this behavior.
    verified_newest_first = True
    recency_scaled_cap_attribute = "max_pages"

    def __init__(
        self,
        max_age_days: int = RECENCY_WINDOW_DAYS,
        india_or_remote_only: bool = True,
    ):
        if max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        self.max_age_days = max_age_days
        self.india_or_remote_only = india_or_remote_only
        self.max_pages = MAX_SEARCH_PAGES

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _search_page(self, params: dict[str, object]) -> dict:
        response = self.client.get(ALGOLIA_SEARCH_URL, params=params)
        response.raise_for_status()
        return response.json()

    def _find_recent_thread_ids(self, cutoff: datetime) -> list[str]:
        # The preceding monthly thread may still receive comments after cutoff.
        thread_cutoff = cutoff - timedelta(days=THREAD_BOUNDARY_BUFFER_DAYS)
        base_params: dict[str, object] = {
            "tags": "story,author_whoishiring",
            "query": "Who is hiring",
            "numericFilters": f"created_at_i>={int(thread_cutoff.timestamp())}",
            "hitsPerPage": SEARCH_PAGE_SIZE,
        }
        hits: list[dict] = []
        page = 0
        while page < self.max_pages:
            data = self._search_page({**base_params, "page": page})
            hits.extend(data.get("hits") or [])
            page += 1
            if page >= int(data.get("nbPages") or 0):
                break

        return [
            str(hit["objectID"])
            for hit in hits
            if hit.get("objectID") and _HIRING_THREAD_RE.search(hit.get("title") or "")
        ]

    def _fetch_recent_comments(self, thread_id: str, cutoff: datetime) -> list[dict]:
        base_params: dict[str, object] = {
            "tags": f"comment,story_{thread_id}",
            "numericFilters": f"created_at_i>={int(cutoff.timestamp())}",
            "hitsPerPage": SEARCH_PAGE_SIZE,
        }
        comments: list[dict] = []
        page = 0
        while page < self.max_pages:
            data = self._search_page({**base_params, "page": page})
            comments.extend(data.get("hits") or [])
            page += 1
            if page >= int(data.get("nbPages") or 0):
                break
        return comments

    def fetch(self) -> list[NormalizedJob]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)
        thread_ids = self._find_recent_thread_ids(cutoff)
        if not thread_ids:
            return []

        normalized: list[NormalizedJob] = []
        seen_ids: set[str] = set()
        with ThreadPoolExecutor(max_workers=min(THREAD_WORKERS, len(thread_ids))) as pool:
            batches = pool.map(
                lambda thread_id: self._fetch_recent_comments(thread_id, cutoff),
                thread_ids,
            )

            for thread_id, comments in zip(thread_ids, batches):
                for comment in comments:
                    if str(comment.get("parent_id")) != thread_id:
                        continue
                    comment_id = comment.get("objectID")
                    raw_text = comment.get("comment_text")
                    created = comment.get("created_at")
                    if not comment_id or not raw_text or not created:
                        continue
                    if str(comment_id) in seen_ids:
                        continue
                    try:
                        posted_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if posted_at < cutoff:
                        continue
                    seen_ids.add(str(comment_id))

                    clean_text = _strip_html(raw_text)
                    first_line = clean_text.split("\n")[0][:300].strip()
                    title, company, location_hint = _parse_first_line(first_line)

                    # The central location gate only sees structured location
                    # fields, so this source-specific free-text extraction is
                    # necessary. It does not act as the role authority.
                    is_remote = bool(_REMOTE_RE.search(clean_text))
                    mentions_india = bool(_INDIA_RE.search(clean_text))
                    if self.india_or_remote_only and not (is_remote or mentions_india):
                        continue

                    normalized.append(
                        NormalizedJob(
                            source=self.source_name,
                            external_job_id=str(comment_id),
                            title=title or "HN Hiring Post",
                            company_name_raw=company[:255],
                            location_raw=location_hint,
                            is_remote=is_remote,
                            remote_scope=location_hint,
                            employment_type=None,
                            seniority=None,
                            description_raw=clean_text,
                            salary_raw=None,
                            apply_url=f"https://news.ycombinator.com/item?id={comment_id}",
                            job_url=f"https://news.ycombinator.com/item?id={comment_id}",
                            posted_at=posted_at,
                            raw_payload=comment,
                        )
                    )

        return normalized
