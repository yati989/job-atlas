"""Exact public selection-scope adapter for truthful resume tailoring."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import (
    Job, JobPostingVersion, PublicJobPhaseAEvidence,
    PublicSelectionScope, PublicSelectionScopeItem, PublicTailoredResume,
)
from app.resume.persistence import upsert_tailored_resume
from app.resume.render import render_resume
from app.resume.schema import ResumeMaster
from app.resume.tailor import validate_gap_report


def tailoring_context(session: Session, scope_id: int) -> dict[str, list[dict]]:
    scope = session.get(PublicSelectionScope, scope_id)
    if scope is None:
        raise ValueError(f"unknown public selection scope: {scope_id}")
    if scope.purpose != "selection":
        raise ValueError("tailoring requires a final selection scope")
    rows = list(session.execute(
        select(PublicSelectionScopeItem, JobPostingVersion, Job)
        .join(JobPostingVersion, PublicSelectionScopeItem.posting_version_id == JobPostingVersion.id)
        .join(Job, JobPostingVersion.job_id == Job.id)
        .where(PublicSelectionScopeItem.scope_id == scope_id)
        .order_by(PublicSelectionScopeItem.posting_version_id)
    ))
    items = []
    for scope_item, version, job in rows:
        resume = session.scalar(select(PublicTailoredResume).where(
            PublicTailoredResume.posting_version_id == version.id,
        ))
        artifact_exists = bool(
            resume and resume.artifact_dir
            and (Path(resume.artifact_dir) / "resume.pdf").exists()
        )
        reusable = bool(
            resume
            and resume.material_content_hash == version.material_content_hash
            and artifact_exists
        )
        phase_a = session.scalar(select(PublicJobPhaseAEvidence).where(
            PublicJobPhaseAEvidence.posting_version_id == version.id,
        ))
        if phase_a is None:
            raise ValueError(
                f"posting version {version.id} has no public Phase A evidence"
            )
        snapshot = version.snapshot if isinstance(version.snapshot, dict) else {}
        evidence = phase_a.evidence if isinstance(phase_a.evidence, dict) else {}
        requirements = {
            "job_id": job.id,
            "posting_version_id": version.id,
            "title": snapshot.get("title") or job.title,
            "company": snapshot.get("company_name_raw") or job.company_name_raw,
            "location": snapshot.get("location_raw") or job.location_raw,
            "seniority": snapshot.get("seniority") or job.seniority,
            "employment_type": snapshot.get("employment_type") or job.employment_type,
            "description_raw": snapshot.get("description_raw"),
            "job_url": snapshot.get("job_url") or snapshot.get("apply_url"),
            "experience_min_years": evidence.get("experience_min_years"),
            "experience_max_years": evidence.get("experience_max_years"),
            "education_requirement": evidence.get("education_requirement"),
            "qualification_other": evidence.get("qualification_other"),
            "hard_skills": list(evidence.get("hard_skills") or []),
            "soft_skills": list(evidence.get("soft_skills") or []),
        }
        items.append({
            "scope_id": scope_item.scope_id,
            "posting_version_id": version.id,
            "job_id": job.id,
            "material_content_hash": version.material_content_hash,
            "status": "reused" if reusable else "needs_tailoring",
            "requirements": requirements,
            "artifact_dir": resume.artifact_dir if reusable else None,
        })
    return {
        "items": items,
        "needs_tailoring": [item for item in items if item["status"] == "needs_tailoring"],
        "reused": [item for item in items if item["status"] == "reused"],
    }


def save_public_tailored(
    session: Session,
    *,
    scope_id: int,
    posting_version_id: int,
    tailored,
    score: float,
    gap_report: dict,
    out_root: str | Path,
):
    scope = session.get(PublicSelectionScope, scope_id)
    if scope is None or scope.purpose != "selection":
        raise ValueError("tailoring requires a final selection scope")
    row = session.execute(
        select(PublicSelectionScopeItem, JobPostingVersion)
        .join(JobPostingVersion, PublicSelectionScopeItem.posting_version_id == JobPostingVersion.id)
        .where(
            PublicSelectionScopeItem.scope_id == scope_id,
            PublicSelectionScopeItem.posting_version_id == posting_version_id,
        )
    ).one_or_none()
    if row is None:
        raise ValueError("posting version is outside the exact public selection scope")
    _scope_item, version = row
    snapshot = deepcopy(version.snapshot)
    if not isinstance(snapshot, dict):
        raise ValueError("posting version snapshot must be an object")
    validate_gap_report(gap_report)
    master = tailored if isinstance(tailored, ResumeMaster) else ResumeMaster.model_validate(tailored)
    artifact_dir = Path(out_root) / f"posting_version_{version.id}"
    rendered = render_resume(master, artifact_dir, basename="resume").raise_if_failed()
    resume = session.scalar(select(PublicTailoredResume).where(
        PublicTailoredResume.posting_version_id == version.id,
    ))
    if resume is None:
        resume = PublicTailoredResume(
            posting_version_id=version.id,
            material_content_hash=version.material_content_hash,
            posting_snapshot=snapshot,
        )
        session.add(resume)
    elif (
        resume.material_content_hash != version.material_content_hash
        or resume.posting_snapshot != snapshot
    ):
        raise ValueError("stored public resume has a different immutable posting context")
    resume.score = score
    resume.gap_report = gap_report
    resume.tailored_yaml = master.model_dump()
    resume.artifact_dir = str(artifact_dir)
    session.flush()
    return resume, rendered


def materialize_public_resume_for_outreach(
    session: Session, *, scope_id: int, posting_version_id: int,
):
    """Expose one exact-scope public resume through the existing draft seam.

    Outreach predates the public workflow and reads ``tailored_resumes`` by
    job ID. This adapter validates the immutable public scope and material
    hash before creating that compatibility row, so a draft cannot silently
    attach a resume rendered for a different posting version.
    """
    row = session.execute(
        select(PublicSelectionScopeItem, JobPostingVersion, PublicTailoredResume)
        .join(
            JobPostingVersion,
            PublicSelectionScopeItem.posting_version_id == JobPostingVersion.id,
        )
        .join(
            PublicTailoredResume,
            PublicTailoredResume.posting_version_id == JobPostingVersion.id,
        )
        .where(
            PublicSelectionScopeItem.scope_id == scope_id,
            PublicSelectionScopeItem.posting_version_id == posting_version_id,
        )
    ).one_or_none()
    scope = session.get(PublicSelectionScope, scope_id)
    if scope is None or scope.purpose != "selection":
        raise ValueError("outreach requires a final public selection scope")
    if row is None:
        raise ValueError("selected posting has no completed public tailored resume")
    _item, version, public_resume = row
    if public_resume.material_content_hash != version.material_content_hash:
        raise ValueError("public tailored resume does not match the selected posting material")
    artifact_dir = Path(public_resume.artifact_dir or "")
    if not artifact_dir or not (artifact_dir / "resume.pdf").is_file():
        raise ValueError("public tailored resume PDF is missing")
    return upsert_tailored_resume(
        session,
        job_id=version.job_id,
        score=public_resume.score,
        gap_report=public_resume.gap_report,
        tailored_yaml=public_resume.tailored_yaml,
        artifact_dir=str(artifact_dir),
        jd_hash=version.material_content_hash,
        jd_snapshot=json.dumps(version.snapshot, sort_keys=True),
    )
