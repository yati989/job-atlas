"""
Streamlit entrypoint for the analysis dashboard (issue #46).

Run with:
    streamlit run app/dashboard/app.py

Local, single-user tool — no auth, no hosting. Reads live from Postgres via
app/db/session.py. Query logic lives in app/dashboard/queries.py; this file
only renders sidebar filters, tabs, and charts.
"""
from datetime import datetime, date

import pandas as pd
import plotly.express as px
import streamlit as st

from app.config.categories import CATEGORY_KEYWORDS
from app.dashboard import queries
from app.dashboard.queries import JOB_CATEGORY_LABELS, DashboardFilters
from app.dashboard.stage_tracker import render_stage_tracker
from app.dashboard.job_funnel import render_job_funnel
from app.dashboard.company_funnel import render_company_funnel
from app.db.session import get_session
from app.pipeline.progress import (
    default_progress_run_id,
    load_progress,
    load_progress_history,
    process_is_alive,
    run_elapsed_seconds,
    source_rows,
    total_source_row,
)

CACHE_TTL_SECONDS = 600  # 10 minutes
PROGRESS_REFRESH_INTERVAL_SECONDS = 2

# "Top hard skills" chart: fetch a much larger pool than what's shown by
# default, so zooming/panning out on the x-axis reveals rarer skills instead
# of the chart simply having nothing past the initial view.
TOP_SKILLS_DEFAULT_VISIBLE = 30
TOP_SKILLS_POOL_SIZE = 150

st.set_page_config(page_title="Job Agent Dashboard", layout="wide")
st.markdown(
    """
    <style>
    /* Fragment polling briefly mounts Streamlit's global Running widget.
       The progress tables already show their own refresh timestamp, so keep
       this background implementation detail from flashing on every poll. */
    [data-testid="stStatusWidget"]:has([data-testid="stStatusWidgetRunningIcon"]) {
        visibility: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- Cached query wrappers -------------------------------------------------
# st.cache_data needs hashable args; DashboardFilters is a plain dataclass of
# hashable fields so it works as-is (no eq=False needed since it's frozen by
# convention of use — only app.py constructs and passes it through unchanged).

@st.cache_data(ttl=CACHE_TTL_SECONDS)
def _run(query_name: str, filters: DashboardFilters, **kwargs) -> pd.DataFrame:
    fn = getattr(queries, query_name)
    with get_session() as session:
        return fn(session, filters, **kwargs)


def run(query_name: str, filters: DashboardFilters, **kwargs) -> pd.DataFrame:
    return _run(query_name, filters, **kwargs)


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@st.fragment(run_every=PROGRESS_REFRESH_INTERVAL_SECONDS)
def render_live_connector_progress() -> None:
    st.subheader("Live connector progress")
    st.button("Refresh connector progress", key="refresh_connector_progress")
    snapshots = load_progress_history()
    default_run_id = default_progress_run_id(snapshots)
    if default_run_id is None:
        st.info("No connector run has published progress yet.")
        return

    latest = load_progress()

    def display_status_for(item: dict) -> str:
        status = str(item.get("status", "unknown"))
        item_is_latest = bool(
            latest
            and str(latest.get("run_id")) == str(item.get("run_id"))
            and latest.get("command") == item.get("command")
        )
        if status == "running" and (
            not item_is_latest or not process_is_alive(item)
        ):
            return "interrupted"
        return status

    run_ids = [str(snapshot["run_id"]) for snapshot in snapshots]
    labels = {
        str(snapshot["run_id"]): (
            f"{snapshot['run_id']} · {display_status_for(snapshot).replace('_', ' ')}"
            f" · {snapshot.get('command') or 'connector run'}"
        )
        for snapshot in snapshots
    }
    selected_run_id = st.selectbox(
        "Connector Run",
        run_ids,
        index=run_ids.index(default_run_id),
        format_func=labels.get,
        key="connector_run_progress_selector",
    )
    snapshot = next(
        item for item in snapshots if str(item["run_id"]) == selected_run_id
    )

    status = snapshot.get("status", "unknown")
    interrupted = (
        status == "running" and display_status_for(snapshot) == "interrupted"
    )
    display_status = "interrupted" if interrupted else status
    rows = source_rows(snapshot, interrupted=interrupted)
    if interrupted:
        for row in rows:
            if row["status"] in {"pending", "running"}:
                row["status"] = "interrupted"

    total_jobs = sum(row["fetched"] for row in rows)
    instances_finished = sum(row["instances_finished"] for row in rows)
    instances_total = sum(row["instances_total"] for row in rows)
    failed = sum(row["instances_failed"] for row in rows)

    metric_status, metric_elapsed, metric_jobs, metric_instances = st.columns(4)
    metric_status.metric("Run status", display_status.replace("_", " ").title())
    elapsed_seconds = run_elapsed_seconds(snapshot, interrupted=interrupted)
    metric_elapsed.metric("Elapsed", _format_elapsed(elapsed_seconds))
    metric_jobs.metric("Jobs fetched", f"{total_jobs:,}")
    metric_instances.metric(
        "Instances finished", f"{instances_finished:,} / {instances_total:,}"
    )

    st.caption(
        f"Run {snapshot.get('run_id', 'unknown')} · {snapshot.get('command') or 'connector run'} · "
        f"failed instances: {failed} · auto-refreshes every "
        f"{PROGRESS_REFRESH_INTERVAL_SECONDS}s"
    )
    if interrupted:
        st.error(
            "The publishing process is no longer running. Completed counts are preserved; "
            "unfinished connectors are marked interrupted."
        )

    status_order = {
        "running": 0,
        "interrupted": 1,
        "partial": 2,
        "failed": 3,
        "pending": 4,
        "completed": 5,
    }
    table = pd.DataFrame(rows)
    if table.empty:
        st.info("This run selected no connectors.")
        return
    table["sort_order"] = table["status"].map(status_order).fillna(9)
    table["elapsed"] = table["elapsed_seconds"].map(_format_elapsed)
    table["instances"] = table.apply(
        lambda row: f"{int(row['instances_finished'])} / {int(row['instances_total'])}",
        axis=1,
    )
    table = table.sort_values(["sort_order", "source"])
    total = pd.DataFrame([
        total_source_row(
            rows,
            status=display_status,
            elapsed_seconds=elapsed_seconds,
        )
    ])
    total["elapsed"] = total["elapsed_seconds"].map(_format_elapsed)
    total["instances"] = total.apply(
        lambda row: f"{int(row['instances_finished'])} / {int(row['instances_total'])}",
        axis=1,
    )
    table = pd.concat([table, total], ignore_index=True).rename(
        columns={
            "source": "Connector",
            "status": "Status",
            "elapsed": "Elapsed",
            "fetched": "Fetched",
            "kept": "Kept",
            "kept_pct": "Kept %",
            "drop_role": "Drop: role",
            "drop_seniority": "Drop: seniority",
            "drop_location": "Drop: location",
            "drop_recency": "Drop: recency",
            "instances": "Instances finished",
            "instances_succeeded": "Succeeded",
            "instances_failed": "Failed",
        }
    )
    st.dataframe(
        table[
            [
                "Connector",
                "Status",
                "Elapsed",
                "Fetched",
                "Kept",
                "Kept %",
                "Drop: role",
                "Drop: seniority",
                "Drop: location",
                "Drop: recency",
                "Instances finished",
                "Succeeded",
                "Failed",
            ]
        ],
        width="stretch",
        hide_index=True,
    )


def render_decision_run_health() -> None:
    """Render stable run controls, live stage truth, and analytical funnels."""
    st.subheader("Decision run progress")
    with get_session() as session:
        options = queries.decision_run_options(session)
    default_run_id = queries.default_decision_run_id(options)
    if default_run_id is None:
        st.info("No Decision Runs are available yet.")
        return

    run_ids = options["run_id"].astype(str).tolist()
    labels = {
        str(row.run_id): (
            f"{row.run_id} · {str(row.state).replace('_', ' ')}"
            + (" · Legacy run — incomplete telemetry" if row.telemetry != "complete" else "")
        )
        for row in options.itertuples(index=False)
    }
    selected_run_id = st.selectbox(
        "Decision Run",
        run_ids,
        index=run_ids.index(default_run_id),
        format_func=labels.get,
        key="decision_run_progress_selector",
    )
    selected = options.loc[options["run_id"] == selected_run_id].iloc[0]
    format_fact = lambda value: "—" if pd.isna(value) else value.isoformat()
    st.caption(
        f"Posting window: {format_fact(selected['since_at'])} → {format_fact(selected['cutoff_at'])} · "
        f"run started: {format_fact(selected['started_at'])}"
    )
    telemetry = str(selected["telemetry"])
    render_live_decision_run_progress(selected_run_id, telemetry)
    render_decision_run_funnels(selected_run_id, telemetry)


@st.fragment(run_every=PROGRESS_REFRESH_INTERVAL_SECONDS)
def render_live_decision_run_progress(selected_run_id: str, telemetry: str) -> None:
    """Refresh only durable stage truth; keep stable controls and funnels outside."""
    refreshed_at = datetime.now().astimezone()
    st.button("Refresh run progress", key="refresh_decision_run_progress")
    with get_session() as session:
        table = queries.decision_run_progress(session, selected_run_id)

    if telemetry != "complete":
        st.warning("Legacy run — incomplete telemetry. Only recorded facts are shown.")
    else:
        render_stage_tracker(st, table)
    st.caption(
        f"Run {selected_run_id} · auto-refreshes every "
        f"{PROGRESS_REFRESH_INTERVAL_SECONDS}s · last refreshed "
        f"{refreshed_at.strftime('%H:%M:%S')}"
    )


def render_decision_run_funnels(selected_run_id: str, telemetry: str) -> None:
    """Render analytical drill-downs only on stable, full-page reruns."""
    if telemetry == "complete":
        with get_session() as session:
            funnel = queries.decision_run_job_funnel(session, selected_run_id)
        st.subheader("Job funnel")
        display_funnel = funnel.copy()
        for column in ("input", "advanced", "dropped", "failed", "pending"):
            display_funnel[column] = display_funnel[column].map(
                lambda value: "—" if pd.isna(value) else int(value)
            )
        render_job_funnel(st, display_funnel)
        with st.expander("Inspect job-funnel records"):
            stage_name = st.selectbox(
                "Funnel transition", funnel["stage"].tolist(),
                format_func=lambda value: str(value).replace("_", " ").title(),
                key="job_funnel_stage",
            )
            with get_session() as session:
                all_records = queries.decision_run_job_funnel_drilldown(
                    session, selected_run_id, stage_name=stage_name,
                )
            sources = ["All"] + sorted(all_records["source"].dropna().unique().tolist())
            reasons = ["All"] + sorted(all_records["reason"].dropna().unique().tolist())
            selected_source, selected_reason = st.columns(2)
            source = selected_source.selectbox("Source", sources, key="job_funnel_source")
            reason = selected_reason.selectbox("Reason", reasons, key="job_funnel_reason")
            with get_session() as session:
                subset = queries.decision_run_job_funnel_drilldown(
                    session, selected_run_id, stage_name=stage_name,
                    source=None if source == "All" else source,
                    reason=None if reason == "All" else reason,
                )
            st.caption("This is a drill-down subset; the funnel totals above remain the official run totals.")
            st.dataframe(subset, width="stretch", hide_index=True)
        with get_session() as session:
            company_funnel = queries.decision_run_company_funnel(session, selected_run_id)
        st.subheader("Company funnel")
        render_company_funnel(st, company_funnel)
        if not company_funnel.empty:
            with st.expander("Inspect company-funnel records"):
                stage_name = st.selectbox(
                    "Company funnel transition", company_funnel["stage"].tolist(),
                    format_func=lambda value: str(value).replace("_", " ").title(),
                    key="company_funnel_stage",
                )
                with get_session() as session:
                    all_records = queries.decision_run_company_funnel_drilldown(
                        session, selected_run_id, stage_name=stage_name,
                    )
                sources = ["All"] + sorted(all_records["source"].dropna().unique().tolist())
                reasons = ["All"] + sorted(all_records["reason"].dropna().unique().tolist())
                selected_source, selected_reason = st.columns(2)
                source = selected_source.selectbox("Source", sources, key="company_funnel_source")
                reason = selected_reason.selectbox("Reason", reasons, key="company_funnel_reason")
                with get_session() as session:
                    subset = queries.decision_run_company_funnel_drilldown(
                        session, selected_run_id, stage_name=stage_name,
                        source=None if source == "All" else source,
                        reason=None if reason == "All" else reason,
                    )
                st.caption("This is a drill-down subset; the funnel totals above remain the official run totals.")
                st.dataframe(subset, width="stretch", hide_index=True)
        with get_session() as session:
            tailoring_funnel = queries.decision_run_resume_tailoring_funnel(session, selected_run_id)
        st.subheader("Resume tailoring")
        st.dataframe(tailoring_funnel, width="stretch", hide_index=True)
        with st.expander("Inspect resume-tailoring records"):
            with get_session() as session:
                tailoring_records = queries.decision_run_resume_tailoring_drilldown(session, selected_run_id)
            reasons = ["All"] + sorted(tailoring_records["reason"].dropna().unique().tolist())
            reason = st.selectbox("Reason", reasons, key="resume_tailoring_reason")
            with get_session() as session:
                subset = queries.decision_run_resume_tailoring_drilldown(
                    session, selected_run_id, reason=None if reason == "All" else reason,
                )
            st.caption("This is a drill-down subset; the funnel total above remains the official run total.")
            st.dataframe(subset, width="stretch", hide_index=True)
        with get_session() as session:
            contact_funnel = queries.decision_run_contact_funnel(session, selected_run_id)
        st.subheader("Contact-enrichment funnel")
        if contact_funnel.empty:
            st.info("Contact-enrichment telemetry is not available for this run yet.")
        else:
            st.dataframe(contact_funnel, width="stretch", hide_index=True)
            with st.expander("Inspect contact-enrichment coverage"):
                with get_session() as session:
                    all_contacts = queries.decision_run_contact_funnel_drilldown(session, selected_run_id)
                outcomes = ["All"] + sorted(all_contacts["outcome"].dropna().unique().tolist())
                reasons = ["All"] + sorted(all_contacts["reason"].dropna().unique().tolist())
                left, right = st.columns(2)
                outcome = left.selectbox("Outcome", outcomes, key="contact_funnel_outcome")
                reason = right.selectbox("Reason", reasons, key="contact_funnel_reason")
                with get_session() as session:
                    subset = queries.decision_run_contact_funnel_drilldown(
                        session, selected_run_id,
                        outcome=None if outcome == "All" else outcome,
                        reason=None if reason == "All" else reason,
                    )
                st.dataframe(subset, width="stretch", hide_index=True)
        with get_session() as session:
            outreach_funnel = queries.decision_run_outreach_funnel(session, selected_run_id)
        st.subheader("Draft and Gmail-Draft funnel")
        st.dataframe(outreach_funnel, width="stretch", hide_index=True)
        with st.expander("Inspect draft and Gmail-Draft records"):
            stage_name = st.selectbox(
                "Outreach transition", outreach_funnel["stage"].tolist(),
                format_func=lambda value: str(value).replace("_", " ").title(),
                key="outreach_funnel_stage",
            )
            with get_session() as session:
                records = queries.decision_run_outreach_funnel_drilldown(
                    session, selected_run_id, stage_name=stage_name,
                )
            st.dataframe(records, width="stretch", hide_index=True)


# --- Sidebar filters ---------------------------------------------------

def build_filters() -> DashboardFilters:
    st.sidebar.header("Filters")

    if st.sidebar.button("Refresh data"):
        st.cache_data.clear()

    with get_session() as session:
        sources = [r[0] for r in session.query(queries.Job.source).distinct().all()]
        seniorities = [
            r[0] for r in session.query(queries.Job.seniority).filter(
                queries.Job.seniority.isnot(None)
            ).distinct().all()
        ]
        skills = [
            r[0] for r in session.query(queries.JobSkill.skill).distinct().order_by(
                queries.JobSkill.skill
            ).all()
        ]

    source = st.sidebar.selectbox("Source", ["(all)"] + sorted(sources))
    source = None if source == "(all)" else source

    date_range = st.sidebar.date_input("First-seen date range", value=())
    date_from = date_to = None
    if isinstance(date_range, tuple) and len(date_range) == 2:
        date_from = datetime.combine(date_range[0], datetime.min.time())
        date_to = datetime.combine(date_range[1], datetime.max.time())
    elif isinstance(date_range, tuple) and len(date_range) == 1:
        # Only a start date picked so far (range mode returns a 1-tuple, not
        # a plain date, until the user also picks an end date).
        date_from = datetime.combine(date_range[0], datetime.min.time())
    elif isinstance(date_range, date):
        date_from = datetime.combine(date_range, datetime.min.time())

    enrichment_status = st.sidebar.selectbox(
        "Enrichment status", ["(all)", "pending", "done"]
    )
    enrichment_status = None if enrichment_status == "(all)" else enrichment_status

    company = st.sidebar.text_input("Company name contains") or None

    skill_search = st.sidebar.selectbox("Skill", ["(all)"] + skills)
    skill_search = None if skill_search == "(all)" else skill_search

    seniority = st.sidebar.selectbox("Seniority", ["(all)"] + sorted(seniorities))
    seniority = None if seniority == "(all)" else seniority

    remote_choice = st.sidebar.selectbox("Remote / on-site", ["(all)", "Remote", "On-site"])
    remote = {"(all)": None, "Remote": True, "On-site": False}[remote_choice]

    salary_range = st.sidebar.slider("Salary range (Lakhs/annum, INR)", 0, 100, (0, 100), step=5)
    salary_min_lpa = None if salary_range[0] == 0 else float(salary_range[0])
    salary_max_lpa = None if salary_range[1] == 100 else float(salary_range[1])
    st.sidebar.caption(
        "Only excludes jobs with a parseable INR salary figure confirmed outside this range; "
        "jobs with no salary listed or unparseable/other-currency text are always kept."
    )

    posted_range = st.sidebar.date_input("Posted-at date range", value=(), key="posted_range")
    posted_from = posted_to = None
    if isinstance(posted_range, tuple) and len(posted_range) == 2:
        posted_from = datetime.combine(posted_range[0], datetime.min.time())
        posted_to = datetime.combine(posted_range[1], datetime.max.time())
    elif isinstance(posted_range, tuple) and len(posted_range) == 1:
        posted_from = datetime.combine(posted_range[0], datetime.min.time())
    elif isinstance(posted_range, date):
        posted_from = datetime.combine(posted_range, datetime.min.time())

    category_options = ["(all)"] + list(CATEGORY_KEYWORDS.keys())
    job_category = st.sidebar.selectbox(
        "Job broad category", category_options, format_func=lambda c: JOB_CATEGORY_LABELS.get(c, c)
    )
    job_category = None if job_category == "(all)" else job_category

    min_total_contacts = st.sidebar.number_input(
        "Min total contacts (company)", min_value=0, value=0, step=1
    )
    min_total_contacts = None if min_total_contacts == 0 else int(min_total_contacts)

    min_head_hm_contacts = st.sidebar.number_input(
        "Min head + hiring_manager contacts (company)", min_value=0, value=0, step=1
    )
    min_head_hm_contacts = None if min_head_hm_contacts == 0 else int(min_head_hm_contacts)

    return DashboardFilters(
        source=source,
        date_from=date_from,
        date_to=date_to,
        enrichment_status=enrichment_status,
        company=company,
        skill_search=skill_search,
        seniority=seniority,
        remote=remote,
        salary_min_lpa=salary_min_lpa,
        salary_max_lpa=salary_max_lpa,
        posted_from=posted_from,
        posted_to=posted_to,
        job_category=job_category,
        min_total_contacts=min_total_contacts,
        min_head_hm_contacts=min_head_hm_contacts,
    )


def render_pipeline_health(filters: DashboardFilters) -> None:
    render_decision_run_health()
    render_live_connector_progress()

    st.subheader("Jobs per source")
    df = run("jobs_per_source", filters)
    if df.empty:
        st.info("No jobs match the current filters.")
    else:
        st.plotly_chart(px.bar(df, x="source", y="job_count"), width="stretch")

    st.subheader("Jobs ingested over time")
    df = run("jobs_over_time", filters)
    if df.empty:
        st.info("No jobs match the current filters.")
    else:
        st.plotly_chart(px.line(df, x="day", y="job_count"), width="stretch")

    st.subheader("Enrichment coverage")
    overall = run("enrichment_coverage_overall", filters)
    if not overall.empty:
        row = overall.iloc[0]
        st.metric(
            "Overall enrichment coverage",
            f"{row['pct_enriched']:.1f}%",
            help=f"{int(row['enriched'])} / {int(row['total'])} jobs enriched",
        )
    by_source = run("enrichment_coverage_by_source", filters)
    if by_source.empty:
        st.info("No jobs match the current filters.")
    else:
        st.plotly_chart(
            px.bar(by_source, x="source", y="pct_enriched", hover_data=["total", "enriched"]),
            width="stretch",
        )

    st.subheader("Field completeness by source")
    df = run("field_completeness_by_source", filters)
    if df.empty:
        st.info("No jobs match the current filters.")
    else:
        # A grouped bar chart is unreadable once there are dozens of sources
        # (36 sources x 7 fields = 252 bars) -- a heatmap reads as a matrix
        # instead, and scales to any number of sources.
        pivot = df.pivot(index="source", columns="field", values="pct_complete")
        totals = df.drop_duplicates("source").set_index("source")["total"]
        pivot = pivot.loc[totals.sort_values(ascending=False).index]
        pivot.index = [f"{src} (n={totals[src]})" for src in pivot.index]
        fig = px.imshow(
            pivot,
            color_continuous_scale="RdYlGn",
            zmin=0,
            zmax=100,
            text_auto=".0f",
            aspect="auto",
            labels=dict(color="% complete"),
        )
        fig.update_layout(height=max(400, 24 * len(pivot)))
        st.plotly_chart(fig, width="stretch")

    st.subheader("Company count over time")
    df = run("company_growth_over_time", filters)
    if df.empty:
        st.info("No company data available.")
    else:
        st.plotly_chart(px.line(df, x="day", y="cumulative_count"), width="stretch")


def render_market_insight(filters: DashboardFilters) -> None:
    st.subheader("Top hard skills")
    df = run("top_skills", filters, skill_type="hard", limit=TOP_SKILLS_POOL_SIZE)
    n = df.attrs.get("n", 0)
    st.caption(
        f"Based on {n} job(s) with at least one recorded hard skill. "
        f"Showing the top {TOP_SKILLS_DEFAULT_VISIBLE} by default — scroll/drag to zoom out "
        "(or use the range slider below the chart) to reveal rarer skills."
    )
    if df.empty:
        st.info("No enriched hard-skill data matches the current filters.")
    else:
        fig = px.bar(df, x="skill", y="count")
        fig.update_xaxes(range=[-0.5, TOP_SKILLS_DEFAULT_VISIBLE - 0.5], rangeslider_visible=True)
        st.plotly_chart(fig, width="stretch", config={"scrollZoom": True})

    st.subheader("Top soft skills")
    df = run("top_skills", filters, skill_type="soft", limit=TOP_SKILLS_POOL_SIZE)
    n = df.attrs.get("n", 0)
    st.caption(f"Based on {n} job(s) with at least one recorded soft skill.")
    if df.empty:
        st.info("No enriched soft-skill data matches the current filters.")
    else:
        st.plotly_chart(px.bar(df, x="skill", y="count"), width="stretch")

    st.subheader("Seniority distribution")
    df = run("seniority_distribution", filters)
    n = df.attrs.get("n", 0)
    st.caption(f"Based on {n} job(s) with a recorded seniority.")
    if df.empty:
        st.info("No enriched seniority data matches the current filters.")
    else:
        st.plotly_chart(px.pie(df, names="seniority", values="count"), width="stretch")

    st.subheader("Salary distribution (Lakhs/annum, INR)")
    df = run("salary_distribution", filters)
    n = df.attrs.get("n", 0)
    n_field = df.attrs.get("n_with_salary_field", 0)
    st.caption(
        f"Based on {n} job(s) with a parseable INR salary figure, out of {n_field} with any salary text "
        "(other currencies and non-numeric text like 'Competitive' are excluded)."
    )
    if n == 0:
        st.info("No parseable INR salary data matches the current filters.")
    else:
        st.plotly_chart(px.bar(df, x="band", y="count"), width="stretch")

    st.subheader("Top hiring companies")
    df = run("top_hiring_companies", filters)
    if df.empty:
        st.info("No jobs match the current filters.")
    else:
        st.plotly_chart(px.bar(df, x="company_name", y="job_count"), width="stretch")

    st.subheader("Industry breakdown")
    df = run("industry_breakdown", filters)
    n = df.attrs.get("n", 0)
    st.caption(f"Based on {n} compan(y/ies) with a recorded industry, grouped into broad categories.")
    if df.empty:
        st.info("No enriched industry data matches the current filters.")
    else:
        df = df.sort_values("company_count", ascending=True)
        st.plotly_chart(
            px.bar(df, x="company_count", y="industry", orientation="h"), width="stretch"
        )

    st.subheader("Remote vs on-site")
    df = run("remote_split", filters)
    if df.empty:
        st.info("No jobs match the current filters.")
    else:
        st.plotly_chart(px.pie(df, names="remote_status", values="count"), width="stretch")

    st.subheader("Company-wise distribution (jobs & contacts)")
    df = run("company_wise_distribution", filters)
    if df.empty:
        st.info("No jobs match the current filters.")
    else:
        st.dataframe(
            df.rename(columns={
                "company_name": "Company",
                "job_count": "Jobs",
                "head_hiring_manager_contacts": "Head + Hiring Manager contacts",
                "rest_contacts": "Rest contacts (IC/TA/exec)",
            }),
            width="stretch",
            hide_index=True,
        )

    st.subheader("Jobs by minimum required experience")
    df = run("experience_years_distribution", filters)
    n = df.attrs.get("n", 0)
    st.caption(f"Based on {n} job(s) with a recorded minimum experience.")
    if df.empty:
        st.info("No enriched experience data matches the current filters.")
    else:
        st.plotly_chart(
            px.bar(df, x="experience_min_years", y="count").update_xaxes(dtick=1),
            width="stretch",
        )


def render_explore(filters: DashboardFilters) -> None:
    st.subheader("Raw job rows")
    df = run("explore_jobs", filters)
    st.caption(f"Showing {len(df)} row(s) (capped at 500 on-screen).")
    st.dataframe(
        df,
        width="stretch",
        column_config={
            "job_url": st.column_config.LinkColumn("job_url", display_text="Open"),
            "apply_url": st.column_config.LinkColumn("apply_url", display_text="Apply"),
        },
    )
    all_jobs_df = run("explore_jobs", filters, limit=None)
    st.download_button(
        "Download all filtered jobs (CSV)",
        data=all_jobs_df.to_csv(index=False).encode("utf-8"),
        file_name="jobs_filtered.csv",
        mime="text/csv",
        help=f"All {len(all_jobs_df)} job(s) matching the current filters, not just the {len(df)} shown above.",
    )

    st.subheader("Companies")
    company_df = run("company_explore_table", filters)
    st.caption(f"Showing {len(company_df)} companies (capped at 500 on-screen), ordered by jobs posted.")
    st.dataframe(
        company_df,
        width="stretch",
        hide_index=True,
        row_height=100,
        column_config={
            "company_name": "Company",
            "job_count": "Jobs",
            "overall_rating": st.column_config.NumberColumn(
                "Overall rating", format="%.1f"
            ),
            "wlb_rating": st.column_config.NumberColumn("WLB", format="%.1f"),
            "estimated_salary_lpa": st.column_config.NumberColumn(
                "Estimated salary (LPA)", format="%.1f"
            ),
            "pain_points": st.column_config.TextColumn("Pain points", width="large"),
            "company_url": st.column_config.LinkColumn("Company URL", display_text="Visit"),
        },
    )
    all_companies_df = run("company_explore_table", filters, limit=None)
    st.download_button(
        "Download all filtered companies (CSV)",
        data=all_companies_df.to_csv(index=False).encode("utf-8"),
        file_name="companies_filtered.csv",
        mime="text/csv",
    )


def main() -> None:
    st.title("Job Agent Dashboard")
    filters = build_filters()

    tab_health, tab_market, tab_explore = st.tabs(
        ["Pipeline Health", "Job Market Insight", "Explore"]
    )
    with tab_health:
        render_pipeline_health(filters)
    with tab_market:
        render_market_insight(filters)
    with tab_explore:
        render_explore(filters)


if __name__ == "__main__":
    main()
