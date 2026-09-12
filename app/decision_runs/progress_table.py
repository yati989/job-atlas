"""Shared presentation model for the Decision Run stage summary table."""
from __future__ import annotations

import pandas as pd

from app.decision_runs.progress import StageName


def _elapsed(value: object) -> str:
    if value is None or pd.isna(value):
        return "—"
    total = max(0, int(float(value)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def stage_summary_table(table: pd.DataFrame) -> pd.DataFrame:
    """Return the exact stage table shared by the dashboard and self-email."""
    if "stage" not in table.columns or table.empty:
        return table.copy()
    summary = table.copy()
    # Connector collection can be visibly terminal before the recovery-safe
    # transaction closes the durable fetch stage and opens the ingestion
    # funnel stages.  Project that completed work at the presentation boundary
    # without weakening the atomic crash/retry invariant underneath it.
    fetch_collected = (
        (summary["stage"] == StageName.FETCH_JOBS.value)
        & (summary["status"] == "running")
        & summary["processed"].notna()
        & summary["expected"].notna()
        & (summary["processed"] == summary["expected"])
        & (summary["pending"] == 0)
    )
    summary.loc[fetch_collected, "status"] = "completed"
    summary.loc[fetch_collected, "reason"] = (
        "connector collection completed; durable funnel handoff in progress"
    )
    summary["Stage"] = summary["stage"].map(lambda value: str(value).replace("_", " ").title())
    summary["Status"] = summary["status"].map(lambda value: str(value).replace("_", " ").title())
    summary["Processed / expected"] = summary.apply(
        lambda row: "—" if pd.isna(row["processed"]) else f"{int(row['processed']):,} / {int(row['expected']):,}", axis=1,
    )
    summary["Active elapsed"] = summary["active_elapsed_seconds"].map(_elapsed)
    summary["Approval wait"] = summary["waiting_seconds"].map(_elapsed)
    summary["Last update"] = summary["updated_at"].map(
        lambda value: "—" if value is None or pd.isna(value) else value.isoformat()
    )
    summary["Selected companies"] = summary.apply(
        lambda row: "—" if pd.isna(row["selected_companies"]) else (
            f"{int(row['selected_companies'])} / {int(row['available_companies'])}"
        ), axis=1,
    )
    summary["Selected jobs"] = summary["selected_jobs"].map(
        lambda value: "—" if pd.isna(value) else int(value)
    )
    return summary[[
        "Stage", "Status", "Processed / expected", "advanced", "dropped", "failed", "pending",
        "Active elapsed", "Approval wait", "Last update", "Selected companies",
        "Selected jobs", "attempt", "reason",
    ]].rename(columns={
        "advanced": "Advanced", "dropped": "Dropped", "failed": "Failed", "pending": "Pending",
        "attempt": "Attempt", "reason": "Result",
    })


def project_completion_delivery(progress: pd.DataFrame) -> pd.DataFrame:
    """Project the final-report delivery currently being accepted by Gmail."""
    projected = progress.copy()
    final = projected["stage"] == StageName.FINAL_REPORT.value
    if final.any():
        projected.loc[final, "status"] = "completed"
        projected.loc[final, "processed"] = 1
        projected.loc[final, "expected"] = 1
        projected.loc[final, "input"] = 1
        projected.loc[final, "advanced"] = 1
        projected.loc[final, ["dropped", "failed", "pending"]] = 0
        projected.loc[final, "reason"] = (
            "final workbook created and completion email delivered"
        )
    return projected
