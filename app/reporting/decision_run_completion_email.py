"""Render a Decision Run completion email from the dashboard read model."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

import pandas as pd

from app.decision_runs.progress_table import (
    project_completion_delivery,
    stage_summary_table,
)


@dataclass(frozen=True)
class DecisionRunCompletionEmail:
    subject: str
    plain_text: str
    html: str


def _value(value: object) -> str:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return "—"
    return str(value)


def render_decision_run_completion_email(
    run: object,
    progress: pd.DataFrame,
    *,
    project_delivery_success: bool = False,
) -> DecisionRunCompletionEmail:
    """Render the durable Decision Run stage summary shown by the dashboard.

    ``project_delivery_success`` is used only after the workbook exists and a
    committed Gmail delivery intent has been recorded. The recipient can only
    observe that projection if Gmail accepts the message, after which the
    durable final stage is completed to the same state.
    """
    if project_delivery_success:
        progress = project_completion_delivery(progress)
    summary = stage_summary_table(progress)
    run_id = str(getattr(run, "id"))
    since_at = getattr(run, "since_at", None)
    cutoff_at = getattr(run, "cutoff_at", None)
    selected_companies = progress["selected_companies"].dropna()
    selected_jobs = progress["selected_jobs"].dropna()
    company_count = int(selected_companies.iloc[-1]) if not selected_companies.empty else 0
    job_count = int(selected_jobs.iloc[-1]) if not selected_jobs.empty else 0
    stage_counts = progress["status"].value_counts().to_dict()
    completed = int(stage_counts.get("completed", 0))
    completed_with_errors = int(stage_counts.get("completed_with_errors", 0))
    failed = int(stage_counts.get("failed", 0))

    headers = list(summary.columns)
    rows = [[_value(value) for value in row] for row in summary.itertuples(index=False, name=None)]
    plain_table = summary.to_string(index=False) if not summary.empty else "No durable stage telemetry."
    decision_plain_text = (
        f"Decision Run completed\n\nRun: {run_id}\n"
        f"Window: {since_at.isoformat() if since_at else '—'} -> {cutoff_at.isoformat() if cutoff_at else '—'}\n"
        f"Stages: {completed} completed, {completed_with_errors} completed with errors, {failed} failed\n"
        f"Approved scope: {company_count} companies, {job_count} jobs\n\n{plain_table}\n"
    )
    html_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in row) + "</tr>"
        for row in rows
    ) or f'<tr><td colspan="{len(headers)}">No durable stage telemetry.</td></tr>'
    decision_html = (
        "<h2>Decision Run completed</h2>"
        f"<p><b>Run:</b> {escape(run_id)}<br>"
        f"<b>Window:</b> {escape(since_at.isoformat() if since_at else '—')} &rarr; "
        f"{escape(cutoff_at.isoformat() if cutoff_at else '—')}<br>"
        f"<b>Stages:</b> {completed} completed, {completed_with_errors} completed with errors, "
        f"{failed} failed<br><b>Approved scope:</b> {company_count} companies, {job_count} jobs</p>"
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
        "<thead><tr>"
        + "".join(f"<th>{escape(header)}</th>" for header in headers)
        + f"</tr></thead><tbody>{html_rows}</tbody></table>"
    )
    return DecisionRunCompletionEmail(
        subject=f"job_agent Decision Run completed — {run_id}",
        plain_text=decision_plain_text,
        html=decision_html,
    )
