"""Self-addressed completion reports for connector runs.

The report is deliberately derived from the atomic dashboard snapshot, rather
than from runner logs or a second aggregation path.  The one public interface
returns a renderable report; delivery is kept as a small best-effort adapter
so a completed collection run stays completed if Gmail is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
import logging
from typing import Callable

from app.pipeline.progress import run_elapsed_seconds, source_rows, total_source_row


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompletionEmail:
    subject: str
    plain_text: str
    html: str


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_completion_email(
    snapshot: dict, *, now: datetime | None = None
) -> CompletionEmail:
    """Render the dashboard's source aggregates as a self-email report."""
    status = str(snapshot.get("status", "completed"))
    run_id = str(snapshot.get("run_id", "unknown"))
    rows = source_rows(snapshot, now=now)
    jobs = sum(row["jobs_finished"] for row in rows)
    finished = sum(row["instances_finished"] for row in rows)
    total = sum(row["instances_total"] for row in rows)
    elapsed = _duration(run_elapsed_seconds(snapshot, now=now))
    command = snapshot.get("command") or "connector pipeline"
    table_rows = [
        *rows,
        total_source_row(
            rows,
            status=status,
            elapsed_seconds=run_elapsed_seconds(snapshot, now=now),
        ),
    ] if rows else []

    headers = (
        "Connector", "Status", "Elapsed", "Fetched", "Kept", "Kept %",
        "Drop role", "Drop seniority", "Drop location", "Drop recency",
        "Instances", "Succeeded", "Failed",
    )
    def outcome(value: int | None) -> str:
        return "—" if value is None else str(value)

    text_rows = [
        (
            row["source"], row["status"], _duration(row["elapsed_seconds"]),
            str(row["fetched"]), outcome(row["kept"]),
            "—" if row["kept_pct"] is None else f"{row['kept_pct']:.1f}%",
            outcome(row["drop_role"]), outcome(row["drop_seniority"]),
            outcome(row["drop_location"]), outcome(row["drop_recency"]),
            f"{row['instances_finished']}/{row['instances_total']}",
            str(row["instances_succeeded"]), str(row["instances_failed"]),
        )
        for row in table_rows
    ]
    widths = [max([len(header), *(len(row[index]) for row in text_rows)]) for index, header in enumerate(headers)]
    line = lambda values: " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))
    plain_table = "\n".join([line(headers), "-+-".join("-" * width for width in widths), *(line(row) for row in text_rows)])
    plain_text = (
        f"Connector run {status}\n\n"
        f"Run: {run_id}\nCommand: {command}\nElapsed: {elapsed}\n"
        f"Jobs fetched: {jobs}\nInstances finished: {finished}/{total}\n\n{plain_table}\n"
    )
    html_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in row) + "</tr>"
        for row in text_rows[:-1]
    ) or "<tr><td colspan=\"13\">No connector instances were selected.</td></tr>"
    html_total = (
        "<tr style=\"font-weight:bold;background:#f2f2f2\">"
        + "".join(f"<td>{escape(value)}</td>" for value in text_rows[-1])
        + "</tr>"
        if text_rows else ""
    )
    html = (
        f"<h2>Connector run {escape(status)}</h2>"
        f"<p><b>Run:</b> {escape(run_id)}<br><b>Command:</b> {escape(str(command))}<br>"
        f"<b>Elapsed:</b> {elapsed}<br><b>Jobs fetched:</b> {jobs}<br>"
        f"<b>Instances finished:</b> {finished}/{total}</p>"
        "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" "
        "style=\"border-collapse:collapse\"><thead><tr>"
        + "".join(f"<th>{header}</th>" for header in headers)
        + f"</tr></thead><tbody>{html_rows}</tbody><tfoot>{html_total}</tfoot></table>"
    )
    return CompletionEmail(
        subject=f"job_agent connector run {status} — {run_id}",
        plain_text=plain_text,
        html=html,
    )


def send_completion_email(snapshot: dict) -> bool:
    """Send one self-only report, without turning Gmail trouble into a run failure."""
    report = render_completion_email(snapshot)
    try:
        from app.outreach.gmail import get_service, send_self_report
        send_self_report(
            get_service(), subject=report.subject, body=report.plain_text,
            html_body=report.html,
        )
    except Exception:
        logger.exception("Connector completion email could not be sent")
        return False
    return True
