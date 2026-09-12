"""Fast, deterministic company market-profile batch orchestration.

One search request resolves each Glassdoor Overview URL. AmbitionBox resolves
canonical slugs concurrently through Naukri taxonomy, then requests at most
four adaptively paced role salary pages; Levels.fyi uses a deterministic
company slug only to reach the broad salaries page, then follows its role inventory
exposed there. Both verify identity from their own page data before anything
is persisted. Database writes are isolated per company and commit
automatically.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.companies.ambitionbox import (
    AmbitionBoxCollector,
    AmbitionBoxObservation,
    AmbitionBoxTarget,
    load_naukri_company_judgments,
)
from app.companies.market_profile import (
    calculate_market_profile,
    save_ambitionbox_market_profile,
    save_glassdoor_market_profile,
)
from app.companies.market_scraper import (
    _role_family,
    scrape_glassdoor_market_profile,
    scrape_levels_fyi_market_profile,
    scrape_market_profile,
)
from app.companies.naming import is_placeholder, normalize
from app.config import settings
from app.config.categories import SEARCH_TERMS
from app.contacts.search_source import bright_data_search_once
from app.db.session import SessionLocal
from app.models.orm import Company, Job, utcnow

try:  # POSIX cross-process file locks.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows CI.
    _fcntl = None

try:  # Windows cross-process file locks.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX CI.
    _msvcrt = None


BRIGHT_DATA_DATASET_ID = "gd_l7j0bx501ockwldaqf"
BRIGHT_DATA_TRIGGER_ENDPOINT = "https://api.brightdata.com/datasets/v3/trigger"
BRIGHT_DATA_PROGRESS_ENDPOINT = "https://api.brightdata.com/datasets/v3/progress"
BRIGHT_DATA_SNAPSHOT_ENDPOINT = "https://api.brightdata.com/datasets/v3/snapshot"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
}
_GLASSDOOR_ID = re.compile(r"EI_IE(\d+)")
_LEGAL_SUFFIXES = re.compile(
    r"\b(?:private\s+limited|pvt\.?\s+ltd\.?|limited|incorporated|"
    r"corporation|llc|inc\.?|ltd\.?|corp\.?|gmbh|llp|plc)\b",
    re.I,
)
_TRUNCATED_COMPANY = re.compile(r"^[A-Za-z]\.{3}$")
logger = logging.getLogger(__name__)

_AMBITIONBOX_PROCESS_LOCK = Path(tempfile.gettempdir()) / "job-atlas-ambitionbox.lock"


@contextmanager
def _ambitionbox_process_lock():
    """Serialize AmbitionBox collection across independently running workers."""
    _AMBITIONBOX_PROCESS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _AMBITIONBOX_PROCESS_LOCK.open("a+b") as handle:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        elif _msvcrt is not None:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - Python's supported desktop OSes use one.
            raise RuntimeError("no supported process-lock implementation")
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            else:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True)
class CompanyTarget:
    company_id: int
    company_name: str
    salary_role: str
    qualifying_job_count: int


@dataclass(frozen=True)
class LevelsFyiTarget:
    company_id: int
    company_name: str
    salary_role: str
    qualifying_job_count: int
    company_slug: str


@dataclass(frozen=True)
class SourceResolution:
    target: CompanyTarget
    source_urls: dict[str, dict[str, str]]
    glassdoor_source_id: str | None
    errors: dict[str, str]


@dataclass(frozen=True)
class CompanyRunResult:
    company_id: int
    company_name: str
    status: str
    source_statuses: dict[str, str]
    error: str | None = None


def load_company_ids_file(path: str | Path) -> list[int]:
    """Load an exact, ordered company-ID manifest.

    Accept either one integer per line or a CSV with a ``company_id`` column.
    The manifest order is authoritative; callers must not replace it with the
    database selector's qualifying-job ordering.
    """
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"company ID manifest is empty: {source}")
    if lines[0].lower().split(",", 1)[0].strip() == "company_id":
        reader = csv.DictReader(text.splitlines())
        raw_ids = [row.get("company_id", "") for row in reader]
    else:
        raw_ids = [line.split(",", 1)[0].strip() for line in lines]
    ids: list[int] = []
    for index, raw in enumerate(raw_ids, start=1):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid company ID at manifest row {index}: {raw!r}") from exc
        if value <= 0:
            raise ValueError(f"company ID must be positive at manifest row {index}")
        ids.append(value)
    if len(ids) != len(set(ids)):
        raise ValueError(f"company ID manifest contains duplicates: {source}")
    return ids


def load_glassdoor_resolution_artifact(
    path: str | Path,
) -> dict[int, dict[str, Any]]:
    """Load reviewed internal-search decisions keyed by company ID."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("Glassdoor resolution artifact must use schema_version=1")
    rows = payload.get("resolutions")
    if not isinstance(rows, list):
        raise ValueError("Glassdoor resolution artifact requires a resolutions list")

    decisions: dict[int, dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"Glassdoor resolution row {index} must be an object")
        company_id = raw.get("company_id")
        if isinstance(company_id, bool) or not isinstance(company_id, int):
            raise ValueError(f"Glassdoor resolution row {index} has invalid company_id")
        if company_id in decisions:
            raise ValueError(f"duplicate Glassdoor decision for company {company_id}")
        decision = dict(raw)
        status = decision.get("status")
        if status not in {"accepted", "unresolved"}:
            raise ValueError(
                f"Glassdoor resolution row {index} has invalid status {status!r}"
            )
        if status == "accepted":
            employer_id = str(decision.get("employer_id") or "")
            overview_url = str(decision.get("overview_url") or "")
            match = _GLASSDOOR_ID.search(overview_url)
            if not employer_id.isdigit() or match is None:
                raise ValueError(
                    f"accepted Glassdoor row {index} requires a valid employer ID URL"
                )
            if match.group(1) != employer_id:
                raise ValueError(
                    f"Glassdoor row {index} employer ID does not match its URL"
                )
            if not str(decision.get("observed_name") or "").strip():
                raise ValueError(
                    f"accepted Glassdoor row {index} requires observed_name"
                )
        decisions[company_id] = decision
    return decisions


def _resolution_from_reviewed_decision(
    target: CompanyTarget,
    decision: Mapping[str, Any] | None,
) -> SourceResolution:
    slug = _source_slug(target.company_name)
    source_urls: dict[str, dict[str, str]] = {
        "ambitionbox": {
            "company_name": target.company_name,
            "slug": slug,
            "salaries": f"https://www.ambitionbox.com/salaries/{slug}-salaries",
        },
        "levels_fyi": {
            "company_name": target.company_name,
            "company_slug": slug,
        },
    }
    if decision and decision.get("status") == "accepted":
        source_id = str(decision["employer_id"])
        source_urls["glassdoor"] = {
            "company_name": str(decision["observed_name"]),
            "overview": str(decision["overview_url"]),
            "source_id": source_id,
            "searched_name": str(decision.get("searched_name") or target.company_name),
            "resolution_pass": str(decision.get("resolution_pass") or "original"),
        }
        return SourceResolution(target, source_urls, source_id, {})

    source_urls["glassdoor"] = {"company_name": target.company_name}
    reason = (
        str(decision.get("reason") or "internal search left unresolved")
        if decision
        else "no reviewed internal-search decision supplied"
    )
    return SourceResolution(target, source_urls, None, {"glassdoor": reason})


Search = Callable[[str], Sequence[Mapping[str, Any]]]


def _source_slug(name: str) -> str:
    value = re.sub(r"\([^)]*\)", " ", name.lower())
    value = _LEGAL_SUFFIXES.sub(" ", value)
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value)).strip("-")


def _glassdoor_title_company(title: str) -> str:
    value = re.sub(r"\s*[|\-]\s*Glassdoor.*$", "", title, flags=re.I).strip()
    return re.sub(r"^Working at\s+", "", value, flags=re.I).strip()


def _glassdoor_resolution(
    company_name: str,
    results: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None, str | None]:
    matches: list[tuple[str, str, str]] = []
    for result in results:
        link = str(result.get("link") or "")
        title = str(result.get("title") or "")
        source_id = _GLASSDOOR_ID.search(link)
        if not source_id or "/Overview/" not in link:
            continue
        observed_name = _glassdoor_title_company(title)
        if normalize(observed_name) != normalize(company_name):
            continue
        candidate = (source_id.group(1), link, observed_name)
        if candidate not in matches:
            matches.append(candidate)

    if not matches:
        return None, None, "no identity-matched Glassdoor Overview result"
    # Search rank is the deterministic tiebreak when Glassdoor has duplicate
    # exact-name profiles. The paid record must still return this same employer
    # ID and identity before its values are accepted.
    source_id, url, observed_name = matches[0]
    return source_id, url, observed_name


def resolve_company_sources(target: CompanyTarget, search: Search) -> SourceResolution:
    """Resolve one company without an LLM, using search rank as the tiebreak."""
    errors: dict[str, str] = {}
    try:
        results = search(
            f'site:glassdoor.com/Overview "Working at" "{target.company_name}"'
        )
    except Exception as exc:
        results = []
        errors["glassdoor"] = f"{type(exc).__name__}: {exc}"

    source_id, overview_url, observed_name = _glassdoor_resolution(
        target.company_name,
        results,
    )
    if source_id is None and "glassdoor" not in errors:
        errors["glassdoor"] = observed_name or "unresolved"

    slug = _source_slug(target.company_name)
    source_urls: dict[str, dict[str, str]] = {
        "ambitionbox": {
            "company_name": target.company_name,
            "slug": slug,
            "salaries": f"https://www.ambitionbox.com/salaries/{slug}-salaries",
        },
        "levels_fyi": {
            "company_name": target.company_name,
            "company_slug": slug,
        },
    }
    if source_id and overview_url and observed_name:
        source_urls["glassdoor"] = {
            "company_name": observed_name,
            "overview": overview_url,
            "source_id": source_id,
        }
    else:
        source_urls["glassdoor"] = {"company_name": target.company_name}

    return SourceResolution(target, source_urls, source_id, errors)


class BrightDataCompanySearch:
    """One Bright Data SERP Direct search for each company resolution."""

    def __init__(self) -> None:
        if not settings.BRIGHT_DATA_API_KEY:
            raise RuntimeError(
                "BRIGHT_DATA_API_KEY is required for company resolution"
            )

    def close(self) -> None:
        """Match the batch search lifecycle; the shared transport is stateless."""

    def __call__(self, query: str) -> Sequence[Mapping[str, Any]]:
        return [
            {
                "title": row.title,
                "link": row.url,
                "snippet": row.snippet,
            }
            for row in bright_data_search_once(query)
        ]


class BrightDataGlassdoorClient:
    """Bulk Glassdoor adapter; one paid snapshot for the whole resolved batch."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.BRIGHT_DATA_API_KEY
        if not self._api_key:
            raise RuntimeError("BRIGHT_DATA_API_KEY is required for Glassdoor")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=120,
        )

    def close(self) -> None:
        self._client.close()

    def _download_snapshot(
        self, snapshot_id: str,
    ) -> list[Mapping[str, Any]] | None:
        download = self._client.get(
            f"{BRIGHT_DATA_SNAPSHOT_ENDPOINT}/{snapshot_id}",
            params={"format": "json"},
        )
        if getattr(download, "status_code", None) == 202:
            return None
        download.raise_for_status()
        records = download.json()
        if not isinstance(records, list) or not all(
            isinstance(record, Mapping) for record in records
        ):
            raise RuntimeError("Bright Data snapshot was not a JSON record list")
        return records

    @staticmethod
    def _is_terminal_payload(
        records: Sequence[Mapping[str, Any]], expected_count: int,
    ) -> bool:
        """Bright Data can expose a complete payload before progress says ready."""
        return len(records) == expected_count and all(
            record.get("id") is not None or record.get("error")
            for record in records
        )

    def collect(
        self,
        resolutions: Sequence[SourceResolution],
        *,
        snapshot_id: str | None = None,
        snapshot_callback: Callable[[str, int], None] | None = None,
        poll_seconds: float = 5,
        timeout_seconds: float = 900,
    ) -> dict[str, Mapping[str, Any]]:
        # Dataset billing is per input record. Keying by the resolved employer
        # ID guarantees no company/employer can contribute more than one input.
        inputs_by_id = {
            item.glassdoor_source_id: {
                "url": item.source_urls["glassdoor"]["overview"],
            }
            for item in resolutions
            if item.glassdoor_source_id is not None
        }
        inputs = list(inputs_by_id.values())
        if not inputs:
            return {}

        if snapshot_id is None:
            trigger = self._client.post(
                BRIGHT_DATA_TRIGGER_ENDPOINT,
                params={
                    "dataset_id": BRIGHT_DATA_DATASET_ID,
                    "include_errors": "true",
                },
                json=inputs,
            )
            trigger.raise_for_status()
            snapshot_id = trigger.json()["snapshot_id"]
            if snapshot_callback is not None:
                snapshot_callback(snapshot_id, len(inputs))
            logger.info(
                "Triggered Glassdoor snapshot %s for %d resolved companies",
                snapshot_id,
                len(inputs),
            )
        else:
            if snapshot_callback is not None:
                snapshot_callback(snapshot_id, len(inputs))
            logger.info(
                "Resuming existing Glassdoor snapshot %s; no new snapshot triggered",
                snapshot_id,
            )

        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                progress = self._client.get(
                    f"{BRIGHT_DATA_PROGRESS_ENDPOINT}/{snapshot_id}",
                )
            except httpx.TimeoutException as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Bright Data snapshot {snapshot_id} timed out",
                    ) from exc
                logger.warning(
                    "Transient timeout polling Glassdoor snapshot %s; retrying",
                    snapshot_id,
                )
                time.sleep(poll_seconds)
                continue
            progress.raise_for_status()
            status = progress.json().get("status")
            if status in {"failed", "error"}:
                raise RuntimeError(f"Bright Data snapshot {snapshot_id} {status}")
            # Bright Data's progress endpoint can remain "running" after its
            # snapshot endpoint has materialized one terminal result per input.
            # Accept that payload rather than spinning until the poll timeout.
            downloaded = self._download_snapshot(snapshot_id)
            if downloaded is not None and self._is_terminal_payload(
                downloaded, len(inputs),
            ):
                logger.info(
                    "Glassdoor snapshot %s payload is complete while progress is %s",
                    snapshot_id,
                    status,
                )
                return {
                    str(record["id"]): record
                    for record in downloaded
                    if record.get("id") is not None
                }
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Bright Data snapshot {snapshot_id} timed out")
            time.sleep(poll_seconds)


class BoundedPageLoader:
    """Shared HTTP adapter for Levels.fyi's independent request limit."""

    def __init__(
        self,
        *,
        levels_requests: int = 8,
    ) -> None:
        self._client = httpx.Client(
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
            timeout=30,
        )
        self._limits = {
            "levels_fyi": threading.Semaphore(max(1, levels_requests)),
        }

    def close(self) -> None:
        self._client.close()

    def __call__(self, source: str, url: str) -> str:
        semaphore = self._limits.get(source)
        if semaphore is None:
            raise ValueError(f"unsupported page source {source!r}")
        with semaphore:
            response = self._client.get(url)
            response.raise_for_status()
            return response.text


def _job_role_rank(title: str) -> int:
    family = _role_family(title)
    families = [_role_family(term) for term in SEARCH_TERMS]
    return families.index(family) if family in families else len(families)


def select_market_profile_targets(
    session: Session,
    *,
    limit: int | None,
    include_in_progress: bool = False,
    include_done: bool = False,
    cutoff_days: int = 15,
    remote_only: bool = False,
    claim: bool = True,
    company_ids: Sequence[int] | None = None,
) -> list[CompanyTarget]:
    """Select the live company cohort and deterministically choose a role.

    Work mode is not an intrinsic market-profile eligibility rule. Callers
    may preserve an explicitly requested remote-only cohort by setting
    ``remote_only``; otherwise both remote and non-remote postings qualify.
    """
    statuses = ["pending", "partial"]
    if include_in_progress:
        statuses.append("in_progress")
    if include_done:
        statuses.append("done")
    cutoff = utcnow() - timedelta(days=cutoff_days)
    filters = [
        Job.status == "active",
        Company.company_type == "employer",
        Company.market_profile_status.in_(statuses),
    ]
    manifest_order: dict[int, int] | None = None
    if company_ids is not None:
        manifest_order = {company_id: index for index, company_id in enumerate(company_ids)}
        filters.append(Company.id.in_(list(manifest_order)))
    else:
        filters.append(Job.posted_at >= cutoff)
    if remote_only:
        filters.append(Job.is_remote.is_(True))
    rows = session.execute(
        select(Company, Job)
        .join(Job, Job.company_id == Company.id)
        .where(*filters)
    ).all()

    grouped: dict[int, tuple[Company, list[Job]]] = {}
    for company, job in rows:
        if is_placeholder(company.name) or _TRUNCATED_COMPANY.fullmatch(company.name):
            continue
        grouped.setdefault(company.id, (company, []))[1].append(job)

    targets: list[CompanyTarget] = []
    if manifest_order is not None:
        ordered = sorted(
            grouped.values(),
            key=lambda item: manifest_order.get(item[0].id, len(manifest_order)),
        )
    else:
        ordered = sorted(grouped.values(), key=lambda item: (-len(item[1]), item[0].id))
    for company, jobs in ordered[:limit]:
        selected_job = min(
            jobs,
            key=lambda job: (
                _job_role_rank(job.title),
                -(job.posted_at.timestamp() if job.posted_at else 0),
            ),
        )
        targets.append(
            CompanyTarget(company.id, company.name, selected_job.title, len(jobs))
        )
        if claim:
            company.market_profile_status = "in_progress"
    if claim:
        session.flush()
    return targets


def select_levels_fyi_retry_targets(
    session: Session,
    *,
    limit: int | None,
    cutoff_days: int = 15,
    remote_only: bool = False,
) -> list[LevelsFyiTarget]:
    """Select live companies whose Levels.fyi observation is not successful."""
    company_targets = select_market_profile_targets(
        session,
        limit=None,
        include_in_progress=True,
        include_done=True,
        cutoff_days=cutoff_days,
        remote_only=remote_only,
        claim=False,
    )
    selected: list[LevelsFyiTarget] = []
    for target in company_targets:
        company = session.get(Company, target.company_id)
        if company is None:
            continue
        profile = company.market_profile_evidence
        profile = profile if isinstance(profile, Mapping) else {}
        sources = profile.get("sources")
        sources = sources if isinstance(sources, Mapping) else {}
        source = sources.get("levels_fyi")
        source = source if isinstance(source, Mapping) else {}
        if source.get("status") == "ok":
            continue
        slug = str(source.get("slug") or source.get("company_slug") or "")
        if not slug:
            broad_url = str(source.get("broad_url") or source.get("url") or "")
            match = re.search(r"/companies/([^/]+)/salaries(?:/|$)", broad_url)
            slug = match.group(1) if match else _source_slug(company.name)
        selected.append(
            LevelsFyiTarget(
                company.id,
                company.name,
                target.salary_role,
                target.qualifying_job_count,
                slug,
            )
        )
        if limit is not None and len(selected) >= limit:
            break
    return selected


def resolve_targets(
    targets: Sequence[CompanyTarget],
    search: Search,
    *,
    workers: int = 16,
) -> list[SourceResolution]:
    """Resolve companies concurrently while preserving input order."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(resolve_company_sources, target, search): index
            for index, target in enumerate(targets)
        }
        resolved: dict[int, SourceResolution] = {}
        for future in as_completed(futures):
            resolved[futures[future]] = future.result()
    return [resolved[index] for index in range(len(targets))]


def _ambitionbox_targets(
    resolutions: Sequence[SourceResolution],
) -> list[AmbitionBoxTarget]:
    targets: list[AmbitionBoxTarget] = []
    for resolution in resolutions:
        config = resolution.source_urls.get("ambitionbox", {})
        salary_url = config.get("salaries")
        slug = config.get("slug")
        if not salary_url or not slug:
            continue
        targets.append(
            AmbitionBoxTarget(
                company_id=resolution.target.company_id,
                company_name=config.get(
                    "company_name",
                    resolution.target.company_name,
                ),
                salary_role=resolution.target.salary_role,
                slug=slug,
                salary_url=salary_url,
            ),
        )
    return targets


def _ambitionbox_retryable(
    source: Mapping[str, Any],
    *,
    salary_present: bool,
) -> bool:
    status = source.get("status")
    resolution = source.get("resolution")
    already_resolved = isinstance(resolution, Mapping)
    if status in {None, "not_attempted", "deferred"}:
        return True
    if status == "ok":
        if salary_present:
            return False
        lookup = source.get("salary_lookup")
        if not isinstance(lookup, Mapping):
            return True
        return lookup.get("status") in {"error", "deferred", "not_attempted"} and (
            lookup.get("retryable") is not False
        )
    if status == "missing":
        if already_resolved:
            return False
        legacy_error = " ".join(
            str(source.get(key) or "")
            for key in ("error", "legacy_error", "reason")
        )
        return "404" in legacy_error
    if status != "error":
        return False
    if source.get("retryable") is True:
        return True
    error = str(source.get("error") or "")
    if "403" in error or "429" in error:
        return True
    return "404" in error and not already_resolved


def select_ambitionbox_retry_targets(
    session: Session,
    *,
    limit: int | None,
    cutoff_days: int = 15,
    remote_only: bool = False,
    company_ids: Sequence[int] | None = None,
) -> list[AmbitionBoxTarget]:
    """Select only retryable AmbitionBox work from the live company cohort."""
    company_targets = select_market_profile_targets(
        session,
        limit=None,
        include_done=True,
        cutoff_days=cutoff_days,
        remote_only=remote_only,
        claim=False,
        company_ids=company_ids,
    )
    selected: list[AmbitionBoxTarget] = []
    for target in company_targets:
        company = session.get(Company, target.company_id)
        if company is None:
            continue
        evidence = company.market_profile_evidence
        evidence = evidence if isinstance(evidence, Mapping) else {}
        sources = evidence.get("sources")
        sources = sources if isinstance(sources, Mapping) else {}
        source = sources.get("ambitionbox")
        source = source if isinstance(source, Mapping) else {}
        if not _ambitionbox_retryable(
            source,
            salary_present=company.ambitionbox_estimated_salary_lpa is not None,
        ):
            continue
        old_urls = source.get("urls")
        old_urls = old_urls if isinstance(old_urls, Mapping) else {}
        resolution = source.get("resolution")
        resolution = resolution if isinstance(resolution, Mapping) else {}
        slug = str(
            resolution.get("resolved_slug")
            or source.get("slug")
            or old_urls.get("slug")
            or _source_slug(company.name)
        )
        salary_url = str(
            resolution.get("resolved_url")
            or source.get("url")
            or source.get("salaries_url")
            or old_urls.get("salaries")
            or f"https://www.ambitionbox.com/salaries/{slug}-salaries"
        )
        known_broad_observation = None
        if (
            source.get("status") == "ok"
            and company.ambitionbox_estimated_salary_lpa is None
        ):
            known_broad_observation = AmbitionBoxObservation(
                company_id=company.id,
                status="ok",
                overall_rating=company.ambitionbox_overall_rating,
                wlb_rating=company.ambitionbox_wlb_rating,
                salary_lpa=None,
                evidence=dict(source),
            )
        selected.append(
            AmbitionBoxTarget(
                company_id=company.id,
                company_name=company.name,
                salary_role=target.salary_role,
                slug=slug,
                salary_url=salary_url,
                known_broad_observation=known_broad_observation,
            ),
        )
        if limit is not None and len(selected) >= limit:
            break
    return selected


def normalize_legacy_ambitionbox_404s(
    session_factory: sessionmaker = SessionLocal,
    *,
    cutoff_days: int = 15,
    remote_only: bool = False,
    company_ids: Sequence[int] | None = None,
) -> int:
    """Convert old retry-era 404 errors to the terminal ``missing`` state."""
    with session_factory() as session:
        candidates = select_market_profile_targets(
            session,
            limit=None,
            cutoff_days=cutoff_days,
            remote_only=remote_only,
            claim=False,
            company_ids=company_ids,
        )
        company_ids = [target.company_id for target in candidates]

    normalized = 0
    for company_id in company_ids:
        with session_factory() as session:
            company = session.get(Company, company_id)
            if company is None:
                continue
            profile = company.market_profile_evidence
            profile = profile if isinstance(profile, Mapping) else {}
            sources = profile.get("sources")
            sources = sources if isinstance(sources, Mapping) else {}
            source = sources.get("ambitionbox")
            source = source if isinstance(source, Mapping) else {}
            error = str(source.get("error") or "")
            if source.get("status") != "error" or "404" not in error:
                continue
            old_urls = source.get("urls")
            old_urls = old_urls if isinstance(old_urls, Mapping) else {}
            evidence = {
                "status": "missing",
                "reason": "HTTP 404 Not Found",
                "url": (
                    source.get("url")
                    or source.get("salaries_url")
                    or old_urls.get("salaries")
                ),
                "slug": source.get("slug") or old_urls.get("slug"),
                "legacy_error": error,
            }
            save_ambitionbox_market_profile(
                session,
                company.id,
                overall_rating=None,
                wlb_rating=None,
                salary_lpa=None,
                evidence=evidence,
            )
            calculate_market_profile(session, company.id)
            session.commit()
            normalized += 1
    if normalized:
        logger.info("Normalized %d legacy AmbitionBox 404 rows", normalized)
    return normalized


def _save_unexpected_failure(
    session_factory: sessionmaker,
    resolution: SourceResolution,
    exc: Exception,
) -> CompanyRunResult:
    error = f"{type(exc).__name__}: {exc}"
    with session_factory() as session:
        company = session.get(Company, resolution.target.company_id)
        if company is not None:
            evidence = dict(company.market_profile_evidence or {})
            evidence["batch_error"] = error
            evidence["resolution_errors"] = resolution.errors
            company.market_profile_evidence = evidence
            company.market_profile_status = "partial"
            company.market_profile_updated_at = utcnow()
            session.commit()
    return CompanyRunResult(
        resolution.target.company_id,
        resolution.target.company_name,
        "partial",
        {},
        error,
    )


def _persist_resolved_glassdoor_ids(
    session_factory: sessionmaker,
    resolutions: Sequence[SourceResolution],
) -> None:
    """Save resolved employer IDs before any downstream paid collection."""
    with session_factory() as session:
        for resolution in resolutions:
            if resolution.glassdoor_source_id is None:
                continue
            company = session.get(Company, resolution.target.company_id)
            if company is None:
                raise LookupError(
                    f"company {resolution.target.company_id} was not found"
                )
            company.glassdoor_employer_id = resolution.glassdoor_source_id
        session.commit()


def _verified_glassdoor_source(
    company: Company,
    expected_employer_id: str,
) -> Mapping[str, Any] | None:
    """Return reusable successful evidence only when its employer ID is proven."""
    evidence = company.market_profile_evidence
    sources = evidence.get("sources") if isinstance(evidence, Mapping) else None
    source = sources.get("glassdoor") if isinstance(sources, Mapping) else None
    if not isinstance(source, Mapping) or source.get("status") != "ok":
        return None
    source_id = str(company.glassdoor_employer_id or source.get("source_id") or "")
    source_url = str(source.get("url") or source.get("requested_url") or "")
    match = _GLASSDOOR_ID.search(source_url)
    if (
        source_id != expected_employer_id
        or match is None
        or match.group(1) != expected_employer_id
    ):
        return None
    return source


def _reuse_verified_glassdoor_groups(
    session_factory: sessionmaker,
    resolutions: Sequence[SourceResolution],
) -> tuple[list[SourceResolution], list[CompanyRunResult]]:
    """Reuse one fresh row for every matching employer-ID alias group.

    Any employer ID with a verified successful row is excluded from paid
    collection. Its ratings and source evidence are copied to unresolved
    aliases in the reviewed artifact, preserving each alias's requested URL.
    """
    by_employer_id: dict[str, list[SourceResolution]] = {}
    for resolution in resolutions:
        if resolution.glassdoor_source_id is not None:
            by_employer_id.setdefault(resolution.glassdoor_source_id, []).append(
                resolution,
            )

    reusable_results: list[CompanyRunResult] = []
    pending: list[SourceResolution] = []
    with session_factory() as session:
        for employer_id, group in by_employer_id.items():
            companies = {
                resolution.target.company_id: session.get(
                    Company,
                    resolution.target.company_id,
                )
                for resolution in group
            }
            donor = next(
                (
                    company
                    for company in companies.values()
                    if company is not None
                    and _verified_glassdoor_source(company, employer_id) is not None
                ),
                None,
            )
            if donor is None:
                pending.extend(group)
                continue

            donor_source = _verified_glassdoor_source(donor, employer_id)
            assert donor_source is not None
            for resolution in group:
                company = companies[resolution.target.company_id]
                if company is None:
                    raise LookupError(
                        f"company {resolution.target.company_id} was not found"
                    )
                existing = _verified_glassdoor_source(company, employer_id)
                if existing is None:
                    reused_evidence = dict(donor_source)
                    reused_evidence.update(
                        {
                            "requested_url": resolution.source_urls["glassdoor"][
                                "overview"
                            ],
                            "source_id": employer_id,
                            "reused_from_company_id": donor.id,
                            "resolution_pass": resolution.source_urls["glassdoor"].get(
                                "resolution_pass",
                            ),
                        },
                    )
                    company = save_glassdoor_market_profile(
                        session,
                        company.id,
                        overall_rating=donor.glassdoor_overall_rating,
                        wlb_rating=donor.glassdoor_wlb_rating,
                        review_count=donor.glassdoor_review_count,
                        employee_count_range=donor.employee_count_range,
                        ownership_type=donor.ownership_type,
                        revenue=donor.revenue,
                        evidence=reused_evidence,
                    )
                    calculate_market_profile(session, company.id)
                reusable_results.append(
                    CompanyRunResult(
                        company.id,
                        company.name,
                        company.market_profile_status,
                        {"glassdoor": "ok"},
                    ),
                )
        session.commit()
    return pending, reusable_results


def _populate_one(
    session_factory: sessionmaker,
    resolution: SourceResolution,
    glassdoor_records: Mapping[str, Mapping[str, Any]],
    ambitionbox_records: Mapping[int, Mapping[str, Any]],
) -> CompanyRunResult:
    try:
        record = (
            glassdoor_records.get(resolution.glassdoor_source_id)
            if resolution.glassdoor_source_id
            else None
        )
        with session_factory() as session:
            company = scrape_market_profile(
                session,
                resolution.target.company_id,
                salary_role=resolution.target.salary_role,
                source_urls=resolution.source_urls,
                glassdoor_record=record,
                ambitionbox_record=ambitionbox_records.get(
                    resolution.target.company_id,
                ),
            )
            evidence = dict(company.market_profile_evidence or {})
            evidence["resolution_errors"] = resolution.errors
            company.market_profile_evidence = evidence
            calculate_market_profile(session, company.id)
            session.commit()
            statuses = {
                source: str(item.get("status"))
                for source, item in evidence.get("sources", {}).items()
                if isinstance(item, Mapping)
            }
            return CompanyRunResult(
                company.id,
                company.name,
                company.market_profile_status,
                statuses,
            )
    except Exception as exc:
        return _save_unexpected_failure(session_factory, resolution, exc)


def run_market_profile_batch(
    *,
    limit: int | None = 100,
    include_in_progress: bool = False,
    resolution_workers: int = 16,
    company_workers: int = 12,
    levels_requests: int = 8,
    remote_only: bool = False,
    ambitionbox_judgments_path: str | None = None,
    glassdoor_resolutions_path: str | None = None,
    company_ids: Sequence[int] | None = None,
    session_factory: sessionmaker = SessionLocal,
) -> list[CompanyRunResult]:
    """Resolve, collect, calculate, and commit one resumable company batch."""
    with session_factory() as session:
        targets = select_market_profile_targets(
            session,
            limit=limit,
            include_in_progress=include_in_progress,
            remote_only=remote_only,
            company_ids=company_ids,
        )
        session.commit()
    if not targets:
        return []
    logger.info("Claimed %d company market-profile targets", len(targets))

    if not glassdoor_resolutions_path:
        raise ValueError(
            "a reviewed --glassdoor-resolutions artifact is required; "
            "company resolution no longer uses Bright Data SERP"
        )
    reviewed = load_glassdoor_resolution_artifact(glassdoor_resolutions_path)
    resolutions = [
        _resolution_from_reviewed_decision(target, reviewed.get(target.company_id))
        for target in targets
    ]
    logger.info(
        "Resolved %d/%d Glassdoor Overview URLs",
        sum(item.glassdoor_source_id is not None for item in resolutions),
        len(resolutions),
    )
    _persist_resolved_glassdoor_ids(session_factory, resolutions)

    bright_data = BrightDataGlassdoorClient()
    try:
        glassdoor_records = bright_data.collect(resolutions)
    finally:
        bright_data.close()

    taxonomy_judgments = (
        load_naukri_company_judgments(ambitionbox_judgments_path)
        if ambitionbox_judgments_path
        else None
    )
    with AmbitionBoxCollector(taxonomy_judgments=taxonomy_judgments) as ambitionbox:
        with _ambitionbox_process_lock():
            ambitionbox_records = {
                item.company_id: item.as_market_profile_record()
                for item in ambitionbox.collect_salaries(
                    _ambitionbox_targets(resolutions),
                )
            }

    with ThreadPoolExecutor(max_workers=max(1, company_workers)) as executor:
        futures = {
            executor.submit(
                _populate_one,
                session_factory,
                resolution,
                glassdoor_records,
                ambitionbox_records,
            ): resolution
            for resolution in resolutions
        }
        results: list[CompanyRunResult] = []
        for index, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if index % 25 == 0 or index == len(futures):
                logger.info("Saved %d/%d company market profiles", index, len(futures))
        by_id = {item.company_id: item for item in results}
        return [by_id[item.target.company_id] for item in resolutions]


def run_glassdoor_batch(
    *,
    resolutions_path: str,
    snapshot_id: str | None = None,
    company_ids: Sequence[int] | None = None,
    session_factory: sessionmaker = SessionLocal,
    client: Any | None = None,
    snapshot_callback: Callable[[str, int], None] | None = None,
) -> list[CompanyRunResult]:
    """Persist reviewed IDs, collect one deduplicated snapshot, and save Glassdoor."""
    reviewed = load_glassdoor_resolution_artifact(resolutions_path)
    allowed_ids = set(company_ids) if company_ids is not None else None
    if allowed_ids is not None:
        outside = set(reviewed) - allowed_ids
        if outside:
            raise ValueError(
                "Glassdoor artifact contains IDs outside the exact manifest: "
                + ", ".join(str(item) for item in sorted(outside))
            )
    resolutions: list[SourceResolution] = []
    with session_factory() as session:
        for company_id, decision in reviewed.items():
            if decision.get("status") != "accepted":
                continue
            company = session.get(Company, company_id)
            if company is None:
                raise LookupError(f"company {company_id} was not found")
            artifact_name = str(decision.get("company_name") or "")
            if artifact_name != company.name:
                raise ValueError(
                    f"company {company_id} name changed: artifact={artifact_name!r}, "
                    f"database={company.name!r}"
                )
            target = CompanyTarget(company.id, company.name, "Data Scientist", 0)
            resolutions.append(_resolution_from_reviewed_decision(target, decision))

    _persist_resolved_glassdoor_ids(session_factory, resolutions)
    resolutions, reused_results = _reuse_verified_glassdoor_groups(
        session_factory,
        resolutions,
    )
    if not resolutions:
        return reused_results
    owned_client = client is None
    bright_data = client or BrightDataGlassdoorClient()
    collect_kwargs: dict[str, Any] = {}
    if snapshot_callback is not None:
        collect_kwargs["snapshot_callback"] = snapshot_callback
    try:
        if snapshot_id is None:
            glassdoor_records = bright_data.collect(
                resolutions,
                **collect_kwargs,
            )
        else:
            glassdoor_records = bright_data.collect(
                resolutions,
                snapshot_id=snapshot_id,
                **collect_kwargs,
            )
    finally:
        if owned_client:
            bright_data.close()

    results: list[CompanyRunResult] = list(reused_results)
    for resolution in resolutions:
        try:
            with session_factory() as session:
                company = scrape_glassdoor_market_profile(
                    session,
                    resolution.target.company_id,
                    glassdoor_config=resolution.source_urls["glassdoor"],
                    glassdoor_record=glassdoor_records.get(
                        str(resolution.glassdoor_source_id),
                    ),
                )
                calculate_market_profile(session, company.id)
                session.commit()
                source = company.market_profile_evidence.get("sources", {}).get(
                    "glassdoor", {}
                )
                results.append(
                    CompanyRunResult(
                        company.id,
                        company.name,
                        company.market_profile_status,
                        {"glassdoor": str(source.get("status"))},
                    )
                )
        except Exception as exc:
            results.append(_save_unexpected_failure(session_factory, resolution, exc))
    return results


def run_ambitionbox_batch(
    *,
    limit: int | None = 100,
    remote_only: bool = False,
    session_factory: sessionmaker = SessionLocal,
    collector: Any | None = None,
    judgments_path: str | None = None,
    company_ids: Sequence[int] | None = None,
    progress_callback: Callable[[CompanyRunResult], None] | None = None,
) -> list[CompanyRunResult]:
    """Recover AmbitionBox gaps and commit each company independently."""
    normalize_legacy_ambitionbox_404s(
        session_factory,
        remote_only=remote_only,
        company_ids=company_ids,
    )
    with session_factory() as session:
        targets = select_ambitionbox_retry_targets(
            session,
            limit=limit,
            remote_only=remote_only,
            company_ids=company_ids,
        )
    if not targets:
        return []
    logger.info("Selected %d AmbitionBox-only retry targets", len(targets))

    target_by_id = {target.company_id: target for target in targets}
    owned_collector = collector is None
    if collector is not None and judgments_path is not None:
        raise ValueError("pass collector or judgments_path, not both")
    taxonomy_judgments = (
        load_naukri_company_judgments(judgments_path)
        if judgments_path
        else None
    )
    active_collector = collector or AmbitionBoxCollector(
        taxonomy_judgments=taxonomy_judgments,
    )
    results: list[CompanyRunResult] = []
    try:
        with _ambitionbox_process_lock():
            observations = active_collector.collect_salaries(targets)
            for index, observation in enumerate(observations, start=1):
                target = target_by_id[observation.company_id]
                try:
                    with session_factory() as session:
                        company = save_ambitionbox_market_profile(
                            session,
                            observation.company_id,
                            overall_rating=observation.overall_rating,
                            wlb_rating=observation.wlb_rating,
                            salary_lpa=observation.salary_lpa,
                            evidence=observation.evidence,
                        )
                        calculate_market_profile(session, company.id)
                        session.commit()
                        result = CompanyRunResult(
                            company.id,
                            company.name,
                            company.market_profile_status,
                            {"ambitionbox": observation.status},
                        )
                except Exception as exc:
                    result = CompanyRunResult(
                        target.company_id,
                        target.company_name,
                        "partial",
                        {"ambitionbox": "error"},
                        f"{type(exc).__name__}: {exc}",
                    )
                results.append(result)
                if progress_callback is not None:
                    progress_callback(result)
                if index % 25 == 0 or index == len(targets):
                    logger.info(
                        "Saved %d/%d AmbitionBox retry results",
                        index,
                        len(targets),
                    )
    finally:
        if owned_collector:
            active_collector.close()
    return results


def _populate_levels_fyi_one(
    session_factory: sessionmaker,
    target: LevelsFyiTarget,
    page_loader: BoundedPageLoader,
) -> CompanyRunResult:
    try:
        with session_factory() as session:
            company = scrape_levels_fyi_market_profile(
                session,
                target.company_id,
                salary_role=target.salary_role,
                levels_config={
                    "company_name": target.company_name,
                    "company_slug": target.company_slug,
                },
                page_loader=page_loader,
            )
            calculate_market_profile(session, company.id)
            session.commit()
            source = company.market_profile_evidence.get("sources", {}).get(
                "levels_fyi", {}
            )
            return CompanyRunResult(
                company.id,
                company.name,
                company.market_profile_status,
                {"levels_fyi": str(source.get("status"))},
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with session_factory() as session:
            company = session.get(Company, target.company_id)
            if company is not None:
                evidence = dict(company.market_profile_evidence or {})
                evidence["levels_fyi_batch_error"] = error
                company.market_profile_evidence = evidence
                company.market_profile_status = "partial"
                company.market_profile_updated_at = utcnow()
                session.commit()
        return CompanyRunResult(
            target.company_id,
            target.company_name,
            "partial",
            {"levels_fyi": "error"},
            error,
        )


def run_levels_fyi_batch(
    *,
    limit: int | None = 100,
    remote_only: bool = False,
    company_workers: int = 12,
    levels_requests: int = 8,
    session_factory: sessionmaker = SessionLocal,
    page_loader: BoundedPageLoader | None = None,
) -> list[CompanyRunResult]:
    """Retry only Levels.fyi work and preserve Glassdoor/AmbitionBox state."""
    with session_factory() as session:
        targets = select_levels_fyi_retry_targets(
            session,
            limit=limit,
            remote_only=remote_only,
        )
    if not targets:
        return []
    logger.info("Selected %d Levels.fyi-only retry targets", len(targets))

    owned_loader = page_loader is None
    active_loader = page_loader or BoundedPageLoader(
        levels_requests=levels_requests,
    )
    try:
        with ThreadPoolExecutor(max_workers=max(1, company_workers)) as executor:
            futures = {
                executor.submit(
                    _populate_levels_fyi_one,
                    session_factory,
                    target,
                    active_loader,
                ): target
                for target in targets
            }
            unordered: list[CompanyRunResult] = []
            for index, future in enumerate(as_completed(futures), start=1):
                unordered.append(future.result())
                if index % 25 == 0 or index == len(futures):
                    logger.info(
                        "Saved %d/%d Levels.fyi retry results",
                        index,
                        len(futures),
                    )
        by_id = {item.company_id: item for item in unordered}
        return [by_id[target.company_id] for target in targets]
    finally:
        if owned_loader:
            active_loader.close()
