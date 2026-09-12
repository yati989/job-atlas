"""Durable optional Phase B market-evidence seam for public selection scopes.

This module deliberately owns planning, authorization, reservation, and result
persistence only.  A caller supplies the source-specific provider callable;
the reservation is committed before that callable is invoked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.companies.market_profile import (
    calculate_market_profile, save_ambitionbox_market_profile,
    save_glassdoor_market_profile,
)
from app.models.orm import (
    Company, PublicPhaseBAuthorization, PublicPhaseBCall, PublicPhaseBEvidence,
    PublicSelectionScope, PublicSelectionScopeItem,
)


SUPPORTED_SOURCES = frozenset({"glassdoor", "ambitionbox"})
_REUSABLE_STATUSES = frozenset({"ok", "missing"})


class PublicPhaseBBudgetExceeded(RuntimeError):
    """The approved public Phase B provider-call ceiling is exhausted."""


@dataclass(frozen=True)
class PublicPhaseBPlanItem:
    company_id: int
    company_name: str
    source: str
    reusable: bool
    reusable_evidence: dict | None = None

    def as_dict(self) -> dict:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "source": self.source,
            "reusable": self.reusable,
            "reusable_evidence": self.reusable_evidence,
        }


@dataclass(frozen=True)
class PublicPhaseBPreview:
    scope_id: int
    company_count: int
    source_count: int
    planned_call_count: int
    reusable_evidence_count: int
    items: tuple[PublicPhaseBPlanItem, ...]

    @property
    def call_plan(self) -> list[dict]:
        return [item.as_dict() for item in self.items if not item.reusable]


@dataclass(frozen=True)
class PublicPhaseBReservation:
    call: PublicPhaseBCall
    created: bool


class PublicPhaseBResult(BaseModel):
    """One provider observation, restricted to the two approved source shapes."""
    model_config = ConfigDict(extra="forbid")

    company_id: int = Field(gt=0)
    source: Literal["glassdoor", "ambitionbox"]
    status: Literal["ok", "missing", "error", "ambiguous"]
    evidence: dict = Field(default_factory=dict)
    overall_rating: float | None = Field(default=None, ge=1, le=5)
    wlb_rating: float | None = Field(default=None, ge=1, le=5)
    review_count: int | None = Field(default=None, ge=0)
    estimated_salary_lpa: float | None = Field(default=None, gt=0)
    evidence_url: str | None = None

    @model_validator(mode="after")
    def source_fields_are_coherent(self):
        if self.source == "glassdoor" and self.estimated_salary_lpa is not None:
            raise ValueError("Glassdoor results cannot include India salary estimates")
        if self.source == "ambitionbox" and self.review_count is not None:
            raise ValueError("AmbitionBox results cannot include Glassdoor review counts")
        if self.status != "ok" and any((
            self.overall_rating is not None, self.wlb_rating is not None,
            self.review_count is not None, self.estimated_salary_lpa is not None,
        )):
            raise ValueError("only an ok source result can include market values")
        return self


Provider = Callable[[int, str], PublicPhaseBResult | dict]


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _scope(session: Session, scope_id: int) -> PublicSelectionScope:
    scope = session.get(PublicSelectionScope, scope_id)
    if scope is None:
        raise ValueError(f"unknown public selection scope {scope_id}")
    return scope


def _normalize_sources(sources: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized = tuple(source.strip().lower() for source in sources)
    if not normalized:
        raise ValueError("Phase B requires at least one supported source")
    unsupported = sorted(set(normalized) - SUPPORTED_SOURCES)
    if unsupported:
        raise ValueError("unsupported Phase B source: " + ", ".join(unsupported))
    if len(set(normalized)) != len(normalized):
        raise ValueError("Phase B sources must be unique")
    return tuple(sorted(normalized))


def preview_phase_b(
    session: Session, *, scope_id: int, sources: tuple[str, ...] | list[str],
) -> PublicPhaseBPreview:
    """Show exactly which scoped company/source calls require approval."""
    _scope(session, scope_id)
    selected_sources = _normalize_sources(sources)
    companies = session.execute(
        select(Company).join(
            PublicSelectionScopeItem,
            PublicSelectionScopeItem.company_id == Company.id,
        ).where(PublicSelectionScopeItem.scope_id == scope_id).distinct().order_by(Company.id)
    ).scalars().all()
    items: list[PublicPhaseBPlanItem] = []
    for company in companies:
        source_evidence = company.market_profile_evidence
        source_evidence = source_evidence if isinstance(source_evidence, dict) else {}
        source_evidence = source_evidence.get("sources", {})
        source_evidence = source_evidence if isinstance(source_evidence, dict) else {}
        for source in selected_sources:
            evidence = source_evidence.get(source)
            reusable = isinstance(evidence, dict) and evidence.get("status") in _REUSABLE_STATUSES
            items.append(PublicPhaseBPlanItem(
                company_id=company.id, company_name=company.name, source=source,
                reusable=reusable, reusable_evidence=dict(evidence) if reusable else None,
            ))
    return PublicPhaseBPreview(
        scope_id=scope_id, company_count=len(companies), source_count=len(selected_sources),
        planned_call_count=sum(not item.reusable for item in items),
        reusable_evidence_count=sum(item.reusable for item in items), items=tuple(items),
    )


def authorize_phase_b(
    session: Session, *, scope_id: int, sources: tuple[str, ...] | list[str], maximum_calls: int,
) -> PublicPhaseBAuthorization:
    """Freeze the exact scope/source plan and its global provider-call cap."""
    _scope(session, scope_id)
    preview = preview_phase_b(session, scope_id=scope_id, sources=sources)
    if maximum_calls < 0 or maximum_calls > preview.planned_call_count:
        raise ValueError(
            f"maximum_calls must be between 0 and {preview.planned_call_count}"
        )
    call_plan = preview.call_plan
    fingerprint = _fingerprint(call_plan)
    existing = session.scalar(select(PublicPhaseBAuthorization).where(
        PublicPhaseBAuthorization.scope_id == scope_id,
    ))
    if existing is not None:
        if (
            existing.plan_fingerprint != fingerprint
            or existing.source_plan != call_plan
            or existing.maximum_calls != maximum_calls
        ):
            raise ValueError("selection scope already has a different Phase B authorization")
        return existing
    authorization = PublicPhaseBAuthorization(
        scope_id=scope_id, plan_fingerprint=fingerprint, source_plan=call_plan,
        maximum_calls=maximum_calls, planned_call_count=preview.planned_call_count,
    )
    session.add(authorization)
    session.flush()
    return authorization


def reserve_phase_b_call(
    session: Session, *, authorization_id: int, company_id: int, source: str,
) -> PublicPhaseBReservation:
    """Reserve one approved call.  The caller must commit before provider I/O."""
    authorization = session.get(PublicPhaseBAuthorization, authorization_id)
    if authorization is None:
        raise ValueError(f"unknown Phase B authorization {authorization_id}")
    source = source.strip().lower()
    approved = {(
        item["company_id"], item["source"],
    ) for item in authorization.source_plan}
    if (company_id, source) not in approved:
        raise ValueError("company/source call is outside the approved Phase B plan")
    existing = session.scalar(select(PublicPhaseBCall).where(
        PublicPhaseBCall.authorization_id == authorization_id,
        PublicPhaseBCall.company_id == company_id,
        PublicPhaseBCall.source == source,
    ))
    if existing is not None:
        return PublicPhaseBReservation(existing, False)
    reserved = session.execute(
        update(PublicPhaseBAuthorization)
        .where(
            PublicPhaseBAuthorization.id == authorization_id,
            PublicPhaseBAuthorization.reserved_call_count
            < PublicPhaseBAuthorization.maximum_calls,
        )
        .values(
            reserved_call_count=PublicPhaseBAuthorization.reserved_call_count + 1,
        )
    )
    if reserved.rowcount != 1:
        session.expire(authorization, ["reserved_call_count"])
        spent = authorization.reserved_call_count
        raise PublicPhaseBBudgetExceeded(
            f"approved Phase B budget exhausted: {spent}/{authorization.maximum_calls}"
        )
    call = PublicPhaseBCall(
        authorization_id=authorization_id, company_id=company_id, source=source,
        status="reserved",
    )
    session.add(call)
    session.flush()
    return PublicPhaseBReservation(call, True)


def complete_phase_b_call(
    session: Session, *, call_id: int, result: PublicPhaseBResult | dict,
) -> PublicPhaseBCall:
    """Persist one terminal source observation and update only its company fields."""
    result = PublicPhaseBResult.model_validate(result)
    call = session.get(PublicPhaseBCall, call_id)
    if call is None:
        raise ValueError(f"unknown Phase B call {call_id}")
    if (call.company_id, call.source) != (result.company_id, result.source):
        raise ValueError("Phase B result does not match its reserved company/source call")
    payload = result.model_dump(mode="json")
    existing = session.scalar(select(PublicPhaseBEvidence).where(
        PublicPhaseBEvidence.call_id == call.id,
    ))
    if existing is not None:
        if existing.evidence != payload:
            raise ValueError("Phase B call already has different terminal evidence")
        return call
    if call.status != "reserved":
        raise ValueError("Phase B call has no terminal evidence but is not reservable")

    source_status = "error" if result.status == "ambiguous" else result.status
    source_evidence = dict(result.evidence)
    source_evidence["status"] = source_status
    if result.evidence_url:
        source_evidence["url"] = result.evidence_url
    if result.source == "glassdoor":
        save_glassdoor_market_profile(
            session, result.company_id, overall_rating=result.overall_rating,
            wlb_rating=result.wlb_rating, review_count=result.review_count,
            evidence=source_evidence,
        )
    else:
        save_ambitionbox_market_profile(
            session, result.company_id, overall_rating=result.overall_rating,
            wlb_rating=result.wlb_rating, salary_lpa=result.estimated_salary_lpa,
            evidence=source_evidence,
        )
    calculate_market_profile(session, result.company_id)
    call.status = {
        "ok": "succeeded", "missing": "missing", "error": "failed", "ambiguous": "ambiguous",
    }[result.status]
    call.completed_at = datetime.now(timezone.utc)
    session.add(PublicPhaseBEvidence(
        call_id=call.id, company_id=result.company_id, source=result.source,
        status=result.status, evidence=payload,
    ))
    session.flush()
    return call


def execute_phase_b_call(
    session: Session, *, authorization_id: int, company_id: int, source: str, provider: Provider,
) -> PublicPhaseBCall:
    """Commit reservation, invoke a supplied provider, then persist its outcome.

    This is intentionally the only helper that invokes provider code, making
    the reserve-before-I/O boundary auditable and easy to test without network.
    """
    reservation = reserve_phase_b_call(
        session, authorization_id=authorization_id, company_id=company_id, source=source,
    )
    if not reservation.created:
        return reservation.call
    session.commit()
    try:
        result = PublicPhaseBResult.model_validate(provider(company_id, source))
    except Exception as exc:  # provider errors are durable terminal evidence
        result = PublicPhaseBResult(
            company_id=company_id, source=source, status="error",
            evidence={"error": f"{type(exc).__name__}: {exc}"},
        )
    completed = complete_phase_b_call(session, call_id=reservation.call.id, result=result)
    session.commit()
    return completed
