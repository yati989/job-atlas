"""
Standing leak-rate audit (issue #8 / ADR-0002 user story 19): read-only
regression guard that re-checks every stored job against the relevance gate
and reports how many/what % would leak through today.

A near-zero leak rate is the acceptance signal for the gate; run this after
each ingestion run to catch a bug or regression in the gate before it
accumulates in the data.

Usage:
    python -m scripts.audit_leak_rate
"""
from app.db.session import get_session
from app.models.orm import Job
from app.pipeline.relevance import AXIS_NAMES, first_failing_axis


def run() -> None:
    drop_counts: dict[str, int] = {name: 0 for name in AXIS_NAMES}
    non_bangalore_onsite = 0
    foreign_remote = 0
    leaked = 0

    with get_session() as session:
        jobs = session.query(Job).all()
        total = len(jobs)

        if total == 0:
            print("No jobs in the table.")
            return

        for job in jobs:
            axis = first_failing_axis(job)
            if axis is not None:
                leaked += 1
                drop_counts[axis] += 1
                if axis == "location":
                    combined = " ".join(
                        filter(None, [job.location_raw, job.remote_scope])
                    ).lower()
                    is_remote_job = bool(job.is_remote) or "remote" in combined
                    if "hybrid" in combined or not is_remote_job:
                        non_bangalore_onsite += 1
                    else:
                        foreign_remote += 1

    kept = total - leaked
    leak_pct = (leaked / total) * 100

    print(f"Total stored jobs: {total}")
    print(f"Kept (pass gate):  {kept}")
    print(f"Leaked (fail gate): {leaked} ({leak_pct:.1f}%)")
    print()
    print("Leak breakdown by first-failing axis:")
    for axis_name in AXIS_NAMES:
        count = drop_counts[axis_name]
        pct = (count / total) * 100
        print(f"  {axis_name:10s}: {count:5d} ({pct:.1f}%)")
    print()
    print(f"  of which title-off-role %:        {(drop_counts['role'] / total) * 100:.1f}%")
    print(f"  of which non-Bangalore onsite:     {non_bangalore_onsite}")
    print(f"  of which foreign-only remote:      {foreign_remote}")
    print(f"  of which stale (recency):          {drop_counts['recency']}")


if __name__ == "__main__":
    run()
