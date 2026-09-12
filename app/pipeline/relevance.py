"""
Central relevance gate (ADR-0002, issue #8): the single check every
connector's output passes through before storage.

The profile-driven gate checks role, job type, experience, and location. It
does not reject ordinary jobs by seniority. Internships and part-time jobs are
excluded by default and included only when the accepted profile explicitly
opts in. Posting age is
stored for display and ranking, but a job returned by a source is not rejected
for being older than the source search window. See CONTEXT.md for the
glossary (relevant job, in-scope role, India-eligible, onsite/hybrid,
seniority band) and docs/adr/0002-central-relevance-gate.md
for the rationale.

Predicates duck-type over any object exposing `.title`, `.location_raw`,
`.is_remote`, `.remote_scope`, `.posted_at` — both NormalizedJob and the
SQLAlchemy Job ORM row satisfy this, so the purge and audit scripts can pass
either.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.config.categories import (
    BANGALORE_MARKERS,
    EXEC_TITLE_MARKERS,
    FOREIGN_COUNTRY_MARKERS,
    FOREIGN_ONLY_MARKERS,
    INDIA_ELIGIBLE_MARKERS,
    JUNIOR_TITLE_MARKERS,
    OFF_ROLE_TITLE_MARKERS,
    RECENCY_WINDOW_DAYS,
    ROLE_TITLE_KEYWORDS,
    ROLE_TITLE_OVERRIDE_KEYWORDS,
    SENIORITY_BAND_OVERRIDES,
)
from app.models.schemas import NormalizedJob
from app.config.geography import city_aliases, has_indian_city


_COUNTRY_MARKERS: dict[str, tuple[str, ...]] = {
    "AU": ("australia",),
    "CA": ("canada",),
    "DE": ("germany",),
    "FR": ("france",),
    "GB": ("united kingdom", "uk"),
    "IN": ("india",),
    "US": ("united states", "usa", "us"),
}
_GLOBAL_REMOTE_MARKERS = ("worldwide", "anywhere", "global")


@dataclass(frozen=True)
class RelevancePolicy:
    role_terms: tuple[str, ...]
    title_exclusions: tuple[str, ...]
    countries: tuple[str, ...]
    country_markers: tuple[str, ...]
    cities: tuple[str, ...]
    arrangements: tuple[str, ...]
    # Kept for callers that still construct policies from older saved data.
    # It is informational and no longer gates public eligibility.
    seniority: tuple[str, ...] = ()
    include_internships: bool = False
    include_part_time: bool = False
    experience_minimum_years: float | None = None
    experience_maximum_years: float | None = None
    hard_rejects: tuple[str, ...] = ()
    exact_country_as_remote: bool = False
    unspecified_remote: bool = False

    @classmethod
    def from_profile(cls, profile: Any) -> "RelevancePolicy":
        markers = tuple(
            marker
            for country in profile.countries
            for marker in _COUNTRY_MARKERS.get(country, ())
        )
        return cls(
            role_terms=tuple(profile.relevance_terms),
            title_exclusions=tuple(profile.title_exclusions),
            countries=tuple(profile.countries),
            country_markers=markers,
            cities=tuple(profile.cities),
            arrangements=tuple(profile.arrangements),
            seniority=tuple(getattr(profile, "seniority", ())),
            include_internships=bool(getattr(profile, "include_internships", False)),
            include_part_time=bool(getattr(profile, "include_part_time", False)),
            experience_minimum_years=profile.experience.minimum_years,
            experience_maximum_years=profile.experience.maximum_years,
            hard_rejects=tuple(profile.hard_rejects),
            exact_country_as_remote=profile.location_fallbacks.exact_country_as_remote,
            unspecified_remote=profile.location_fallbacks.unspecified_remote,
        )


@dataclass(frozen=True)
class RelevanceDecision:
    outcome: str
    axis: str | None = None
    reason: str | None = None


def _word_match(marker: str, text: str) -> bool:
    # Normalize punctuation and repeated whitespace on both sides so title
    # variants such as "AI-Enabled", "AI/ML", "Associate - Risk", and
    # "Founding Engineer, AI" match the same vocabulary phrases. Retaining
    # word boundaries still prevents partial-token matches.
    text = re.sub(r"[\W_]+", " ", text).strip().lower()
    marker = re.sub(r"[\W_]+", " ", marker).strip().lower()
    return re.search(r"\b" + re.escape(marker) + r"\b", text) is not None


def _job_type_decision(
    job: NormalizedJob, policy: RelevancePolicy,
) -> RelevanceDecision | None:
    """Apply only the two default public job-type exclusions."""
    title = str(getattr(job, "title", None) or "")
    employment_type = str(getattr(job, "employment_type", None) or "")
    declared_seniority = str(getattr(job, "seniority", None) or "")
    evidence = " ".join((title, employment_type, declared_seniority))
    if not policy.include_internships and any(
        _word_match(marker, evidence) for marker in ("intern", "internship")
    ):
        return RelevanceDecision(
            "rejected", "job_type", "internships are excluded by default"
        )
    if not policy.include_part_time and _word_match("part time", evidence):
        return RelevanceDecision(
            "rejected", "job_type", "part-time jobs are excluded by default"
        )
    return None


def _profile_location_decision(
    job: NormalizedJob, policy: RelevancePolicy
) -> RelevanceDecision:
    location = str(getattr(job, "location_raw", None) or "").strip().lower()
    remote_scope = str(getattr(job, "remote_scope", None) or "").strip().lower()
    combined = " ".join(part for part in (location, remote_scope) if part)
    is_hybrid = _word_match("hybrid", combined)
    is_remote = bool(getattr(job, "is_remote", None)) or _word_match("remote", combined)
    arrangement = "hybrid" if is_hybrid else "remote" if is_remote else "onsite"

    exact_country = bool(policy.country_markers) and any(
        re.sub(r"[\W_]+", " ", location).strip()
        == re.sub(r"[\W_]+", " ", marker).strip()
        for marker in policy.country_markers
    )
    if (arrangement == "onsite" and "onsite" not in policy.arrangements
            and "remote" in policy.arrangements and exact_country):
        if policy.exact_country_as_remote:
            return RelevanceDecision("kept", reason="exact-country remote fallback")
        return RelevanceDecision(
            "needs_review", "location", "exact-country listing has unclear arrangement"
        )

    if arrangement not in policy.arrangements:
        return RelevanceDecision(
            "rejected", "location", f"{arrangement} work is not selected"
        )

    has_target_country = any(
        _word_match(marker, combined) for marker in policy.country_markers
    )
    has_global_scope = any(
        _word_match(marker, combined) for marker in _GLOBAL_REMOTE_MARKERS
    )
    foreign_markers = tuple(
        marker
        for marker in FOREIGN_COUNTRY_MARKERS
        if marker not in policy.country_markers
    )
    has_foreign_country = any(
        _word_match(marker, combined) for marker in foreign_markers
    )

    if arrangement == "remote":
        if has_target_country or has_global_scope:
            return RelevanceDecision("kept")
        if has_foreign_country:
            return RelevanceDecision("rejected", "location", "remote country is outside profile")
        if policy.unspecified_remote:
            return RelevanceDecision("kept", reason="unspecified-remote fallback")
        return RelevanceDecision(
            "needs_review", "location", "remote country is unclear"
        )

    if has_foreign_country and not has_target_country:
        return RelevanceDecision("rejected", "location", "work location is outside profile")
    has_target_city = any(
        _word_match(alias, combined)
        for city in policy.cities for alias in city_aliases(city)
    )
    if policy.cities and has_target_city:
        return RelevanceDecision("kept")
    if not policy.cities and (
        has_target_country or ("IN" in policy.countries and has_indian_city(combined))
    ):
        return RelevanceDecision("kept")
    if has_foreign_country:
        return RelevanceDecision("rejected", "location", "work location is outside profile")
    return RelevanceDecision(
        "needs_review", "location", "onsite or hybrid city is unclear"
    )


def evaluate_relevance(
    job: NormalizedJob,
    policy: RelevancePolicy,
    *,
    cutoff_at: datetime | None = None,
) -> RelevanceDecision:
    """Classify one job using an accepted profile's frozen relevance policy."""
    title = str(getattr(job, "title", None) or "").lower()
    posting_text = " ".join((
        title,
        str(getattr(job, "description_raw", None) or "").lower(),
    ))
    if any(_word_match(marker, posting_text) for marker in policy.hard_rejects):
        return RelevanceDecision(
            "rejected", "hard_reject", "posting matches an explicit hard reject"
        )
    if not title or any(
        _word_match(marker, title) for marker in policy.title_exclusions
    ):
        return RelevanceDecision("rejected", "role", "title is explicitly excluded")
    job_type_decision = _job_type_decision(job, policy)
    if job_type_decision is not None:
        return job_type_decision
    if not any(_word_match(marker, title) for marker in policy.role_terms):
        return RelevanceDecision("rejected", "role", "title does not match relevance terms")

    return evaluate_after_semantic_role(job, policy, cutoff_at=cutoff_at)


def evaluate_after_semantic_role(
    job: NormalizedJob,
    policy: RelevancePolicy,
    *,
    cutoff_at: datetime | None = None,
) -> RelevanceDecision:
    """Evaluate non-role axes after the agent has accepted the role intent."""
    del cutoff_at  # Accepted for caller compatibility; recency is not gated.
    title = str(getattr(job, "title", None) or "").lower()
    posting_text = " ".join((
        title,
        str(getattr(job, "description_raw", None) or "").lower(),
    ))
    if any(_word_match(marker, posting_text) for marker in policy.hard_rejects):
        return RelevanceDecision(
            "rejected", "hard_reject", "posting matches an explicit hard reject"
        )
    if not title or any(
        _word_match(marker, title) for marker in policy.title_exclusions
    ):
        return RelevanceDecision("rejected", "role", "title is explicitly excluded")
    job_type_decision = _job_type_decision(job, policy)
    if job_type_decision is not None:
        return job_type_decision

    if (
        policy.experience_minimum_years is not None
        or policy.experience_maximum_years is not None
    ):
        payload = getattr(job, "raw_payload", None) or {}
        evidence = payload.get("pre_gate_job_enrichment") or {}
        required_min = evidence.get("experience_min_years")
        required_max = evidence.get("experience_max_years")
        if required_min is None and required_max is None:
            return RelevanceDecision(
                "needs_review", "experience", "experience requirement is unknown"
            )
        if (
            policy.experience_maximum_years is not None
            and required_min is not None
            and required_min > policy.experience_maximum_years
        ):
            return RelevanceDecision(
                "rejected", "experience", "minimum required experience exceeds profile"
            )
        if (
            policy.experience_minimum_years is not None
            and required_max is not None
            and required_max < policy.experience_minimum_years
        ):
            return RelevanceDecision(
                "rejected", "experience", "maximum required experience is below profile"
            )
    return _profile_location_decision(job, policy)


def role_ok(job: NormalizedJob) -> bool:
    """Title must name a target role keyword and no off-role exclude marker.

    Title-only per spec — description is never consulted.
    """
    title = getattr(job, "title", None)
    if not title:
        return False
    title_lower = title.lower()

    # A narrow set of explicit occupations wins when an otherwise off-role
    # word is only a domain qualifier (for example, Growth Marketing).
    if any(
        _word_match(marker, title_lower)
        for marker in ROLE_TITLE_OVERRIDE_KEYWORDS
    ):
        return True

    if any(_word_match(marker, title_lower) for marker in OFF_ROLE_TITLE_MARKERS):
        return False

    return any(_word_match(kw, title_lower) for kw in ROLE_TITLE_KEYWORDS)


def seniority_ok(job: NormalizedJob) -> bool:
    """Keep the IC-through-manager band; drop junior and exec-level titles."""
    title = getattr(job, "title", None) or ""
    title_lower = title.lower()

    if any(_word_match(marker, title_lower) for marker in JUNIOR_TITLE_MARKERS):
        return False
    # An in-band override (e.g. AVP) wins over a contained exec marker.
    if any(_word_match(marker, title_lower) for marker in SENIORITY_BAND_OVERRIDES):
        return True
    if any(_word_match(marker, title_lower) for marker in EXEC_TITLE_MARKERS):
        return False
    return True


def _location_drop_reason(job: NormalizedJob) -> str | None:
    """Return why `location_ok` would drop this job — "foreign" (explicitly
    non-India-eligible) or "no_signal" (no Bangalore marker found, which is
    often a connector capture gap rather than a confirmed-foreign job) — or
    None if the job passes. `location_ok` is a thin bool wrapper over this so
    the two can never disagree; the kept/dropped decision is unchanged from
    before this split was added, only the reason is now surfaced.
    """
    location_raw = getattr(job, "location_raw", None)
    remote_scope = getattr(job, "remote_scope", None)
    is_remote = getattr(job, "is_remote", None)

    combined = " ".join(filter(None, [location_raw, remote_scope])).lower()

    is_hybrid = "hybrid" in combined

    if is_hybrid:
        if any(_word_match(marker, combined) for marker in BANGALORE_MARKERS):
            return None
        return "no_signal"

    is_remote_job = bool(is_remote) or "remote" in combined

    if not is_remote_job:
        if any(_word_match(marker, combined) for marker in BANGALORE_MARKERS):
            return None
        return "no_signal"

    # Remote path.
    if not combined.strip():
        return None  # bare "Remote" / no location signal — keep (recall exception)

    if any(_word_match(marker, combined) for marker in INDIA_ELIGIBLE_MARKERS):
        return None

    if any(_word_match(marker, combined) for marker in FOREIGN_ONLY_MARKERS):
        return "foreign"

    return None  # ambiguous — keep


def location_ok(job: NormalizedJob) -> bool:
    """Onsite/hybrid must be Bangalore; remote must not be foreign-only."""
    return _location_drop_reason(job) is None


def recency_ok(
    job: NormalizedJob, *, cutoff_at: datetime | None = None
) -> bool:
    """Keep if posted_at unknown; drop if older than the recency window."""
    posted_at = getattr(job, "posted_at", None)
    if posted_at is None:
        return True
    if not isinstance(posted_at, datetime):
        return True  # unparseable/string — keep, don't crash

    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)

    cutoff = cutoff_at or (
        datetime.now(timezone.utc) - timedelta(days=RECENCY_WINDOW_DAYS)
    )
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    else:
        cutoff = cutoff.astimezone(timezone.utc)
    return posted_at >= cutoff


# Active axes in check order. Recency remains a compatibility-only summary
# field so existing dashboards and reports show zero instead of changing
# their schema; it is deliberately absent from the checks.
AXIS_CHECKS: list[tuple[str, Callable[[Any], bool]]] = [
    ("role", role_ok),
    ("seniority", seniority_ok),
    ("location", location_ok),
]

AXIS_NAMES: list[str] = [name for name, _ in AXIS_CHECKS] + ["recency"]


def first_failing_axis(
    job: NormalizedJob, *, cutoff_at: datetime | None = None
) -> str | None:
    """Return the name of the first axis this job fails, or None if it passes."""
    del cutoff_at  # Accepted for caller compatibility; recency is not gated.
    for axis_name, check in AXIS_CHECKS:
        if not check(job):
            return axis_name
    return None


def filter_relevant(
    jobs: list[NormalizedJob],
    *,
    cutoff_at: datetime | None = None,
) -> tuple[list[NormalizedJob], dict[str, int], list[dict[str, Any]]]:
    """Apply the three active relevance axes; attribute each drop to its first-failed
    axis. Returns (kept, drop_counts, drop_details) — drop_details is one
    dict per dropped job with an "axis" key, plus a "location_reason" key
    ("foreign" | "no_signal") for axis=="location" drops, per ADR-0002's
    location_ok() foreign-vs-no_signal split (see CONTEXT.md/CLAUDE.md)."""
    drop_counts: dict[str, int] = {name: 0 for name in AXIS_NAMES}
    kept: list[NormalizedJob] = []
    drop_details: list[dict[str, Any]] = []

    for job in jobs:
        axis = first_failing_axis(job, cutoff_at=cutoff_at)
        if axis is None:
            kept.append(job)
            continue

        drop_counts[axis] += 1
        detail: dict[str, Any] = {"axis": axis}
        if axis == "location":
            detail["location_reason"] = _location_drop_reason(job)
        drop_details.append(detail)

    return kept, drop_counts, drop_details
