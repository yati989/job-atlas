import json
from datetime import datetime, timedelta, timezone

import app.collectors.html.cutshort as cutshort_module
from app.collectors.html.cutshort import (
    CUTSHORT_MAX_RESULTS,
    CUTSHORT_PAGE_SIZE,
    CUTSHORT_ROLE_TYPE,
    CUTSHORT_TAGS,
    CutshortConnector,
)
from app.pipeline.registry import ACTIVE_CONNECTORS
from app.pipeline.window import apply_sync_window
from scripts.run_headed_sources import CONNECTORS as HEADED_CONNECTORS


def _item(job_id: int) -> dict:
    return {
        "short_id": str(job_id),
        "headline": "Data Scientist",
        "company": "Acme",
        "locationsText": "Bengaluru",
        "remoteRole": True,
        "remoteType": "Remote",
        "roleTypes": ["Full time"],
        "salaryRangeText": "₹20L - ₹30L",
        "creationDate": "2026-08-01T00:00:00Z",
        "publicUrl": f"https://cutshort.io/job/data-scientist-{job_id}",
        "sanitizedComment": "<p>Build forecasting models</p>",
    }


def test_cutshort_moved_to_http_registry():
    active = [c for c in ACTIVE_CONNECTORS if c.source_name == "cutshort"]
    headed = [c for c in HEADED_CONNECTORS if c.source_name == "cutshort"]
    assert len(active) == 1
    assert not headed
    assert active[0].page_size == CUTSHORT_PAGE_SIZE == 1_000
    assert active[0].max_results == CUTSHORT_MAX_RESULTS == 1_000


def test_cutshort_sends_verified_role_and_creation_date_filters(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [], "total_count": 0}

    class FakeSession:
        def get(self, url, *, params, timeout):
            captured.update(url=url, params=params, timeout=timeout)
            return FakeResponse()

    monkeypatch.setattr(
        cutshort_module,
        "_session_from_storage",
        lambda _path: FakeSession(),
    )

    connector = CutshortConnector()
    run_start = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
    apply_sync_window(
        [connector],
        since_at=run_start - timedelta(hours=25),
        cutoff_at=run_start,
    )
    connector._fetch_page(1)

    assert CUTSHORT_ROLE_TYPE == "full_time"
    assert CUTSHORT_TAGS == (
        "data_science-data_analytics-ml_engineering-"
        "others_data_science_analytics-big_data"
    )
    assert captured["params"] == {
        "page": 1,
        "pageSize": 1_000,
        "roletype": CUTSHORT_ROLE_TYPE,
        "tags": CUTSHORT_TAGS,
        "creationDate": "2-days",
    }


def test_cutshort_uses_total_count_instead_of_stale_total_pages(monkeypatch):
    connector = CutshortConnector(max_results=1_000, page_size=250)
    calls = []

    def fake_fetch(page):
        calls.append(page)
        start = (page - 1) * 250
        return {
            "results": [_item(start + i) for i in range(250)],
            "total_count": 3_531,
            "totalPages": 707,
        }

    monkeypatch.setattr(connector, "_fetch_page", fake_fetch)
    assert len(connector.fetch()) == 1_000
    assert sorted(calls) == [1, 2, 3, 4]


def test_cutshort_normalizes_embedded_description_and_json_safe_payload():
    job = CutshortConnector._normalize(_item(7))
    assert job is not None
    assert job.description_raw == "Build forecasting models"
    assert job.is_remote is True
    assert job.remote_scope == "Remote"
    assert job.employment_type == "Full time"
    assert job.posted_at.isoformat() == "2026-08-01T00:00:00+00:00"
    json.dumps(job.raw_payload)

    onsite_item = _item(8)
    onsite_item.update(
        remoteRole=False,
        remoteType="remote_not_okay",
        locationsText="Bengaluru (Bangalore)",
    )
    onsite = CutshortConnector._normalize(onsite_item)
    assert onsite is not None
    assert onsite.is_remote is False
