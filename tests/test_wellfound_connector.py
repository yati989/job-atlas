from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import requests
import pytest

from app.collectors.base import PartialFetchError
from app.collectors.html.wellfound import (
    ROLE_SLUGS,
    WELLFOUND_MAX_RESULTS,
    WELLFOUND_MIN_REQUEST_INTERVAL_S,
    WELLFOUND_PAGE_WORKERS,
    WELLFOUND_SOURCE_MAX_REQUESTS,
    WellfoundCircuitOpen,
    WellfoundRequestCoordinator,
    WellfoundConnector,
)
from app.config.categories import RECENCY_WINDOW_DAYS, SEARCH_TERMS
from app.pipeline.registry import ACTIVE_CONNECTORS
from app.pipeline.runner import fetch_all, retry_failed
from scripts import verify_source
from scripts.run_headed_sources import CONNECTORS as HEADED_CONNECTORS


def _job(job_id: int, *, remote: bool = False) -> dict:
    return {
        "id": str(job_id),
        "slug": "data-scientist",
        "title": "Data Scientist",
        "company": "Acme",
        "description": "Build machine-learning models",
        "jobType": "full-time",
        "liveStartAt": 1_786_579_200,
        "locationNames": ["Bengaluru"],
        "remote": remote,
        "remoteConfig": {"kind": "REMOTE" if remote else "ONSITE"},
        "acceptedRemoteLocationNames": ["India"] if remote else [],
        "compensation": "₹20L – ₹30L",
    }


def _response(
    jobs: list[dict],
    *,
    page_count: int = 1,
    status: int = 200,
    headers: dict[str, str] | None = None,
):
    data = {
        "ROOT_QUERY": {
            "talent": {
                'seoLandingPageJobSearchResults({"page":1})': {
                    "pageCount": page_count,
                    "startups": [{"__ref": "Startup:1"}],
                }
            }
        },
        "Startup:1": {
            "name": "Acme",
            "highlightedJobListings": [
                {"__ref": f"Job:{item['id']}"} for item in jobs
            ],
        },
        **{f"Job:{item['id']}": item for item in jobs},
    }
    response = requests.Response()
    response.status_code = status
    response.url = "https://wellfound.com/role/r/data-scientist"
    response.headers.update(headers or {})
    response._content = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"apolloState": {"data": data}}}})
        + "</script>"
    ).encode()
    return response


def test_wellfound_fetches_share_bounded_request_coordination(monkeypatch):
    defaults = [
        WellfoundConnector("data scientist", "remote_india") for _ in range(2)
    ]
    assert defaults[0].request_coordinator is defaults[1].request_coordinator

    elapsed = 0.0
    coordinator = WellfoundRequestCoordinator(
        max_in_flight=1,
        min_interval_s=2,
        clock=lambda: elapsed,
        sleeper=lambda seconds: _advance(seconds),
    )
    active = 0
    max_active = 0
    starts: list[float] = []
    calls = 0
    lock = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()

    def _advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def fake_get(*_args, **_kwargs):
        nonlocal active, calls, max_active
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
            starts.append(elapsed)
            call_number = calls
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=1)
        with lock:
            active -= 1
        return _response([_job(call_number, remote=True)])

    monkeypatch.setattr(requests, "get", fake_get)
    connectors = [
        WellfoundConnector(
            "data scientist",
            "remote_india",
            request_coordinator=coordinator,
        )
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connector.fetch) for connector in connectors]
        assert first_entered.wait(timeout=1)
        with lock:
            assert calls == 1
            assert active == 1
        release_first.set()
        results = [future.result() for future in futures]

    assert [len(result) for result in results] == [1, 1]
    assert max_active == 1
    assert starts == [0.0, 2.0]
    assert connectors[0].request_observability() == {
        "http_requests_started": 2,
        "max_in_flight_limit": 1,
        "max_in_flight_observed": 1,
        "cooldown_events": 0,
        "cooldown_committed_s": 0.0,
        "circuit_open": False,
    }


@pytest.mark.parametrize(
    ("retry_after", "expected_wait"),
    [
        ("3", 3.0),
        ("Fri, 14 Aug 2026 12:00:05 GMT", 5.0),
        ("not-a-delay", 2.0),
    ],
)
def test_wellfound_fetch_honors_retry_after_and_bounded_fallback(
    monkeypatch, retry_after, expected_wait
):
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    elapsed = 100.0
    sleeps: list[float] = []
    responses = [
        _response([], status=429, headers={"Retry-After": retry_after}),
        _response([_job(1, remote=True)]),
    ]

    def clock():
        return elapsed

    def sleep(seconds):
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    coordinator = WellfoundRequestCoordinator(
        min_interval_s=0,
        max_source_wait_s=10,
        clock=clock,
        sleeper=sleep,
        utcnow=lambda: now,
    )
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: responses.pop(0))

    jobs = WellfoundConnector(
        "data scientist",
        "remote_india",
        request_coordinator=coordinator,
    ).fetch()

    assert [job.external_job_id for job in jobs] == ["1"]
    assert sleeps == [expected_wait]


def test_wellfound_fetch_bounds_retries_and_opens_circuit_without_long_sleep(
    monkeypatch,
):
    calls = 0
    sleeps: list[float] = []

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _response([], status=429, headers={"Retry-After": "120"})

    monkeypatch.setattr(requests, "get", fake_get)
    coordinator = WellfoundRequestCoordinator(
        min_interval_s=0,
        max_attempts=3,
        max_source_wait_s=10,
        sleeper=sleeps.append,
    )

    with pytest.raises(WellfoundCircuitOpen):
        WellfoundConnector(
            "data scientist",
            "remote_india",
            request_coordinator=coordinator,
        ).fetch()

    assert calls == 1
    assert sleeps == []
    assert coordinator.snapshot()["circuit_open"] is True
    assert coordinator.snapshot()["cooldown_events"] == 1


def test_wellfound_runner_retry_recovers_every_instance_after_circuit_cooldown(
    monkeypatch,
):
    elapsed = 0.0
    calls = 0

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response([], status=429, headers={"Retry-After": "120"})
        return _response([_job(calls, remote=True)])

    coordinator = WellfoundRequestCoordinator(
        max_in_flight=1,
        min_interval_s=0,
        max_source_wait_s=30,
        clock=lambda: elapsed,
        sleeper=lambda _seconds: None,
    )
    monkeypatch.setattr(requests, "get", fake_get)
    connectors = [
        WellfoundConnector(
            "data scientist",
            location_mode,
            request_coordinator=coordinator,
        )
        for location_mode in ("bengaluru", "remote_india")
    ]

    results = fetch_all(connectors)
    assert all(result.error is not None for result in results)

    elapsed = 120.0
    retry_failed(results)

    assert all(result.error is None for result in results)
    assert all(result.retry_succeeded is True for result in results)
    assert coordinator.snapshot()["circuit_open"] is False


def test_wellfound_fetch_caps_missing_header_retries(monkeypatch):
    calls = 0
    elapsed = 50.0
    sleeps: list[float] = []

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _response([], status=429)

    def sleep(seconds):
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    monkeypatch.setattr(requests, "get", fake_get)
    coordinator = WellfoundRequestCoordinator(
        min_interval_s=0,
        max_attempts=3,
        max_source_wait_s=14,
        clock=lambda: elapsed,
        sleeper=sleep,
    )

    with pytest.raises(requests.HTTPError):
        WellfoundConnector(
            "data scientist",
            "remote_india",
            request_coordinator=coordinator,
        ).fetch()

    assert calls == 3
    assert sleeps == [2.0, 4.0]


def test_wellfound_cooldown_does_not_block_unrelated_connector(monkeypatch):
    unrelated_done = threading.Event()
    elapsed = 0.0
    responses = [
        _response([], status=429, headers={"Retry-After": "2"}),
        _response([_job(1, remote=True)]),
    ]

    def sleep(seconds):
        nonlocal elapsed
        assert unrelated_done.wait(timeout=1)
        elapsed += seconds

    coordinator = WellfoundRequestCoordinator(
        min_interval_s=0,
        max_source_wait_s=5,
        clock=lambda: elapsed,
        sleeper=sleep,
    )
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: responses.pop(0))

    class UnrelatedConnector:
        source_name = "unrelated"

        def fetch(self):
            unrelated_done.set()
            return []

    results = fetch_all(
        [
            WellfoundConnector(
                "data scientist",
                "remote_india",
                request_coordinator=coordinator,
            ),
            UnrelatedConnector(),
        ]
    )

    assert unrelated_done.is_set()
    assert all(result.error is None for result in results)


def test_wellfound_fetch_retains_completed_pages_on_terminal_failure(monkeypatch):
    coordinator = WellfoundRequestCoordinator(
        max_in_flight=1,
        min_interval_s=0,
        max_attempts=1,
        max_source_wait_s=0,
    )

    def fake_get(url, **_kwargs):
        if "page=2" in url:
            return _response([], page_count=2, status=429)
        return _response([_job(1, remote=True)], page_count=2)

    monkeypatch.setattr(requests, "get", fake_get)
    connector = WellfoundConnector(
        "data scientist",
        "remote_india",
        page_workers=2,
        request_coordinator=coordinator,
    )

    with pytest.raises(PartialFetchError) as captured:
        connector.fetch()

    assert [job.external_job_id for job in captured.value.jobs] == ["1"]
    assert isinstance(captured.value.cause, Exception)


def test_verify_source_reports_wellfound_request_policy_observability(monkeypatch):
    progress_events = []

    class RecordingProgress:
        def __init__(self, connectors, **kwargs):
            progress_events.append((
                "created", len(connectors), kwargs["command"], kwargs.get("path"),
            ))

        def instance_started(self, connector, *, attempt):
            progress_events.append(("started", connector.source_name, attempt))

        def instance_finished(self, result):
            progress_events.append(("finished", result.source, len(result.jobs or [])))

        def finish(self):
            progress_events.append(("finished-run",))

    class ObservableWellfound:
        source_name = "wellfound"

        def __init__(self):
            self.completed = False

        def fetch(self):
            self.completed = True
            return [WellfoundConnector._normalize(_job(1, remote=True))]

        def request_observability(self):
            return {
                "http_requests_started": 12 if self.completed else 10,
                "max_in_flight_limit": 2,
                "max_in_flight_observed": 2,
                "cooldown_events": 4 if self.completed else 3,
                "cooldown_committed_s": 9.0 if self.completed else 6.0,
                "circuit_open": False,
            }

    monkeypatch.setattr(
        verify_source, "_find_connectors", lambda _source: [ObservableWellfound()]
    )
    monkeypatch.setattr(verify_source, "ConnectorProgress", RecordingProgress)

    card = verify_source.score("wellfound", max_instances=1)

    assert card["request_observability"] == {
        "http_request_count": 2,
        "max_in_flight_limit": 2,
        "max_in_flight_observed": 2,
        "cooldown_events": 1,
        "cooldown_wait_s": 3.0,
        "cooldown_outcome": "observed",
        "circuit_outcome": "closed",
    }
    assert progress_events == [
        (
            "created", 1, "scripts.verify_source wellfound",
            verify_source.VERIFY_SOURCE_PROGRESS_PATH,
        ),
        ("started", "wellfound", 1),
        ("finished", "wellfound", 1),
        ("finished-run",),
    ]


def test_wellfound_moved_to_html_registry_with_twelve_instances():
    active = [c for c in ACTIVE_CONNECTORS if c.source_name == "wellfound"]
    headed = [c for c in HEADED_CONNECTORS if c.source_name == "wellfound"]

    assert len(active) == 12
    assert not headed
    assert sorted((c.search, c.location_mode) for c in active) == sorted(
        (term, mode)
        for term in SEARCH_TERMS
        for mode in ("bengaluru", "remote_india")
    )


def test_wellfound_uses_verified_role_slugs_and_global_defaults():
    assert ROLE_SLUGS["credit risk"] == "risk-analyst"
    connector = WellfoundConnector("credit risk", "remote_india")

    assert connector._build_url() == (
        "https://wellfound.com/role/r/risk-analyst"
    )
    assert connector._build_url(2).endswith("/r/risk-analyst?page=2")
    assert connector.max_results == WELLFOUND_MAX_RESULTS == 500
    assert connector.page_workers == WELLFOUND_PAGE_WORKERS == 2
    assert WELLFOUND_SOURCE_MAX_REQUESTS == 1
    assert WELLFOUND_MIN_REQUEST_INTERVAL_S == 1.0
    assert connector.recency_window_days == RECENCY_WINDOW_DAYS


def test_wellfound_city_and_india_routes_use_native_location_slugs():
    pune = WellfoundConnector(
        "data scientist", "city", location="Pune"
    )
    bangalore = WellfoundConnector(
        "data scientist", "city", location="Bengaluru"
    )
    india = WellfoundConnector("data scientist", "india")

    assert pune._build_url() == "https://wellfound.com/role/l/data-scientist/pune"
    assert bangalore._build_url() == (
        "https://wellfound.com/role/l/data-scientist/bangalore-urban"
    )
    assert india._build_url() == "https://wellfound.com/role/l/data-scientist/india"


def test_wellfound_city_requires_a_location_and_rejects_route_fallback(monkeypatch):
    with pytest.raises(
        ValueError, match="location is required when location_mode is 'city'"
    ):
        WellfoundConnector("data scientist", "city")

    connector = WellfoundConnector("data scientist", "city", location="Pune")
    response = requests.Response()
    response.status_code = 200
    response.url = "https://wellfound.com/role/data-scientist"
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match="redirected away from city route"):
        connector._fetch_page(1)


def test_wellfound_fetches_all_reported_pages_dedupes_and_filters_remote(monkeypatch):
    connector = WellfoundConnector("data scientist", "remote_india")
    calls = []

    def fake_fetch_page(page):
        calls.append(page)
        if page == 1:
            return [_job(1), _job(2, remote=True)], 3
        if page == 2:
            return [_job(2, remote=True), _job(3, remote=True)], 3
        return [_job(4)], 3

    monkeypatch.setattr(connector, "_fetch_page", fake_fetch_page)

    jobs = connector.fetch()

    assert sorted(calls) == [1, 2, 3]
    assert [job.external_job_id for job in jobs] == ["2", "3"]


def test_wellfound_normalizes_embedded_detail_fields():
    job = WellfoundConnector._normalize(_job(7, remote=True))

    assert job.company_name_raw == "Acme"
    assert job.description_raw == "Build machine-learning models"
    assert job.location_raw == "Bengaluru"
    assert job.is_remote is True
    assert job.remote_scope == "Remote, India"
    assert job.employment_type == "full-time"
    assert job.salary_raw == "₹20L – ₹30L"
    assert job.posted_at == datetime.fromtimestamp(1_786_579_200, tz=timezone.utc)
    assert job.job_url == "https://wellfound.com/jobs/7-data-scientist"

    onsite = WellfoundConnector._normalize(_job(8))
    assert onsite.is_remote is False
    assert onsite.remote_scope == "Onsite"
