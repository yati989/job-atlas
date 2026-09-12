from datetime import datetime, timezone

from app.models.orm import Job
from scripts.dedup_jobs import _posted_at_close_enough


def test_posted_at_proximity_accepts_mixed_sqlite_timezone_representations():
    same_instant_naive = datetime(2026, 9, 10, 12, 0)
    same_instant_aware = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    job_a = Job(posted_at=same_instant_naive)
    job_b = Job(posted_at=same_instant_aware)

    assert _posted_at_close_enough(job_a, job_b) is True
