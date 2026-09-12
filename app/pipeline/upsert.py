"""
Upsert logic: turn NormalizedJob objects into Company + Job rows.

Design choices for this first milestone:
- Company matching is done by exact (lowercased) name for now. This is
  intentionally simple — a later milestone will add fuzzy/domain-based
  company resolution once we have enough volume to justify it. If the
  lowercased name matches more than one existing row (legacy case-variant
  duplicates), `app.companies.dedup.pick_canonical` resolves which one
  wins — the same rule `scripts/dedup_companies.py` uses to merge them,
  so this path picks the same winner the batch merge would.
- Jobs are deduplicated by (source, external_job_id). Re-running a collector
  updates last_seen_at instead of creating duplicate rows.
"""
from datetime import datetime, timezone
import hashlib
import json
from sqlalchemy.orm import Session
from sqlalchemy import func, select

from app.companies.dedup import pick_canonical
from app.models.orm import Company, Job, JobPostingVersion, JobSkill
from app.models.schemas import NormalizedJob


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bounded_text(value: str | None, *, column) -> str | None:
    """Fit a compact projection to its SQL column; full evidence stays in JSON."""
    if value is None:
        return None
    limit = getattr(column.type, "length", None)
    return value[:limit] if limit is not None else value


def _version_snapshot(job: Job) -> dict:
    return {key: getattr(job, key) for key in (
        "title", "description_raw", "salary_raw", "location_raw", "is_remote",
        "remote_scope", "employment_type", "seniority", "apply_url", "job_url",
    )}


def ensure_posting_version(session: Session, job: Job) -> JobPostingVersion:
    """Reuse an unchanged episode; append on a changed posting date or material.
    This is deliberately called after every upsert, making versions durable even
    for callers that never run the decision pipeline."""
    snapshot = _version_snapshot(job)
    encoded = json.dumps(snapshot, sort_keys=True, default=str, separators=(",", ":"))
    content_hash = hashlib.sha256(encoded.encode()).hexdigest()
    posted = job.posted_at.isoformat() if job.posted_at else "unknown"
    key = hashlib.sha256(f"{job.id}|{posted}|{content_hash}".encode()).hexdigest()
    existing = session.execute(select(JobPostingVersion).where(JobPostingVersion.posting_instance_key == key)).scalar_one_or_none()
    if existing:
        existing.last_seen_at = utcnow()
        return existing
    root_id = job.duplicate_of_job_id or job.id
    version = JobPostingVersion(job_id=job.id, canonical_duplicate_root_id=root_id,
                                posted_at=job.posted_at, material_content_hash=content_hash,
                                posting_instance_key=key, snapshot=snapshot,
                                first_seen_at=utcnow(), last_seen_at=utcnow())
    session.add(version)
    session.flush()
    return version


def get_or_create_company(session: Session, name_raw: str) -> Company:
    clean_name = (name_raw or "Unknown").strip()

    # Case-insensitive match (the "lowercased name" the module docstring
    # promises) — matching only, doesn't change the stored casing of
    # clean_name so existing display names are left alone.
    #
    # Fetch ALL matches, not just one: legacy case-variant duplicates
    # (e.g. "GE Healthcare" / "Ge Healthcare" / "GE HealthCare") predate
    # this case-insensitive match and still exist in the table. A
    # `scalar_one_or_none()` here raised `MultipleResultsFound` on every
    # such name, silently dropping the job mid-upsert (issue #83, measured
    # live: 13 relevant, already-gated jobs lost per run). Deduping the
    # `companies` table fixes this for *future* runs; it can't recover a
    # job that already failed *during* the run that hit the duplicate, so
    # this path must tolerate multiple matches itself.
    stmt = select(Company).where(func.lower(Company.name) == clean_name.lower())
    matches = session.execute(stmt).scalars().all()

    if matches:
        existing = matches[0] if len(matches) == 1 else pick_canonical(list(matches))
        existing.last_seen_at = utcnow()
        return existing

    company = Company(
        name=clean_name,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    session.add(company)
    session.flush()
    return company


def _apply_agent_job_enrichment(
    session: Session, job: Job, item: NormalizedJob,
) -> dict | None:
    """Persist optional facts carried by the combined LinkedIn pre-gate pass."""
    enrichment = item.raw_payload.get("pre_gate_job_enrichment")
    if not isinstance(enrichment, dict) or enrichment.get("status") not in {
        "done", "no_description",
    }:
        return None
    job.experience_min_years = enrichment.get("experience_min_years")
    job.experience_max_years = enrichment.get("experience_max_years")
    job.qualification_other = enrichment.get("qualification_other")
    job.enrichment_status = enrichment["status"]
    enriched_at = enrichment.get("enriched_at")
    job.enriched_at = (
        datetime.fromisoformat(enriched_at) if enriched_at else utcnow()
    )
    job.education_requirement = _bounded_text(
        enrichment.get("education_requirement"),
        column=Job.__table__.c.education_requirement,
    )
    existing = {(row.skill, row.skill_type) for row in job.skills}
    for skill_type, skills in (
        ("hard", enrichment.get("hard_skills") or []),
        ("soft", enrichment.get("soft_skills") or []),
    ):
        bounded_skills = (
            _bounded_text(value.strip(), column=JobSkill.__table__.c.skill)
            for value in skills
            if value.strip()
        )
        for skill in dict.fromkeys(bounded_skills):
            if (skill, skill_type) in existing:
                continue
            session.add(JobSkill(job_id=job.id, skill=skill, skill_type=skill_type))
            existing.add((skill, skill_type))
    session.flush()
    return enrichment


def _record_agent_screening_facts(
    session: Session,
    item: NormalizedJob,
    enrichment: dict | None,
    version: JobPostingVersion,
) -> None:
    if enrichment is None:
        return
    if not any((
        item.salary_raw,
        enrichment.get("salary"),
        enrichment.get("minimum_experience"),
        enrichment.get("maximum_experience"),
    )):
        return
    from app.jobs.screening_facts import record_screening_facts
    record_screening_facts(
        session,
        posting_version_id=version.id,
        salary=enrichment.get("salary"),
        minimum_experience=enrichment.get("minimum_experience"),
        maximum_experience=enrichment.get("maximum_experience"),
    )


def upsert_job(session: Session, item: NormalizedJob) -> Job:
    company = get_or_create_company(session, item.company_name_raw)

    stmt = select(Job).where(
        Job.source == item.source,
        Job.external_job_id == item.external_job_id,
    )
    existing = session.execute(stmt).scalar_one_or_none()

    if existing:
        existing.last_seen_at = utcnow()
        existing.status = "active"
        # Connector rows are current source truth: refresh every mutable field,
        # including explicit nulls where the normalized contract reports them.
        for field in ("title", "description_raw", "location_raw", "is_remote",
                      "remote_scope", "employment_type", "seniority", "salary_raw",
                      "apply_url", "job_url", "posted_at", "raw_payload"):
            setattr(existing, field, getattr(item, field))
        existing.company_id = company.id
        existing.company_name_raw = item.company_name_raw
        enrichment = _apply_agent_job_enrichment(session, existing, item)
        version = ensure_posting_version(session, existing)
        _record_agent_screening_facts(session, item, enrichment, version)
        return existing

    job = Job(
        source=item.source,
        external_job_id=item.external_job_id,
        company_id=company.id,
        company_name_raw=item.company_name_raw,
        title=item.title,
        location_raw=item.location_raw,
        is_remote=item.is_remote,
        remote_scope=item.remote_scope,
        employment_type=item.employment_type,
        seniority=item.seniority,
        description_raw=item.description_raw,
        salary_raw=item.salary_raw,
        apply_url=item.apply_url,
        job_url=item.job_url,
        posted_at=item.posted_at,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
        status="active",
        raw_payload=item.raw_payload,
    )
    session.add(job)
    # Flush immediately (issue #30 fix) rather than leaving this add pending
    # for the caller's eventual session.commit(). SessionLocal is
    # autoflush=False, so without this, SQLAlchemy 2.0's insertmany
    # optimization silently batches every pending add() across an entire
    # per-source loop into one multi-row INSERT at commit time — meaning a
    # single genuinely-duplicate (source, external_job_id) pair (e.g. the
    # same posting appearing in two of a connector's own RSS categories)
    # raises IntegrityError outside the caller's per-item try/except,
    # crashing the whole run instead of being isolated to that one item as
    # this module's docstring promises.
    session.flush()
    enrichment = _apply_agent_job_enrichment(session, job, item)
    version = ensure_posting_version(session, job)
    _record_agent_screening_facts(session, item, enrichment, version)
    return job
