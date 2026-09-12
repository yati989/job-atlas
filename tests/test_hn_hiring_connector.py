from datetime import datetime, timedelta, timezone

from app.collectors.feed.hn_hiring import (
    SEARCH_PAGE_SIZE,
    THREAD_BOUNDARY_BUFFER_DAYS,
    HNHiringConnector,
    _parse_first_line,
)
from app.config.categories import RECENCY_WINDOW_DAYS


def _comment(
    comment_id: int,
    thread_id: int,
    text: str,
    *,
    age_days: int = 1,
    parent_id: int | None = None,
) -> dict:
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    return {
        "objectID": str(comment_id),
        "parent_id": thread_id if parent_id is None else parent_id,
        "comment_text": text,
        "created_at": created.isoformat(),
    }


def test_default_depth_uses_shared_recency_window():
    connector = HNHiringConnector()

    assert connector.max_age_days == RECENCY_WINDOW_DAYS


def test_thread_discovery_uses_boundary_buffer_paginates_and_excludes_wants(monkeypatch):
    connector = HNHiringConnector(max_age_days=60)
    cutoff = datetime(2026, 6, 15, tzinfo=timezone.utc)
    calls = []
    pages = {
        0: {
            "hits": [
                {"objectID": "3", "title": "Ask HN: Who is hiring? (August 2026)"},
                {"objectID": "2", "title": "Ask HN: Who wants to be hired? (August 2026)"},
            ],
            "nbPages": 2,
        },
        1: {
            "hits": [{"objectID": "1", "title": "Ask HN: Who is hiring? (July 2026)"}],
            "nbPages": 2,
        },
    }

    def search(params):
        calls.append(params)
        return pages[params["page"]]

    monkeypatch.setattr(connector, "_search_page", search)

    assert connector._find_recent_thread_ids(cutoff) == ["3", "1"]
    expected_thread_cutoff = cutoff - timedelta(days=THREAD_BOUNDARY_BUFFER_DAYS)
    assert calls[0]["numericFilters"] == (
        f"created_at_i>={int(expected_thread_cutoff.timestamp())}"
    )
    assert calls[0]["hitsPerPage"] == SEARCH_PAGE_SIZE
    assert [call["page"] for call in calls] == [0, 1]


def test_fetch_keeps_only_fresh_top_level_remote_or_india_jobs(monkeypatch):
    connector = HNHiringConnector(max_age_days=60)
    monkeypatch.setattr(connector, "_find_recent_thread_ids", lambda _cutoff: ["10", "20"])
    batches = {
        "10": [
            _comment(
                101,
                10,
                "Acme | Data Scientist | Remote (Worldwide)<p>Build models.</p>",
            ),
            _comment(102, 10, "Reply with no job", parent_id=101),
            _comment(103, 10, "Old | Data Engineer | Remote", age_days=61),
            _comment(104, 10, "LocalCo | Data Engineer | Bengaluru"),
            _comment(105, 10, "ForeignCo | Data Analyst | London"),
        ],
        "20": [
            _comment(101, 20, "Duplicate | Data Scientist | Remote"),
        ],
    }
    monkeypatch.setattr(
        connector,
        "_fetch_recent_comments",
        lambda thread_id, _cutoff: batches[thread_id],
    )

    jobs = connector.fetch()

    assert [job.external_job_id for job in jobs] == ["101", "104"]
    assert jobs[0].title == "Data Scientist | Remote (Worldwide)"
    assert jobs[0].company_name_raw == "Acme"
    assert jobs[0].location_raw == "Remote (Worldwide)"
    assert jobs[0].is_remote is True
    assert jobs[0].description_raw == "Acme | Data Scientist | Remote (Worldwide)\nBuild models."
    assert jobs[1].location_raw == "Bengaluru"
    assert jobs[1].is_remote is False


def test_comment_search_uses_exact_cutoff_and_exhausts_metadata(monkeypatch):
    connector = HNHiringConnector(max_age_days=60)
    cutoff = datetime(2026, 6, 15, tzinfo=timezone.utc)
    calls = []

    def search(params):
        calls.append(params)
        return {
            "hits": [{"objectID": str(params["page"])}],
            "nbPages": 2,
        }

    monkeypatch.setattr(connector, "_search_page", search)

    assert connector._fetch_recent_comments("99", cutoff) == [
        {"objectID": "0"},
        {"objectID": "1"},
    ]
    assert [call["page"] for call in calls] == [0, 1]
    assert all(call["tags"] == "comment,story_99" for call in calls)
    assert all(
        call["numericFilters"] == f"created_at_i>={int(cutoff.timestamp())}"
        for call in calls
    )


def test_pipe_parser_preserves_role_when_location_precedes_it():
    title, company, location = _parse_first_line(
        "Acme | Bengaluru / Remote | Machine Learning Engineer | Full-time"
    )

    assert company == "Acme"
    assert title == "Bengaluru / Remote | Machine Learning Engineer | Full-time"
    assert location == "Bengaluru / Remote"
