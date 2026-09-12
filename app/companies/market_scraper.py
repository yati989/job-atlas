"""Collect and persist independent V1 company market-profile observations.

Glassdoor is supplied as an already-retrieved Bright Data company-overview
record, and AmbitionBox as an already-parsed adaptive-collector observation.
This module never triggers either collection or opens Glassdoor. Levels.fyi
uses direct HTTP only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from sqlalchemy.orm import Session

from app.companies.market_profile import (
    save_glassdoor_market_profile,
    save_levels_fyi_market_profile,
    save_source_market_profile,
)
from app.companies.naming import normalize
from app.config.categories import SEARCH_TERMS
from app.models.orm import Company


PageLoader = Callable[[str, str], str]
SourceUrls = Mapping[str, Mapping[str, str]]
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    )
}
_SEARCH_ROLE_FAMILIES = {
    "data scientist": "data_scientist",
    "data analyst": "data_analyst",
    "data engineer": "data_engineer",
    "machine learning engineer": "machine_learning_engineer",
    "ai engineer": "ai_engineer",
    "credit risk": "credit_risk",
}
if set(_SEARCH_ROLE_FAMILIES) != set(SEARCH_TERMS):
    raise RuntimeError("market-profile roles must cover every registry search term")

_ROLE_ALIASES = {
    "data_scientist": ("data scientist", "data science", "applied scientist"),
    "data_analyst": (
        "data analyst", "analytics", "bi analyst", "business intelligence analyst",
    ),
    "data_engineer": ("data engineer", "data engineering", "analytics engineer"),
    "machine_learning_engineer": (
        "machine learning engineer", "ml engineer", "mlops engineer", "ml ops",
        "mlops", "ml engr", "machine learning engr", "computer vision",
        "ai/ml", "ml/ai", "ai ml",
    ),
    "ai_engineer": (
        "ai engineer", "artificial intelligence engineer", "applied ai",
        "generative ai", "genai developer", "gen ai developer",
        "generative ai developer", "genai engineer", "gen ai engineer",
        "generative ai engineer", "genai", "gen ai", "llm", "agentic ai",
        "ai engr",
    ),
    "credit_risk": (
        "credit risk", "risk analyst", "risk analytics", "credit analyst",
        "fraud risk", "fraud analyst", "model risk", "underwriting", "underwriter",
    ),
    "business_analyst": ("business analyst",),
    "software_engineer": (
        "software engineer",
        "software development engineer",
        "software developer",
    ),
}
_ROLE_RANKINGS = {
    "data_scientist": (
        "data_scientist", "machine_learning_engineer", "ai_engineer",
        "software_engineer", "data_engineer", "data_analyst", "business_analyst",
    ),
    "data_analyst": ("data_analyst", "business_analyst", "data_scientist", "software_engineer"),
    "data_engineer": (
        "data_engineer", "machine_learning_engineer", "ai_engineer",
        "software_engineer", "data_scientist", "data_analyst",
    ),
    "machine_learning_engineer": (
        "machine_learning_engineer", "ai_engineer", "software_engineer",
        "data_scientist", "data_engineer",
    ),
    "ai_engineer": (
        "ai_engineer", "machine_learning_engineer", "data_scientist",
        "software_engineer","data_engineer"
    ),
    "credit_risk": (
        "credit_risk",  "data_scientist",  "software_engineer", "data_analyst"
    ),
    "business_analyst": ("business_analyst", "data_analyst", "data_scientist", "software_engineer"),
    "software_engineer": ("software_engineer", "machine_learning_engineer", "ai_engineer", "data_scientist", "data_engineer"),
}
_UNKNOWN_RANKED_ROLE_FAMILIES = {
    family
    for ranking in _ROLE_RANKINGS.values()
    for family in ranking
    if family not in _ROLE_ALIASES
}
if _UNKNOWN_RANKED_ROLE_FAMILIES:
    raise RuntimeError(
        "market-profile rankings contain unknown role families: "
        f"{sorted(_UNKNOWN_RANKED_ROLE_FAMILIES)}"
    )
_SENIOR_MARKERS = {
    "senior", "sr", "lead", "principal", "staff", "manager", "director", "head", "vp",
}
_JUNIOR_MARKERS = {"junior", "jr", "intern", "trainee", "entry"}
_VERIFIED_COMPANY_IDENTITY_GROUPS = (
    # AmbitionBox's EY salary route identifies the company as Ernst & Young.
    frozenset({"ey", "ernst young", "ernst and young"}),
)


class _HttpPageLoader:
    """One reusable client for the remaining direct-page sources."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
            timeout=30,
        )

    def close(self) -> None:
        self._client.close()

    def __call__(self, source: str, url: str) -> str:
        response = self._client.get(url)
        response.raise_for_status()
        return response.text


def _normalize_role(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _role_family(value: str) -> str | None:
    normalized_role = _normalize_role(value)
    padded_role = f" {normalized_role} "
    matches: list[tuple[tuple[bool, int, int, int], str]] = []
    for family_order, (family, aliases) in enumerate(_ROLE_ALIASES.items()):
        for alias in aliases:
            if f" {alias} " not in padded_role:
                continue
            specificity = (
                alias == normalized_role,
                len(alias.split()),
                len(alias),
                -family_order,
            )
            matches.append((specificity, family))
    return max(matches)[1] if matches else None


def _seniority_band(value: str) -> str:
    words = set(_normalize_role(value).split())
    if words & _SENIOR_MARKERS:
        return "senior"
    if words & _JUNIOR_MARKERS:
        return "junior"
    return "standard"


def _closest_role(items: list[dict], role: str, name_key: str) -> dict | None:
    """Choose the first same-seniority role in the explicit fallback ladder."""
    target = _normalize_role(role)
    target_family = _role_family(role)
    target_seniority = _seniority_band(role)
    if target_family is None:
        return next(
            (
                item for item in items
                if target in _normalize_role(str(item.get(name_key, "")))
                and _seniority_band(str(item.get(name_key, ""))) == target_seniority
            ),
            None,
        )

    for family in _ROLE_RANKINGS[target_family]:
        matches = [
            item for item in items
            if _role_family(str(item.get(name_key, ""))) == family
            and _seniority_band(str(item.get(name_key, ""))) == target_seniority
        ]
        if matches:
            return min(
                matches,
                key=lambda item: (
                    _normalize_role(str(item.get(name_key, ""))) != target,
                    len(_normalize_role(str(item.get(name_key, ""))).split()),
                ),
            )
    return None


def _valid_rating(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rating = float(value)
    return rating if 1 <= rating <= 5 else None


def _glassdoor_review_count(value: Any) -> int | None:
    """Normalize Bright Data's overview review count without inventing one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*([\d,]+)\s*", value)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def _source_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _glassdoor_ownership_type(value: str | None) -> str | None:
    """Retain only the requested public/private ownership classification."""
    if value is None:
        return None
    normalized = value.lower()
    if re.search(r"\bprivate\b", normalized):
        return "private"
    if re.search(r"\bpublic\b", normalized):
        return "public"
    return None


def _inr_lpa(value: str | float | int) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 100_000 if value > 0 else None
    if not isinstance(value, str):
        return None
    raw_number = re.fullmatch(r"[\d,.]+", value.strip())
    if raw_number:
        amount = float(value.replace(",", ""))
        return amount / 100_000 if amount > 0 else None
    match = re.search(r"(?:₹|INR\s*)\s*([\d,.]+)\s*(Cr|L|M|K)?", value, re.I)
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    unit = (match.group(2) or "").lower()
    return amount * {"cr": 100, "l": 1, "m": 10, "k": 0.01, "": 0.00001}[unit]


def _identity_matches(expected: str, observed: str | None) -> bool:
    if not observed:
        return False
    expected_normalized = normalize(expected)
    observed_normalized = normalize(observed)
    if expected_normalized == observed_normalized or (
        expected_normalized.replace(" ", "")
        == observed_normalized.replace(" ", "")
    ):
        return True
    return any(
        expected_normalized in group and observed_normalized in group
        for group in _VERIFIED_COMPANY_IDENTITY_GROUPS
    )


def _identity_mentioned(expected: str, text: str) -> bool:
    expected_normalized = normalize(expected)
    text_normalized = normalize(text)
    return expected_normalized in text_normalized or (
        expected_normalized.replace(" ", "")
        in text_normalized.replace(" ", "")
    )


def _glassdoor(
    record: Mapping[str, Any],
    expected_name: str,
    requested_url: str | None = None,
) -> dict[str, Any]:
    requested_id_match = re.search(r"EI_IE(\d+)", requested_url or "")
    if requested_id_match and str(record.get("id")) != requested_id_match.group(1):
        raise ValueError(
            f"Glassdoor employer ID mismatch: expected {requested_id_match.group(1)!r}, "
            f"received {record.get('id')!r}"
        )
    observed_name = record.get("company")
    if not isinstance(observed_name, str) or not _identity_matches(
        expected_name, observed_name,
    ):
        raise ValueError(
            f"Glassdoor identity mismatch: expected {expected_name!r}, "
            f"received {observed_name!r}"
        )
    review_count = _glassdoor_review_count(record.get("reviews_count"))
    employee_count_range = _source_text(record.get("details_size"))
    ownership_type_raw = _source_text(
        record.get("details_type") or record.get("company_type"),
    )
    revenue = _source_text(record.get("details_revenue"))
    ownership_type = _glassdoor_ownership_type(ownership_type_raw)
    return {
        "overall_rating": _valid_rating(record.get("ratings_overall")),
        "wlb_rating": _valid_rating(record.get("ratings_work_life_balance")),
        "review_count": review_count,
        "employee_count_range": employee_count_range,
        "ownership_type": ownership_type,
        "revenue": revenue,
        "evidence": {
            "status": "ok",
            "company": observed_name,
            "source_id": record.get("id"),
            "url": record.get("url_overview") or record.get("url"),
            "review_count": review_count,
            "review_count_raw": record.get("reviews_count"),
            "employee_count_range": employee_count_range,
            "ownership_type": ownership_type,
            "ownership_type_raw": ownership_type_raw,
            "revenue": revenue,
        },
    }


_LEVELS_MEDIAN = re.compile(
    r"median yearly compensation package in India totals\s*"
    r"((?:₹|INR\s*)\s*[\d,.]+\s*(?:Cr|L|M|K)?)",
    re.I,
)
_LEVELS_INR_AMOUNT = re.compile(
    r"(?:₹|INR\s*)\s*[\d,.]+\s*(?:Cr|L|M|K)?",
    re.I,
)


def _levels_fyi(html: str, expected_name: str, selected_role: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    identity_text = " ".join(
        value for value in (
            soup.title.get_text(" ", strip=True) if soup.title else "",
            (soup.select_one('meta[property="og:title"]') or {}).get("content", ""),
        )
        if value
    )
    if not _identity_mentioned(expected_name, identity_text):
        raise ValueError(
            f"Levels.fyi identity mismatch: expected {expected_name!r}"
        )

    candidates = [
        tag.get("content", "")
        for tag in soup.select('meta[name="description"], meta[property="og:description"]')
    ]
    candidates.append(soup.get_text(" ", strip=True))
    for text in candidates:
        match = _LEVELS_MEDIAN.search(text)
        if match:
            estimate = _inr_lpa(match.group(1))
            if estimate is not None:
                return {
                    "salary_lpa": estimate,
                    "evidence": {
                        "status": "ok",
                        "company": expected_name,
                        "selected_role": selected_role,
                        "selected_seniority": _seniority_band(selected_role),
                        "observed_median": match.group(1),
                        "estimation_method": "displayed_median",
                        "currency": "INR",
                        "location": "India",
                    },
                }

    range_container = soup.select_one(
        '[class*="averageTotalCompensationContainer"]'
    )
    if range_container:
        range_text = range_container.get_text(" ", strip=True)
        range_display = range_container.select_one('[class*="rangeDisplay"]')
        if (
            "Average Total Compensation" in range_text
            and "India" in range_text
            and range_display is not None
        ):
            observed_range = _LEVELS_INR_AMOUNT.findall(
                range_display.get_text(" ", strip=True)
            )
            if len(observed_range) >= 2:
                endpoints = [_inr_lpa(value) for value in observed_range[:2]]
                if all(value is not None for value in endpoints):
                    low, high = endpoints
                    assert low is not None and high is not None
                    return {
                        "salary_lpa": (low + high) / 2,
                        "evidence": {
                            "status": "ok",
                            "company": expected_name,
                            "selected_role": selected_role,
                            "selected_seniority": _seniority_band(selected_role),
                            "observed_range": observed_range[:2],
                            "estimation_method": "displayed_range_midpoint",
                            "currency": "INR",
                            "location": "India",
                        },
                    }
    return {
        "salary_lpa": None,
        "evidence": {
            "status": "missing",
            "company": expected_name,
            "selected_role": selected_role,
            "reason": "visible India median was not present",
        },
    }


def _levels_rows_url(company_slug: str, role_slug: str) -> str:
    query = urlencode(
        [
            ("companySlug", company_slug),
            ("jobFamilySlug", role_slug),
            ("countryIds[]", "113"),
            ("limit", "10"),
            ("sortBy", "offer_date"),
            ("sortOrder", "DESC"),
        ]
    )
    return f"https://api.levels.fyi/v3/salary/search?{query}"


def _decode_levels_rows(raw: str) -> Mapping[str, Any]:
    response = json.loads(raw)
    if not isinstance(response, Mapping):
        raise ValueError("Levels.fyi salary rows response was not an object")
    payload = response.get("payload")
    if payload is None:
        return response
    if not isinstance(payload, str):
        raise ValueError("Levels.fyi salary rows payload was not a string")

    key = base64.b64encode(hashlib.md5(b"levelstothemoon!!").digest())
    decryptor = Cipher(algorithms.AES(key[:16]), modes.ECB()).decryptor()
    padded = decryptor.update(base64.b64decode(payload)) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    compressed = unpadder.update(padded) + unpadder.finalize()
    decoded = json.loads(zlib.decompress(compressed))
    if not isinstance(decoded, Mapping):
        raise ValueError("Levels.fyi decoded salary rows were not an object")
    return decoded


def _levels_row_fallback(
    raw: str,
    expected_name: str,
    selected_role: str,
    company_slug: str,
    role_slug: str,
) -> dict[str, Any]:
    response = _decode_levels_rows(raw)
    rows = response.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Levels.fyi salary rows were not a list")

    observed_rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        observed_company = row.get("company")
        company_info = row.get("companyInfo")
        if not observed_company and isinstance(company_info, Mapping):
            observed_company = company_info.get("name")
        if not isinstance(observed_company, str) or not _identity_matches(
            expected_name, observed_company,
        ):
            raise ValueError(
                f"Levels.fyi row identity mismatch: expected {expected_name!r}, "
                f"received {observed_company!r}"
            )
        if row.get("jobFamilySlug") != role_slug:
            raise ValueError(
                f"Levels.fyi row role mismatch: expected {role_slug!r}, "
                f"received {row.get('jobFamilySlug')!r}"
            )
        if row.get("countryId") != 113:
            continue
        total = row.get("totalCompensation")
        exchange_rate = row.get("exchangeRate")
        if (
            isinstance(total, bool)
            or not isinstance(total, (int, float))
            or isinstance(exchange_rate, bool)
            or not isinstance(exchange_rate, (int, float))
            or total <= 0
            or exchange_rate <= 0
        ):
            continue
        salary_lpa = total * exchange_rate / 100_000
        observed_rows.append(
            {
                "location": row.get("location"),
                "total_compensation_usd": total,
                "inr_exchange_rate": exchange_rate,
                "salary_lpa": salary_lpa,
            }
        )

    if not observed_rows:
        return {
            "salary_lpa": None,
            "evidence": {
                "status": "missing",
                "company": expected_name,
                "selected_role": selected_role,
                "reason": "no usable India salary rows were present",
                "row_count": 0,
            },
        }

    salaries = [row["salary_lpa"] for row in observed_rows]
    return {
        "salary_lpa": median(salaries),
        "evidence": {
            "status": "ok",
            "company": expected_name,
            "selected_role": selected_role,
            "selected_seniority": _seniority_band(selected_role),
            "estimation_method": "india_rows_median",
            "currency": "INR",
            "location": "India",
            "row_count": len(observed_rows),
            "api_total": response.get("total"),
            "observed_row_salaries_lpa": salaries,
            "observed_rows": observed_rows,
            "company_slug": company_slug,
        },
    }


def _levels_broad_url(config: Mapping[str, str]) -> str | None:
    if slug := config.get("company_slug"):
        return f"https://www.levels.fyi/companies/{slug}/salaries"
    if salary_url := config.get("salary"):
        match = re.match(
            r"(https?://[^/]+/companies/[^/]+/salaries)(?:/.*)?$",
            salary_url,
        )
        return match.group(1) if match else None
    return None


def _levels_catalog(
    html: str,
    expected_name: str,
    requested_url: str,
) -> dict[str, Any]:
    """Read the canonical company and role inventory from a broad salary page."""
    soup = BeautifulSoup(html, "html.parser")
    identity_text = " ".join(
        value for value in (
            soup.title.get_text(" ", strip=True) if soup.title else "",
            (soup.select_one('meta[property="og:title"]') or {}).get("content", ""),
        )
        if value
    )
    if not _identity_mentioned(expected_name, identity_text):
        raise ValueError(
            f"Levels.fyi identity mismatch: expected {expected_name!r}"
        )

    canonical_tag = soup.select_one('link[rel="canonical"]')
    canonical_meta = soup.select_one('meta[property="og:url"]')
    canonical_url = (
        (canonical_tag or {}).get("href")
        or (canonical_meta or {}).get("content")
        or requested_url
    )
    canonical_url = (
        urljoin(requested_url, canonical_url).split("?", 1)[0].rstrip("/")
    )
    parsed = urlparse(canonical_url)
    broad_path = parsed.path.rstrip("/")
    broad_match = re.fullmatch(r"/companies/([^/]+)/salaries", broad_path)
    if not broad_match:
        raise ValueError(
            f"Levels.fyi canonical salaries URL was invalid: {canonical_url!r}"
        )

    roles = []
    seen_urls = set()
    role_path = re.compile(rf"{re.escape(broad_path)}/([^/]+)/?")
    for anchor in soup.select('a[href*="/salaries/"]'):
        role_url = urljoin(canonical_url + "/", anchor.get("href", ""))
        role_parsed = urlparse(role_url)
        if role_parsed.netloc != parsed.netloc:
            continue
        match = role_path.fullmatch(role_parsed.path)
        if not match or role_url in seen_urls:
            continue
        heading = anchor.find(re.compile(r"^h[1-6]$"))
        role_name = (heading or anchor).get_text(" ", strip=True)
        if not role_name:
            continue
        role_url = role_url.split("?", 1)[0].rstrip("/")
        seen_urls.add(role_url)
        roles.append({"name": role_name, "slug": match.group(1), "url": role_url})

    return {
        "broad_url": canonical_url,
        "slug": broad_match.group(1),
        "roles": roles,
    }


def _error_evidence(urls: Mapping[str, str], exc: Exception) -> dict[str, Any]:
    return {
        "status": "error",
        "urls": {key: value for key, value in urls.items() if key != "company_name"},
        "error": f"{type(exc).__name__}: {exc}",
    }


def collect_levels_fyi_profile(
    *,
    expected_name: str,
    salary_role: str,
    levels_config: Mapping[str, str],
    page_loader: PageLoader,
) -> dict[str, Any]:
    """Collect one verified Levels.fyi salary observation without persisting it."""
    requested_broad_url = _levels_broad_url(levels_config)
    if not requested_broad_url:
        return {
            "salary_lpa": None,
            "evidence": {
                "status": "not_attempted",
                "reason": "company slug or salary URL was not supplied",
            },
        }

    catalog = None
    selected = None
    india_url = None
    try:
        catalog = _levels_catalog(
            page_loader("levels_fyi", requested_broad_url),
            expected_name,
            requested_broad_url,
        )
        selected = _closest_role(catalog["roles"], salary_role, "name")
        if selected is None:
            return {
                "salary_lpa": None,
                "evidence": {
                    "status": "missing",
                    "reason": (
                        "no compatible role was present on the broad salaries page"
                    ),
                    "broad_url": catalog["broad_url"],
                    "slug": catalog["slug"],
                    "discovered_roles": catalog["roles"],
                },
            }

        india_url = selected["url"] + "/locations/india"
        result = _levels_fyi(
            page_loader("levels_fyi", india_url),
            expected_name,
            selected["name"],
        )
        rows_url = None
        if result["salary_lpa"] is None:
            rows_url = _levels_rows_url(catalog["slug"], selected["slug"])
            result = _levels_row_fallback(
                page_loader("levels_fyi", rows_url),
                expected_name,
                selected["name"],
                catalog["slug"],
                selected["slug"],
            )
        evidence = {
            **result["evidence"],
            "url": india_url,
            "broad_url": catalog["broad_url"],
            "slug": catalog["slug"],
            "selected_role_slug": selected["slug"],
            "discovered_roles": catalog["roles"],
        }
        if rows_url:
            evidence["rows_url"] = rows_url
        return {"salary_lpa": result["salary_lpa"], "evidence": evidence}
    except Exception as exc:
        evidence = {
            "status": "error",
            "broad_url": catalog["broad_url"] if catalog else requested_broad_url,
            "slug": (
                catalog["slug"] if catalog else levels_config.get("company_slug")
            ),
            "error": f"{type(exc).__name__}: {exc}",
        }
        if catalog:
            evidence["discovered_roles"] = catalog["roles"]
        if selected:
            evidence.update(
                {
                    "url": india_url,
                    "selected_role": selected["name"],
                    "selected_role_slug": selected["slug"],
                }
            )
        return {"salary_lpa": None, "evidence": evidence}


def scrape_levels_fyi_market_profile(
    session: Session,
    company_id: int,
    *,
    salary_role: str,
    levels_config: Mapping[str, str],
    page_loader: PageLoader | None = None,
) -> Company:
    """Collect and persist only Levels.fyi, preserving other source state."""
    company = session.get(Company, company_id)
    if company is None:
        raise LookupError(f"company {company_id} was not found")

    owned_loader = _HttpPageLoader() if page_loader is None else None
    load = page_loader or owned_loader
    assert load is not None
    retrieved_at = datetime.now(timezone.utc).isoformat()
    try:
        result = collect_levels_fyi_profile(
            expected_name=levels_config.get("company_name", company.name),
            salary_role=salary_role,
            levels_config=levels_config,
            page_loader=load,
        )
        evidence = dict(result["evidence"])
        evidence.setdefault("retrieved_at", retrieved_at)
        return save_levels_fyi_market_profile(
            session,
            company_id,
            salary_lpa=result["salary_lpa"],
            evidence=evidence,
            requested_salary_role=salary_role,
            requested_seniority=_seniority_band(salary_role),
            retrieved_at=retrieved_at,
        )
    finally:
        if owned_loader is not None:
            owned_loader.close()


def scrape_glassdoor_market_profile(
    session: Session,
    company_id: int,
    *,
    glassdoor_config: Mapping[str, str],
    glassdoor_record: Mapping[str, Any] | None,
) -> Company:
    """Validate and persist only a reviewed Glassdoor snapshot record."""
    company = session.get(Company, company_id)
    if company is None:
        raise LookupError(f"company {company_id} was not found")

    retrieved_at = datetime.now(timezone.utc).isoformat()
    if glassdoor_record is None:
        return save_glassdoor_market_profile(
            session,
            company_id,
            overall_rating=None,
            wlb_rating=None,
            review_count=None,
            evidence={
                "status": "error",
                "requested_url": glassdoor_config.get("overview"),
                "source_id": glassdoor_config.get("source_id"),
                "reason": "Bright Data snapshot returned no company-overview record",
                "retrieved_at": retrieved_at,
            },
        )

    try:
        result = _glassdoor(
            glassdoor_record,
            glassdoor_config.get("company_name", company.name),
            glassdoor_config.get("overview"),
        )
        evidence = {
            **result["evidence"],
            "requested_url": glassdoor_config.get("overview"),
            "searched_name": glassdoor_config.get("searched_name"),
            "resolution_pass": glassdoor_config.get("resolution_pass"),
            "resolution_source": "codex_internal_web_search",
            "retrieved_at": retrieved_at,
        }
        return save_glassdoor_market_profile(
            session,
            company_id,
            overall_rating=result["overall_rating"],
            wlb_rating=result["wlb_rating"],
            review_count=result["review_count"],
            employee_count_range=result["employee_count_range"],
            ownership_type=result["ownership_type"],
            revenue=result["revenue"],
            evidence=evidence,
        )
    except Exception as exc:
        return save_glassdoor_market_profile(
            session,
            company_id,
            overall_rating=None,
            wlb_rating=None,
            review_count=None,
            evidence={
                **_error_evidence(glassdoor_config, exc),
                "requested_url": glassdoor_config.get("overview"),
                "searched_name": glassdoor_config.get("searched_name"),
                "resolution_pass": glassdoor_config.get("resolution_pass"),
                "resolution_source": "codex_internal_web_search",
                "retrieved_at": retrieved_at,
            },
        )


def scrape_market_profile(
    session: Session,
    company_id: int,
    *,
    salary_role: str,
    source_urls: SourceUrls,
    glassdoor_record: Mapping[str, Any] | None,
    ambitionbox_record: Mapping[str, Any] | None = None,
    page_loader: PageLoader | None = None,
) -> Company:
    """Collect active sources and preserve parked Levels.fyi observations."""
    company = session.get(Company, company_id)
    if company is None:
        raise LookupError(f"company {company_id} was not found")

    retrieved_at = datetime.now(timezone.utc).isoformat()
    values: dict[str, Any] = {
        "glassdoor_overall_rating": None,
        "glassdoor_wlb_rating": None,
        "glassdoor_review_count": None,
        "employee_count_range": None,
        "ownership_type": None,
        "revenue": None,
        "ambitionbox_overall_rating": None,
        "ambitionbox_wlb_rating": None,
        "ambitionbox_estimated_salary_lpa": None,
        "levels_fyi_estimated_salary_lpa": company.levels_fyi_estimated_salary_lpa,
    }
    evidence: dict[str, Any] = {
        "requested_salary_role": salary_role,
        "requested_seniority": _seniority_band(salary_role),
        "retrieved_at": retrieved_at,
        "sources": {},
    }

    glassdoor_urls = source_urls.get("glassdoor", {})
    if glassdoor_record is None:
        evidence["sources"]["glassdoor"] = {
            "status": "not_attempted",
            "url": glassdoor_urls.get("overview"),
            "reason": "no Bright Data company-overview record supplied",
        }
    else:
        try:
            result = _glassdoor(
                glassdoor_record,
                glassdoor_urls.get("company_name", company.name),
                glassdoor_urls.get("overview"),
            )
            values["glassdoor_overall_rating"] = result["overall_rating"]
            values["glassdoor_wlb_rating"] = result["wlb_rating"]
            values["glassdoor_review_count"] = result["review_count"]
            values["employee_count_range"] = result["employee_count_range"]
            values["ownership_type"] = result["ownership_type"]
            values["revenue"] = result["revenue"]
            evidence["sources"]["glassdoor"] = {
                **result["evidence"],
                "requested_url": glassdoor_urls.get("overview"),
            }
        except Exception as exc:
            evidence["sources"]["glassdoor"] = _error_evidence(
                glassdoor_urls, exc,
            )

    if ambitionbox_record is not None:
        result_evidence = ambitionbox_record.get("evidence")
        if not isinstance(result_evidence, Mapping):
            raise ValueError("AmbitionBox observation evidence is required")
        values["ambitionbox_overall_rating"] = ambitionbox_record.get(
            "overall_rating",
        )
        values["ambitionbox_wlb_rating"] = ambitionbox_record.get("wlb_rating")
        values["ambitionbox_estimated_salary_lpa"] = ambitionbox_record.get(
            "salary_lpa",
        )
        evidence["sources"]["ambitionbox"] = dict(result_evidence)
    else:
        evidence["sources"]["ambitionbox"] = {
            "status": "not_attempted",
            "url": source_urls.get("ambitionbox", {}).get("salaries"),
            "reason": "no adaptive AmbitionBox observation supplied",
        }

    prior_profile = company.market_profile_evidence
    prior_profile = prior_profile if isinstance(prior_profile, Mapping) else {}
    prior_sources = prior_profile.get("sources")
    prior_sources = prior_sources if isinstance(prior_sources, Mapping) else {}
    prior_levels = prior_sources.get("levels_fyi")
    if isinstance(prior_levels, Mapping):
        evidence["sources"]["levels_fyi"] = {
            **prior_levels,
            "policy_status": "parked",
        }
    else:
        evidence["sources"]["levels_fyi"] = {
            "status": "not_attempted",
            "policy_status": "parked",
            "reason": "Levels.fyi collection is parked by source policy",
        }
    for source_evidence in evidence["sources"].values():
        source_evidence.setdefault("retrieved_at", retrieved_at)

    return save_source_market_profile(
        session,
        company_id,
        **values,
        evidence=evidence,
    )
