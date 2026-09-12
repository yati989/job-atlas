from datetime import datetime, timedelta, timezone

from app.collectors.api.himalayas import COUNTRY, HIMALAYAS_URL, HimalayasConnector


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, *, params):
        self.calls.append((url, params))
        return _Response(self.payload)


def _row(job_id: int, *, age_days: int = 1, locations=None) -> dict:
    posted = datetime.now(timezone.utc) - timedelta(days=age_days)
    return {
        "guid": f"job-{job_id}",
        "title": "Data Engineer",
        "companyName": "Acme",
        "locationRestrictions": [] if locations is None else locations,
        "employmentType": "Full Time",
        "seniority": ["Mid-level"],
        "description": "Build data systems.",
        "applicationLink": f"https://himalayas.app/jobs/{job_id}",
        "pubDate": int(posted.timestamp()),
        "minSalary": 100,
        "maxSalary": 200,
        "currency": "USD",
        "salaryPeriod": "annual",
    }


def test_search_page_uses_role_country_recent_sort_and_one_based_page():
    connector = HimalayasConnector(search="credit risk")
    connector._client = _Client({"jobs": []})

    connector._fetch_page(2)

    assert connector._client.calls == [
        (
            HIMALAYAS_URL,
            {
                "q": "credit risk",
                "country": COUNTRY,
                "sort": "relevant",
                "page": 2,
            },
        )
    ]


def test_default_page_guardrail_is_thirty_without_a_result_ceiling():
    connector = HimalayasConnector()

    assert connector.max_pages == 30
    assert not hasattr(connector, "max_results")


def test_fetch_continues_after_short_page_until_total_is_exhausted(monkeypatch):
    connector = HimalayasConnector(search="data engineer", max_pages=5)
    pages = {
        1: {"offset": 0, "limit": 20, "totalCount": 21, "jobs": [_row(1)]},
        2: {
            "offset": 20,
            "limit": 20,
            "totalCount": 21,
            "jobs": [_row(1), _row(2, locations=["India"])],
        },
    }
    calls = []

    def fetch_page(page):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(connector, "_fetch_page", fetch_page)
    jobs = connector.fetch()

    assert calls == [1, 2]
    assert [job.external_job_id for job in jobs] == ["job-1", "job-2"]
    assert jobs[0].location_raw == "Worldwide"
    assert jobs[1].location_raw == "India"
    assert jobs[0].posted_at is not None
    assert jobs[0].salary_raw == "100-200 USD annual"


def test_fetch_ignores_relevance_drift_and_stops_at_max_pages(monkeypatch):
    connector = HimalayasConnector(search="data engineer", max_pages=2, max_age_days=60)
    pages = {
        1: {
            "offset": 0,
            "limit": 20,
            "totalCount": 100,
            "jobs": [_row(job_id) for job_id in range(1, 21)],
        },
        2: {
            "offset": 20,
            "limit": 20,
            "totalCount": 100,
            "jobs": [
                {**_row(job_id), "title": "Computational Chemist"}
                for job_id in range(21, 41)
            ],
        },
    }
    calls = []

    def fetch_page(page):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(connector, "_fetch_page", fetch_page)
    jobs = connector.fetch()

    assert calls == [1, 2]
    assert len(jobs) == 40
