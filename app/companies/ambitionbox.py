"""Adaptive, resumable AmbitionBox market-profile collection.

The broad salary page contains aggregate company ratings and only one slice of
role salary ranges. A missing role is resolved through at most four ranked
direct-role pages. This module owns the whole request policy behind one
``collect`` interface: URL deduplication, global pacing, bounded retry,
throttle circuit breaking, canonical company resolution, and parsing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from app.companies.market_scraper import (
    _ROLE_RANKINGS,
    _closest_role,
    _identity_matches,
    _inr_lpa,
    _role_family,
    _seniority_band,
    _valid_rating,
)


logger = logging.getLogger(__name__)
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
}
_THROTTLE_STATUSES = {403, 429}
_RETRYABLE_STATUSES = {500, 502, 503, 504}
_COMPANY_SEARCH_ENDPOINT = (
    "https://taxonomy-suggest.naukri.com/suggest/abcommonsuggest"
)
_COMPANY_SEARCH_SUFFIX = (
    "&appId=112&category=ab_company&limit=25&callback=_1684833062882"
    "&resultField=tagOne,tagTwo,tagThree,tagFour,tagFive,tagSix,"
    "tagSeven,tagEight,type,id,name&matchValueFlag=true&fuzzyEnableFlag=true"
)
_COMPANY_NAME_STOPWORDS = {
    "company",
    "corporation",
    "group",
    "inc",
    "incorporated",
    "india",
    "limited",
    "llc",
    "ltd",
    "private",
    "pvt",
}
_PARENT_SUFFIXES = (
    re.compile(r"\s+(?:india|science|research|labs?|acc)$", re.IGNORECASE),
    re.compile(
        r"\s+(?:global\s+)?(?:capability|development|technology|knowledge)"
        r"\s+cent(?:er|re)$",
        re.IGNORECASE,
    ),
    re.compile(r"\s+(?:global\s+)?shared\s+services$", re.IGNORECASE),
)
_DIRECT_ROLE_ROUTES = {
    "data_scientist": ("Data Scientist", "data-scientist"),
    "data_analyst": ("Data Analyst", "data-analyst"),
    "data_engineer": ("Data Engineer", "data-engineer"),
    "machine_learning_engineer": (
        "Machine Learning Engineer",
        "machine-learning-engineer",
    ),
    "ai_engineer": (
        "Artificial Intelligence Engineer",
        "artificial-intelligence-engineer",
    ),
    "credit_risk": ("Credit Risk Analyst", "credit-risk-analyst"),
    "business_analyst": ("Business Analyst", "business-analyst"),
    "software_engineer": ("Software Engineer", "software-engineer"),
}
_UNROUTABLE_RANKED_ROLE_FAMILIES = {
    family
    for ranking in _ROLE_RANKINGS.values()
    for family in ranking
    if family not in _DIRECT_ROLE_ROUTES
}
if _UNROUTABLE_RANKED_ROLE_FAMILIES:
    raise RuntimeError(
        "AmbitionBox has no direct route for ranked role families: "
        f"{sorted(_UNROUTABLE_RANKED_ROLE_FAMILIES)}"
    )


@dataclass(frozen=True)
class AmbitionBoxTarget:
    company_id: int
    company_name: str
    salary_role: str
    slug: str
    salary_url: str
    known_broad_observation: AmbitionBoxObservation | None = None


@dataclass(frozen=True)
class AmbitionBoxObservation:
    company_id: int
    status: str
    overall_rating: float | None
    wlb_rating: float | None
    salary_lpa: float | None
    evidence: dict[str, Any]

    def as_market_profile_record(self) -> dict[str, Any]:
        return {
            "overall_rating": self.overall_rating,
            "wlb_rating": self.wlb_rating,
            "salary_lpa": self.salary_lpa,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class _CompanyCandidate:
    name: str
    slug: str


@dataclass(frozen=True)
class _CompanyResolution:
    status: str
    target: AmbitionBoxTarget | None
    response: Response | None
    error: str | None
    circuit_opened: bool
    attempts: int
    evidence: dict[str, Any]


@dataclass(frozen=True)
class NaukriCompanyResolution:
    """Canonical AmbitionBox company identity returned by Naukri taxonomy."""

    status: str
    target: AmbitionBoxTarget | None
    error: str | None
    attempts: int
    evidence: dict[str, Any]


@dataclass(frozen=True)
class NaukriCompanyJudgment:
    """Agent decision for one harvested Naukri company candidate set.

    ``accepted_slug=None`` explicitly rejects every candidate.  Omitting a
    judgment is different: only an exact or legal-suffix-only identity may be
    resolved automatically; all looser candidates are returned for review.
    """

    accepted_slug: str | None
    reason: str
    company_name: str | None = None
    accepted_company: str | None = None


def load_naukri_company_judgments(
    path: str | Path,
) -> dict[int, NaukriCompanyJudgment]:
    """Load a reviewed candidate artifact keyed by stable company ID."""
    payload = json.loads(Path(path).read_text())
    rows = payload.get("judgments") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("Naukri judgment artifact must contain a judgments list")
    judgments: dict[int, NaukriCompanyJudgment] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Naukri judgment rows must be objects")
        company_id = row.get("company_id")
        accepted_slug = row.get("accepted_slug")
        reason = row.get("reason")
        company_name = row.get("company")
        accepted_company = row.get("accepted_company")
        if not isinstance(company_id, int) or isinstance(company_id, bool):
            raise ValueError("Naukri judgment company_id must be an integer")
        if accepted_slug is not None and not isinstance(accepted_slug, str):
            raise ValueError("Naukri accepted_slug must be a string or null")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Naukri judgment reason must be a non-empty string")
        if company_name is not None and not isinstance(company_name, str):
            raise ValueError("Naukri judgment company must be a string")
        if accepted_company is not None and not isinstance(accepted_company, str):
            raise ValueError("Naukri accepted_company must be a string or null")
        if company_id in judgments:
            raise ValueError(f"duplicate Naukri judgment for company {company_id}")
        judgments[company_id] = NaukriCompanyJudgment(
            accepted_slug=accepted_slug,
            reason=reason.strip(),
            company_name=company_name,
            accepted_company=accepted_company,
        )
    return judgments


@dataclass(frozen=True)
class AmbitionBoxPolicy:
    initial_interval_seconds: float = 1.0
    minimum_interval_seconds: float = 0.75
    maximum_interval_seconds: float = 12.0
    cooldown_seconds: float = 60.0
    transient_backoff_seconds: float = 5.0
    success_window: int = 20
    acceleration_seconds: float = 0.05
    max_attempts: int = 2
    max_direct_role_requests: int = 4

    def __post_init__(self) -> None:
        if self.minimum_interval_seconds < 0:
            raise ValueError("minimum AmbitionBox interval cannot be negative")
        if not (
            self.minimum_interval_seconds
            <= self.initial_interval_seconds
            <= self.maximum_interval_seconds
        ):
            raise ValueError("AmbitionBox intervals must satisfy minimum <= initial <= maximum")
        if self.cooldown_seconds < 0 or self.transient_backoff_seconds < 0:
            raise ValueError("AmbitionBox backoffs cannot be negative")
        if (
            self.success_window < 1
            or self.max_attempts < 1
            or self.max_direct_role_requests < 1
        ):
            raise ValueError("AmbitionBox windows and request limits must be positive")
        if self.acceleration_seconds < 0:
            raise ValueError("AmbitionBox acceleration cannot be negative")


class Response(Protocol):
    status_code: int
    text: str
    headers: Mapping[str, str]


class Transport(Protocol):
    def get(self, url: str) -> Response: ...

    def close(self) -> None: ...


class HttpAmbitionBoxTransport:
    """Production adapter at the external AmbitionBox HTTP seam."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
            timeout=30,
        )

    def get(self, url: str) -> httpx.Response:
        return self._client.get(url)

    def close(self) -> None:
        self._client.close()


class HttpNaukriTaxonomyTransport:
    """Independent transport for concurrent, unpaced taxonomy lookups."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
            timeout=15,
        )

    def get(self, url: str) -> httpx.Response:
        return self._client.get(url)

    def close(self) -> None:
        self._client.close()


def _nested(data: Mapping[str, Any], *path: str) -> Any:
    value: Any = data
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _first_mapping(
    data: Mapping[str, Any],
    paths: Sequence[tuple[str, ...]],
) -> Mapping[str, Any]:
    for path in paths:
        value = _nested(data, *path)
        if isinstance(value, Mapping):
            return value
    return {}


def _first_list(
    data: Mapping[str, Any],
    paths: Sequence[tuple[str, ...]],
) -> list[dict[str, Any]]:
    for path in paths:
        value = _nested(data, *path)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _company_identity(data: Mapping[str, Any]) -> str | None:
    for path in (
        ("props", "pageProps", "companyName"),
        ("props", "pageProps", "companyInfo", "companyName"),
        ("props", "pageProps", "companyInfo", "name"),
        ("props", "pageProps", "companyData", "companyName"),
        ("props", "pageProps", "companyData", "name"),
        ("props", "pageProps", "companyMetaData", "companyName"),
        ("props", "pageProps", "companyMetaData", "name"),
    ):
        value = _nested(data, *path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _direct_role_candidates(
    target: AmbitionBoxTarget,
) -> list[tuple[str, str]]:
    family = _role_family(target.salary_role)
    families = _ROLE_RANKINGS.get(family or "", ())
    seniority = _seniority_band(target.salary_role)
    parsed = urlsplit(target.salary_url)
    broad_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    candidates: list[tuple[str, str]] = []
    for ranked_family in families:
        route = _DIRECT_ROLE_ROUTES.get(ranked_family)
        if route is None:
            continue
        role_name, slug = route
        if seniority == "senior":
            role_name = f"Senior {role_name}"
            slug = f"senior-{slug}"
        elif seniority == "junior":
            role_name = f"Junior {role_name}"
            slug = f"junior-{slug}"
        candidates.append((role_name, f"{broad_url}/{slug}"))
    return candidates


def _direct_role_url(target: AmbitionBoxTarget) -> str | None:
    candidates = _direct_role_candidates(target)
    return candidates[0][1] if candidates else None


def _salary_count(value: Any) -> int | None:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def parse_salary_page(
    html: str,
    *,
    target: AmbitionBoxTarget,
) -> AmbitionBoxObservation:
    script = BeautifulSoup(html, "html.parser").select_one("#__NEXT_DATA__")
    if script is None:
        raise ValueError("AmbitionBox salary page has no __NEXT_DATA__ payload")
    data = json.loads(script.get_text())
    if not isinstance(data, Mapping):
        raise ValueError("AmbitionBox salary page payload is not an object")

    observed_name = _company_identity(data)
    if observed_name is None:
        raise ValueError("AmbitionBox salary page has no company identity")
    identity_match = _identity_matches(target.company_name, observed_name)

    ratings = _first_mapping(
        data,
        (
            (
                "props", "pageProps", "reviewsAggregateData",
                "ratingDistribution", "data", "ratings",
            ),
            (
                "props", "pageProps", "reviewsAggregateData",
                "ratingDistribution", "data", "ratingsTwoDecimal",
            ),
            ("props", "pageProps", "ratingsData"),
        ),
    )
    roles = _first_list(
        data,
        (
            ("props", "pageProps", "filtersData", "data", "jobProfiles"),
            ("props", "pageProps", "jobProfiles"),
        ),
    )
    match = _closest_role(roles, target.salary_role, "jobProfileName")
    salary_count = None
    experience = None
    if match is None:
        salary_data = _first_mapping(
            data,
            (("props", "pageProps", "salaryData", "data"),),
        )
        profile = salary_data.get("profileInfo")
        profile = profile if isinstance(profile, Mapping) else {}
        summary = salary_data.get("summaryData")
        summary = summary if isinstance(summary, Mapping) else {}
        profile_name = profile.get("profileName")
        if not isinstance(profile_name, str) or not profile_name.strip():
            profile_name = _nested(data, "props", "pageProps", "designation")
        if isinstance(profile_name, str) and profile_name.strip() and summary:
            candidate = dict(summary)
            candidate["jobProfileName"] = profile_name.strip()
            match = _closest_role([candidate], target.salary_role, "jobProfileName")
            if match:
                salary_count = _salary_count(summary.get("totalSalaryDataPoints"))
                minimum_experience = summary.get("minExp")
                maximum_experience = summary.get("maxExp")
                if minimum_experience is not None and maximum_experience is not None:
                    experience = f"{minimum_experience}–{maximum_experience} years"
    estimate = None
    observed_range = None
    if match:
        low = _inr_lpa(match.get("typicalMinCtc", ""))
        high = _inr_lpa(match.get("typicalMaxCtc", ""))
        if low is not None and high is not None and low <= high:
            estimate = (low + high) / 2
            observed_range = {
                "currency": "INR",
                "minimum_lpa": round(low, 1),
                "maximum_lpa": round(high, 1),
            }

    evidence = {
        "status": "ok",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "company": observed_name,
        "requested_company": target.company_name,
        "identity_match": identity_match,
        "url": target.salary_url,
        "slug": target.slug,
        "selected_role": match and match.get("jobProfileName"),
        "selected_seniority": _seniority_band(
            str(match.get("jobProfileName", "")) if match else "",
        ),
        "experience": experience or (
            match.get("experience")
            or match.get("experienceRange")
            or match.get("experience_range")
            if match else None
        ),
        "observed_salary_range": observed_range,
    }
    if salary_count is not None:
        evidence["salary_count"] = salary_count
    return AmbitionBoxObservation(
        company_id=target.company_id,
        status="ok",
        overall_rating=_valid_rating(
            ratings.get("overallCompanyRating")
            or ratings.get("overallRating")
            or ratings.get("overall"),
        ),
        wlb_rating=_valid_rating(ratings.get("workLifeRating")),
        salary_lpa=estimate,
        evidence=evidence,
    )


def _with_salary_lookup(
    observation: AmbitionBoxObservation,
    *,
    status: str,
    url: str,
    source_status: str | None = None,
    **details: Any,
) -> AmbitionBoxObservation:
    evidence = dict(observation.evidence)
    lookup = {"status": status, "url": url, **details}
    evidence["salary_lookup"] = lookup
    final_status = source_status or observation.status
    evidence["status"] = final_status
    return AmbitionBoxObservation(
        company_id=observation.company_id,
        status=final_status,
        overall_rating=observation.overall_rating,
        wlb_rating=observation.wlb_rating,
        salary_lpa=observation.salary_lpa,
        evidence=evidence,
    )


def _merge_direct_salary(
    broad: AmbitionBoxObservation,
    detail: AmbitionBoxObservation,
    *,
    role_attempts: list[dict[str, Any]] | None = None,
) -> AmbitionBoxObservation:
    evidence = dict(broad.evidence)
    for key in (
        "company",
        "requested_company",
        "identity_match",
        "selected_role",
        "selected_seniority",
        "experience",
        "observed_salary_range",
    ):
        if key not in evidence or evidence[key] is None:
            evidence[key] = detail.evidence.get(key)
    salary_lookup = {
        "status": "ok",
        "url": detail.evidence.get("url"),
    }
    if detail.evidence.get("salary_count") is not None:
        salary_lookup["salary_count"] = detail.evidence["salary_count"]
    if role_attempts and len(role_attempts) > 1:
        salary_lookup["role_attempts"] = role_attempts
    evidence["salary_lookup"] = salary_lookup
    return AmbitionBoxObservation(
        company_id=broad.company_id,
        status=broad.status,
        overall_rating=broad.overall_rating,
        wlb_rating=broad.wlb_rating,
        salary_lpa=detail.salary_lpa,
        evidence=evidence,
    )


def _empty_observation(
    target: AmbitionBoxTarget,
    *,
    status: str,
    **evidence: Any,
) -> AmbitionBoxObservation:
    return AmbitionBoxObservation(
        company_id=target.company_id,
        status=status,
        overall_rating=None,
        wlb_rating=None,
        salary_lpa=None,
        evidence={
            "status": status,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "url": target.salary_url,
            "slug": target.slug,
            **evidence,
        },
    )


def _company_search_url(query: str) -> str:
    return f"{_COMPANY_SEARCH_ENDPOINT}?astext={quote(query)}{_COMPANY_SEARCH_SUFFIX}"


def _first_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def _parse_company_candidates(payload: str) -> list[_CompanyCandidate]:
    text = payload.strip()
    if text.startswith("_1684833062882(") and text.endswith(")"):
        text = text[len("_1684833062882(") : -1]
    data = json.loads(text)
    merged = _nested(data, "resultList", "merged")
    if not isinstance(merged, list):
        return []
    candidates: list[_CompanyCandidate] = []
    for item in merged:
        if not isinstance(item, Mapping) or item.get("type") != "company":
            continue
        name = _first_string(item.get("name"))
        slug = _first_string(item.get("tagTwo"))
        if name and slug:
            candidates.append(_CompanyCandidate(name=name, slug=slug))
    return candidates


def _name_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


_LEGAL_SUFFIXES = (
    ("private", "limited"),
    ("pvt", "ltd"),
    ("pte", "ltd"),
    ("incorporated",),
    ("corporation",),
    ("limited",),
    ("company",),
    ("corp",),
    ("llc",),
    ("plc",),
    ("inc",),
    ("ltd",),
)


def _legal_identity_tokens(value: str) -> tuple[str, ...]:
    tokens = _name_tokens(value)
    changed = True
    while tokens and changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if len(tokens) >= len(suffix) and tokens[-len(suffix) :] == suffix:
                tokens = tokens[: -len(suffix)]
                changed = True
                break
    return tokens


def _automatic_company_identity_match(
    requested_name: str,
    candidate_name: str,
) -> bool:
    requested = _name_tokens(requested_name)
    candidate = _name_tokens(candidate_name)
    if not requested or not candidate:
        return False
    if requested == candidate:
        return True
    return _legal_identity_tokens(requested_name) == _legal_identity_tokens(
        candidate_name,
    )


def _candidate_score(requested_name: str, candidate_name: str) -> float:
    requested = _name_tokens(requested_name)
    candidate = _name_tokens(candidate_name)
    if not requested or not candidate:
        return 0.0
    requested_joined = "".join(requested)
    candidate_joined = "".join(candidate)
    if requested_joined == candidate_joined:
        return 1.0
    requested_core = tuple(
        token for token in requested if token not in _COMPANY_NAME_STOPWORDS
    ) or requested
    candidate_core = tuple(
        token for token in candidate if token not in _COMPANY_NAME_STOPWORDS
    ) or candidate
    overlap = set(requested_core) & set(candidate_core)
    if not overlap:
        return 0.0
    requested_coverage = len(overlap) / len(set(requested_core))
    candidate_coverage = len(overlap) / len(set(candidate_core))
    containment = float(
        requested_joined.startswith(candidate_joined)
        or candidate_joined.startswith(requested_joined)
    )
    return 0.7 * requested_coverage + 0.2 * candidate_coverage + 0.1 * containment


def _best_company_candidate(
    requested_name: str,
    candidates: Sequence[_CompanyCandidate],
) -> _CompanyCandidate | None:
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            _candidate_score(requested_name, candidate.name),
            -len(candidate.name),
        ),
        reverse=True,
    )
    if not ranked or _candidate_score(requested_name, ranked[0].name) < 0.4:
        return None
    return ranked[0]


def _parent_brand_query(company_name: str) -> str | None:
    candidate = re.split(r"\s+(?:[-|:]|\(|/)\s*", company_name, maxsplit=1)[0]
    candidate = candidate.strip(" -|:()/")
    for suffix in _PARENT_SUFFIXES:
        stripped = suffix.sub("", candidate).strip()
        if stripped != candidate:
            candidate = stripped
            break
    if not candidate or candidate.casefold() == company_name.strip().casefold():
        return None
    return candidate


def _with_company_resolution(
    observation: AmbitionBoxObservation,
    resolution: Mapping[str, Any] | None,
) -> AmbitionBoxObservation:
    if not resolution:
        return observation
    evidence = dict(observation.evidence)
    evidence["resolution"] = dict(resolution)
    return replace(observation, evidence=evidence)


class NaukriTaxonomyResolver:
    """Harvest canonical candidates concurrently and enforce identity judgment."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        workers: int = 16,
        judgments: Mapping[int, NaukriCompanyJudgment] | None = None,
    ) -> None:
        self._transport = transport or HttpNaukriTaxonomyTransport()
        self._owns_transport = transport is None
        self._workers = max(1, workers)
        self._judgments = dict(judgments or {})

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def __enter__(self) -> NaukriTaxonomyResolver:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _resolve_one(self, target: AmbitionBoxTarget) -> NaukriCompanyResolution:
        judgment = self._judgments.get(target.company_id)
        if judgment is not None:
            if (
                judgment.company_name is not None
                and _name_tokens(judgment.company_name)
                != _name_tokens(target.company_name)
            ):
                return NaukriCompanyResolution(
                    status="error",
                    target=None,
                    error="judgment company name does not match current company",
                    attempts=0,
                    evidence={
                        "status": "error",
                        "requested_company": target.company_name,
                        "judgment_company": judgment.company_name,
                    },
                )
            if judgment.accepted_slug is None:
                return NaukriCompanyResolution(
                    status="rejected",
                    target=None,
                    error=None,
                    attempts=0,
                    evidence={
                        "status": "rejected",
                        "method": "reviewed_naukri_judgment",
                        "requested_company": target.company_name,
                        "requested_slug": target.slug,
                        "requested_url": target.salary_url,
                        "judgment": {
                            "decision": "rejected",
                            "reason": judgment.reason,
                        },
                    },
                )
            resolved_url = (
                "https://www.ambitionbox.com/salaries/"
                f"{judgment.accepted_slug}-salaries"
            )
            return NaukriCompanyResolution(
                status="resolved",
                target=replace(
                    target,
                    slug=judgment.accepted_slug,
                    salary_url=resolved_url,
                ),
                error=None,
                attempts=0,
                evidence={
                    "status": "resolved",
                    "method": "reviewed_naukri_judgment",
                    "requested_company": target.company_name,
                    "requested_slug": target.slug,
                    "requested_url": target.salary_url,
                    "resolved_company": (
                        judgment.accepted_company or target.company_name
                    ),
                    "resolved_slug": judgment.accepted_slug,
                    "resolved_url": resolved_url,
                    "judgment": {
                        "decision": "accepted",
                        "reason": judgment.reason,
                    },
                },
            )

        parent_query = _parent_brand_query(target.company_name)
        queries = [("naukri_taxonomy", target.company_name)]
        if parent_query:
            queries.append(("naukri_parent_brand", parent_query))
        search_attempts: list[dict[str, Any]] = []
        harvested: list[tuple[str, str, _CompanyCandidate]] = []

        for method, query in queries:
            try:
                response = self._transport.get(_company_search_url(query))
            except Exception as exc:
                return NaukriCompanyResolution(
                    status="error",
                    target=None,
                    error=f"{type(exc).__name__}: {exc}",
                    attempts=len(search_attempts) + 1,
                    evidence={
                        "status": "error",
                        "requested_company": target.company_name,
                        "requested_slug": target.slug,
                        "requested_url": target.salary_url,
                        "search_attempts": search_attempts,
                    },
                )
            if response.status_code != 200:
                return NaukriCompanyResolution(
                    status="error",
                    target=None,
                    error=f"HTTP {response.status_code}",
                    attempts=len(search_attempts) + 1,
                    evidence={
                        "status": "error",
                        "requested_company": target.company_name,
                        "requested_slug": target.slug,
                        "requested_url": target.salary_url,
                        "search_attempts": search_attempts,
                    },
                )
            try:
                candidates = _parse_company_candidates(response.text)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                return NaukriCompanyResolution(
                    status="error",
                    target=None,
                    error=f"{type(exc).__name__}: {exc}",
                    attempts=len(search_attempts) + 1,
                    evidence={
                        "status": "error",
                        "requested_company": target.company_name,
                        "requested_slug": target.slug,
                        "requested_url": target.salary_url,
                        "search_attempts": search_attempts,
                    },
                )
            search_attempts.append(
                {"query": query, "candidate_count": len(candidates)},
            )
            harvested.extend((method, query, candidate) for candidate in candidates)

        unique_candidates: list[_CompanyCandidate] = []
        candidate_origin: dict[str, tuple[str, str]] = {}
        seen_slugs: set[str] = set()
        for method, query, candidate in harvested:
            if candidate.slug in seen_slugs:
                continue
            seen_slugs.add(candidate.slug)
            unique_candidates.append(candidate)
            candidate_origin[candidate.slug] = (method, query)

        candidate: _CompanyCandidate | None = None
        judgment_evidence: dict[str, Any]
        automatic_matches = [
            item
            for item in unique_candidates
            if _automatic_company_identity_match(target.company_name, item.name)
        ]
        if len(automatic_matches) == 1:
            candidate = automatic_matches[0]
            judgment_evidence = {
                "decision": "automatic_exact_identity",
                "reason": "exact name or legal-suffix-only identity",
            }
        else:
            return NaukriCompanyResolution(
                status="review_required" if unique_candidates else "missing",
                target=None,
                error=None,
                attempts=len(search_attempts),
                evidence={
                    "status": (
                        "review_required" if unique_candidates else "missing"
                    ),
                    "requested_company": target.company_name,
                    "requested_slug": target.slug,
                    "requested_url": target.salary_url,
                    "search_attempts": search_attempts,
                    "candidates": [item.__dict__ for item in unique_candidates],
                },
            )

        method, query = candidate_origin[candidate.slug]
        resolved_url = (
            "https://www.ambitionbox.com/salaries/"
            f"{candidate.slug}-salaries"
        )
        resolved_target = replace(
            target,
            slug=candidate.slug,
            salary_url=resolved_url,
        )
        return NaukriCompanyResolution(
            status="resolved",
            target=resolved_target,
            error=None,
            attempts=len(search_attempts),
            evidence={
                "status": "resolved",
                "method": method,
                "query": query,
                "requested_company": target.company_name,
                "requested_slug": target.slug,
                "requested_url": target.salary_url,
                "resolved_company": candidate.name,
                "resolved_slug": candidate.slug,
                "resolved_url": resolved_url,
                "search_attempts": search_attempts,
                "candidates": [item.__dict__ for item in unique_candidates],
                "judgment": judgment_evidence,
            },
        )

    def resolve(
        self,
        targets: Sequence[AmbitionBoxTarget],
    ) -> list[NaukriCompanyResolution]:
        """Resolve independent company names concurrently in input order."""
        with ThreadPoolExecutor(max_workers=self._workers) as executor:
            futures = {
                executor.submit(self._resolve_one, target): index
                for index, target in enumerate(targets)
            }
            resolved: dict[int, NaukriCompanyResolution] = {}
            for future in as_completed(futures):
                resolved[futures[future]] = future.result()
        return [resolved[index] for index in range(len(targets))]


class AmbitionBoxCollector:
    """Collect broad and direct-role observations under one global quota."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        policy: AmbitionBoxPolicy | None = None,
        taxonomy_resolver: NaukriTaxonomyResolver | None = None,
        taxonomy_judgments: Mapping[int, NaukriCompanyJudgment] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport or HttpAmbitionBoxTransport()
        self._owns_transport = transport is None
        self._policy = policy or AmbitionBoxPolicy()
        if taxonomy_resolver is not None and taxonomy_judgments is not None:
            raise ValueError(
                "pass taxonomy_resolver or taxonomy_judgments, not both"
            )
        self._taxonomy_resolver = taxonomy_resolver or NaukriTaxonomyResolver(
            judgments=taxonomy_judgments,
        )
        self._owns_taxonomy_resolver = taxonomy_resolver is None
        self._monotonic = monotonic
        self._sleep = sleep
        self._interval = self._policy.initial_interval_seconds
        self._next_dispatch_at = 0.0
        self._last_dispatch_at = 0.0
        self._consecutive_successes = 0
        self._company_search_cache: dict[str, list[_CompanyCandidate]] = {}

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()
        if self._owns_taxonomy_resolver:
            self._taxonomy_resolver.close()

    def __enter__(self) -> AmbitionBoxCollector:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _wait_until_dispatch(self) -> None:
        delay = self._next_dispatch_at - self._monotonic()
        if delay > 0:
            self._sleep(delay)

    def _after_success(self) -> None:
        self._consecutive_successes += 1
        if self._consecutive_successes >= self._policy.success_window:
            self._interval = max(
                self._policy.minimum_interval_seconds,
                self._interval - self._policy.acceleration_seconds,
            )
            self._consecutive_successes = 0
        self._next_dispatch_at = self._last_dispatch_at + self._interval

    def _retry_after_seconds(self, response: Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
                return max(0.0, parsed.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    def _after_throttle(self, response: Response) -> None:
        self._consecutive_successes = 0
        self._interval = min(
            self._policy.maximum_interval_seconds,
            max(self._policy.minimum_interval_seconds, self._interval * 2),
        )
        cooldown = self._retry_after_seconds(response)
        cooldown = self._policy.cooldown_seconds if cooldown is None else cooldown
        self._next_dispatch_at = self._monotonic() + max(cooldown, self._interval)

    def _after_transient_error(self, attempt: int) -> None:
        self._consecutive_successes = 0
        backoff = self._policy.transient_backoff_seconds * (2 ** (attempt - 1))
        self._next_dispatch_at = self._monotonic() + max(backoff, self._interval)

    def _fetch(self, url: str) -> tuple[Response | None, str | None, bool, int]:
        last_error = None
        for attempt in range(1, self._policy.max_attempts + 1):
            self._wait_until_dispatch()
            self._last_dispatch_at = self._monotonic()
            try:
                response = self._transport.get(url)
            except (httpx.RequestError, TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._after_transient_error(attempt)
                if attempt < self._policy.max_attempts:
                    continue
                return None, last_error, False, attempt

            if response.status_code in _THROTTLE_STATUSES:
                last_error = f"HTTP {response.status_code} after {attempt} attempt(s)"
                self._after_throttle(response)
                if attempt < self._policy.max_attempts:
                    continue
                return response, last_error, True, attempt
            if response.status_code in _RETRYABLE_STATUSES:
                last_error = f"HTTP {response.status_code} after {attempt} attempt(s)"
                self._after_transient_error(attempt)
                if attempt < self._policy.max_attempts:
                    continue
                return response, last_error, False, attempt

            self._after_success()
            return response, None, False, attempt
        raise AssertionError("AmbitionBox attempt loop did not return")

    def _search_companies(
        self,
        query: str,
    ) -> tuple[list[_CompanyCandidate] | None, str | None, bool, int]:
        cache_key = query.casefold().strip()
        cached = self._company_search_cache.get(cache_key)
        if cached is not None:
            return cached, None, False, 0
        response, error, opened, attempts = self._fetch(_company_search_url(query))
        if response is None:
            return None, error, opened, attempts
        if response.status_code != 200:
            return (
                None,
                error or f"HTTP {response.status_code}",
                opened,
                attempts,
            )
        try:
            candidates = _parse_company_candidates(response.text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return (
                None,
                f"{type(exc).__name__}: {exc}",
                opened,
                attempts,
            )
        self._company_search_cache[cache_key] = candidates
        return candidates, None, opened, attempts

    def _resolve_company_404(
        self,
        target: AmbitionBoxTarget,
    ) -> _CompanyResolution:
        search_attempts: list[dict[str, Any]] = []
        parent_query = _parent_brand_query(target.company_name)
        queries = [("ambitionbox_search", target.company_name)]
        if parent_query:
            queries.append(("parent_brand_search", parent_query))

        total_attempts = 0
        for method, query in queries:
            candidates, error, opened, attempts = self._search_companies(query)
            total_attempts += attempts
            if candidates is None:
                evidence = {
                    "status": "error",
                    "requested_company": target.company_name,
                    "requested_slug": target.slug,
                    "requested_url": target.salary_url,
                    "search_attempts": search_attempts,
                }
                return _CompanyResolution(
                    status="error",
                    target=None,
                    response=None,
                    error=error,
                    circuit_opened=opened,
                    attempts=total_attempts,
                    evidence=evidence,
                )
            search_attempts.append(
                {"query": query, "candidate_count": len(candidates)},
            )
            candidate = _best_company_candidate(query, candidates)
            if candidate is None:
                continue

            resolved_url = (
                "https://www.ambitionbox.com/salaries/"
                f"{candidate.slug}-salaries"
            )
            resolved_target = replace(
                target,
                company_name=target.company_name,
                slug=candidate.slug,
                salary_url=resolved_url,
            )
            response, error, opened, attempts = self._fetch(resolved_url)
            total_attempts += attempts
            evidence = {
                "status": "resolved",
                "method": method,
                "query": query,
                "requested_company": target.company_name,
                "requested_slug": target.slug,
                "requested_url": target.salary_url,
                "resolved_company": candidate.name,
                "resolved_slug": candidate.slug,
                "resolved_url": resolved_url,
                "search_attempts": search_attempts,
            }
            return _CompanyResolution(
                status="resolved" if response is not None else "error",
                target=resolved_target,
                response=response,
                error=error,
                circuit_opened=opened,
                attempts=total_attempts,
                evidence=evidence,
            )

        return _CompanyResolution(
            status="missing",
            target=None,
            response=None,
            error=None,
            circuit_opened=False,
            attempts=total_attempts,
            evidence={
                "status": "missing",
                "requested_company": target.company_name,
                "requested_slug": target.slug,
                "requested_url": target.salary_url,
                "search_attempts": search_attempts,
            },
        )

    def _collect_direct_salary(
        self,
        target: AmbitionBoxTarget,
        broad: AmbitionBoxObservation,
        detail_cache: dict[
            str,
            tuple[Response | None, str | None, bool, int],
        ],
    ) -> tuple[AmbitionBoxObservation, bool, Response | None]:
        candidates = _direct_role_candidates(target)[
            : self._policy.max_direct_role_requests
        ]
        if not candidates:
            return (
                _with_salary_lookup(
                    broad,
                    status="missing",
                    url=target.salary_url,
                    reason="requested role has no canonical direct route",
                    retryable=False,
                ),
                False,
                None,
            )
        role_attempts: list[dict[str, Any]] = []
        for rank, (role_name, detail_url) in enumerate(candidates, start=1):
            detail_result = detail_cache.get(detail_url)
            if detail_result is None:
                detail_result = self._fetch(detail_url)
                detail_cache[detail_url] = detail_result
            detail_response, detail_error, detail_opened, request_attempts = (
                detail_result
            )
            attempt_evidence: dict[str, Any] = {
                "rank": rank,
                "role": role_name,
                "url": detail_url,
            }

            if detail_response is None:
                attempt_evidence["status"] = "error"
                role_attempts.append(attempt_evidence)
                return (
                    _with_salary_lookup(
                        broad,
                        status="error",
                        url=detail_url,
                        source_status="error",
                        error=detail_error,
                        attempts=request_attempts,
                        retryable=True,
                        role_attempts=role_attempts,
                    ),
                    detail_opened,
                    detail_response,
                )
            if detail_response.status_code == 200:
                detail_target = replace(
                    target,
                    salary_role=role_name,
                    salary_url=detail_url,
                )
                try:
                    detail = parse_salary_page(
                        detail_response.text,
                        target=detail_target,
                    )
                except Exception as exc:
                    attempt_evidence["status"] = "error"
                    role_attempts.append(attempt_evidence)
                    return (
                        _with_salary_lookup(
                            broad,
                            status="error",
                            url=detail_url,
                            source_status="error",
                            error=f"{type(exc).__name__}: {exc}",
                            attempts=request_attempts,
                            retryable=False,
                            role_attempts=role_attempts,
                        ),
                        detail_opened,
                        detail_response,
                    )
                selected_role = str(detail.evidence.get("selected_role") or "")
                exact_profile = (
                    _role_family(selected_role) == _role_family(role_name)
                    and _seniority_band(selected_role) == _seniority_band(role_name)
                )
                if detail.salary_lpa is not None and exact_profile:
                    attempt_evidence["status"] = "ok"
                    if detail.evidence.get("salary_count") is not None:
                        attempt_evidence["salary_count"] = detail.evidence[
                            "salary_count"
                        ]
                    role_attempts.append(attempt_evidence)
                    return (
                        _merge_direct_salary(
                            broad,
                            detail,
                            role_attempts=role_attempts,
                        ),
                        detail_opened,
                        detail_response,
                    )
                attempt_evidence["status"] = "missing"
                if selected_role and not exact_profile:
                    attempt_evidence["observed_role"] = selected_role
                role_attempts.append(attempt_evidence)
                continue
            if detail_response.status_code == 404:
                attempt_evidence.update({"status": "missing", "http_status": 404})
                role_attempts.append(attempt_evidence)
                continue

            retryable = detail_response.status_code in (
                _THROTTLE_STATUSES | _RETRYABLE_STATUSES
            )
            attempt_evidence.update(
                {
                    "status": "error",
                    "http_status": detail_response.status_code,
                },
            )
            role_attempts.append(attempt_evidence)
            return (
                _with_salary_lookup(
                    broad,
                    status="error",
                    url=detail_url,
                    source_status="error",
                    http_status=detail_response.status_code,
                    error=detail_error or f"HTTP {detail_response.status_code}",
                    attempts=request_attempts,
                    retryable=retryable,
                    role_attempts=role_attempts,
                ),
                detail_opened,
                detail_response,
            )

        return (
            _with_salary_lookup(
                broad,
                status="missing",
                url=role_attempts[-1]["url"],
                reason="ranked direct roles had no compatible salary range",
                retryable=False,
                role_attempts=role_attempts,
            ),
            False,
            None,
        )

    def collect_salaries(
        self,
        targets: Sequence[AmbitionBoxTarget],
    ) -> Iterator[AmbitionBoxObservation]:
        """Resolve canonical slugs first, then fetch only ranked role pages."""
        resolutions = self._taxonomy_resolver.resolve(targets)
        detail_cache: dict[
            str,
            tuple[Response | None, str | None, bool, int],
        ] = {}
        circuit_open = False
        circuit_reason = None

        for original_target, resolution in zip(targets, resolutions, strict=True):
            if resolution.status == "missing" or resolution.target is None:
                status = "error" if resolution.status == "error" else "missing"
                yield _empty_observation(
                    original_target,
                    status=status,
                    **(
                        {"error": resolution.error, "retryable": True}
                        if status == "error"
                        else {
                            "reason": (
                                "Naukri taxonomy found no compatible "
                                "AmbitionBox company"
                            ),
                            "retryable": False,
                        }
                    ),
                    attempts=resolution.attempts,
                    resolution=resolution.evidence,
                )
                continue

            active_target = resolution.target
            known_broad = original_target.known_broad_observation
            if known_broad is not None:
                base = _with_company_resolution(
                    known_broad,
                    resolution.evidence,
                )
            else:
                base = AmbitionBoxObservation(
                    company_id=active_target.company_id,
                    status="ok",
                    overall_rating=None,
                    wlb_rating=None,
                    salary_lpa=None,
                    evidence={
                        "status": "ok",
                        "attempted_at": datetime.now(timezone.utc).isoformat(),
                        "requested_company": active_target.company_name,
                        "url": active_target.salary_url,
                        "slug": active_target.slug,
                        "selected_role": None,
                        "resolution": dict(resolution.evidence),
                    },
                )

            detail_url = _direct_role_url(active_target)
            if detail_url is None:
                yield _with_salary_lookup(
                    base,
                    status="missing",
                    url=active_target.salary_url,
                    source_status="missing" if known_broad is None else None,
                    reason="requested role has no canonical direct route",
                    retryable=False,
                )
                continue
            if circuit_open:
                yield _with_salary_lookup(
                    base,
                    status="deferred",
                    url=detail_url,
                    source_status="deferred",
                    reason=circuit_reason,
                    retryable=True,
                )
                continue

            observation, detail_opened, detail_response = self._collect_direct_salary(
                active_target,
                base,
                detail_cache,
            )
            if (
                known_broad is None
                and observation.salary_lpa is None
                and observation.evidence.get("salary_lookup", {}).get("status")
                == "missing"
            ):
                evidence = dict(observation.evidence)
                evidence["status"] = "missing"
                observation = replace(observation, status="missing", evidence=evidence)
            if detail_opened:
                circuit_open = True
                circuit_reason = (
                    "global circuit opened after repeated HTTP "
                    f"{detail_response.status_code if detail_response else 'throttle'}"
                )
            yield observation

        logger.info(
            "AmbitionBox salary collection completed for %d targets at %.2fs interval",
            len(targets),
            self._interval,
        )

    def collect(
        self,
        targets: Sequence[AmbitionBoxTarget],
    ) -> Iterator[AmbitionBoxObservation]:
        """Yield one observation per target under one global request budget.

        The anonymous broad page exposes only one 20-role slice. When that
        slice has no compatible salary, try at most four canonical direct-role
        pages in ranking order instead of crawling arbitrary page numbers.
        """
        by_url: dict[str, list[AmbitionBoxTarget]] = defaultdict(list)
        for target in targets:
            by_url[target.salary_url].append(target)

        circuit_open = False
        circuit_reason = None
        detail_cache: dict[
            str,
            tuple[Response | None, str | None, bool, int],
        ] = {}
        for url, grouped_targets in by_url.items():
            if circuit_open:
                for target in grouped_targets:
                    broad = target.known_broad_observation
                    detail_url = _direct_role_url(target)
                    if broad is not None and detail_url is None:
                        yield _with_salary_lookup(
                            broad,
                            status="missing",
                            url=target.salary_url,
                            reason="requested role has no canonical direct route",
                            retryable=False,
                        )
                    elif broad is not None:
                        yield _with_salary_lookup(
                            broad,
                            status="deferred",
                            url=detail_url or target.salary_url,
                            source_status="deferred",
                            reason=circuit_reason,
                            retryable=True,
                        )
                    else:
                        yield _empty_observation(
                            target,
                            status="deferred",
                            reason=circuit_reason,
                            retryable=True,
                        )
                continue

            uncached_targets = [
                target
                for target in grouped_targets
                if target.known_broad_observation is None
            ]
            response: Response | None = None
            error: str | None = None
            opened = False
            attempts = 0
            if uncached_targets:
                response, error, opened, attempts = self._fetch(url)
                if opened:
                    circuit_open = True
                    circuit_reason = (
                        f"global circuit opened after repeated HTTP "
                        f"{response.status_code if response else 'throttle'}"
                    )

            for target in grouped_targets:
                active_target = target
                resolution_evidence: Mapping[str, Any] | None = None
                target_response = response
                target_error = error
                target_attempts = attempts
                broad = target.known_broad_observation
                if broad is None and target_response is None:
                    yield _empty_observation(
                        target,
                        status="error",
                        error=target_error,
                        attempts=target_attempts,
                        retryable=True,
                    )
                    continue
                if (
                    broad is None
                    and target_response is not None
                    and target_response.status_code == 404
                ):
                    resolution = self._resolve_company_404(target)
                    resolution_evidence = resolution.evidence
                    if resolution.circuit_opened:
                        circuit_open = True
                        circuit_reason = (
                            "global circuit opened during company resolution"
                        )
                    if resolution.status == "missing":
                        yield _empty_observation(
                            target,
                            status="missing",
                            reason=(
                                "AmbitionBox company search found no compatible "
                                "candidate"
                            ),
                            attempts=target_attempts + resolution.attempts,
                            retryable=False,
                            resolution=resolution.evidence,
                        )
                        continue
                    if resolution.status == "error" or resolution.target is None:
                        yield _empty_observation(
                            target,
                            status="error",
                            error=resolution.error,
                            attempts=target_attempts + resolution.attempts,
                            retryable=True,
                            resolution=resolution.evidence,
                        )
                        continue
                    active_target = resolution.target
                    target_response = resolution.response
                    target_error = resolution.error
                    target_attempts += resolution.attempts
                    if target_response is None:
                        yield _empty_observation(
                            target,
                            status="error",
                            error=target_error,
                            attempts=target_attempts,
                            retryable=True,
                            resolution=resolution.evidence,
                        )
                        continue
                if (
                    broad is None
                    and target_response is not None
                    and target_response.status_code == 200
                ):
                    try:
                        broad = parse_salary_page(
                            target_response.text,
                            target=active_target,
                        )
                        broad = _with_company_resolution(
                            broad,
                            resolution_evidence,
                        )
                    except Exception as exc:
                        yield _empty_observation(
                            active_target,
                            status="error",
                            error=f"{type(exc).__name__}: {exc}",
                            attempts=target_attempts,
                            retryable=False,
                            **(
                                {"resolution": dict(resolution_evidence)}
                                if resolution_evidence
                                else {}
                            ),
                        )
                        continue
                elif (
                    broad is None
                    and target_response is not None
                    and target_response.status_code == 404
                ):
                    yield _empty_observation(
                        active_target,
                        status="missing",
                        reason="HTTP 404 Not Found",
                        attempts=target_attempts,
                        retryable=False,
                        **(
                            {"resolution": dict(resolution_evidence)}
                            if resolution_evidence
                            else {}
                        ),
                    )
                    continue
                elif broad is None and target_response is not None:
                    yield _empty_observation(
                        active_target,
                        status="error",
                        error=(
                            target_error or f"HTTP {target_response.status_code}"
                        ),
                        attempts=target_attempts,
                        retryable=target_response.status_code in (
                            _THROTTLE_STATUSES | _RETRYABLE_STATUSES
                        ),
                        **(
                            {"resolution": dict(resolution_evidence)}
                            if resolution_evidence
                            else {}
                        ),
                    )
                    continue

                if broad is None:
                    raise AssertionError("AmbitionBox broad observation was not resolved")
                if broad.salary_lpa is not None:
                    yield broad
                    continue

                detail_url = _direct_role_url(active_target)
                if detail_url is None:
                    yield _with_salary_lookup(
                        broad,
                        status="missing",
                        url=active_target.salary_url,
                        reason="requested role has no canonical direct route",
                        retryable=False,
                    )
                    continue
                if circuit_open:
                    yield _with_salary_lookup(
                        broad,
                        status="deferred",
                        url=detail_url,
                        source_status="deferred",
                        reason=circuit_reason,
                        retryable=True,
                    )
                    continue

                observation, detail_opened, detail_response = (
                    self._collect_direct_salary(
                        active_target,
                        broad,
                        detail_cache,
                    )
                )
                if detail_opened:
                    circuit_open = True
                    circuit_reason = (
                        "global circuit opened after repeated HTTP "
                        f"{detail_response.status_code if detail_response else 'throttle'}"
                    )
                yield observation

        logger.info(
            "AmbitionBox collection completed for %d targets at %.2fs interval",
            len(targets),
            self._interval,
        )
