"""Small flow-style company funnel renderer; query semantics remain pure."""
import pandas as pd


def render_company_funnel(st, funnel: pd.DataFrame) -> None:
    if funnel.empty:
        st.info("Company-funnel telemetry is not available for this run yet.")
        return
    display = funnel.copy()
    for column in ("companies", "eligible_jobs", "advanced", "dropped", "failed", "pending", "duplicates",
                   "found", "confirmed_missing", "source_error", "group_1", "group_2", "group_3", "group_4", "ungroupable"):
        if column in display:
            display[column] = display[column].map(lambda value: "—" if pd.isna(value) else int(value))
    display["Companies / eligible jobs"] = display.apply(
        lambda row: f"{row['companies']} companies · {row['eligible_jobs']} eligible jobs", axis=1)
    columns = ["from", "to", "Companies / eligible jobs", "advanced", "dropped", "failed", "pending"]
    extras = [item for item in ("duplicates", "found", "confirmed_missing", "source_error", "group_1", "group_1_eligible_jobs", "group_2", "group_2_eligible_jobs", "group_3", "group_3_eligible_jobs", "group_4", "group_4_eligible_jobs", "ungroupable", "ungroupable_eligible_jobs") if item in display and display[item].notna().any()]
    st.dataframe(display[columns + extras].rename(columns={
        "from": "From", "to": "To", "advanced": "Advanced", "dropped": "Dropped", "failed": "Failed", "pending": "Pending",
        "duplicates": "Duplicate eligible jobs", "found": "Evidence found", "confirmed_missing": "Evidence confirmed missing", "source_error": "Source errors",
        "group_1": "Group 1 companies", "group_1_eligible_jobs": "Group 1 eligible jobs", "group_2": "Group 2 companies", "group_2_eligible_jobs": "Group 2 eligible jobs", "group_3": "Group 3 companies", "group_3_eligible_jobs": "Group 3 eligible jobs", "group_4": "Group 4 companies", "group_4_eligible_jobs": "Group 4 eligible jobs", "ungroupable": "Ungroupable companies", "ungroupable_eligible_jobs": "Ungroupable eligible jobs",
    }), width="stretch", hide_index=True)
