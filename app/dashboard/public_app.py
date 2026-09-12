"""Streamlit dashboard for one local, guided public job-search run."""
from __future__ import annotations

from collections import Counter
from html import escape
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import select

from app.dashboard.public_read_model import (
    PublicDashboardFilters,
    combine_public_snapshots,
    dated_search_label,
    filter_kept_jobs,
    friendly_search_label,
    load_public_snapshot,
    outreach_draft_caption,
    profile_link_output_message,
    stage_message,
)
from app.models.orm import DecisionRun, GuidedRunPlan
from app.reporting.public_export import cumulative_workbook_bytes
from app.workflows.profiles import default_private_home
from app.workflows.public_database import public_session


st.set_page_config(page_title="Job Atlas", page_icon="🔎", layout="wide")
st.markdown(
    """
    <style>
      .block-container {padding-top: 2rem; padding-bottom: 3rem;}
      [data-testid="stSidebar"] {
        width: 21rem !important;
        min-width: 21rem !important;
        max-width: 21rem !important;
      }
      [data-testid="stMetric"] {
        background: rgba(91, 117, 255, 0.08);
        border: 1px solid rgba(91, 117, 255, 0.22);
        border-radius: 14px;
        padding: 14px 16px;
      }
      .run-summary {
        border: 1px solid rgba(128, 128, 128, 0.25);
        border-radius: 14px;
        padding: 14px 18px;
        margin: 0.4rem 0 1.2rem;
      }
      .eyebrow {font-size: 0.82rem; opacity: 0.7; letter-spacing: 0.04em;}
    </style>
    """,
    unsafe_allow_html=True,
)


def _choose(value: str) -> str | None:
    return None if value == "All" else value


def _table_frame(rows: tuple[dict, ...]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column in ("hard_skills", "soft_skills"):
        if column in frame:
            frame[column] = frame[column].apply(
                lambda values: ", ".join(values) if isinstance(values, (list, tuple)) else values
            )
    return frame


def _download_csv(label: str, frame: pd.DataFrame, filename: str) -> None:
    st.download_button(
        label,
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
    )


STAGE_DESCRIPTIONS = {
    "role_review": ("Match jobs to your request", "AI reviews job titles and unclear descriptions for the kind of work you asked for."),
    "location_review": ("Check where you can work", "AI checks unclear locations against your selected city, India, or remote preference."),
    "phase_a": ("Read jobs and understand companies", "Extracts experience, education, qualifications, skills, salary evidence, and basic company facts."),
    "phase_b": ("Optional workplace research", "Looks for deeper salary, rating, and work-life balance evidence when you choose this paid step."),
    "selection": ("Freeze your shortlist", "Saves the exact jobs and companies you want to use in later steps."),
    "tailoring": ("Prepare tailored resumes", "Creates a truthful role-specific resume for each selected job when requested."),
    "profile_links": ("Find useful LinkedIn profiles", "Finds reviewable LinkedIn profile links for people at selected companies."),
    "export": ("Build the Excel workbook", "Creates the portable workbook containing jobs, companies, links, and optional outputs."),
}


private_home = Path(st.sidebar.text_input("Local data folder", str(default_private_home())))
with public_session(private_home) as session:
    run_ids = list(session.scalars(select(DecisionRun.id).order_by(DecisionRun.created_at.desc())))
    if not run_ids:
        st.info("No searches yet. Start a guided search from your coding-agent chat.")
        st.stop()
    plan_rows = session.execute(select(
        GuidedRunPlan.run_id,
        GuidedRunPlan.profile_snapshot,
        DecisionRun.created_at,
        DecisionRun.input_timezone,
    ).join(DecisionRun, GuidedRunPlan.run_id == DecisionRun.id)).all()
    run_labels = {
        saved_run_id: dated_search_label(
            profile if isinstance(profile, dict) else {},
            created_at,
            input_timezone,
        )
        for saved_run_id, profile, created_at, input_timezone in plan_rows
    }
    longest_search_label = max(
        (len(label) for label in run_labels.values()), default=len("All searches")
    )
    search_content_width = min(max(longest_search_label + 4, 48), 160)
    st.markdown(
        f"""
        <style>
          [data-testid="stSelectboxVirtualDropdown"]:has(
            [role="listbox"][aria-label="Search"]
          ) {{
            width: calc(21rem - 2.5rem) !important;
            max-width: calc(21rem - 2.5rem) !important;
            overflow-x: auto !important;
          }}
          [role="listbox"][aria-label="Search"],
          [role="listbox"][aria-label="Search"] [role="presentation"],
          [role="listbox"][aria-label="Search"] [role="option"] {{
            min-width: {search_content_width}ch !important;
          }}
          [role="listbox"][aria-label="Search"] [role="option"] > div {{
            overflow: visible !important;
            text-overflow: clip !important;
            white-space: nowrap !important;
          }}
          .search-label-scroll {{
            overflow-x: auto;
            padding: 0.15rem 0 0.45rem;
            white-space: nowrap;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    search_run_ids = run_ids
    search_choice = st.sidebar.selectbox(
        "Search",
        ["all", *search_run_ids],
        format_func=lambda value: "All searches" if value == "all" else run_labels.get(
            value, "Saved search"
        ),
        key="search_picker",
    )
    if search_choice != "all":
        selected_search_label = escape(run_labels.get(search_choice, "Saved search"))
        st.sidebar.markdown(
            f'<div class="search-label-scroll">{selected_search_label}</div>',
            unsafe_allow_html=True,
        )
    is_cumulative = search_choice == "all"
    if is_cumulative:
        snapshots = tuple(
            load_public_snapshot(session, saved_run_id) for saved_run_id in search_run_ids
        )
        snapshot = combine_public_snapshots(snapshots)
        cumulative_excel = cumulative_workbook_bytes(session, search_run_ids)
        run_id = "all-searches"
    else:
        snapshot = load_public_snapshot(session, search_choice)
        cumulative_excel = None
        run_id = search_choice

st.sidebar.header("Filter jobs")
if st.sidebar.button("Refresh dashboard", width="stretch"):
    st.rerun()
sources = sorted({job["source"] for job in snapshot.kept_jobs})
statuses = sorted({job["enrichment_status"] for job in snapshot.kept_jobs})
skills = sorted({
    skill
    for job in snapshot.kept_jobs
    for skill in (*job["hard_skills"], *job["soft_skills"])
})
source = _choose(st.sidebar.selectbox("Source", ["All", *sources]))
company = st.sidebar.text_input("Company contains") or None
work_mode = st.sidebar.selectbox("Work mode", ["All", "Remote", "On-site / unspecified"])
remote = {"All": None, "Remote": True, "On-site / unspecified": False}[work_mode]
enrichment_status = _choose(st.sidebar.selectbox("Enrichment", ["All", *statuses]))
skill = _choose(st.sidebar.selectbox("Skill", ["All", *skills]))
selected_only = False
if any(job["selected"] for job in snapshot.kept_jobs):
    selected_only = st.sidebar.checkbox(
        "Final selection only", value=not is_cumulative,
    )

with st.sidebar.expander("Optional company research filters"):
    st.caption("These fields are available only when optional company-market research was run.")
    minimum_glassdoor = st.slider("Minimum Glassdoor rating", 0.0, 5.0, 0.0, 0.1)
    include_blank_glassdoor = st.checkbox("Keep jobs with no Glassdoor rating", value=True)
    minimum_wlb = st.slider("Minimum work-life balance rating", 0.0, 5.0, 0.0, 0.1)
    include_blank_wlb = st.checkbox("Keep jobs with no WLB rating", value=True)
    minimum_salary = st.number_input(
        "Minimum company salary estimate (LPA)", min_value=0.0, value=0.0, step=1.0,
    )
    include_blank_salary = st.checkbox("Keep jobs with no company salary estimate", value=True)

filters = PublicDashboardFilters(
    source=source,
    enrichment_status=enrichment_status,
    company=company,
    skill=skill,
    remote=remote,
    selected_only=selected_only,
    minimum_glassdoor_overall_rating=minimum_glassdoor or None,
    minimum_glassdoor_wlb_rating=minimum_wlb or None,
    minimum_estimated_salary_lpa=minimum_salary or None,
    include_blank_glassdoor_overall_rating=include_blank_glassdoor,
    include_blank_glassdoor_wlb_rating=include_blank_wlb,
    include_blank_estimated_salary_lpa=include_blank_salary,
)
visible_jobs = filter_kept_jobs(snapshot.kept_jobs, filters)
visible_company_names = {job["company"] for job in visible_jobs}
visible_companies = tuple(
    company_row for company_row in snapshot.companies
    if company_row["company"] in visible_company_names
    and (not selected_only or company_row["selected"])
)

st.markdown('<div class="eyebrow">AI-ASSISTED INDIA JOB SEARCH</div>', unsafe_allow_html=True)
st.title(snapshot.run["label"])
terms = ", ".join(snapshot.run["search_terms"]) or "Search terms unavailable"
places = [*snapshot.run["cities"], *snapshot.run["countries"]]
place_text = ", ".join(dict.fromkeys(places)) or "Location not specified"
arrangements = ", ".join(snapshot.run["arrangements"]) or "Any work mode"
window = snapshot.run["collection_window_days"]
window_text = f"{window}-day source search" if window else "Source search window unavailable"
if is_cumulative:
    st.markdown(
        f'<div class="run-summary"><strong>{len(search_run_ids)} saved searches together</strong><br>'
        'Repeated listings from the same source are shown once. Use the Search filter to return to one search.</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        f'<div class="run-summary"><strong>{terms}</strong><br>'
        f'{place_text} · {arrangements} · {window_text}</div>',
        unsafe_allow_html=True,
    )

selected_count = sum(bool(job["selected"]) for job in snapshot.kept_jobs)
metric_cols = st.columns(5)
metric_cols[0].metric("Collected observations" if is_cumulative else "Collected", f"{snapshot.collected_count:,}")
metric_cols[1].metric("Unique eligible jobs" if is_cumulative else "Eligible after all checks", f"{len(snapshot.kept_jobs):,}")
metric_cols[2].metric("Selected jobs", f"{selected_count:,}" if selected_count else "None yet")
metric_cols[3].metric("Jobs enriched", f"{snapshot.enriched_job_count:,}")
metric_cols[4].metric("Companies", f"{len(snapshot.companies):,}")

if is_cumulative:
    st.download_button(
        "Export all searches to Excel",
        data=cumulative_excel,
        file_name="all-job-searches.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
elif snapshot.workbook_path:
    workbook = Path(snapshot.workbook_path).expanduser()
    if workbook.is_file():
        st.download_button(
            "Download Excel results",
            data=workbook.read_bytes(),
            file_name=workbook.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
    else:
        st.info(f"Excel export was recorded at {workbook}, but the file is no longer there.")
else:
    st.caption("The Excel download appears here after the export step is completed.")

tab_pipeline, tab_insights, tab_explore, tab_outputs = st.tabs(
    ["Pipeline", "Job insights", "Explore jobs & companies", "Other outputs"]
)

with tab_pipeline:
    if is_cumulative:
        st.info("Choose one saved search in the sidebar to see its live source and pipeline progress.")
    else:
        st.subheader("Source progress")
        st.caption("Each source updates this table as the search runs. Use Refresh dashboard for the latest state.")
        source_frame = pd.DataFrame(snapshot.source_statuses)
        st.dataframe(source_frame, width="stretch", hide_index=True)

        st.subheader("Pipeline steps")
        stage_frame = pd.DataFrame([{
            "step": STAGE_DESCRIPTIONS.get(stage["name"], (stage["name"], ""))[0],
            "what it does": STAGE_DESCRIPTIONS.get(stage["name"], ("", ""))[1],
            "status": stage["status"].replace("_", " ").title(),
            "progress": f"{stage['completed_count']:,} / {stage['expected_count']:,}",
            "message": stage_message(stage),
        } for stage in snapshot.stage_statuses])
        st.dataframe(stage_frame, width="stretch", hide_index=True)

    st.subheader("Search decisions")
    outcome_frame = pd.DataFrame([
        {"decision": decision.replace("_", " ").title(), "jobs": count}
        for decision, count in snapshot.outcome_counts.items()
    ])
    if outcome_frame.empty:
        st.info("No job decisions have been recorded yet.")
    else:
        st.plotly_chart(
            px.bar(outcome_frame, x="decision", y="jobs", text_auto=",.0f"),
            width="stretch",
        )

with tab_insights:
    if not visible_jobs:
        st.info("No jobs match the current filters.")
    else:
        source_counts = Counter(job["source"] for job in visible_jobs)
        mode_counts = Counter(job["work_mode"] for job in visible_jobs)
        skill_counts = Counter(
            skill_name
            for job in visible_jobs
            for skill_name in (*job["hard_skills"], *job["soft_skills"])
        )
        company_counts = Counter(job["company"] for job in visible_jobs)
        left, right = st.columns(2)
        with left:
            st.subheader("Jobs by source")
            st.plotly_chart(px.bar(
                pd.DataFrame(source_counts.most_common(), columns=["source", "jobs"]),
                x="source", y="jobs", text_auto=True,
            ), width="stretch")
        with right:
            st.subheader("Remote and on-site")
            st.plotly_chart(px.pie(
                pd.DataFrame(mode_counts.items(), columns=["work_mode", "jobs"]),
                names="work_mode", values="jobs", hole=0.5,
            ), width="stretch")
        left, right = st.columns(2)
        with left:
            st.subheader("Most requested skills")
            if skill_counts:
                st.plotly_chart(px.bar(
                    pd.DataFrame(skill_counts.most_common(15), columns=["skill", "jobs"]),
                    x="jobs", y="skill", orientation="h", text_auto=True,
                ), width="stretch")
            else:
                st.info("No skill requirements were extracted for these jobs.")
        with right:
            st.subheader("Top hiring companies")
            st.plotly_chart(px.bar(
                pd.DataFrame(company_counts.most_common(15), columns=["company", "jobs"]),
                x="jobs", y="company", orientation="h", text_auto=True,
            ), width="stretch")

with tab_explore:
    st.subheader("Enriched jobs")
    st.caption(
        f"Showing {len(visible_jobs)} of {len(snapshot.kept_jobs)} eligible jobs"
        + (" across all saved searches. " if is_cumulative else " from this search. ")
        + (
            "Use the final-selection filter to narrow the cumulative list."
            if is_cumulative
            else "The final-selection filter starts on after a shortlist is frozen."
        )
    )
    jobs_frame = _table_frame(visible_jobs)
    job_columns = (["search"] if is_cumulative else []) + [
        "selected", "source", "title", "company", "job_url", "apply_url",
        "location", "work_mode", "posted_at",
        "employment_type", "seniority", "salary", "salary_evidence",
        "experience_min_years", "experience_max_years", "education",
        "other_qualifications", "hard_skills", "soft_skills", "enrichment_status",
    ]
    st.dataframe(
        jobs_frame,
        width="stretch",
        hide_index=True,
        column_order=[column for column in job_columns if column in jobs_frame],
        column_config={
            "selected": st.column_config.CheckboxColumn("Selected"),
            "job_url": st.column_config.LinkColumn("Job", display_text="Open"),
            "apply_url": st.column_config.LinkColumn("Apply", display_text="Apply"),
            "other_qualifications": st.column_config.TextColumn("Other qualifications", width="large"),
            "salary_evidence": st.column_config.TextColumn("Extracted salary evidence", width="large"),
        },
    )
    _download_csv("Download these jobs (CSV)", jobs_frame, f"{run_id}-jobs.csv")

    st.subheader("Enriched companies")
    st.caption(f"Showing {len(visible_companies)} companies represented by the filtered jobs above.")
    companies_frame = pd.DataFrame(visible_companies)
    st.dataframe(
        companies_frame,
        width="stretch",
        hide_index=True,
        row_height=90,
        column_config={
            "selected": st.column_config.CheckboxColumn("Selected"),
            "company": "Company",
            "company_type": "Employer or agency",
            "pain_points": st.column_config.TextColumn("What the company may need", width="large"),
            "description": st.column_config.TextColumn("Company summary", width="large"),
            "company_url": st.column_config.LinkColumn("Website", display_text="Visit"),
        },
    )
    _download_csv("Download these companies (CSV)", companies_frame, f"{run_id}-companies.csv")

with tab_outputs:
    st.subheader("Final selection")
    if is_cumulative:
        st.info(
            f"{selected_count:,} unique jobs are selected across these saved searches. "
            "Choose one search to inspect its exact frozen selection."
        )
    elif snapshot.latest_scope:
        st.json(snapshot.latest_scope)
    else:
        st.info("No final selection has been frozen yet.")
    st.subheader("LinkedIn profile links")
    if snapshot.profile_links:
        st.dataframe(
            snapshot.profile_links,
            width="stretch",
            hide_index=True,
            column_config={
                "company": st.column_config.TextColumn("Company"),
                "linkedin_url": st.column_config.LinkColumn(
                    "LinkedIn profile", display_text="Open LinkedIn"
                ),
            },
        )
    else:
        st.info(profile_link_output_message(snapshot.stage_statuses))
    st.subheader("Tailored resumes")
    if snapshot.resumes:
        st.dataframe(snapshot.resumes, width="stretch", hide_index=True)
    else:
        st.info("No tailored resumes were created for this selection.")
    st.subheader("Outreach drafts")
    if is_cumulative:
        st.info("Choose one saved search to see its outreach drafts.")
    elif snapshot.outreach_drafts:
        st.caption(outreach_draft_caption(snapshot.outreach_drafts))
        st.dataframe(
            snapshot.outreach_drafts,
            width="stretch",
            hide_index=True,
            column_config={
                "evidence_source_url": st.column_config.LinkColumn(
                    "Recipient evidence", display_text="Open source"
                ),
            },
        )
    else:
        st.info("No outreach drafts were created for this search.")
