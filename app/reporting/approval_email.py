"""Safe, compact approval summaries for durable decision-run self reports."""
from collections import Counter
from dataclasses import dataclass
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import DecisionRun, DecisionRunCompany, DecisionRunJob


_GROUP_DESCRIPTIONS = {
    1: "Remote roles with salary at or above ₹20 LPA, or salary still unknown.",
    2: "Onsite or hybrid roles with salary at or above ₹20 LPA, or salary still unknown.",
    3: "Remote roles with known salary below ₹20 LPA.",
    4: "Onsite or hybrid roles with known salary below ₹20 LPA.",
}


@dataclass(frozen=True)
class ApprovalEmail:
    """The two alternatives supplied to Gmail's multipart message."""

    text_body: str
    html_body: str


def _label(value: str | None) -> str:
    return (value or "unknown").replace("_", " ")


def _counts_text(counts: Counter) -> str:
    return ", ".join(f"{_label(str(key))}: {value}" for key, value in sorted(counts.items())) or "none"


def _count(value: int, singular: str) -> str:
    return f"{value} {singular if value == 1 else singular + 's'}"


def render_approval_email(session: Session, run_id: str, spreadsheet_url: str) -> ApprovalEmail:
    """Build a plain-text and escaped HTML overview from immutable snapshots."""
    run = session.get(DecisionRun, run_id)
    if run is None:
        raise ValueError(f"unknown run {run_id}")
    jobs = list(session.execute(select(DecisionRunJob).where(DecisionRunJob.run_id == run_id)).scalars())
    companies = list(session.execute(select(DecisionRunCompany).where(DecisionRunCompany.run_id == run_id)).scalars())
    outcomes = Counter(job.outcome for job in jobs)
    roles = Counter(job.role_family or "unknown" for job in jobs if job.outcome == "eligible")

    header = (
        f"Decision run {run.id} is awaiting your approval.\n"
        f"Window: {run.since_at.isoformat()} to {run.cutoff_at.isoformat()} ({run.input_timezone})\n"
        f"State: {run.state}\n"
        f"All outcomes: {_counts_text(outcomes)}\n"
        f"Eligible role families: {_counts_text(roles)}\n\n"
        f"Approval Google Sheet: {spreadsheet_url}\n"
        "Open the first populated Group tab and use "
        "Column A (company_approval) to set each company to Approved or Rejected. The adjacent "
        "contact_search_selection column controls posting-derived or explicit contact roles. Save it, "
        "Group 1 defaults to Approved; Groups 2-4 default to Rejected. Save it, then reply in this "
        "email thread when your edits are ready. You do not need to attach or paste anything.\n"
    )
    text_groups: list[str] = []
    html_groups: list[str] = []
    for group in range(1, 5):
        group_jobs = [job for job in jobs if job.group_number == group and job.outcome == "eligible"]
        group_companies = [company for company in companies if company.group_number == group]
        known = sum(job.effective_salary_lpa is not None for job in group_jobs)
        unknown = len(group_jobs) - known
        role_counts = Counter(job.role_family or "unknown" for job in group_jobs)
        details = (
            f"{_count(len(group_companies), 'company')}; {_count(len(group_jobs), 'eligible job')}; "
            f"salary known: {known}; salary unknown: {unknown}; "
            f"roles: {_counts_text(role_counts)}"
        )
        text_groups.append(f"Group {group} — {_GROUP_DESCRIPTIONS[group]}\n{details}")
        html_groups.append(
            "<section>"
            f"<h2>Group {group}</h2>"
            f"<p>{escape(_GROUP_DESCRIPTIONS[group])}</p>"
            "<ul>"
            f"<li>Companies: {len(group_companies)}</li>"
            f"<li>Eligible jobs: {len(group_jobs)}</li>"
            f"<li>Salary known: {known}; unknown: {unknown}</li>"
            f"<li>Role families: {escape(_counts_text(role_counts))}</li>"
            "</ul></section>"
        )

    html = (
        "<html><body>"
        f"<p>Decision run <strong>{escape(run.id)}</strong> is awaiting your approval.</p>"
        "<ul>"
        f"<li>Window: {escape(run.since_at.isoformat())} to {escape(run.cutoff_at.isoformat())} ({escape(run.input_timezone)})</li>"
        f"<li>State: {escape(run.state)}</li>"
        f"<li>All outcomes: {escape(_counts_text(outcomes))}</li>"
        f"<li>Eligible role families: {escape(_counts_text(roles))}</li>"
        f'</ul><p><a href="{escape(spreadsheet_url, quote=True)}"><strong>Open the approval Google Sheet</strong></a>. '
        "Open the first populated Group tab and use <strong>Column A (company_approval)</strong> to set each company "
        "to <strong>Approved</strong> or <strong>Rejected</strong>. The adjacent "
        "<strong>contact_search_selection</strong> column controls posting-derived or explicit "
        "contact roles. Group 1 defaults to <strong>Approved</strong>; Groups 2-4 default to "
        "<strong>Rejected</strong>. When your edits are ready, reply in this email thread. "
        "You do not need to attach or paste anything; the pipeline reads this exact Sheet.</p>"
        + "".join(html_groups)
        + "</body></html>"
    )
    return ApprovalEmail(text_body=header + "\n\n".join(text_groups), html_body=html)
