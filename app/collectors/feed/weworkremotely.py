"""We Work Remotely connector using its official advanced search.

Live-verified 2026-08-14:

* ``/remote-jobs/search?term=...`` genuinely searches titles and descriptions;
  all six canonical terms return their full advertised inventory in one HTML
  response and a nonsense term returns zero.
* ``country[]=IN`` is real but excludes ``Anywhere in the World`` jobs, so an
  unrestricted role search plus the central India/remote gate is the correct
  inclusive policy. The separate official India route is useful evidence but
  currently contains only explicitly India-tagged jobs.
* ``page``, ``per_page``, and ``limit`` are ignored. The visible date filters
  work only up to two weeks, shorter than the shared recency window, so exact
  detail dates are filtered locally.

Six searches and the six public category feeds run with two workers; a larger
live burst timed out. RSS enriches current search hits cheaply. Detail JSON-LD
is fetched only for centrally role-viable jobs absent from those feeds, while all search
cards remain in raw inventory for gate accounting.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html as html_lib
import json
import re
import requests
from urllib.parse import urljoin
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.base import BaseConnector
from app.config.categories import RECENCY_WINDOW_DAYS, classify_title
from app.models.schemas import NormalizedJob

WWR_BASE_URL = "https://weworkremotely.com"
WWR_SEARCH_URL = f"{WWR_BASE_URL}/remote-jobs/search"
SEARCH_WORKERS = 2
DETAIL_WORKERS = 2
USER_AGENT = "job-agent/0.1 (personal job search audit)"
WWR_FEED_URL = f"{WWR_BASE_URL}/categories/{{category}}.rss"
RSS_CATEGORIES = [
    "remote-programming-jobs",
    "remote-management-and-finance-jobs",
    "remote-full-stack-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-devops-sysadmin-jobs",
    "remote-product-jobs",
]

_NON_LOCATION_LABELS = {"featured", "boosted", "top 100", "full-time", "contract"}


def _relative_date(value: str | None) -> datetime | None:
    if not value:
        return None
    now = datetime.now(timezone.utc)
    text = value.strip().lower()
    if text == "new":
        return now
    match = re.fullmatch(r"(\d+)\s*([hd])", text)
    if not match:
        return None
    amount = int(match.group(1))
    delta = timedelta(hours=amount) if match.group(2) == "h" else timedelta(days=amount)
    return now - delta


def _exact_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace(" UTC", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def _visible_labels(card) -> list[str]:
    return [
        node.get_text(" ", strip=True)
        for node in card.select(".new-listing__categories__category")
        if node.get_text(" ", strip=True)
    ]


def _card_scope(card, labels: list[str]) -> str | None:
    locations = []
    for label in labels:
        lowered = label.lower()
        if lowered in _NON_LOCATION_LABELS or lowered.startswith("$"):
            continue
        locations.append(label)
    if locations:
        return ", ".join(dict.fromkeys(locations))
    headquarters = card.select_one(".new-listing__company-headquarters")
    return headquarters.get_text(" ", strip=True) if headquarters else None


def _employment_type(labels: list[str]) -> str | None:
    for label in labels:
        if label.lower() in {"full-time", "contract"}:
            return label
    return None


def _salary(labels: list[str]) -> str | None:
    return next((label for label in labels if label.startswith("$")), None)


class WeWorkRemotelyConnector(BaseConnector):
    source_name = "weworkremotely"

    def __init__(
        self,
        search_terms: list[str],
        max_age_days: int | None = RECENCY_WINDOW_DAYS,
    ):
        self.search_terms = search_terms
        self.max_age_days = max_age_days

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_search(self, term: str) -> str:
        response = requests.get(
            WWR_SEARCH_URL,
            params={"term": term},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
        return response.text

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_detail(self, url: str) -> str:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        response.raise_for_status()
        return response.text

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_feed(self, category: str) -> str:
        response = requests.get(
            WWR_FEED_URL.format(category=category),
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
        return response.text

    def _parse_feed(self, feed_xml: str) -> dict[str, dict]:
        root = ElementTree.fromstring(feed_xml)
        parsed = {}
        for item in root.findall(".//item"):
            link = item.findtext("link")
            if not link:
                continue
            posted_at = None
            try:
                posted_at = parsedate_to_datetime(item.findtext("pubDate"))
            except (TypeError, ValueError):
                pass
            parsed[link] = {
                "description": (
                    BeautifulSoup(item.findtext("description") or "", "lxml").get_text("\n", strip=True)
                    or None
                ),
                "posted_at": posted_at,
                "location": item.findtext("region"),
                "payload": {
                    "title": item.findtext("title"),
                    "link": link,
                    "region": item.findtext("region"),
                    "pubDate": item.findtext("pubDate"),
                },
            }
        return parsed

    def _parse_search(self, term: str, page_html: str) -> list[dict]:
        soup = BeautifulSoup(page_html, "lxml")
        cards = []
        for link in soup.select("a.listing-link--unlocked[href]"):
            card = link.select_one(".new-listing")
            if card is None:
                continue
            title_node = card.select_one(".new-listing__header__title__text")
            company_node = card.select_one(".new-listing__company-name")
            date_node = card.select_one(".new-listing__header__icons__date")
            if title_node is None:
                continue
            labels = _visible_labels(card)
            relative_url = link.get("href")
            cards.append(
                {
                    "external_job_id": relative_url,
                    "job_url": urljoin(WWR_BASE_URL, relative_url),
                    "title": title_node.get_text(" ", strip=True),
                    "company": company_node.get_text(" ", strip=True) if company_node else "Unknown",
                    "posted_at": _relative_date(date_node.get_text(strip=True) if date_node else None),
                    "location": _card_scope(card, labels),
                    "employment_type": _employment_type(labels),
                    "salary": _salary(labels),
                    "labels": labels,
                    "search_terms": {term},
                }
            )

        count_node = soup.select_one("[data-job-filter-count]")
        if count_node:
            advertised_text = count_node.get_text(strip=True)
            if advertised_text.isdigit() and int(advertised_text) != len(cards):
                raise RuntimeError(
                    f"WWR search {term!r} advertised {advertised_text} jobs but returned {len(cards)}"
                )
        return cards

    def _parse_detail(self, detail_html: str) -> dict:
        soup = BeautifulSoup(detail_html, "lxml")
        payload = None
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                candidate = json.loads(script.get_text(strip=True))
            except (TypeError, json.JSONDecodeError):
                continue
            if candidate.get("@type") == "JobPosting":
                payload = candidate
                break
        if payload is None:
            return {}

        description = None
        if payload.get("description"):
            decoded = html_lib.unescape(payload["description"])
            description = BeautifulSoup(decoded, "lxml").get_text("\n", strip=True) or None

        region_node = soup.select_one(".box--region")
        region = region_node.get_text(" ", strip=True) if region_node else None
        salary = None
        base_salary = payload.get("baseSalary")
        value = base_salary.get("value") if isinstance(base_salary, dict) else None
        if isinstance(value, dict):
            low, high = value.get("minValue"), value.get("maxValue")
            if str(low or "0") != "0" or str(high or "0") != "0":
                bounds = f"{low or ''}-{high or ''}".strip("-")
                salary = " ".join(
                    part
                    for part in (bounds, base_salary.get("currency"), value.get("unitText"))
                    if part
                )
        return {
            "description": description,
            "posted_at": _exact_date(payload.get("datePosted")),
            "employment_type": payload.get("employmentType"),
            "salary": salary,
            "location": region,
            "payload": payload,
        }

    def fetch(self) -> list[NormalizedJob]:
        cards_by_url: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as executor:
            future_terms = {
                executor.submit(self._fetch_search, term): term for term in self.search_terms
            }
            for future in as_completed(future_terms):
                term = future_terms[future]
                for card in self._parse_search(term, future.result()):
                    existing = cards_by_url.get(card["job_url"])
                    if existing is None:
                        cards_by_url[card["job_url"]] = card
                    else:
                        existing["search_terms"].add(term)

        rss_details = {}
        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as executor:
            futures = [executor.submit(self._fetch_feed, category) for category in RSS_CATEGORIES]
            for future in as_completed(futures):
                rss_details.update(self._parse_feed(future.result()))

        detail_urls = [
            url
            for url, card in cards_by_url.items()
            if classify_title(card["title"]) is not None
            and not rss_details.get(url, {}).get("description")
        ]
        details = {}
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
            future_urls = {executor.submit(self._fetch_detail, url): url for url in detail_urls}
            for future in as_completed(future_urls):
                url = future_urls[future]
                details[url] = self._parse_detail(future.result())

        cutoff = None
        if self.max_age_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)

        normalized = []
        for url, card in cards_by_url.items():
            detail = details.get(url) or rss_details.get(url) or {}
            posted_at = detail.get("posted_at") or card["posted_at"]
            if cutoff is not None and posted_at is not None and posted_at < cutoff:
                continue
            scope = detail.get("location") or card["location"]
            normalized.append(
                NormalizedJob(
                    source=self.source_name,
                    external_job_id=card["external_job_id"],
                    title=card["title"],
                    company_name_raw=card["company"][:255],
                    location_raw=scope and scope[:255],
                    is_remote=True,
                    remote_scope=scope and scope[:255],
                    employment_type=detail.get("employment_type") or card["employment_type"],
                    seniority=None,
                    description_raw=detail.get("description"),
                    salary_raw=detail.get("salary") or card["salary"],
                    apply_url=url,
                    job_url=url,
                    posted_at=posted_at,
                    raw_payload={
                        "search_terms": sorted(card["search_terms"]),
                        "labels": card["labels"],
                        "detail": detail.get("payload"),
                    },
                )
            )
        return normalized
