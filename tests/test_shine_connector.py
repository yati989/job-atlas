from datetime import datetime, timedelta, timezone

import pytest

from app.collectors.html.shine import PAGE_SIZE, ShineConnector, _parse_posted


def _row(job_id: int, title: str = "Data Scientist", **overrides):
    row = {
        "id": job_id,
        "jJT": title,
        "jCName": "Example Co",
        "jLoc": ["Bangalore"],
        "jPDate": datetime.now(timezone.utc).isoformat(),
        "jSlug": f"data-scientist/example/{job_id}",
        "jJD": "<p>Build models</p>",
        "jSal": "[Salary Hidden]",
        "jEType": 1,
        "jWM": 0,
    }
    row.update(overrides)
    return row


def test_verified_params_and_closed_location_modes():
    india = ShineConnector("ai engineer", location_mode="india")
    assert india._params(2) == {
        "q": "artificial intelligence engineer jobs",
        "page": 2,
        "perpage": PAGE_SIZE,
    }

    bangalore = ShineConnector("data scientist", location_mode="bangalore")
    assert bangalore._params(1) == {
        "q": "data scientist jobs in bangalore",
        "page": 1,
        "perpage": PAGE_SIZE,
        "loc": "Bangalore",
    }

    with pytest.raises(ValueError):
        ShineConnector("data scientist", location_mode="invalid")


def test_city_query_and_remote_broad_feed_preserve_location_evidence(monkeypatch):
    city = ShineConnector("data analyst", location_mode="city", location="Pune")
    assert city._params(1)["loc"] == "Pune"
    assert city._params(1)["q"] == "data analyst jobs in Pune"
    remote = ShineConnector("data analyst", location_mode="remote")
    monkeypatch.setattr(remote, "_fetch_page", lambda page: {
        "results": [_row(1, "Data Analyst", jLoc=["Pune"])], "next": None,
    })
    assert not remote.fetch()[0].is_remote


def test_api_pagination_uses_distinct_pages_until_relevance_drift(monkeypatch):
    connector = ShineConnector("data scientist", max_results=300)
    calls = []

    def fake_fetch(page):
        calls.append(page)
        if page == 1:
            return {"results": [_row(i) for i in range(1, 51)], "next": "page-2"}
        return {
            "results": [_row(i, title="Office Administrator") for i in range(51, 101)],
            "next": "page-3",
        }

    monkeypatch.setattr(connector, "_fetch_page", fake_fetch)
    jobs = connector.fetch()

    assert calls == [1, 2]
    # Six off-slice rows make the trailing ten-row window 40% relevant,
    # below the shared 50% continuation threshold.
    assert len(jobs) == 56
    assert len({job.external_job_id for job in jobs}) == 56


def test_full_row_mapping_preserves_real_fields(monkeypatch):
    connector = ShineConnector("data scientist")
    row = _row(
        7,
        title="Remote Data Scientist",
        jLoc=["Bangalore", "All India"],
        jJD="<p>Build<br>models</p>",
        jSal="Rs 20 - 25 Lakh/Yr",
        jEType=4,
        jExp="3 to 5 Years",
        jInd="IT Services",
        jKwd="Python, Machine Learning",
    )
    monkeypatch.setattr(connector, "_fetch_page", lambda page: {"results": [row], "next": None})

    [job] = connector.fetch()

    assert job.location_raw == "Bangalore, All India"
    assert job.description_raw == "Build\nmodels"
    assert job.salary_raw == "Rs 20 - 25 Lakh/Yr"
    assert job.employment_type == "Work from home"
    assert job.is_remote is True
    assert job.remote_scope == "Work from home"
    assert job.raw_payload["experience_scraped"] == "3 to 5 Years"
    assert job.raw_payload["skills_scraped"] == ["Python", "Machine Learning"]


def test_stale_rows_are_filtered_but_unknown_dates_are_kept(monkeypatch):
    connector = ShineConnector("data scientist", freshness_days=60)
    stale = (datetime.now(timezone.utc) - timedelta(days=61)).isoformat()
    rows = [_row(1, jPDate=stale), _row(2, jPDate="not-a-date")]
    monkeypatch.setattr(connector, "_fetch_page", lambda page: {"results": rows, "next": None})

    jobs = connector.fetch()

    assert [job.external_job_id for job in jobs] == ["2"]
    assert jobs[0].posted_at is None


def test_parse_posted_normalizes_naive_and_aware_values():
    assert _parse_posted("2026-08-13T10:00:00").tzinfo == timezone.utc
    assert _parse_posted("2026-08-13T10:00:00Z").tzinfo == timezone.utc
    assert _parse_posted("bad") is None
