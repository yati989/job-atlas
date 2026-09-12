import json

import pytest

import app.collectors.browser.indeed as indeed_module
import app.pipeline.runner as runner_module
from app.collectors.base import PartialFetchError
from app.collectors.browser.indeed import (
    IndeedConnector,
    IndeedRateLimitError,
    _extract_detail,
)
from app.pipeline.runner import fetch_all, retry_failed


def _jobcards_html(results: list[dict]) -> str:
    payload = {"metaData": {"mosaicProviderJobCardsModel": {"results": results}}}
    return (
        '<script>window.mosaic.initialData={"isLoggedIn":true};window.mosaic.providerData["mosaic-provider-jobcards"] = '
        + json.dumps(payload)
        + "; window.next = true;</script>"
    )


class _FakeLocator:
    def __init__(self, events: list[tuple], event: tuple):
        self._events = events
        self._event = event

    def click(self) -> None:
        self._events.append(self._event)


class _FakePage:
    def __init__(self, html: str, events: list[tuple]):
        self._html = html
        self._events = events
        self.url = ""

    def goto(self, url: str, **kwargs) -> None:
        self._events.append(("goto", url))
        self.url = url

    def get_by_role(self, role: str, *, name: str, exact: bool = False):
        return _FakeLocator(self._events, ("role_click", role, name, exact))

    def get_by_text(self, text: str, *, exact: bool = False):
        return _FakeLocator(self._events, ("text_click", text, exact))

    def content(self) -> str:
        return self._html

    def bring_to_front(self) -> None:
        self._events.append(("bring_to_front",))

    def wait_for_timeout(self, milliseconds: int) -> None:
        self._events.append(("wait_for_timeout", milliseconds))


class _FakeContext:
    def __init__(self, page: _FakePage):
        self._page = page
        self.pages = [page]

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        pass


class _FakeBrowser:
    def __init__(self, context: _FakeContext):
        self._context = context

    def new_context(self) -> _FakeContext:
        return self._context

    def close(self) -> None:
        pass


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser):
        self._browser = browser

    def launch(self, **kwargs) -> _FakeBrowser:
        return self._browser

    def launch_persistent_context(self, **kwargs) -> _FakeContext:
        return self._browser.new_context()


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser):
        self.chromium = _FakeChromium(browser)


class _FakePlaywrightManager:
    def __init__(self, playwright: _FakePlaywright):
        self._playwright = playwright

    def __enter__(self) -> _FakePlaywright:
        return self._playwright

    def __exit__(self, *args) -> None:
        pass


def _install_fake_browser(monkeypatch, html: str):
    events: list[tuple] = []
    page = _FakePage(html, events)
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)
    return events


def test_fetch_passes_global_window_without_assuming_ui_preset_is_a_ceiling(monkeypatch):
    events = _install_fake_browser(monkeypatch, _jobcards_html([]))

    connector = IndeedConnector(
        search="data scientist & ML",
        location_mode="bengaluru",
        date_filter_days=60,
        fetch_descriptions=False,
    )
    connector.fetch()

    assert events == [
        (
            "goto",
            "https://in.indeed.com/jobs?"
            "q=data+scientist+%26+ML&l=Bengaluru&fromage=60",
        ),
    ]
    assert connector.filter_state["date_posted"] == {
        "configured_window_days": 60,
        "effective_window_days": 60,
        "selection": "Last 60 days",
    }


def test_search_uses_exact_native_filter_when_global_window_fits():
    connector = IndeedConnector(
        search="data scientist",
        location_mode="bengaluru",
        date_filter_days=10,
        fetch_descriptions=False,
    )

    assert connector._search_url() == (
        "https://in.indeed.com/jobs?q=data+scientist&l=Bengaluru&fromage=10"
    )
    assert connector.filter_state["date_posted"] == {
        "configured_window_days": 10,
        "effective_window_days": 10,
        "selection": "Last 10 days",
    }


def test_city_and_india_modes_use_their_native_locations_in_all_search_paths(
    monkeypatch,
):
    city = IndeedConnector(
        search="data scientist",
        location_mode="city",
        location="Pune",
        fetch_descriptions=False,
    )
    india = IndeedConnector(
        search="data scientist", location_mode="india", fetch_descriptions=False
    )

    assert city._search_url() == (
        "https://in.indeed.com/jobs?q=data+scientist&l=Pune&fromage=60"
    )
    assert india._search_url() == (
        "https://in.indeed.com/jobs?q=data+scientist&l=India&fromage=60"
    )

    events = _install_fake_browser(monkeypatch, _jobcards_html([]))
    city._scrape_once()

    assert events == [
        (
            "goto",
            "https://in.indeed.com/jobs?q=data+scientist&l=Pune&fromage=60",
        ),
    ]


def test_city_mode_requires_a_location():
    with pytest.raises(
        ValueError, match="location is required when location_mode is 'city'"
    ):
        IndeedConnector(search="data scientist", location_mode="city")


def test_fetch_records_experience_years_as_unsupported_without_using_job_type_proxy(monkeypatch):
    _install_fake_browser(
        monkeypatch,
        _jobcards_html(
            [
                {
                    "jobkey": "job-1",
                    "displayTitle": "Data Scientist",
                    "company": "Example Co",
                    "formattedLocation": "Bengaluru, Karnataka",
                    "jobTypes": ["Full-time", "Fresher"],
                }
            ]
        ),
    )
    connector = IndeedConnector(
        search="data scientist", fetch_descriptions=False, max_results=1
    )

    jobs = connector.fetch()

    assert connector.filter_state["experience_years"] == {
        "support": "unsupported_in_observed_ui",
        "selection": None,
    }
    assert jobs[0].seniority is None
    assert jobs[0].employment_type == "Full-time, Fresher"


def test_fetch_preserves_normalized_job_contract_after_filtering(monkeypatch):
    raw_result = {
        "jobkey": "job-2",
        "displayTitle": "Machine Learning Engineer",
        "company": "ML Co",
        "formattedLocation": "Bengaluru, Karnataka",
        "remoteLocation": True,
        "jobTypes": ["Full-time"],
        "extractedSalary": {"min": 1000000, "max": 1500000, "type": "yearly"},
        "createDate": 1_767_225_600_000,
    }
    _install_fake_browser(monkeypatch, _jobcards_html([raw_result]))

    jobs = IndeedConnector(
        search="machine learning engineer",
        fetch_descriptions=False,
        max_results=1,
    ).fetch()

    assert len(jobs) == 1
    assert jobs[0].model_dump() == {
        "source": "indeed",
        "external_job_id": "job-2",
        "title": "Machine Learning Engineer",
        "company_name_raw": "ML Co",
        "location_raw": "Bengaluru, Karnataka",
        "is_remote": True,
        "remote_scope": "Bengaluru, Karnataka",
        "employment_type": "Full-time",
        "seniority": None,
        "description_raw": None,
        "salary_raw": "1000000-1500000 yearly",
        "apply_url": "https://in.indeed.com/viewjob?jk=job-2",
        "job_url": "https://in.indeed.com/viewjob?jk=job-2",
        "posted_at": jobs[0].posted_at,
        "raw_payload": {
            "job_id": "job-2",
            "title": "Machine Learning Engineer",
            "company": "ML Co",
            "location": "Bengaluru, Karnataka",
            "is_remote": True,
            "job_types": ["Full-time"],
            "salary_raw": "1000000-1500000 yearly",
            "posted_at": "2026-01-01T00:00:00+00:00",
        },
    }


def test_remote_mode_uses_real_remote_filter_without_force_marking_jobs_remote():
    connector = IndeedConnector(
        search="data scientist",
        location_mode="remote_india",
        date_filter_days=60,
        fetch_descriptions=False,
    )

    assert connector._search_url(10) == (
        "https://in.indeed.com/jobs?q=data+scientist&l=India&fromage=60&"
        "sc=0kf%3Aattr%28DSQF7%29%3B&start=10"
    )
    job = connector._normalize(
        {
            "job_id": "onsite-1",
            "title": "Data Scientist",
            "company": "Example",
            "location": "Hyderabad, Telangana",
            "is_remote": False,
            "job_types": [],
            "salary_raw": None,
            "posted_at": None,
        }
    )
    assert job.is_remote is False


def test_extract_detail_reads_description_and_job_details_panel():
    detail = _extract_detail(
        """
        <div id="jobDescriptionText"><p>Build production ML systems.</p></div>
        <div role="group" aria-label="Pay">Pay<br>₹10,00,000 a year</div>
        <div role="group" aria-label="Job type">Job type<br>Full-time</div>
        """
    )

    assert detail == {
        "description_raw": "Build production ML systems.",
        "salary_raw": "₹10,00,000 a year",
        "job_type_tags": ["Full-time"],
    }


def test_indeed_instances_share_one_persistent_browser_lifetime(monkeypatch):
    events: list[tuple] = []
    page = _FakePage(_jobcards_html([]), events)
    browser = _FakeBrowser(_FakeContext(page))
    launches = 0

    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )

    def launch_once(_playwright, **_kwargs):
        nonlocal launches
        launches += 1
        return browser.new_context()

    monkeypatch.setattr(indeed_module, "launch_indeed_context", launch_once)
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)

    results = fetch_all(
        [
            IndeedConnector("data analyst", fetch_descriptions=False),
            IndeedConnector("data engineer", fetch_descriptions=False),
        ]
    )

    assert launches == 1
    assert [result.error for result in results] == [None, None]


def test_indeed_batch_has_no_inter_instance_cooldown(monkeypatch):
    _install_fake_browser(monkeypatch, _jobcards_html([]))
    waits: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", waits.append)

    fetch_all(
        [
            IndeedConnector(
                "data analyst",
                fetch_descriptions=False,
                manual_challenge_timeout_s=0,
            ),
            IndeedConnector(
                "data engineer",
                fetch_descriptions=False,
                manual_challenge_timeout_s=0,
            ),
        ]
    )

    assert waits == []


def test_expired_session_prompts_for_manual_login_and_resumes_same_search(
    monkeypatch, caplog
):
    class Response:
        status = 200

    class UserLogsInPage(_FakePage):
        def __init__(self, events):
            super().__init__("<html><body>Sign in</body></html>", events)
            self.logged_in = False
            self.goto_count = 0

        def goto(self, url: str, **kwargs):
            self._events.append(("goto", url))
            self.url = url
            self.goto_count += 1
            if self.goto_count > 1:
                self._html = _jobcards_html([
                    {
                        "jobkey": "after-login",
                        "displayTitle": "Data Analyst",
                        "company": "Example Co",
                        "formattedLocation": "Bengaluru, Karnataka",
                    }
                ])
            return Response()

        def wait_for_timeout(self, milliseconds: int):
            self._events.append(("wait_for_timeout", milliseconds))
            self.logged_in = True

    events: list[tuple] = []
    page = UserLogsInPage(events)
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(
        indeed_module, "page_reports_logged_in", lambda current: current.logged_in
    )
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)

    jobs = IndeedConnector(
        "data analyst",
        fetch_descriptions=False,
        max_results=1,
        manual_challenge_timeout_s=10,
    ).fetch()

    assert [job.external_job_id for job in jobs] == ["after-login"]
    assert [event[0] for event in events] == [
        "goto",
        "bring_to_front",
        "wait_for_timeout",
        "goto",
    ]
    assert "Log in manually in the visible browser" in caplog.text


def test_authentication_failure_is_not_masked_as_successful_zero_yield(monkeypatch):
    _install_fake_browser(monkeypatch, "<html><body>Sign in</body></html>")
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: False)

    connector = IndeedConnector(
        "data analyst",
        fetch_descriptions=False,
        manual_challenge_timeout_s=0,
    )

    with pytest.raises(RuntimeError, match="login was not completed"):
        connector.fetch()


def test_authentication_failure_is_non_retryable_at_pipeline_seam(monkeypatch):
    _install_fake_browser(monkeypatch, "<html><body>Sign in</body></html>")
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: False)
    connector = IndeedConnector(
        "data analyst",
        fetch_descriptions=False,
        manual_challenge_timeout_s=0,
    )

    results = fetch_all([connector])
    retry_failed(results)

    assert results[0].error is not None
    assert results[0].retried is False


def test_authentication_failure_halts_the_shared_batch(monkeypatch):
    events = _install_fake_browser(
        monkeypatch, "<html><body>Sign in</body></html>"
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: False)

    results = fetch_all(
        [
            IndeedConnector(
                "data analyst", fetch_descriptions=False, manual_challenge_timeout_s=0
            ),
            IndeedConnector(
                "data engineer", fetch_descriptions=False, manual_challenge_timeout_s=0
            ),
        ]
    )

    assert len([event for event in events if event[0] == "goto"]) == 1
    assert all(result.error is not None for result in results)


def test_challenge_halts_remaining_instances_without_more_navigation(monkeypatch):
    events: list[tuple] = []
    page = _FakePage(
        "<html><title>Additional Verification Required</title></html>", events
    )
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)

    results = fetch_all(
        [
            IndeedConnector(
                "data analyst",
                fetch_descriptions=False,
                manual_challenge_timeout_s=0,
            ),
            IndeedConnector(
                "data engineer",
                fetch_descriptions=False,
                manual_challenge_timeout_s=0,
            ),
        ]
    )
    retry_failed(results)

    assert len([event for event in events if event[0] == "goto"]) == 1
    assert all(result.error is not None for result in results)
    assert all(getattr(result.error, "retryable", True) is False for result in results)
    assert all(result.retried is False for result in results)


def test_visible_captcha_pauses_for_human_and_resumes_in_same_browser(monkeypatch):
    class Response:
        status = 403

    class HumanClearsChallengePage(_FakePage):
        def goto(self, url: str, **kwargs):
            self._events.append(("goto", url))
            self.url = url
            self._html = "<html><title>Additional Verification Required</title></html>"
            return Response()

        def bring_to_front(self):
            self._events.append(("bring_to_front",))

        def wait_for_timeout(self, milliseconds: int):
            self._events.append(("wait_for_timeout", milliseconds))
            self._html = _jobcards_html([])

    events: list[tuple] = []
    page = HumanClearsChallengePage("", events)
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)

    jobs = IndeedConnector(
        "data analyst",
        fetch_descriptions=False,
        manual_challenge_timeout_s=10,
    ).fetch()

    assert jobs == []
    assert [event[0] for event in events] == [
        "goto",
        "bring_to_front",
        "wait_for_timeout",
    ]


def test_pagination_challenge_preserves_completed_jobs_as_partial_error(monkeypatch):
    class ChallengeOnFetchPage(_FakePage):
        def evaluate(self, _script, urls):
            self._events.append(("evaluate", tuple(urls)))
            return [
                {
                    "url": url,
                    "status": 403,
                    "text": "<title>Additional Verification Required</title>",
                }
                for url in urls
            ]

    initial = _jobcards_html(
        [
            {
                "jobkey": "kept-before-challenge",
                "displayTitle": "Data Analyst",
                "company": "Example Co",
                "formattedLocation": "Bengaluru, Karnataka",
            }
        ]
    )
    events: list[tuple] = []
    page = ChallengeOnFetchPage(initial, events)
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)

    results = fetch_all(
        [
            IndeedConnector(
                "data analyst",
                fetch_descriptions=False,
                max_results=20,
                manual_challenge_timeout_s=0,
            )
        ]
    )
    retry_failed(results)

    assert results[0].error is not None
    assert results[0].retried is False
    assert [job.external_job_id for job in results[0].jobs] == [
        "kept-before-challenge"
    ]
    assert results[0].attempt_details[0].status == "partial-error"


def test_pagination_requests_use_one_bounded_parallel_batch(
    monkeypatch,
):
    class RecordingFetchPage(_FakePage):
        def evaluate(self, _script, value):
            if isinstance(value, list):
                self._events.append(("evaluate-burst", tuple(value)))
                return [
                    {"url": url, "status": 200, "text": _jobcards_html([])}
                    for url in value
                ]
            self._events.append(("evaluate-one", value))
            return {"url": value, "status": 200, "text": _jobcards_html([])}

    initial = _jobcards_html(
        [
            {
                "jobkey": "initial-job",
                "displayTitle": "Data Analyst",
                "company": "Example Co",
                "formattedLocation": "Bengaluru, Karnataka",
            }
        ]
    )
    events: list[tuple] = []
    page = RecordingFetchPage(initial, events)
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)

    jobs = IndeedConnector(
        "data analyst", fetch_descriptions=False, max_results=50
    ).fetch()

    assert [job.external_job_id for job in jobs] == ["initial-job"]
    assert [event[0] for event in events if event[0].startswith("evaluate")] == [
        "evaluate-burst",
    ]
    assert len([event for event in events if event[0] == "evaluate-burst"][0][1]) == 4


def test_initial_429_fails_immediately_without_a_cooldown(monkeypatch):
    class Response:
        def __init__(self, status: int):
            self.status = status

    class RateLimitedInitialPage(_FakePage):
        def __init__(self, events: list[tuple]):
            super().__init__(_jobcards_html([]), events)
            self._goto_count = 0

        def goto(self, url: str, **kwargs):
            self._events.append(("goto", url))
            self.url = url
            self._goto_count += 1
            if self._goto_count == 1:
                self._html = "<html><body>rate limited</body></html>"
                return Response(429)
            self._html = _jobcards_html([])
            return Response(200)

    events: list[tuple] = []
    page = RateLimitedInitialPage(events)
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)
    waits: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", waits.append)

    with pytest.raises(IndeedRateLimitError, match="HTTP 429"):
        IndeedConnector("data analyst", fetch_descriptions=False).fetch()

    assert [event[0] for event in events] == ["goto"]
    assert waits == []


def test_pagination_429_preserves_jobs_without_a_cooldown(monkeypatch):
    class RateLimitedFetchPage(_FakePage):
        def __init__(self, html: str, events: list[tuple]):
            super().__init__(html, events)
            self._responses = [
                {"status": 429, "text": "rate limited"},
                {"status": 200, "text": _jobcards_html([])},
                {"status": 200, "text": _jobcards_html([])},
                {"status": 200, "text": _jobcards_html([])},
            ]

        def evaluate(self, _script, urls):
            self._events.append(("evaluate", tuple(urls)))
            return [
                {"url": url, **self._responses.pop(0)}
                for url in urls
            ]

    initial = _jobcards_html(
        [
            {
                "jobkey": "initial-job",
                "displayTitle": "Data Analyst",
                "company": "Example Co",
                "formattedLocation": "Bengaluru, Karnataka",
            }
        ]
    )
    events: list[tuple] = []
    page = RateLimitedFetchPage(initial, events)
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)
    waits: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", waits.append)

    with pytest.raises(PartialFetchError) as exc:
        IndeedConnector(
            "data analyst", fetch_descriptions=False, max_results=50
        ).fetch()

    assert [job.external_job_id for job in exc.value.jobs] == ["initial-job"]
    assert isinstance(exc.value.cause, IndeedRateLimitError)
    evaluated_batches = [event[1] for event in events if event[0] == "evaluate"]
    assert "start=10" in evaluated_batches[0][0]
    assert waits == []


def test_persistent_429_continues_with_next_instance_without_a_cooldown(monkeypatch):
    class Response:
        status = 200

    class FirstInstanceRateLimitedPage(_FakePage):
        def goto(self, url: str, **kwargs):
            self._events.append(("goto", url))
            self.url = url
            self._html = _jobcards_html(
                [
                    {
                        "jobkey": "initial-job",
                        "displayTitle": "Data Analyst",
                        "company": "Example Co",
                        "formattedLocation": "Bengaluru, Karnataka",
                    }
                ]
            )
            return Response()

        def evaluate(self, _script, urls):
            self._events.append(("evaluate", tuple(urls)))
            return [
                {"url": url, "status": 429, "text": "rate limited"}
                for url in urls
            ]

    events: list[tuple] = []
    page = FirstInstanceRateLimitedPage("", events)
    browser = _FakeBrowser(_FakeContext(page))
    monkeypatch.setattr(
        indeed_module,
        "sync_playwright",
        lambda: _FakePlaywrightManager(_FakePlaywright(browser)),
    )
    monkeypatch.setattr(
        indeed_module,
        "launch_indeed_context",
        lambda _playwright, **_kwargs: browser.new_context(),
    )
    monkeypatch.setattr(indeed_module, "page_reports_logged_in", lambda _page: True)
    monkeypatch.setattr(indeed_module, "human_delay", lambda *_args: None)
    waits: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", waits.append)

    results = fetch_all(
        [
            IndeedConnector(
                "data engineer",
                fetch_descriptions=False,
                max_results=20,
            ),
            IndeedConnector(
                "machine learning engineer",
                fetch_descriptions=False,
                max_results=1,
            ),
        ]
    )

    assert results[0].error is not None
    assert results[1].error is None
    assert len([event for event in events if event[0] == "goto"]) == 2
    assert waits == []
