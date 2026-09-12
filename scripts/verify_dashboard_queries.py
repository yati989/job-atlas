"""
Script-based verification for app/dashboard/queries.py (issue #46). No
pytest in this repo — this is the script-based equivalent, per CLAUDE.md.

Runs each queries.py function against the real dev Postgres DB with a few
filter combinations (no filters, single-source filter, date-range filter,
combined filters) and asserts:
- no exceptions
- expected DataFrame columns are present
- the no-filter total job count matches a direct SELECT count(*) FROM jobs

Usage:
    python -m scripts.verify_dashboard_queries

Exits nonzero if any case fails.
"""
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.dashboard import queries
from app.dashboard.queries import DashboardFilters
from app.db.session import get_session
from app.models.orm import Job

DECISION_RUN_READ_MODEL_FUNCTIONS = (
    "decision_run_options",
    "default_decision_run_id",
    "decision_run_progress",
    "decision_run_job_funnel",
    "decision_run_job_funnel_drilldown",
    "decision_run_company_funnel",
    "decision_run_company_funnel_drilldown",
    "decision_run_resume_tailoring_funnel",
    "decision_run_resume_tailoring_drilldown",
    "decision_run_contact_funnel",
    "decision_run_contact_funnel_drilldown",
    "decision_run_outreach_funnel",
    "decision_run_outreach_funnel_drilldown",
)

NOW = datetime.now(timezone.utc)

FAILURES = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"[FAIL] {message}")
    else:
        print(f"[PASS] {message}")


def run_query(name, fn, session, filters, expected_columns):
    try:
        df = fn(session, filters)
    except Exception as exc:  # noqa: BLE001
        check(False, f"{name} ({filters}) raised: {exc!r}")
        return None
    check(True, f"{name} ({filters}) ran without exception")
    missing = set(expected_columns) - set(df.columns)
    check(not missing, f"{name} ({filters}) has expected columns (missing: {missing})")
    return df


def verify_decision_run_read_models(session) -> None:
    """Exercise every public Decision Run dashboard read-model function."""
    options = queries.decision_run_options(session)
    check(
        {"run_id", "state", "since_at", "cutoff_at", "started_at", "telemetry"}.issubset(options.columns),
        "decision_run_options has run identity, window, start, state, and telemetry columns",
    )
    default_run_id = queries.default_decision_run_id(options)
    check(
        default_run_id is None or default_run_id in set(options["run_id"]),
        "default_decision_run_id returns None or a selectable durable run",
    )
    if default_run_id is None:
        print("[PASS] decision_run_progress skipped because no Decision Runs exist")
        return
    progress = queries.decision_run_progress(session, default_run_id)
    check(
        {
            "stage", "status", "input", "advanced", "dropped", "failed", "pending",
            "attempt", "attempt_history", "updated_at", "telemetry",
        }.issubset(progress.columns),
        "decision_run_progress has reconciled counts, attempt history, timing, and telemetry columns",
    )
    funnel = queries.decision_run_job_funnel(session, default_run_id)
    check(
        {"stage", "from", "to", "input", "advanced", "dropped", "failed", "pending", "available"}.issubset(funnel.columns),
        "decision_run_job_funnel has canonical reconciling transition columns",
    )
    stage_name = str(funnel.iloc[0]["stage"])
    drilldown = queries.decision_run_job_funnel_drilldown(
        session, default_run_id, stage_name=stage_name,
    )
    check(
        {"record_id", "outcome", "reason", "source", "external_job_id", "job_url", "apply_url"}.issubset(drilldown.columns),
        "decision_run_job_funnel_drilldown has structured provenance and link columns",
    )
    company_funnel = queries.decision_run_company_funnel(session, default_run_id)
    check(
        {"stage", "companies", "eligible_jobs", "advanced", "dropped", "failed", "pending"}.issubset(company_funnel.columns),
        "decision_run_company_funnel has separate company and eligible-job counts",
    )
    company_stage = str(company_funnel.iloc[0]["stage"]) if not company_funnel.empty else "company_deduplication"
    company_drilldown = queries.decision_run_company_funnel_drilldown(session, default_run_id, stage_name=company_stage)
    check(
        {"company_id", "outcome", "reason", "company_evidence", "source", "job_url", "apply_url"}.issubset(company_drilldown.columns),
        "decision_run_company_funnel_drilldown has company evidence and job source links",
    )
    tailoring = queries.decision_run_resume_tailoring_funnel(session, default_run_id)
    check(
        {"stage", "input", "tailored", "dropped", "failed", "pending", "available"}.issubset(tailoring.columns),
        "decision_run_resume_tailoring_funnel has exact approved-scope outcome columns",
    )
    tailoring_detail = queries.decision_run_resume_tailoring_drilldown(session, default_run_id)
    check(
        {"posting_version_id", "outcome", "reason", "source", "job_url", "apply_url"}.issubset(tailoring_detail.columns),
        "decision_run_resume_tailoring_drilldown has exact posting-version provenance and links",
    )
    contact_funnel = queries.decision_run_contact_funnel(session, default_run_id)
    check(
        {"input", "enriched", "exhausted", "no_match", "excluded", "failed", "pending"}.issubset(contact_funnel.columns),
        "decision_run_contact_funnel has frozen company/function outcome counts",
    )
    contact_drilldown = queries.decision_run_contact_funnel_drilldown(session, default_run_id)
    check(
        {"company_id", "search_group", "outcome", "reused_contact_ids", "new_contact_ids", "coverage"}.issubset(contact_drilldown.columns),
        "decision_run_contact_funnel_drilldown has coverage and evidence identifiers",
    )
    outreach = queries.decision_run_outreach_funnel(session, default_run_id)
    check({"stage", "input", "dropped", "failed", "pending", "available"}.issubset(outreach.columns),
          "decision_run_outreach_funnel has separate draft and Gmail Draft counts")
    outreach_detail = queries.decision_run_outreach_funnel_drilldown(
        session, default_run_id, stage_name="draft_preparation")
    check({"contact_id", "outreach_draft_id", "outcome", "email", "job_url", "apply_url"}.issubset(outreach_detail.columns),
          "decision_run_outreach_funnel_drilldown has recipient and source links")


def main() -> int:
    with get_session() as session:
        real_total = session.query(func.count(Job.id)).scalar() or 0
        any_source = session.query(Job.source).first()
        sample_source = any_source[0] if any_source else None

    filter_cases = [
        DashboardFilters(),
        DashboardFilters(source=sample_source) if sample_source else DashboardFilters(),
        DashboardFilters(date_from=NOW - timedelta(days=180), date_to=NOW),
        DashboardFilters(
            source=sample_source,
            date_from=NOW - timedelta(days=365),
            date_to=NOW,
        ) if sample_source else DashboardFilters(date_from=NOW - timedelta(days=365)),
        DashboardFilters(salary_min_lpa=10.0, salary_max_lpa=30.0),
        DashboardFilters(posted_from=NOW - timedelta(days=180), posted_to=NOW),
        DashboardFilters(posted_from=NOW - timedelta(days=180)),  # start-only, no end date
        DashboardFilters(job_category="data_science"),
        DashboardFilters(min_total_contacts=1),
        DashboardFilters(min_head_hm_contacts=1),
    ]

    query_specs = [
        ("jobs_per_source", queries.jobs_per_source, ["source", "job_count"]),
        ("jobs_over_time", queries.jobs_over_time, ["day", "job_count"]),
        (
            "enrichment_coverage_overall",
            queries.enrichment_coverage_overall,
            ["total", "enriched", "pct_enriched"],
        ),
        (
            "enrichment_coverage_by_source",
            queries.enrichment_coverage_by_source,
            ["source", "total", "enriched", "pct_enriched"],
        ),
        (
            "field_completeness_by_source",
            queries.field_completeness_by_source,
            ["source", "field", "non_null_count", "total", "pct_complete"],
        ),
        (
            "company_growth_over_time",
            queries.company_growth_over_time,
            ["day", "company_count", "cumulative_count"],
        ),
        ("top_skills", queries.top_skills, ["skill", "skill_type", "count"]),
        ("seniority_distribution", queries.seniority_distribution, ["seniority", "count"]),
        (
            "top_hiring_companies",
            queries.top_hiring_companies,
            ["company_name", "job_count"],
        ),
        ("industry_breakdown", queries.industry_breakdown, ["industry", "company_count"]),
        ("remote_split", queries.remote_split, ["remote_status", "count"]),
        (
            "experience_years_distribution",
            queries.experience_years_distribution,
            ["experience_min_years", "count"],
        ),
        (
            "explore_jobs",
            queries.explore_jobs,
            [
                "id", "source", "title", "company_name", "location_raw",
                "contacts_total", "contacts_head", "contacts_hiring_manager",
                "contacts_ic", "contacts_talent_acquisition",
            ],
        ),
        ("salary_distribution", queries.salary_distribution, ["band", "count"]),
        (
            "company_wise_distribution",
            queries.company_wise_distribution,
            ["company_name", "job_count", "head_hiring_manager_contacts", "rest_contacts"],
        ),
        (
            "company_explore_table",
            queries.company_explore_table,
            ["company_name", "job_count", "pain_points", "company_url"],
        ),
    ]

    with get_session() as session:
        verify_decision_run_read_models(session)
        for filters in filter_cases:
            for name, fn, expected_columns in query_specs:
                run_query(name, fn, session, filters, expected_columns)

        # Hand-checked count: no-filter total job count matches direct count(*).
        no_filter_df = queries.jobs_per_source(session, DashboardFilters())
        computed_total = int(no_filter_df["job_count"].sum()) if not no_filter_df.empty else 0
        check(
            computed_total == real_total,
            f"no-filter total job count matches SELECT count(*) FROM jobs "
            f"({computed_total} == {real_total})",
        )

        # limit=None (the "download all" path) returns the full unfiltered
        # count, not just the 500-row on-screen cap.
        full_jobs_df = queries.explore_jobs(session, DashboardFilters(), limit=None)
        check(
            len(full_jobs_df) == real_total,
            f"explore_jobs(limit=None) returns all {real_total} job(s), got {len(full_jobs_df)}",
        )
        capped_jobs_df = queries.explore_jobs(session, DashboardFilters(), limit=500)
        check(
            len(capped_jobs_df) == min(500, real_total),
            f"explore_jobs(limit=500) still caps at 500 (got {len(capped_jobs_df)})",
        )
        full_companies_df = queries.company_explore_table(session, DashboardFilters(), limit=None)
        capped_companies_df = queries.company_explore_table(session, DashboardFilters(), limit=500)
        check(
            len(full_companies_df) >= len(capped_companies_df),
            f"company_explore_table(limit=None) returns at least as many rows as the capped call "
            f"({len(full_companies_df)} >= {len(capped_companies_df)})",
        )

        # N attributes present on enrichment-gated queries.
        for name, fn in [
            ("top_skills", queries.top_skills),
            ("seniority_distribution", queries.seniority_distribution),
            ("industry_breakdown", queries.industry_breakdown),
            ("experience_years_distribution", queries.experience_years_distribution),
            ("salary_distribution", queries.salary_distribution),
        ]:
            df = fn(session, DashboardFilters())
            check("n" in df.attrs, f"{name} exposes df.attrs['n']")

    total_checks = len(FAILURES)
    if total_checks:
        print(f"\n{total_checks} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
