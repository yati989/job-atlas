from datetime import datetime, timezone

from app.models.schemas import NormalizedJob
from scripts import verify_source


class _Connector:
    def __init__(self, source_name: str, jobs: list[NormalizedJob]):
        self.source_name = source_name
        self._jobs = jobs

    def fetch(self):
        return self._jobs


def _job(source: str, external_id: str, posted_at):
    return NormalizedJob(
        source=source,
        external_job_id=external_id,
        title="Data Scientist",
        company_name_raw="Acme",
        description_raw="Build models",
        location_raw="Bengaluru",
        is_remote=False,
        posted_at=posted_at,
        apply_url=f"https://jobs.example/{external_id}",
    )


def test_active_source_scorecard_fails_when_any_posted_date_is_missing(monkeypatch):
    jobs = [
        _job("linkedin", "dated", datetime.now(timezone.utc)),
        _job("linkedin", "undated", None),
    ]
    monkeypatch.setattr(verify_source, "_find_connectors", lambda _source: [
        _Connector("linkedin", jobs)
    ])

    card = verify_source.score("linkedin", max_instances=1)

    assert card["posted_at_known_pct"] == 50.0
    assert "posted date 50.0% < 100%" in card["verdict"]


def test_instahyre_scorecard_allows_unknown_posted_dates(monkeypatch):
    jobs = [_job("instahyre", "undated", None)]
    monkeypatch.setattr(verify_source, "_find_connectors", lambda _source: [
        _Connector("instahyre", jobs)
    ])

    card = verify_source.score("instahyre", max_instances=1)

    assert card["posted_at_known_pct"] == 0.0
    assert card["verdict"] == "PASS"
