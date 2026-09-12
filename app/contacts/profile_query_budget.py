"""Durable global authorization around the existing contact query plan."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.contacts.agentic_batch import CompanyContext
from app.contacts.search_plan import PlannedQuery, plan_for_company
from app.models.orm import (
    ProfileDiscoveryAuthorization,
    ProfileDiscoveryCall,
    PublicSelectionScope,
    PublicSelectionScopeItem,
)


class ProfileBudgetExceeded(RuntimeError):
    """The approved global provider-call ceiling has been reached."""


@dataclass(frozen=True)
class ProfileQueryPreview:
    company_count: int
    initial_query_count: int
    retry_allowance: int
    planned_query_count: int
    reused_query_count: int
    queries: tuple[PlannedQuery, ...]


@dataclass(frozen=True)
class ProfileQueryReservation:
    call: ProfileDiscoveryCall
    created: bool


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def preview_profile_queries(
    contexts: Sequence[CompanyContext], *, session: Session | None = None,
    run_id: str | None = None,
) -> ProfileQueryPreview:
    all_queries = tuple(
        query
        for context in sorted(contexts, key=lambda item: item.company_id)
        for query in plan_for_company(context)
    )
    if (session is None) != (run_id is None):
        raise ValueError("session and run_id must be supplied together for reuse checks")
    reused_hashes: set[str] = set()
    if session is not None:
        reused_hashes = set(session.scalars(
            select(ProfileDiscoveryCall.query_hash)
            .join(
                ProfileDiscoveryAuthorization,
                ProfileDiscoveryCall.authorization_id == ProfileDiscoveryAuthorization.id,
            )
            .where(
                ProfileDiscoveryAuthorization.run_id == run_id,
                ProfileDiscoveryCall.status == "succeeded",
            )
        ))
    queries = tuple(
        query for query in all_queries
        if _fingerprint(query.as_dict()) not in reused_hashes
    )
    return ProfileQueryPreview(
        company_count=len({context.company_id for context in contexts}),
        initial_query_count=sum(not query.conditional for query in queries),
        retry_allowance=sum(query.conditional for query in queries),
        planned_query_count=len(queries),
        reused_query_count=len(all_queries) - len(queries),
        queries=queries,
    )


def authorize_profile_queries(
    session: Session,
    *,
    run_id: str,
    scope_id: int,
    contexts: Sequence[CompanyContext],
    maximum_calls: int,
) -> ProfileDiscoveryAuthorization:
    """Freeze the exact existing search plan and approved global call cap."""
    scope = session.get(PublicSelectionScope, scope_id)
    if scope is None or scope.run_id != run_id:
        raise ValueError("profile authorization must belong to the selected run scope")
    if scope.purpose != "selection":
        raise ValueError("profile authorization requires a final selection scope")
    scope_companies = set(session.scalars(
        select(PublicSelectionScopeItem.company_id).where(
            PublicSelectionScopeItem.scope_id == scope_id
        )
    ))
    if not {context.company_id for context in contexts} <= scope_companies:
        raise ValueError("profile authorization contains a company outside selection scope")

    preview = preview_profile_queries(contexts, session=session, run_id=run_id)
    if maximum_calls < 0 or maximum_calls > preview.planned_query_count:
        raise ValueError(
            f"maximum_calls must be between 0 and {preview.planned_query_count}"
        )
    query_plan = [query.as_dict() for query in preview.queries]
    fingerprint = _fingerprint(query_plan)
    existing = session.scalar(select(ProfileDiscoveryAuthorization).where(
        ProfileDiscoveryAuthorization.scope_id == scope_id
    ))
    if existing is not None:
        if (
            existing.run_id != run_id
            or existing.plan_fingerprint != fingerprint
            or existing.query_plan != query_plan
            or existing.maximum_calls != maximum_calls
        ):
            raise ValueError("selection scope already has a different profile authorization")
        return existing
    authorization = ProfileDiscoveryAuthorization(
        run_id=run_id,
        scope_id=scope_id,
        plan_fingerprint=fingerprint,
        query_plan=query_plan,
        maximum_calls=maximum_calls,
        retry_allowance=preview.retry_allowance,
        planned_query_count=preview.planned_query_count,
    )
    session.add(authorization)
    session.flush()
    return authorization


def approved_query(
    authorization: ProfileDiscoveryAuthorization,
    *,
    company_id: int,
    search_group: str,
    tier: str,
    attempt: int,
    query_text: str,
) -> PlannedQuery:
    for snapshot in authorization.query_plan:
        if (
            snapshot["company_id"] == company_id
            and snapshot["search_group"] == search_group
            and snapshot["tier"] == tier
            and snapshot["attempt"] == attempt
            and snapshot["query"] == query_text
        ):
            return PlannedQuery(**snapshot)
    raise ValueError("query is outside the approved query plan")


def reserve_profile_query(
    session: Session,
    authorization_id: int,
    query: PlannedQuery,
) -> ProfileQueryReservation:
    """Reserve one call slot. Commit the reservation before provider I/O."""
    authorization = session.get(ProfileDiscoveryAuthorization, authorization_id)
    if authorization is None:
        raise ValueError(f"unknown profile authorization {authorization_id}")
    snapshot = query.as_dict()
    if snapshot not in authorization.query_plan:
        raise ValueError("query is outside the approved query plan")
    query_hash = _fingerprint(snapshot)
    existing = session.scalar(select(ProfileDiscoveryCall).where(
        ProfileDiscoveryCall.authorization_id == authorization_id,
        ProfileDiscoveryCall.query_hash == query_hash,
    ))
    if existing is not None:
        return ProfileQueryReservation(existing, False)
    reserved = session.execute(
        update(ProfileDiscoveryAuthorization)
        .where(
            ProfileDiscoveryAuthorization.id == authorization_id,
            ProfileDiscoveryAuthorization.reserved_call_count
            < ProfileDiscoveryAuthorization.maximum_calls,
        )
        .values(
            reserved_call_count=ProfileDiscoveryAuthorization.reserved_call_count + 1,
        )
    )
    if reserved.rowcount != 1:
        session.expire(authorization, ["reserved_call_count"])
        spent = authorization.reserved_call_count
        raise ProfileBudgetExceeded(
            f"approved profile-search budget exhausted: {spent}/{authorization.maximum_calls}"
        )
    call = ProfileDiscoveryCall(
        authorization_id=authorization_id,
        query_hash=query_hash,
        company_id=query.company_id,
        search_group=query.search_group,
        query_text=query.query,
        status="started",
    )
    session.add(call)
    session.flush()
    return ProfileQueryReservation(call, True)


def complete_profile_query(
    session: Session,
    call_id: int,
    *,
    result_count: int | None = None,
    error: str | None = None,
) -> ProfileDiscoveryCall:
    call = session.get(ProfileDiscoveryCall, call_id)
    if call is None:
        raise ValueError(f"unknown profile discovery call {call_id}")
    if call.status != "started":
        return call
    call.status = "ambiguous" if error else "succeeded"
    call.result_count = result_count
    call.completed_at = datetime.now(timezone.utc)
    authorization = session.get(ProfileDiscoveryAuthorization, call.authorization_id)
    terminal = session.scalar(select(func.count(ProfileDiscoveryCall.id)).where(
        ProfileDiscoveryCall.authorization_id == authorization.id,
        ProfileDiscoveryCall.status.in_(("succeeded", "failed", "ambiguous")),
    )) or 0
    succeeded = session.scalar(select(func.count(ProfileDiscoveryCall.id)).where(
        ProfileDiscoveryCall.authorization_id == authorization.id,
        ProfileDiscoveryCall.status == "succeeded",
    )) or 0
    ambiguous = session.scalar(select(func.count(ProfileDiscoveryCall.id)).where(
        ProfileDiscoveryCall.authorization_id == authorization.id,
        ProfileDiscoveryCall.status == "ambiguous",
    )) or 0
    result_total = session.scalar(select(func.sum(ProfileDiscoveryCall.result_count)).where(
        ProfileDiscoveryCall.authorization_id == authorization.id,
        ProfileDiscoveryCall.status == "succeeded",
    )) or 0
    if succeeded == authorization.planned_query_count:
        stage_status = "completed"
    elif terminal >= authorization.maximum_calls:
        stage_status = "partial"
    else:
        stage_status = "running"
    from app.workflows.public_progress import record_public_stage
    detail = {"maximum_calls": authorization.maximum_calls}
    if stage_status == "partial":
        if ambiguous:
            detail["blocker"] = (
                f"{ambiguous} provider call{'s' if ambiguous != 1 else ''} did not "
                "complete within the approved search limit"
            )
        elif result_total == 0:
            detail["blocker"] = (
                "No usable profile results were returned within the approved "
                f"{authorization.maximum_calls}-call limit"
            )
        else:
            detail["blocker"] = (
                "The approved provider-call limit was reached before optional "
                "profile searches completed"
            )
    record_public_stage(
        session, authorization.run_id, "profile_links", stage_status,
        expected_count=authorization.planned_query_count,
        completed_count=succeeded, failed_count=ambiguous,
        detail=detail,
    )
    session.flush()
    return call
