"""
SQLAlchemy ORM models for the job ingestion pipeline.

Core tables:
- Company: canonical employer record.
- Job: normalized job posting record, one row per (source, external_job_id).
- JobSkill: extracted skill child rows of a Job.
- TailoredResume: one current tailored resume per Job (ATS Resume Builder).
- Contact: a person at a Company, found by the contact-finding module.
- Prospect: a company surfaced by research rather than by a scraped job
  posting — deliberately NOT linked to Company (see ADR-0008).
- OutreachDraft: one cold-email draft to one recipient, addressing across
  Contact/Company/Prospect uniformly (see ADR-0010).
"""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey,
    UniqueConstraint, CheckConstraint, JSON,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, index=True)
    canonical_domain = Column(String(255), nullable=True, index=True)
    # Set by the /enrich-companies skill (issue #22). Distinguishes a
    # company we've never attempted (NULL) from one we searched and could
    # not resolve to an MX-verified domain ("unresolvable") — so the latter
    # isn't reselected on every future enrichment batch. "done" pairs with a
    # populated canonical_domain. Domain resolution is a prerequisite for
    # contact-finding's email step, kept separate from contact_enrichment_status.
    domain_resolution_status = Column(String(20), nullable=True, index=True)
    hq_location = Column(String(255), nullable=True)
    sector = Column(String(100), nullable=True)
    remote_hint = Column(Boolean, nullable=True)
    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    industry = Column(String(255), nullable=True)
    employee_count_range = Column(String(50), nullable=True)
    founding_year = Column(Integer, nullable=True)
    glassdoor_employer_id = Column(String(50), nullable=True)
    glassdoor_overall_rating = Column(Float, nullable=True)
    glassdoor_wlb_rating = Column(Float, nullable=True)
    glassdoor_review_count = Column(Integer, nullable=True)
    ambitionbox_overall_rating = Column(Float, nullable=True)
    ambitionbox_wlb_rating = Column(Float, nullable=True)
    ambitionbox_estimated_salary_lpa = Column(Float, nullable=True)
    levels_fyi_estimated_salary_lpa = Column(Float, nullable=True)
    overall_rating = Column(Float, nullable=True)
    description = Column(Text, nullable=True)
    pain_points = Column(Text, nullable=True)
    wlb_rating = Column(Float, nullable=True)
    estimated_salary_lpa = Column(Float, nullable=True)
    market_profile_evidence = Column(JSON(none_as_null=True), nullable=True)
    market_profile_status = Column(
        String(20), default="pending", nullable=False, index=True,
    )
    market_profile_updated_at = Column(DateTime(timezone=True), nullable=True)
    enrichment_status = Column(String(20), default="pending", nullable=False)
    enriched_at = Column(DateTime(timezone=True), nullable=True)

    linkedin_company_url = Column(String(500), nullable=True)
    linkedin_company_id = Column(String(50), nullable=True)
    # Contact-search prerequisites written by enrich-companies. NULL means
    # not classified; [] means classified with no supported target function.
    contact_search_groups = Column(JSON(none_as_null=True), nullable=True)
    contact_profile_enriched_at = Column(DateTime(timezone=True), nullable=True)
    contact_enrichment_status = Column(String(20), default="pending", nullable=False)
    contact_enriched_at = Column(DateTime(timezone=True), nullable=True)
    # Set by enrich-companies through the contact-company profile seam.
    # "employer" (real
    # hiring company) or "staffing" (agency/recruitment consultancy/aggregator
    # with no in-house data function) — contact-finding picks the stored
    # ladder rather than recomputing the classification at query time.
    # "employer" is the conservative default for anything with no signal,
    # since a full ladder wasted on a staffing firm is cheaper than a
    # recruiter-only ladder missing a real employer's whole data team.
    company_type = Column(String(20), default="employer", nullable=False)
    # Glassdoor ownership (private/public) is distinct from ``company_type``,
    # which classifies the posting entity as employer versus staffing firm.
    ownership_type = Column(String(50), nullable=True)
    revenue = Column(String(255), nullable=True)

    # Historical: confirmed email address shapes found via a real leaked
    # address, from a leak-search mechanism dropped per docs/adr/0005's
    # 2026-08-02 addendum (it found nothing for 10 of 10 companies outside
    # the narrow convention it was built against). No longer written by
    # anything; left populated for the companies resolved before the pivot
    # rather than migrated away, since there's no tool to safely undo a drop.
    email_pattern_1 = Column(String(20), nullable=True)
    email_pattern_2 = Column(String(20), nullable=True)
    email_pattern_3 = Column(String(20), nullable=True)

    jobs = relationship("Job", back_populates="company")
    contacts = relationship("Contact", back_populates="company")

    __table_args__ = (
        UniqueConstraint("name", "canonical_domain", name="uq_company_name_domain"),
    )

    def __repr__(self) -> str:
        return f"<Company id={self.id} name={self.name!r}>"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)

    source = Column(String(50), nullable=False, index=True)
    external_job_id = Column(String(255), nullable=False, index=True)

    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    company_name_raw = Column(String(255), nullable=True)

    title = Column(String(500), nullable=False)
    location_raw = Column(String(255), nullable=True)
    is_remote = Column(Boolean, nullable=True)
    remote_scope = Column(String(500), nullable=True)

    employment_type = Column(String(255), nullable=True)
    seniority = Column(String(255), nullable=True)

    description_raw = Column(Text, nullable=True)
    salary_raw = Column(String(255), nullable=True)
    apply_url = Column(Text, nullable=True)
    job_url = Column(Text, nullable=True)

    posted_at = Column(DateTime(timezone=True), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    status = Column(String(50), default="active", nullable=False)
    raw_payload = Column(JSON, nullable=True)

    experience_min_years = Column(Integer, nullable=True)
    experience_max_years = Column(Integer, nullable=True)
    education_requirement = Column(String(255), nullable=True)
    qualification_other = Column(Text, nullable=True)
    enrichment_status = Column(String(20), default="pending", nullable=False)
    enriched_at = Column(DateTime(timezone=True), nullable=True)

    # Cross-source duplicate detection (scripts/dedup_jobs.py): the same
    # real-world posting can appear under a different (source,
    # external_job_id) on another job board, so it can't be collapsed via
    # the unique constraint below. Instead a later-first-seen duplicate
    # gets flagged pointing at the earliest-seen canonical row, rather than
    # merged/deleted — downstream consumers filter
    # duplicate_of_job_id IS NULL for the canonical set.
    duplicate_of_job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    duplicate_score = Column(Float, nullable=True)

    company = relationship("Company", back_populates="jobs")
    skills = relationship("JobSkill", back_populates="job")
    duplicate_of = relationship("Job", remote_side=[id])
    posting_versions = relationship("JobPostingVersion", back_populates="job", foreign_keys="JobPostingVersion.job_id")

    __table_args__ = (
        UniqueConstraint("source", "external_job_id", name="uq_job_source_external_id"),
    )

    def __repr__(self) -> str:
        return f"<Job id={self.id} source={self.source} title={self.title!r}>"


class RelevanceAuditRun(Base):
    """One opted-in collection whose rejected observations are retained."""

    __tablename__ = "relevance_audit_runs"

    id = Column(String(64), primary_key=True)
    since_at = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    command = Column(String(255), nullable=False)
    state = Column(String(30), nullable=False, default="collecting", index=True)
    instance_count = Column(Integer, nullable=False)
    drop_count = Column(Integer, nullable=False, default=0)
    failure_detail = Column(Text, nullable=True)


class RelevanceAuditObservation(Base):
    """A raw job occurrence rejected by the central relevance gate.

    There is deliberately no uniqueness constraint: the same source job may
    be returned by more than one search instance, and that provenance is part
    of an audit's evidence.
    """

    __tablename__ = "relevance_audit_observations"

    id = Column(Integer, primary_key=True)
    audit_run_id = Column(
        String(64), ForeignKey("relevance_audit_runs.id"), nullable=False, index=True
    )
    source = Column(String(50), nullable=False, index=True)
    external_job_id = Column(String(255), nullable=False, index=True)
    first_failed_axis = Column(String(30), nullable=False, index=True)
    location_reason = Column(String(30), nullable=True)
    dimensions = Column(JSON, nullable=False)
    job_snapshot = Column(JSON, nullable=False)
    observed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class JobPostingVersion(Base):
    """An immutable posting episode.  Jobs remain the connector dedup row;
    versions preserve the facts that a user applied to or ranked."""
    __tablename__ = "job_posting_versions"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    canonical_duplicate_root_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    posted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    material_content_hash = Column(String(64), nullable=False)
    posting_instance_key = Column(String(128), nullable=False, unique=True)
    snapshot = Column(JSON, nullable=False)
    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    job = relationship("Job", foreign_keys=[job_id], back_populates="posting_versions")


class JobApplication(Base):
    __tablename__ = "job_applications"
    id = Column(Integer, primary_key=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=False, unique=True, index=True)
    canonical_duplicate_root_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    applied_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    source = Column(String(100), nullable=True)
    job_url_snapshot = Column(String(1000), nullable=True)
    tailored_resume_id = Column(Integer, ForeignKey("tailored_resumes.id"), nullable=True)
    notes = Column(Text, nullable=True)


class JobScreeningFact(Base):
    __tablename__ = "job_screening_facts"
    id = Column(Integer, primary_key=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=False, index=True)
    extraction_version = Column(String(50), nullable=False, default="1")
    salary = Column(JSON, nullable=True)
    minimum_experience = Column(JSON, nullable=True)
    maximum_experience = Column(JSON, nullable=True)
    extracted_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    evidence_hash = Column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("posting_version_id", "extraction_version", name="uq_screening_fact_version"),)


class PublicJobPhaseAEvidence(Base):
    """Version-bound public requirement evidence produced before selection."""
    __tablename__ = "public_job_phase_a_evidence"
    id = Column(Integer, primary_key=True)
    posting_version_id = Column(
        Integer, ForeignKey("job_posting_versions.id"), nullable=False,
        unique=True, index=True,
    )
    status = Column(String(20), nullable=False)
    evidence = Column(JSON, nullable=False)
    recorded_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "status IN ('done','no_description')",
            name="ck_public_job_phase_a_status",
        ),
    )


class PublicPhaseBAuthorization(Base):
    """Frozen optional market-evidence plan for one exact selection scope."""
    __tablename__ = "public_phase_b_authorizations"

    id = Column(Integer, primary_key=True)
    scope_id = Column(
        Integer, ForeignKey("public_selection_scopes.id"), nullable=False, unique=True, index=True,
    )
    plan_fingerprint = Column(String(64), nullable=False)
    source_plan = Column(JSON, nullable=False)
    maximum_calls = Column(Integer, nullable=False)
    planned_call_count = Column(Integer, nullable=False)
    # Incremented by a conditional UPDATE before a provider call is reserved.
    # Counting child rows is racy on SQLite, where FOR UPDATE is ignored.
    reserved_call_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PublicPhaseBCall(Base):
    """A reserved provider call; every terminal result consumes its slot."""
    __tablename__ = "public_phase_b_calls"

    id = Column(Integer, primary_key=True)
    authorization_id = Column(
        Integer, ForeignKey("public_phase_b_authorizations.id"), nullable=False, index=True,
    )
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    source = Column(String(30), nullable=False)
    status = Column(String(20), nullable=False, default="reserved")
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "authorization_id", "company_id", "source", name="uq_public_phase_b_call",
        ),
        CheckConstraint(
            "status IN ('reserved','succeeded','missing','failed','ambiguous')",
            name="ck_public_phase_b_call_status",
        ),
    )


class PublicPhaseBEvidence(Base):
    """Reviewable, source-specific terminal evidence from a public Phase B call."""
    __tablename__ = "public_phase_b_evidence"

    id = Column(Integer, primary_key=True)
    call_id = Column(
        Integer, ForeignKey("public_phase_b_calls.id"), nullable=False, unique=True, index=True,
    )
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    source = Column(String(30), nullable=False)
    status = Column(String(20), nullable=False)
    evidence = Column(JSON, nullable=False)
    recorded_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class DecisionRun(Base):
    __tablename__ = "decision_runs"
    id = Column(String(64), primary_key=True)
    since_at = Column(DateTime(timezone=True), nullable=False)
    cutoff_at = Column(DateTime(timezone=True), nullable=False)
    input_timezone = Column(String(100), nullable=False)
    state = Column(String(30), nullable=False, default="preparing", index=True)
    policy_snapshot = Column(JSON, nullable=False)
    target_count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    prepared_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    failure_detail = Column(Text, nullable=True)
    # NULL identifies a genuine pre-instrumentation run. Current attended
    # runs set this explicitly when durable funnel scope is first recorded.
    telemetry_version = Column(String(40), nullable=True, index=True)
    final_report_path = Column(String(1000), nullable=True)
    final_report_delivery_requested = Column(Boolean, nullable=False, default=False)
    final_report_delivery_receipt = Column(String(255), nullable=True)
    final_report_delivery_state = Column(String(30), nullable=True)


class DecisionRunStage(Base):
    """Current durable state for one named stage of a decision run."""
    __tablename__ = "decision_run_stages"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    status = Column(String(30), nullable=False, default="pending", index=True)
    expected_count = Column(Integer, nullable=False)
    processed_count = Column(Integer, nullable=False, default=0)
    advanced_count = Column(Integer, nullable=False, default=0)
    dropped_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    pending_count = Column(Integer, nullable=False, default=0)
    reason_counts = Column(JSON, nullable=False, default=dict)
    reason = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    attempt_number = Column(Integer, nullable=False, default=0)
    waiting_started_at = Column(DateTime(timezone=True), nullable=True)
    waiting_seconds = Column(Integer, nullable=False, default=0)
    selection_counts = Column(JSON, nullable=False, default=dict)
    failed_record_dispositions = Column(JSON, nullable=False, default=list)
    __table_args__ = (
        UniqueConstraint("run_id", "name", name="uq_decision_run_stage"),
        CheckConstraint(
            "status IN ('pending','running','waiting_for_approval','completed','completed_with_errors','failed','interrupted','skipped')",
            name="ck_decision_run_stage_status",
        ),
        CheckConstraint(
            "expected_count = advanced_count + dropped_count + failed_count + pending_count "
            "AND processed_count = advanced_count + dropped_count + failed_count",
            name="ck_decision_run_stage_counts",
        ),
    )


class DecisionRunStageAttempt(Base):
    __tablename__ = "decision_run_stage_attempts"
    id = Column(Integer, primary_key=True)
    stage_id = Column(Integer, ForeignKey("decision_run_stages.id"), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False)
    status = Column(String(30), nullable=False)
    process_id = Column(Integer, nullable=True)
    expected_count = Column(Integer, nullable=False)
    processed_count = Column(Integer, nullable=False, default=0)
    advanced_count = Column(Integer, nullable=False, default=0)
    dropped_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    pending_count = Column(Integer, nullable=False, default=0)
    reason_counts = Column(JSON, nullable=False, default=dict)
    reason = Column(Text, nullable=True)
    failed_record_dispositions = Column(JSON, nullable=False, default=list)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    heartbeat_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("stage_id", "attempt_number", name="uq_stage_attempt_number"),
        CheckConstraint(
            "status IN ('running','waiting_for_approval','completed','completed_with_errors','failed','interrupted','skipped')",
            name="ck_decision_run_stage_attempt_status",
        ),
        CheckConstraint(
            "expected_count = advanced_count + dropped_count + failed_count + pending_count "
            "AND processed_count = advanced_count + dropped_count + failed_count",
            name="ck_decision_run_stage_attempt_counts",
        ),
    )


class DecisionRunStageTransition(Base):
    __tablename__ = "decision_run_stage_transitions"
    id = Column(Integer, primary_key=True)
    stage_id = Column(Integer, ForeignKey("decision_run_stages.id"), nullable=False, index=True)
    attempt_id = Column(Integer, ForeignKey("decision_run_stage_attempts.id"), nullable=True, index=True)
    from_status = Column(String(30), nullable=True)
    to_status = Column(String(30), nullable=False)
    reason = Column(Text, nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class DecisionRunStageRecord(Base):
    """Identifier-only drill-down evidence for one stage attempt outcome."""
    __tablename__ = "decision_run_stage_records"
    id = Column(Integer, primary_key=True)
    stage_id = Column(Integer, ForeignKey("decision_run_stages.id"), nullable=False, index=True)
    attempt_id = Column(Integer, ForeignKey("decision_run_stage_attempts.id"), nullable=False, index=True)
    record_type = Column(String(40), nullable=False)
    record_id = Column(String(255), nullable=False)
    outcome = Column(String(20), nullable=False)
    reason = Column(String(100), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "record_type", "record_id",
            name="uq_stage_attempt_record",
        ),
        CheckConstraint(
            "outcome IN ('advanced','dropped','failed')",
            name="ck_decision_run_stage_record_outcome",
        ),
    )


class DecisionRunIngestionObservation(Base):
    """Immutable provenance for one raw job occurrence in a Decision Run."""
    __tablename__ = "decision_run_ingestion_observations"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)
    external_job_id = Column(String(255), nullable=False)
    connector_instance = Column(Integer, nullable=False)
    connector_dimensions = Column(JSON, nullable=False, default=dict)
    fetch_attempt = Column(Integer, nullable=False)
    occurrence_ordinal = Column(Integer, nullable=False)
    job_url = Column(Text, nullable=True)
    apply_url = Column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "run_id", "connector_instance", "fetch_attempt", "occurrence_ordinal",
            name="uq_run_ingestion_observation",
        ),
    )


class ProfiledSearchOutcome(Base):
    """Durable relevance result for one deduplicated posting in a run.

    Rejected and ambiguous observations retain enough evidence to explain the
    decision. Experience-unknown observations temporarily retain their job
    snapshot so Phase A can resolve that one evidence-dependent decision.
    The run/source/external identity makes retries idempotent without
    collapsing evidence across runs.
    """
    __tablename__ = "profiled_search_outcomes"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)
    external_job_id = Column(String(255), nullable=False)
    outcome = Column(String(20), nullable=False, index=True)
    first_failed_axis = Column(String(30), nullable=True)
    reason = Column(Text, nullable=True)
    profile_fingerprint = Column(String(64), nullable=False, index=True)
    job_url = Column(Text, nullable=True)
    apply_url = Column(Text, nullable=True)
    evidence = Column(JSON, nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    posting_version_id = Column(
        Integer, ForeignKey("job_posting_versions.id"), nullable=True, index=True,
    )
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "run_id", "source", "external_job_id",
            name="uq_profiled_search_outcome_run_job",
        ),
        CheckConstraint(
            "outcome IN ('kept','rejected','needs_review')",
            name="ck_profiled_search_outcome",
        ),
    )


class PublicRoleReviewCandidate(Base):
    """Private normalized posting held until agent-owned role judgment."""
    __tablename__ = "public_role_review_candidates"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)
    external_job_id = Column(String(255), nullable=False)
    profile_fingerprint = Column(String(64), nullable=False, index=True)
    candidate_snapshot = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "run_id", "source", "external_job_id",
            name="uq_public_role_review_candidate_run_job",
        ),
        CheckConstraint(
            "status IN ('pending','uncertain','relevant','irrelevant')",
            name="ck_public_role_review_candidate_status",
        ),
    )


class GuidedRunPlan(Base):
    """Exact accepted profile/source plan attached immutably to one run."""
    __tablename__ = "guided_run_plans"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, unique=True)
    profile_fingerprint = Column(String(64), nullable=False)
    profile_snapshot = Column(JSON, nullable=False)
    selected_sources = Column(JSON, nullable=False)
    source_plan = Column(JSON, nullable=False)
    source_count = Column(Integer, nullable=False)
    query_instance_count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class GuidedSourceRun(Base):
    """Current resumable source state for a guided run."""
    __tablename__ = "guided_source_runs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    attempt_count = Column(Integer, nullable=False, default=0)
    outcome_counts = Column(JSON, nullable=False, default=dict)
    failure_detail = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "source", name="uq_guided_source_run"),
        CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_guided_source_run_status",
        ),
    )


class GuidedSourceAttempt(Base):
    """Historical attempt evidence retained when a source later recovers."""
    __tablename__ = "guided_source_attempts"
    id = Column(Integer, primary_key=True)
    source_run_id = Column(Integer, ForeignKey("guided_source_runs.id"), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False)
    failure_detail = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("source_run_id", "attempt_number", name="uq_guided_source_attempt"),
    )


class PublicWorkflowStage(Base):
    """Durable post-collection state for one public guided stage."""
    __tablename__ = "public_workflow_stages"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    name = Column(String(50), nullable=False)
    status = Column(String(30), nullable=False)
    expected_count = Column(Integer, nullable=False, default=0)
    completed_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    detail = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("run_id", "name", name="uq_public_workflow_stage"),
        CheckConstraint(
            "status IN ('not_requested','awaiting_confirmation','running','partial','failed','completed','skipped')",
            name="ck_public_workflow_stage_status",
        ),
    )


class PublicSelectionScope(Base):
    """One immutable research or final downstream posting-version scope."""
    __tablename__ = "public_selection_scopes"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    mode = Column(String(30), nullable=False)
    # Phase B may need a broad, immutable denominator before the user confirms
    # the final shortlist.  Only ``selection`` scopes authorize final-only work.
    purpose = Column(String(30), nullable=False, default="selection")
    filter_snapshot = Column(JSON, nullable=True)
    fingerprint = Column(String(64), nullable=False)
    job_count = Column(Integer, nullable=False)
    company_count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("run_id", "revision", name="uq_public_scope_revision"),
        UniqueConstraint("run_id", "fingerprint", name="uq_public_scope_fingerprint"),
        CheckConstraint(
            "mode IN ('all_eligible','manual','filtered')",
            name="ck_public_scope_mode",
        ),
        CheckConstraint(
            "purpose IN ('selection','phase_b_research')",
            name="ck_public_scope_purpose",
        ),
    )


class PublicSelectionScopeItem(Base):
    __tablename__ = "public_selection_scope_items"
    id = Column(Integer, primary_key=True)
    scope_id = Column(Integer, ForeignKey("public_selection_scopes.id"), nullable=False, index=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    __table_args__ = (
        UniqueConstraint("scope_id", "posting_version_id", name="uq_public_scope_item"),
    )


class ProfileDiscoveryAuthorization(Base):
    """Approved immutable query plan and call ceiling for one scope."""
    __tablename__ = "profile_discovery_authorizations"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    scope_id = Column(Integer, ForeignKey("public_selection_scopes.id"), nullable=False, unique=True)
    plan_fingerprint = Column(String(64), nullable=False)
    query_plan = Column(JSON, nullable=False, default=list)
    maximum_calls = Column(Integer, nullable=False)
    retry_allowance = Column(Integer, nullable=False)
    planned_query_count = Column(Integer, nullable=False)
    # See PublicPhaseBAuthorization.reserved_call_count.
    reserved_call_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProfileDiscoveryCall(Base):
    """Durable provider-call accounting; ambiguous calls still consume budget."""
    __tablename__ = "profile_discovery_calls"
    id = Column(Integer, primary_key=True)
    authorization_id = Column(
        Integer, ForeignKey("profile_discovery_authorizations.id"), nullable=False, index=True,
    )
    query_hash = Column(String(64), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    search_group = Column(String(100), nullable=False)
    query_text = Column(Text, nullable=False)
    status = Column(String(20), nullable=False)
    result_count = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("authorization_id", "query_hash", name="uq_profile_discovery_call"),
        CheckConstraint(
            "status IN ('started','succeeded','failed','ambiguous')",
            name="ck_profile_discovery_call_status",
        ),
    )


class LinkedInProfileLink(Base):
    """Minimal links-only result; intentionally has no email or raw payload."""
    __tablename__ = "linkedin_profile_links"
    id = Column(Integer, primary_key=True)
    authorization_id = Column(
        Integer, ForeignKey("profile_discovery_authorizations.id"), nullable=False, index=True,
    )
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    linkedin_url = Column(String(1000), nullable=False)
    full_name = Column(String(255), nullable=True)
    headline = Column(String(1000), nullable=True)
    search_group = Column(String(100), nullable=False)
    evidence = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "authorization_id", "company_id", "linkedin_url",
            name="uq_profile_discovery_link",
        ),
    )


class DecisionRunCanonicalJob(Base):
    """Exact posting version selected when canonical ingestion scope closes."""
    __tablename__ = "decision_run_canonical_jobs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    ingestion_observation_id = Column(
        Integer, ForeignKey("decision_run_ingestion_observations.id"), nullable=False,
    )
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=False)
    __table_args__ = (
        UniqueConstraint("run_id", "job_id", name="uq_run_canonical_job"),
        UniqueConstraint("run_id", "posting_version_id", name="uq_run_canonical_version"),
    )


class DecisionRunJobEnrichmentManifest(Base):
    """Immutable posting-version scope owned by one Decision Run enrichment pass."""
    __tablename__ = "decision_run_job_enrichment_manifest"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    outcome = Column(String(20), nullable=True)
    reason = Column(String(100), nullable=True)
    failure_disposition = Column(String(20), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "posting_version_id", name="uq_run_job_enrichment_version"),
    )


class DecisionRunJob(Base):
    __tablename__ = "decision_run_jobs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    outcome = Column(String(30), nullable=False)
    snapshot = Column(JSON, nullable=False)
    explicit_salary_lpa = Column(Float, nullable=True)
    ambitionbox_salary_lpa = Column(Float, nullable=True)
    effective_salary_lpa = Column(Float, nullable=True)
    salary_source = Column(String(30), nullable=True)
    role_family = Column(String(40), nullable=True)
    work_mode = Column(String(20), nullable=True)
    qualified_wlb = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=True)
    group_number = Column(Integer, nullable=True)
    within_company_rank = Column(Integer, nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("run_id", "posting_version_id", name="uq_run_posting_version"),)


class DecisionRunFinding(Base):
    __tablename__ = "decision_run_findings"
    id = Column(Integer, primary_key=True)
    decision_run_job_id = Column(Integer, ForeignKey("decision_run_jobs.id"), nullable=False, index=True)
    kind = Column(String(30), nullable=False)
    reason_code = Column(String(80), nullable=False)
    evidence = Column(Text, nullable=True)
    parsed_value = Column(String(255), nullable=True)
    threshold = Column(String(255), nullable=True)
    comparison = Column(String(255), nullable=True)
    details = Column(JSON, nullable=True)


class DecisionRunCompany(Base):
    __tablename__ = "decision_run_companies"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    group_number = Column(Integer, nullable=True)
    rank = Column(Integer, nullable=True)
    primary_run_job_id = Column(Integer, ForeignKey("decision_run_jobs.id"), nullable=True)
    company_type = Column(String(20), nullable=False)
    outreach_success_count = Column(Integer, nullable=False, default=0)
    mode = Column(String(40), nullable=False, default="ranked")
    __table_args__ = (UniqueConstraint("run_id", "company_id", name="uq_run_company"), UniqueConstraint("run_id", "group_number", "rank", name="uq_run_group_rank"))


class DecisionRunCompanyFunnelManifest(Base):
    """Immutable company scope and outcomes for one Decision Run funnel.

    The row is created from eligible DecisionRunJob rows only.  It deliberately
    stores no copied company payload: the dashboard resolves names and source
    links from the normal decision-run/job records.
    """
    __tablename__ = "decision_run_company_funnel_manifest"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    eligible_job_count = Column(Integer, nullable=False)
    phase_a_outcome = Column(String(20), nullable=True)
    phase_a_reason = Column(String(100), nullable=True)
    phase_a_failure_disposition = Column(String(20), nullable=True)
    phase_b_outcome = Column(String(20), nullable=True)
    phase_b_reason = Column(String(100), nullable=True)
    phase_b_failure_disposition = Column(String(20), nullable=True)
    phase_b_evidence_outcome = Column(String(30), nullable=True)
    terminal_outcome = Column(String(20), nullable=True)
    terminal_reason = Column(String(100), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "company_id", name="uq_run_company_funnel_company"),
    )


class DecisionRunCompanyPhaseBEvidence(Base):
    """One terminal Glassdoor/AmbitionBox observation for a funnel company."""
    __tablename__ = "decision_run_company_phase_b_evidence"
    id = Column(Integer, primary_key=True)
    manifest_id = Column(Integer, ForeignKey("decision_run_company_funnel_manifest.id"), nullable=False, index=True)
    source = Column(String(30), nullable=False)
    outcome = Column(String(30), nullable=False)
    reason = Column(String(100), nullable=True)
    failure_disposition = Column(String(20), nullable=True)
    evidence_url = Column(String(1000), nullable=True)
    source_status = Column(String(100), nullable=True)
    __table_args__ = (
        UniqueConstraint("manifest_id", "source", name="uq_run_company_phase_b_source"),
    )


class DecisionRunApproval(Base):
    __tablename__ = "decision_run_approvals"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, unique=True)
    original_text = Column(Text, nullable=False)
    normalized_rules = Column(JSON, nullable=False)
    target_count = Column(Integer, nullable=False)
    approver = Column(String(255), nullable=True)
    approved_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class DecisionRunSelectedJob(Base):
    __tablename__ = "decision_run_selected_jobs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=False)
    company_order = Column(Integer, nullable=False)
    selection_kind = Column(String(30), nullable=False)
    __table_args__ = (UniqueConstraint("run_id", "posting_version_id", name="uq_run_selected_version"),)


class DecisionRunResumeTailoringManifest(Base):
    """Immutable approved posting-version scope and tailoring terminal state."""
    __tablename__ = "decision_run_resume_tailoring_manifest"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    outcome = Column(String(20), nullable=True)
    reason = Column(String(100), nullable=True)
    failure_disposition = Column(String(20), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "posting_version_id", name="uq_run_resume_tailoring_version"),
    )


class DecisionRunContactEnrichmentManifest(Base):
    """One approved company/function contact-search obligation.

    ``search_group`` may be ``none`` (explicit approver opt-out),
    ``no_category_match`` (derived terminal no-match), or
    ``staffing_recruiter``. Keeping those distinct prevents an absent derived
    function from being reported as a human exclusion.
    """
    __tablename__ = "decision_run_contact_enrichment_manifest"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    search_group = Column(String(40), nullable=False)
    outcome = Column(String(30), nullable=True)
    reason = Column(String(100), nullable=True)
    failure_disposition = Column(String(20), nullable=True)
    reused_contact_ids = Column(JSON, nullable=False, default=list)
    new_contact_ids = Column(JSON, nullable=False, default=list)
    coverage = Column(JSON, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint("run_id", "company_id", "search_group", name="uq_run_contact_manifest_function"),
    )


class DecisionRunDraftPreparationManifest(Base):
    """Frozen recipient/pairing denominator for the approved draft stage."""
    __tablename__ = "decision_run_draft_preparation_manifest"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False, index=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    candidate_job_ids = Column(JSON, nullable=False, default=list)
    resume_kind = Column(String(20), nullable=False)
    pairing_reason = Column(String(80), nullable=False)
    outcome = Column(String(20), nullable=True)
    reason = Column(String(100), nullable=True)
    failure_disposition = Column(String(20), nullable=True)
    outreach_draft_id = Column(Integer, ForeignKey("outreach_drafts.id"), nullable=True)
    __table_args__ = (UniqueConstraint("run_id", "contact_id", name="uq_run_draft_manifest_contact"),)


class DecisionRunGmailDraftManifest(Base):
    """Frozen Gmail-Draft posting obligations, separate from copy preparation."""
    __tablename__ = "decision_run_gmail_draft_manifest"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    draft_preparation_manifest_id = Column(Integer, ForeignKey("decision_run_draft_preparation_manifest.id"), nullable=False)
    outreach_draft_id = Column(Integer, ForeignKey("outreach_drafts.id"), nullable=False)
    delivery_attempt_id = Column(Integer, ForeignKey("outreach_delivery_attempts.id"), nullable=False)
    outcome = Column(String(20), nullable=True)
    reason = Column(String(100), nullable=True)
    failure_disposition = Column(String(20), nullable=True)
    # Stable recipient-manifest identity keeps its history while the active
    # attempt can move from calibration sequence 1 to a released retry.
    attempt_evidence = Column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint("run_id", "draft_preparation_manifest_id", name="uq_run_gmail_manifest_draft"),)


class JobSkill(Base):
    __tablename__ = "job_skills"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    skill = Column(String(100), nullable=False)
    skill_type = Column(String(10), nullable=False)

    job = relationship("Job", back_populates="skills")

    __table_args__ = (
        UniqueConstraint("job_id", "skill", "skill_type", name="uq_job_skill"),
    )

    def __repr__(self) -> str:
        return f"<JobSkill job_id={self.job_id} skill={self.skill!r} type={self.skill_type}>"


class TailoredResume(Base):
    """One current tailored resume produced by the ATS Resume Builder.

    For a DB-sourced job, `job_id` points at the `jobs` row it was tailored
    for, and there is exactly one current tailored resume per job (re-running
    updates the row in place — see `app.resume.persistence.upsert_tailored_resume`).
    Ad-hoc runs (a JD not in the DB) are not persisted here; if ever inserted,
    `job_id` is NULL. Heavy binaries (the compiled PDF) live on disk under
    `artifact_dir`; the queryable/regenerable parts live in the row.
    """

    __tablename__ = "tailored_resumes"

    id = Column(Integer, primary_key=True)

    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, unique=True, index=True)

    score = Column(Float, nullable=True)
    gap_report = Column(JSON, nullable=True)      # covered + surfaceable/real gaps
    tailored_yaml = Column(JSON, nullable=True)   # the tailored ResumeMaster content, re-renderable
    artifact_dir = Column(String(1000), nullable=True)  # path to .tex / .pdf on disk

    jd_hash = Column(String(64), nullable=True, index=True)  # detect re-runs of the same JD
    jd_snapshot = Column(Text, nullable=True)     # self-contained JD text (esp. for ad-hoc)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    job = relationship("Job")

    def __repr__(self) -> str:
        return f"<TailoredResume id={self.id} job_id={self.job_id} score={self.score}>"


class PublicTailoredResume(Base):
    """A public-flow resume bound to one immutable posting version.

    This deliberately does not share ``tailored_resumes``: its legacy key is
    the mutable ``jobs`` row, while public reuse must be safe for the exact
    frozen posting snapshot the user selected.
    """

    __tablename__ = "public_tailored_resumes"

    id = Column(Integer, primary_key=True)
    posting_version_id = Column(
        Integer, ForeignKey("job_posting_versions.id"), nullable=False, unique=True, index=True,
    )
    material_content_hash = Column(String(64), nullable=False, index=True)
    posting_snapshot = Column(JSON, nullable=False)
    score = Column(Float, nullable=True)
    gap_report = Column(JSON, nullable=True)
    tailored_yaml = Column(JSON, nullable=True)
    artifact_dir = Column(String(1000), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    posting_version = relationship("JobPostingVersion")


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)

    full_name = Column(String(255), nullable=False)
    # Parsed from full_name at persist time (email_resolution.split_name) —
    # the durable input to email derivation. Kept separate from full_name
    # itself (which can carry a middle name/initial split_name discards) so
    # a future re-derivation always uses exactly what candidate_addresses()
    # actually built the stored guess from. NULL when full_name doesn't
    # split into first/last (e.g. a mononym) — the same case email_guess is
    # already NULL for.
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    title = Column(String(500), nullable=True)
    linkedin_url = Column(String(1000), nullable=False)
    seniority_tier = Column(String(50), nullable=True)

    email_guess = Column(String(255), nullable=True)
    email_verification_status = Column(String(20), nullable=True)

    # True when the judging search turned up more than one LinkedIn profile
    # with this contact's exact full name at this company — a same-name
    # collision means `email_guess` (a pattern derived from the name alone)
    # could land in a stranger's inbox instead of this contact's. Unlike a
    # wrong pattern guess, a same-name collision doesn't bounce, so it can't
    # be caught after the fact; it has to be flagged before outreach.
    name_collision_risk = Column(Boolean, nullable=False, default=False)

    source = Column(String(50), default="linkedin_public_search", nullable=False)
    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    company = relationship("Company", back_populates="contacts")

    __table_args__ = (
        UniqueConstraint("company_id", "linkedin_url", name="uq_contact_company_linkedin_url"),
    )

    def __repr__(self) -> str:
        return f"<Contact id={self.id} company_id={self.company_id} full_name={self.full_name!r}>"


class Prospect(Base):
    """A company surfaced by research as worth cold-emailing, rather than by
    a scraped job posting.

    Deliberately has NO foreign key to Company (ADR-0008). Every Company row
    exists because `upsert_job()` created it, so `companies` means "we saw a
    job here"; `prospects` means "research says this is worth approaching".
    The two are answered against each other by a JOIN on `normalized_name`
    when that question comes up, not by a link maintained on every write.

    Note what that join does and does not tell you: it answers "is this
    company already in our DB", NOT "does this company have a live opening on
    its own careers page". Only the first is in scope here.
    """
    __tablename__ = "prospects"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, index=True)
    # app.companies.naming.normalize(name) — the dedup key, and the join key
    # against `companies`. Unique: a company is a prospect at most once, no
    # matter how many theses surface it.
    normalized_name = Column(String(255), nullable=False, index=True)

    canonical_domain = Column(String(255), nullable=True, index=True)
    website = Column(String(500), nullable=True)
    linkedin_company_url = Column(String(500), nullable=True)

    # Provenance: which thesis produced this row, and the exact query + page
    # it came off. Stored so "have we already mined this directory?" is a
    # query rather than something the agent has to remember between sessions.
    thesis = Column(String(100), nullable=False, index=True)
    source_query = Column(Text, nullable=True)
    source_url = Column(String(1000), nullable=True)

    # candidate  — harvested, not yet qualified
    # qualified  — passed the function gate and the India/remote gate
    # unqualified— failed a gate, or ran out of search budget (see
    #              unqualified_reason). NEVER deleted: free search produces
    #              real false negatives, and a stored negative is both
    #              re-checkable later and protection against re-researching
    #              the same dead end. Mirrors Company.domain_resolution_status.
    # duplicate  — collapsed into another prospect row
    status = Column(String(20), nullable=False, default="candidate", index=True)

    # 1 = remote-first AND matching industry; 2 = Bengaluru + matching
    # industry, or remote-first with a real data org; 3 = India-present,
    # other industry, has a data org. NULL until qualified.
    rank_band = Column(Integer, nullable=True)

    industry = Column(String(255), nullable=True)
    hq_location = Column(String(255), nullable=True)
    remote_posture = Column(String(20), nullable=True)  # remote_first|hybrid|onsite|unknown

    # Both stored VERBATIM, as the snippet or page text they were read from —
    # not as the agent's paraphrase. The trial-gate reviewer has to be able to
    # check the evidence against the live web, which a summary makes
    # impossible. This is also why a qualified row without evidence is a bug.
    india_signal = Column(Text, nullable=True)
    function_evidence = Column(Text, nullable=True)

    # no_function | no_india_signal | cap_reached | staffing | already_in_companies
    unqualified_reason = Column(String(50), nullable=True)

    # The enforced half of the time cap. find-contacts' per-company spend
    # eroded from 3.1 to 2.0 calls under pace while the cap lived only in
    # prose; here it lives in a column that batch.spend_ok() reads.
    searches_spent = Column(Integer, nullable=False, default=0)

    # Set by the human trial-gate review, never by the agent.
    reviewed = Column(Boolean, nullable=False, default=False)

    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_checked_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_prospect_normalized_name"),
    )

    def __repr__(self) -> str:
        return f"<Prospect id={self.id} name={self.name!r} status={self.status} band={self.rank_band}>"


class OutreachDraft(Base):
    """One cold-email draft to one recipient, asking about opportunities.

    Deliberately NOT scoped to Contact (ADR-0010): the input is "a company and
    an email address", and that address may belong to a stored Contact, to a
    company we know only by name (no Contact row), or to a prospect (no
    Company row at all — ADR-0008). `contact_id` / `company_id` / `prospect_id`
    are all nullable; at least one is set, enforced by app/outreach/drafts.py
    rather than a DB constraint (SQLite/Postgres CHECK across nullable FKs is
    awkward and the invariant needs a clear error message, not a DB error).

    Dedup key is `to_email` (unique, case-normalized by the caller): one live
    draft per recipient, re-running updates in place — same idempotency rule
    as TailoredResume.job_id and Prospect.normalized_name.
    """
    __tablename__ = "outreach_drafts"

    id = Column(Integer, primary_key=True)

    to_email = Column(String(255), nullable=False, unique=True, index=True)
    to_name = Column(String(255), nullable=True)
    recipient_title = Column(String(500), nullable=True)
    # head|hiring_manager|ic|talent_acquisition|exec_fallback|unknown — drives
    # which of the five email shapes (references/tier-shapes.md) gets used.
    recipient_tier = Column(String(50), nullable=True)

    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), nullable=True, index=True)

    # tailored|master. tailored requires matched_job_id + tailored_resume_id;
    # master requires neither (rendered once from app/resume/master.yaml).
    resume_kind = Column(String(20), nullable=False)
    tailored_resume_id = Column(Integer, ForeignKey("tailored_resumes.id"), nullable=True)
    resume_path = Column(String(1000), nullable=True)

    # The job used as TAILORING EVIDENCE (or, for a recruiter recipient, the
    # specific req named in the email) — not an application target. Recency
    # doesn't gate this; see CONTEXT.md "Tailoring evidence".
    matched_job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    # recipient_function_match | recruiter_best_match | no_matching_job |
    # no_jobs_at_company | company_is_prospect
    pairing_reason = Column(String(50), nullable=True)

    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    # Both set together or neither — a hook without its source is exactly the
    # unverifiable claim references/hooks.md exists to keep out.
    hook_url = Column(Text, nullable=True)
    hook_quote = Column(Text, nullable=True)

    # drafted -> pushed -> sent. "blocked_collision_risk" is a legacy value
    # (ADR-0012 stopped assigning it — a collision-risk draft is now
    # drafted and pushed like any other, flagged via collision_risk below
    # instead) kept only so old rows and push_draft's defensive check both
    # stay meaningful. 30 chars, not 20 — "blocked_collision_risk" is 22
    # and would silently truncate under Postgres (confirmed live: SQLite's
    # unit tests never enforce VARCHAR length, so this only broke against
    # real Postgres).
    status = Column(String(30), nullable=False, default="drafted", index=True)
    # Copied from contacts.name_collision_risk AT DRAFT TIME, so the flag
    # is self-contained even if the source contact row later changes —
    # this row is the audit trail for why a draft is worth double-checking
    # before the candidate sends it (ADR-0012; no longer a push gate).
    collision_risk = Column(Boolean, nullable=False, default=False)

    gmail_draft_id = Column(String(100), nullable=True)
    gmail_thread_id = Column(String(100), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    # The body pulled back from Gmail after sending — overwrites what we
    # drafted, since an edit made in Gmail is the real message (ADR-0009).
    sent_body = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    contact = relationship("Contact")
    company = relationship("Company")
    prospect = relationship("Prospect")
    tailored_resume = relationship("TailoredResume")
    matched_job = relationship("Job")

    def __repr__(self) -> str:
        return f"<OutreachDraft id={self.id} to_email={self.to_email!r} status={self.status}>"


class OutreachMessage(Base):
    __tablename__ = "outreach_messages"
    id = Column(Integer, primary_key=True)
    legacy_draft_id = Column(Integer, ForeignKey("outreach_drafts.id"), nullable=True, unique=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True, index=True)
    decision_run_job_id = Column(Integer, ForeignKey("decision_run_jobs.id"), nullable=True)
    posting_version_id = Column(Integer, ForeignKey("job_posting_versions.id"), nullable=True)
    message_kind = Column(String(20), nullable=False, default="initial")
    is_calibration = Column(Boolean, nullable=False, default=False)
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    resume_path = Column(String(1000), nullable=True)
    pairing_reason = Column(String(80), nullable=True)
    state = Column(String(30), nullable=False, default="held", index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("run_id", "contact_id", "message_kind", name="uq_run_initial_contact_kind"),)


class OutreachDeliveryAttempt(Base):
    __tablename__ = "outreach_delivery_attempts"
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("outreach_messages.id"), nullable=False, index=True)
    sequence_number = Column(Integer, nullable=False)
    to_email = Column(String(255), nullable=False)
    domain = Column(String(255), nullable=False, index=True)
    pattern_name = Column(String(30), nullable=True)
    gmail_draft_id = Column(String(100), nullable=True)
    gmail_thread_id = Column(String(100), nullable=True)
    gmail_message_id = Column(String(100), nullable=True, unique=True)
    state = Column(String(30), nullable=False, default="held")
    pushed_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    presumed_delivered_at = Column(DateTime(timezone=True), nullable=True)
    replied_at = Column(DateTime(timezone=True), nullable=True)
    bounced_at = Column(DateTime(timezone=True), nullable=True)
    evidence = Column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint("message_id", "sequence_number", name="uq_message_attempt_sequence"),)


class CompanyDomainAlias(Base):
    __tablename__ = "company_domain_aliases"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    domain = Column(String(255), nullable=False)
    first_observed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_observed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_current = Column(Boolean, nullable=False, default=True)
    is_verified = Column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("company_id", "domain", name="uq_company_domain_alias"),)


class DomainEmailPattern(Base):
    __tablename__ = "domain_email_patterns"
    id = Column(Integer, primary_key=True)
    domain = Column(String(255), nullable=False, index=True)
    pattern_name = Column(String(30), nullable=False)
    confidence = Column(String(20), nullable=False, default="unknown")
    success_count = Column(Integer, nullable=False, default=0)
    reply_count = Column(Integer, nullable=False, default=0)
    bounce_count = Column(Integer, nullable=False, default=0)
    first_observed_at = Column(DateTime(timezone=True), nullable=True)
    last_observed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("domain", "pattern_name", name="uq_domain_pattern"),)


class RunCompanyCalibration(Base):
    __tablename__ = "run_company_calibrations"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), ForeignKey("decision_runs.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    domain = Column(String(255), nullable=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    message_id = Column(Integer, ForeignKey("outreach_messages.id"), nullable=True)
    state = Column(String(30), nullable=False, default="waiting_to_send")
    first_sent_at = Column(DateTime(timezone=True), nullable=True)
    deadline_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_pattern = Column(String(30), nullable=True)
    last_reconciled_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("run_id", "company_id", name="uq_run_company_calibration"),)
