"""Deterministic LinkedIn work-location enrichment before relevance gating.

LinkedIn's logged-out listings omit the work-mode badge, so this module reads
the hydrated description and records an auditable rule-based location result.
It never calls an LLM and never searches company career sites.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.schemas import NormalizedJob
from app.config.geography import INDIA_CITIES as _INDIA_CITIES


WorkLocationDecision = Literal[
    "remote_india",
    "remote_unspecified",
    "remote_foreign_only",
    "bengaluru_workplace",
    "onsite_outside_bengaluru",
    "unclear",
]

class WorkLocationEnrichmentInput(BaseModel):
    source: Literal["linkedin"]
    external_job_id: str
    title: str
    company_name: str = ""
    listing_location: str | None
    posted_at: datetime | None = None
    job_url: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    salary_raw: str | None = None
    full_job_enrichment_requested: bool = False
    description_sha256: str | None
    description: str | None


class FullJobEnrichment(BaseModel):
    status: Literal["done", "no_description"]
    experience_min_years: int | None = None
    experience_max_years: int | None = None
    education_requirement: str | None = None
    qualification_other: str | None = None
    hard_skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    salary: dict | None = None
    minimum_experience: dict | None = None
    maximum_experience: dict | None = None

    @model_validator(mode="after")
    def validate_experience_range(self):
        for value in (self.experience_min_years, self.experience_max_years):
            if value is not None and not 0 <= value <= 50:
                raise ValueError("experience years must be between 0 and 50")
        if (
            self.experience_min_years is not None
            and self.experience_max_years is not None
            and self.experience_min_years > self.experience_max_years
        ):
            raise ValueError("minimum experience cannot exceed maximum experience")
        if self.status == "no_description" and any((
            self.experience_min_years is not None,
            self.experience_max_years is not None,
            self.education_requirement,
            self.qualification_other,
            self.hard_skills,
            self.soft_skills,
            self.salary,
            self.minimum_experience,
            self.maximum_experience,
        )):
            raise ValueError("no_description enrichment cannot contain extracted facts")
        if self.education_requirement and len(self.education_requirement) > 255:
            raise ValueError(
                "education_requirement must be a concise fact of at most 255 characters"
            )
        oversized_skills = [
            skill for skill in (*self.hard_skills, *self.soft_skills)
            if len(skill) > 100
        ]
        if oversized_skills:
            raise ValueError("skill labels must be at most 100 characters")
        return self


class WorkLocationEnrichmentResult(BaseModel):
    source: Literal["linkedin"] = "linkedin"
    external_job_id: str
    description_sha256: str | None
    decision: WorkLocationDecision
    evidence: str
    reason: str
    confidence: Literal["high", "medium", "low"]
    required_location: str | None = None
    job_enrichment: FullJobEnrichment | None = None


def description_sha256(description: str | None) -> str | None:
    if not description:
        return None
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def _location_segments(description: str | None) -> list[str]:
    return [
        segment.strip()
        for segment in re.split(
            r"(?<=[.!?])\s+|(?=\b(?:Location|Job Location|Work Location|Mode|Work Mode|Where You.ll Work|In-Office Expectations)\s*[:\-])",
            description or "",
        )
        if segment.strip()
    ]


def classify_work_location(
    packet: WorkLocationEnrichmentInput,
) -> WorkLocationEnrichmentResult:
    """Classify one hydrated LinkedIn description with deterministic rules."""
    description = packet.description or ""
    pieces = _location_segments(description)
    location_pieces = [
        piece for piece in pieces
        if re.search(
            r"\b(remote|remote[ -]?first|work remotely|work from home|work-from-home|work from anywhere|wfh|flexible work arrangements?|hybrid|on[ -]?site|in-office|office|based in|based at|location\s*:|work mode|work arrangement)\b",
            piece,
            re.I,
        )
    ]

    def make(
        decision: WorkLocationDecision,
        evidence: str,
        confidence: Literal["high", "medium", "low"],
        required_location: str | None = None,
        rule: str = "no_workplace_signal",
    ) -> WorkLocationEnrichmentResult:
        return WorkLocationEnrichmentResult(
            external_job_id=packet.external_job_id,
            description_sha256=packet.description_sha256,
            decision=decision,
            evidence=evidence,
            reason=f"deterministic_rule:{rule}",
            confidence=confidence,
            required_location=required_location,
        )

    foreign = [
        piece for piece in location_pieces
        if re.search(r"\b(remote|work from home)\b", piece, re.I)
        and re.search(
            r"\b(United States|USA|U\.S\.|Canada|UK|United Kingdom|Europe|Australia)\b",
            piece,
            re.I,
        )
        and not re.search(r"\bIndia\b", piece, re.I)
        and re.search(r"\b(must|only|eligible|based|located|reside|within)\b", piece, re.I)
    ]
    if foreign:
        evidence = min(foreign, key=len)
        return make("remote_foreign_only", evidence, "high", rule="foreign_remote_only")

    india_remote_city = [
        piece for piece in location_pieces
        if re.search(
            rf"\bremote\s*[-–—]\s*(?:{'|'.join(map(re.escape, _INDIA_CITIES))})\s*,\s*India\b",
            piece,
            re.I,
        )
    ]
    if india_remote_city:
        return make(
            "remote_india", min(india_remote_city, key=len), "high", "India",
            "remote_india_city",
        )

    explicit_remote_benefit = [
        piece for piece in location_pieces
        if re.search(
            r"\blocation\s*:\s*permanent\s+wfh\b|"
            r"\bflexible\s+wfh\s+policy\b|"
            r"\bflexible\s+work\s+arrangements?\s*\(\s*remote\s+and/or\s+office-based\s*\)|"
            r"\bflexible\s+work\s+arrangements?\s*,?\s+supporting\s+work[- ]life\s+balance\b",
            piece,
            re.I,
        )
    ]
    if explicit_remote_benefit:
        return make(
            "remote_unspecified", min(explicit_remote_benefit, key=len), "medium",
            rule="explicit_remote_benefit",
        )

    explicit_remote = [
        piece for piece in location_pieces
        if re.search(
            r"\b(fully remote|100% remote|remote[ -]?first|remote working model|work from home|work-from-home|work from anywhere|remote opportunity|location\s*:\s*remote|remote\s*\(India\)|remote India|Remote[ —-]+India)\b",
            piece,
            re.I,
        )
    ]
    if explicit_remote:
        evidence = min(explicit_remote, key=len)
        india = bool(re.search(r"\bIndia\b", evidence, re.I)) or bool(re.search(
            r"\b(?:based in India|across India|India-based|India based|employees? in India|hire from[^.]{0,100}India)\b",
            description,
            re.I,
        ))
        return make(
            "remote_india" if india else "remote_unspecified",
            evidence,
            "high" if india else "medium",
            "India" if india else None,
            "explicit_remote",
        )

    ambiguous_remote = [
        piece for piece in location_pieces
        if re.search(
            r"offices?\s+and\s+remote\s+work\s+environments?|"
            r"\[[^\]]*\b(?:remote|hybrid)\b[^\]]*\]",
            piece,
            re.I,
        )
    ]
    if ambiguous_remote:
        return make(
            "unclear", min(ambiguous_remote, key=len), "medium",
            rule="generic_or_unresolved_remote_wording",
        )

    remote_mode = [
        piece for piece in location_pieces
        if re.search(
            r"(?:\bLocation\s*:[^.!?]{0,100}\bRemote\b|\bRemote\s*(?:role|position|job|work|option)\b|\bwork remotely\b)",
            piece,
            re.I,
        )
    ]
    if remote_mode:
        evidence = min(remote_mode, key=len)
        india = bool(re.search(r"\bIndia\b", evidence, re.I)) or bool(
            re.search(r"\bbased in India\b", description, re.I)
        )
        return make(
            "remote_india" if india else "remote_unspecified",
            evidence,
            "high" if india else "medium",
            "India" if india else None,
            "remote_mode",
        )

    bengaluru = [
        piece for piece in location_pieces
        if re.search(r"\b(Bengaluru|Bangalore)\b", piece, re.I)
        and re.search(
            r"\b(location|based|office|hybrid|on[ -]?site|in-office|work from|days? per week)\b",
            piece,
            re.I,
        )
    ]
    if bengaluru:
        return make(
            "bengaluru_workplace", min(bengaluru, key=len), "high", "Bengaluru",
            "bengaluru_workplace",
        )

    concrete: list[tuple[str, str]] = []
    for piece in location_pieces:
        if not re.search(
            r"\b(on[ -]?site|in-office|office|hybrid|based in|based at|location\s*:|work from)\b",
            piece,
            re.I,
        ):
            continue
        city = next((
            city for city in _INDIA_CITIES
            if re.search(rf"\b{re.escape(city)}\b", piece, re.I)
        ), None)
        if city:
            concrete.append((piece, city))
    if concrete:
        evidence, city = min(concrete, key=lambda item: len(item[0]))
        return make(
            "onsite_outside_bengaluru", evidence, "high", city,
            "concrete_non_bengaluru_workplace",
        )

    nonremote = [
        piece for piece in location_pieces
        if re.search(r"\b(on[ -]?site|in-office|hybrid)\b", piece, re.I)
        and not re.search(
            r"\b(cloud|retrieval|search|environment|solution|approach|system|model)\b",
            piece,
            re.I,
        )
    ]
    if nonremote:
        return make("unclear", min(nonremote, key=len), "medium", rule="nonremote_ambiguous")
    return make("unclear", "", "low")


def write_deterministic_results(
    path: Path,
    packets: tuple[WorkLocationEnrichmentInput, ...],
) -> tuple[WorkLocationEnrichmentResult, ...]:
    results = tuple(classify_work_location(packet) for packet in packets)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(result.model_dump_json() + "\n" for result in results),
        encoding="utf-8",
    )
    return results


def build_input(
    job: NormalizedJob,
    *,
    full_job_enrichment_requested: bool = False,
) -> WorkLocationEnrichmentInput:
    return WorkLocationEnrichmentInput(
        source="linkedin",
        external_job_id=job.external_job_id,
        title=job.title,
        company_name=job.company_name_raw,
        listing_location=job.location_raw,
        posted_at=job.posted_at,
        job_url=job.job_url,
        employment_type=job.employment_type,
        seniority=job.seniority,
        salary_raw=job.salary_raw,
        full_job_enrichment_requested=full_job_enrichment_requested,
        description_sha256=description_sha256(job.description_raw),
        description=job.description_raw,
    )


def write_inputs(
    path: Path,
    jobs: list[NormalizedJob],
    *,
    full_job_enrichment_requested: bool = False,
) -> tuple[WorkLocationEnrichmentInput, ...]:
    packets = tuple(
        build_input(
            job,
            full_job_enrichment_requested=full_job_enrichment_requested,
        )
        for job in jobs
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(packet.model_dump_json() + "\n" for packet in packets),
        encoding="utf-8",
    )
    return packets


def load_results(
    path: Path,
    expected: tuple[WorkLocationEnrichmentInput, ...],
) -> dict[str, WorkLocationEnrichmentResult]:
    if not path.exists():
        raise FileNotFoundError(
            f"LinkedIn location enrichment is required before relevance: {path}"
        )
    results: dict[str, WorkLocationEnrichmentResult] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            result = WorkLocationEnrichmentResult.model_validate_json(line)
        except Exception as exc:
            raise ValueError(f"invalid location result at line {line_number}: {exc}") from exc
        if result.external_job_id in results:
            raise ValueError(f"duplicate LinkedIn location result {result.external_job_id}")
        results[result.external_job_id] = result

    expected_by_id = {packet.external_job_id: packet for packet in expected}
    if set(results) != set(expected_by_id):
        raise ValueError(
            "LinkedIn location enrichment coverage mismatch: "
            f"missing={sorted(set(expected_by_id) - set(results))} "
            f"unexpected={sorted(set(results) - set(expected_by_id))}"
        )
    for external_job_id, result in results.items():
        packet = expected_by_id[external_job_id]
        expected_hash = packet.description_sha256
        if result.description_sha256 != expected_hash:
            raise ValueError(
                f"stale LinkedIn location result {external_job_id}: "
                f"expected description_sha256={expected_hash!r}"
            )
        if packet.full_job_enrichment_requested and result.job_enrichment is None:
            raise ValueError(
                f"LinkedIn full job enrichment is required for {external_job_id}"
            )
        if result.job_enrichment is not None:
            expected_status = "done" if packet.description_sha256 else "no_description"
            if result.job_enrichment.status != expected_status:
                raise ValueError(
                    f"job enrichment status mismatch for {external_job_id}: "
                    f"expected {expected_status}"
                )
            if (
                packet.salary_raw
                and result.job_enrichment.status == "done"
                and result.job_enrichment.salary is None
            ):
                raise ValueError(
                    f"salary disposition is required for {external_job_id}"
                )
            salary = result.job_enrichment.salary
            if salary is not None:
                if not str(salary.get("evidence") or "").strip():
                    raise ValueError(
                        f"salary evidence is required for {external_job_id}"
                    )
                if (
                    salary.get("guaranteed_max_lpa") is None
                    and not str(salary.get("unusable_reason") or "").strip()
                ):
                    raise ValueError(
                        "salary requires guaranteed_max_lpa or unusable_reason "
                        f"for {external_job_id}"
                    )
    return results


def evidence_from_result(
    result: WorkLocationEnrichmentResult,
    *,
    listing_location: str | None,
    linkedin_remote_query: bool = False,
) -> dict:
    common = {
        "evidence": result.evidence,
        "reason": (
            f"deterministic_{result.decision}"
            if result.reason.startswith("deterministic_rule:")
            else f"agent_{result.decision}"
        ),
        "judgment_method": (
            "deterministic_regex"
            if result.reason.startswith("deterministic_rule:")
            else "agent"
        ),
        "agent_reason": result.reason,
        "confidence": result.confidence,
        "decision": result.decision,
        "description_sha256": result.description_sha256,
        "linkedin_remote_query": linkedin_remote_query,
        "listing_location": listing_location,
        "fallback_remote_tagged": False,
    }
    if result.decision == "remote_india":
        normalized = ("remote", "India", True, "Remote — India")
    elif result.decision == "remote_unspecified":
        normalized = ("remote", None, True, "Remote")
    elif result.decision == "remote_foreign_only":
        scope = result.required_location or "foreign only"
        normalized = ("remote", scope, True, f"Remote — {scope} only")
    elif result.decision == "bengaluru_workplace":
        normalized = ("hybrid", "Bengaluru, Karnataka, India", False, None)
    elif result.decision == "onsite_outside_bengaluru":
        normalized = (
            "onsite",
            result.required_location or listing_location or "Outside Bengaluru",
            False,
            None,
        )
    else:
        if (
            linkedin_remote_query
            and result.confidence in {"low", "medium"}
            and (listing_location or "").strip().casefold() == "india"
        ):
            normalized = ("remote", "India", True, "Remote — India")
            common["fallback_remote_tagged"] = True
        else:
            normalized = ("unclear", listing_location, False, None)
    workplace_type, location_raw, is_remote, remote_scope = normalized
    return {
        **common,
        "workplace_type": workplace_type,
        "location_raw": location_raw,
        "is_remote": is_remote,
        "remote_scope": remote_scope,
    }


def apply_results(
    jobs: list[NormalizedJob],
    results: dict[str, WorkLocationEnrichmentResult],
) -> int:
    applied = 0
    for job in jobs:
        if job.source != "linkedin":
            continue
        result = results.get(job.external_job_id)
        if result is None:
            continue
        evidence = evidence_from_result(
            result,
            listing_location=job.location_raw,
            linkedin_remote_query=(
                job.raw_payload.get("query_location_mode") == "remote_india"
            ),
        )
        job.location_raw = evidence["location_raw"]
        job.is_remote = evidence["is_remote"]
        job.remote_scope = evidence["remote_scope"]
        job.raw_payload["description_location_agent_evidence"] = evidence
        job.raw_payload["description_location_evidence"] = evidence
        if result.job_enrichment is not None:
            enrichment = result.job_enrichment
            payload = enrichment.model_dump(mode="json")
            payload["hard_skills"] = list(dict.fromkeys(enrichment.hard_skills))
            payload["soft_skills"] = list(dict.fromkeys(enrichment.soft_skills))
            payload["enriched_at"] = datetime.now(timezone.utc).isoformat()
            job.raw_payload["pre_gate_job_enrichment"] = payload
        applied += 1
    return applied
