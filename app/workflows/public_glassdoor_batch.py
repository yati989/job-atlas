"""Run reviewed Glassdoor identities through one resumable Bright Data snapshot."""
from __future__ import annotations

from collections import defaultdict
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.companies.market_profile import calculate_market_profile, save_glassdoor_market_profile
from app.companies.market_profile_batch import (
    BrightDataGlassdoorClient,
    CompanyTarget,
    SourceResolution,
    load_glassdoor_resolution_artifact,
)
from app.companies.market_scraper import _glassdoor
from app.models.orm import (
    Company,
    PublicPhaseBAuthorization,
    PublicPhaseBCall,
    PublicSelectionScope,
)
from app.workflows.public_phase_b import complete_phase_b_call, reserve_phase_b_call
from app.workflows.public_progress import record_public_stage


SessionContext = Callable[[], AbstractContextManager[Session]]


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _summary(
    *, decisions: dict[int, dict], accepted_by_employer: dict[str, list[dict]],
    completed: int, failed: int, checkpoint_path: Path,
) -> dict[str, Any]:
    return {
        "reviewed": len(decisions),
        "accepted_companies": sum(len(group) for group in accepted_by_employer.values()),
        "unresolved_companies": sum(
            decision["status"] != "accepted" for decision in decisions.values()
        ),
        "unique_bright_data_inputs": len(accepted_by_employer),
        "completed": completed,
        "failed": failed,
        "checkpoint": str(checkpoint_path),
    }


def run_reviewed_glassdoor_batch(
    *,
    run_id: str,
    authorization_id: int,
    reviewed_path: Path,
    checkpoint_path: Path,
    session_context: SessionContext,
    client: Any | None = None,
) -> dict[str, Any]:
    """Validate a reviewed artifact, deduplicate employer IDs, and persist results.

    Identity resolution stays agent-owned: the artifact must record the exact
    accepted or unresolved decision for every authorized Glassdoor company.
    Provider I/O begins only after every unique employer ID has a committed
    reservation. A checkpointed snapshot ID is always resumed on restart.
    """
    decisions = load_glassdoor_resolution_artifact(reviewed_path)
    with session_context() as session:
        authorization = session.get(PublicPhaseBAuthorization, authorization_id)
        if authorization is None:
            raise ValueError(f"unknown Phase B authorization {authorization_id}")
        scope = session.get(PublicSelectionScope, authorization.scope_id)
        if scope is None or scope.run_id != run_id:
            raise ValueError("Phase B authorization belongs to a different run")
        plan = {
            int(item["company_id"]): item
            for item in authorization.source_plan
            if item["source"] == "glassdoor"
        }
        if not plan:
            raise ValueError("Phase B authorization has no Glassdoor plan")
        if set(decisions) != set(plan):
            raise ValueError("reviewed Glassdoor artifact must exactly cover the authorized plan")
        companies = {
            company.id: company
            for company in session.scalars(select(Company).where(Company.id.in_(plan)))
        }
        if set(companies) != set(plan):
            raise ValueError("authorized company is missing from the public database")
        for company_id, item in plan.items():
            expected_name = item["company_name"]
            if companies[company_id].name != expected_name:
                raise ValueError(f"company name changed for {company_id}")
            if decisions[company_id].get("company_name") != expected_name:
                raise ValueError(f"reviewed company name changed for {company_id}")
        maximum_calls = authorization.maximum_calls

    accepted_by_employer: dict[str, list[dict]] = defaultdict(list)
    unresolved: list[dict] = []
    for decision in decisions.values():
        if decision["status"] == "accepted":
            accepted_by_employer[str(decision["employer_id"])].append(decision)
        else:
            unresolved.append(decision)
    if len(accepted_by_employer) > maximum_calls:
        raise ValueError(
            "unique reviewed Glassdoor employers exceed the approved provider-call ceiling"
        )

    checkpoint = _read_checkpoint(checkpoint_path)
    artifact_fingerprint = hashlib.sha256(reviewed_path.read_bytes()).hexdigest()
    checkpoint_fingerprint = checkpoint.get("reviewed_artifact_fingerprint")
    if checkpoint_fingerprint not in {None, artifact_fingerprint}:
        raise ValueError("reviewed Glassdoor artifact changed after snapshot checkpointing")
    checkpoint.update({
        "run_id": run_id,
        "authorization_id": authorization_id,
        "reviewed_artifact": str(reviewed_path),
        "reviewed_artifact_fingerprint": artifact_fingerprint,
        "reviewed_company_count": len(decisions),
        "accepted_company_count": sum(len(group) for group in accepted_by_employer.values()),
        "unresolved_company_count": len(unresolved),
        "unique_employer_input_count": len(accepted_by_employer),
    })
    _write_checkpoint(checkpoint_path, checkpoint)
    if checkpoint.get("snapshot_status") == "completed":
        return _summary(
            decisions=decisions, accepted_by_employer=accepted_by_employer,
            completed=int(checkpoint.get("completed_company_count", 0)),
            failed=int(checkpoint.get("failed_company_count", 0)),
            checkpoint_path=checkpoint_path,
        )

    representatives = {
        employer_id: sorted(rows, key=lambda item: item["company_id"])[0]
        for employer_id, rows in accepted_by_employer.items()
    }
    call_by_employer: dict[str, int] = {}
    with session_context() as session:
        for employer_id, rows in accepted_by_employer.items():
            for decision in rows:
                session.get(Company, decision["company_id"]).glassdoor_employer_id = employer_id
        session.commit()
        for employer_id, decision in representatives.items():
            reservation = reserve_phase_b_call(
                session,
                authorization_id=authorization_id,
                company_id=decision["company_id"],
                source="glassdoor",
            )
            session.commit()
            call_by_employer[employer_id] = reservation.call.id
        record_public_stage(
            session, run_id, "phase_b", "running",
            expected_count=len(decisions), completed_count=len(unresolved),
            detail={
                "maximum_calls": maximum_calls,
                "resolved_companies": sum(len(group) for group in accepted_by_employer.values()),
                "unresolved_companies": len(unresolved),
                "unique_bright_data_inputs": len(accepted_by_employer),
            },
        )

    resolutions = [
        SourceResolution(
            target=CompanyTarget(
                decision["company_id"], decision["company_name"], "", 0,
            ),
            source_urls={"glassdoor": {
                "company_name": decision["observed_name"],
                "overview": decision["overview_url"],
                "source_id": employer_id,
                "searched_name": decision.get("searched_name") or decision["company_name"],
                "resolution_pass": decision.get("resolution_pass") or "original",
            }},
            glassdoor_source_id=employer_id,
            errors={},
        )
        for employer_id, decision in representatives.items()
    ]

    def snapshot_callback(snapshot_id: str, input_count: int) -> None:
        latest = _read_checkpoint(checkpoint_path)
        active = latest.get("active_snapshot_id")
        if active not in {None, snapshot_id}:
            raise RuntimeError(f"checkpoint already owns snapshot {active}")
        latest.update({
            "active_snapshot_id": snapshot_id,
            "active_snapshot_input_count": input_count,
            "snapshot_status": "in_progress",
            "next_safe_resume_step": f"resume Bright Data snapshot {snapshot_id}",
        })
        _write_checkpoint(checkpoint_path, latest)

    records = {}
    if resolutions:
        owned_client = client is None
        bright_data = client or BrightDataGlassdoorClient()
        try:
            records = bright_data.collect(
                resolutions,
                snapshot_id=checkpoint.get("active_snapshot_id"),
                snapshot_callback=snapshot_callback,
            )
        finally:
            if owned_client:
                bright_data.close()

    retrieved_at = datetime.now(timezone.utc).isoformat()
    with session_context() as session:
        for decision in unresolved:
            save_glassdoor_market_profile(
                session, decision["company_id"],
                overall_rating=None, wlb_rating=None, review_count=None,
                evidence={
                    "status": "missing",
                    "reason": decision.get("review_note") or decision.get("reason")
                    or "No verified Glassdoor Overview identity found.",
                    "resolution_source": "codex_internal_web_search",
                    "retrieved_at": retrieved_at,
                },
            )
            calculate_market_profile(session, decision["company_id"])

    completed = len(unresolved)
    failed = 0
    for employer_id, group in accepted_by_employer.items():
        representative = representatives[employer_id]
        record = records.get(employer_id)
        if record is None:
            result = {
                "company_id": representative["company_id"], "source": "glassdoor",
                "status": "missing",
                "evidence": {
                    "source_id": employer_id,
                    "requested_url": representative["overview_url"],
                    "reason": "Bright Data snapshot returned no record for the verified employer ID.",
                    "resolution_source": "codex_internal_web_search",
                    "retrieved_at": retrieved_at,
                },
                "evidence_url": representative["overview_url"],
            }
        else:
            try:
                parsed = _glassdoor(
                    record, representative["observed_name"], representative["overview_url"],
                )
                result = {
                    "company_id": representative["company_id"], "source": "glassdoor",
                    "status": "ok",
                    "evidence": {
                        **parsed["evidence"],
                        "requested_url": representative["overview_url"],
                        "resolution_source": "codex_internal_web_search",
                        "retrieved_at": retrieved_at,
                    },
                    "overall_rating": parsed["overall_rating"],
                    "wlb_rating": parsed["wlb_rating"],
                    "review_count": parsed["review_count"],
                    "evidence_url": representative["overview_url"],
                }
            except Exception as exc:
                result = {
                    "company_id": representative["company_id"], "source": "glassdoor",
                    "status": "error",
                    "evidence": {
                        "source_id": employer_id,
                        "requested_url": representative["overview_url"],
                        "error": f"{type(exc).__name__}: {exc}",
                        "resolution_source": "codex_internal_web_search",
                        "retrieved_at": retrieved_at,
                    },
                    "evidence_url": representative["overview_url"],
                }
        with session_context() as session:
            call = session.get(PublicPhaseBCall, call_by_employer[employer_id])
            if call.status == "reserved":
                complete_phase_b_call(session, call_id=call.id, result=result)
            representative_company = session.get(Company, representative["company_id"])
            source = representative_company.market_profile_evidence["sources"]["glassdoor"]
            for alias in group:
                if alias["company_id"] == representative["company_id"]:
                    continue
                save_glassdoor_market_profile(
                    session, alias["company_id"],
                    overall_rating=representative_company.glassdoor_overall_rating,
                    wlb_rating=representative_company.glassdoor_wlb_rating,
                    review_count=representative_company.glassdoor_review_count,
                    evidence={
                        **source,
                        "reused_from_company_id": representative["company_id"],
                        "reused_employer_id": employer_id,
                    },
                )
                calculate_market_profile(session, alias["company_id"])
        if result["status"] in {"ok", "missing"}:
            completed += len(group)
        else:
            failed += len(group)

    with session_context() as session:
        record_public_stage(
            session, run_id, "phase_b", "completed" if failed == 0 else "partial",
            expected_count=len(decisions), completed_count=completed, failed_count=failed,
            detail={
                "maximum_calls": maximum_calls,
                "unresolved_companies": len(unresolved),
                "unique_bright_data_inputs": len(accepted_by_employer),
                "alias_reuses": sum(len(group) - 1 for group in accepted_by_employer.values()),
                "snapshot_id": _read_checkpoint(checkpoint_path).get("active_snapshot_id"),
            },
        )
    checkpoint = _read_checkpoint(checkpoint_path)
    checkpoint.update({
        "snapshot_status": "completed",
        "completed_company_count": completed,
        "failed_company_count": failed,
        "next_safe_resume_step": "review Phase B and freeze the final selection",
    })
    _write_checkpoint(checkpoint_path, checkpoint)
    return _summary(
        decisions=decisions, accepted_by_employer=accepted_by_employer,
        completed=completed, failed=failed, checkpoint_path=checkpoint_path,
    )
