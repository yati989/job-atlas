from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from app.reporting.decision_run_completion_email import (
    render_decision_run_completion_email,
)


def test_completion_email_embeds_the_dashboard_stage_table_in_html():
    run = SimpleNamespace(
        id="run-42",
        since_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    progress = pd.DataFrame([
        {
            "stage": "approval", "status": "completed", "processed": 3,
            "expected": 3, "input": 3, "advanced": 2, "dropped": 1,
            "failed": 0, "pending": 0, "active_elapsed_seconds": 12,
            "waiting_seconds": 3600, "updated_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
            "selected_companies": 2, "available_companies": 3,
            "selected_jobs": 2, "attempt": 1, "reason": "named approval",
        },
        {
            "stage": "final_report", "status": "running", "processed": 0,
            "expected": 1, "input": 1, "advanced": 0, "dropped": 0,
            "failed": 0, "pending": 1, "active_elapsed_seconds": 2,
            "waiting_seconds": None, "updated_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
            "selected_companies": None, "available_companies": None,
            "selected_jobs": None, "attempt": 1, "reason": "building workbook",
        },
    ])

    email = render_decision_run_completion_email(
        run, progress, project_delivery_success=True,
    )

    assert email.subject == "job_agent Decision Run completed — run-42"
    assert "Approved scope: 2 companies, 2 jobs" in email.plain_text
    assert "<th>Approval wait</th>" in email.html
    assert "<td>Approval</td>" in email.html
    assert "<td>01:00:00</td>" in email.html
    assert "<td>Final Report</td>" in email.html
    assert "<td>Completed</td>" in email.html
    assert "Connector run" not in email.html
    assert "<th>Drop role</th>" not in email.html
