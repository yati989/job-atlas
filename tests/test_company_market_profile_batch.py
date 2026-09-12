import json
from pathlib import Path
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.companies.market_profile_batch import (
    BrightDataGlassdoorClient,
    CompanyTarget,
    LevelsFyiTarget,
    SourceResolution,
    _ambitionbox_targets,
    normalize_legacy_ambitionbox_404s,
    _persist_resolved_glassdoor_ids,
    load_glassdoor_resolution_artifact,
    load_company_ids_file,
    resolve_company_sources,
    run_ambitionbox_batch,
    run_glassdoor_batch,
    run_levels_fyi_batch,
    select_ambitionbox_retry_targets,
    select_levels_fyi_retry_targets,
    select_market_profile_targets,
)
from app.companies.ambitionbox import AmbitionBoxObservation
from app.companies import market_profile_batch
from app.models.orm import Base, Company, Job, utcnow


FIXTURES = Path(__file__).parent / "fixtures"


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_ambitionbox_process_lock_uses_platform_temp_directory(tmp_path, monkeypatch):
    lock_path = tmp_path / "ambitionbox.lock"
    monkeypatch.setattr(market_profile_batch, "_AMBITIONBOX_PROCESS_LOCK", lock_path)

    with market_profile_batch._ambitionbox_process_lock():
        assert lock_path.exists()


def test_company_id_manifest_preserves_order_and_rejects_duplicates(tmp_path):
    manifest = tmp_path / "wave.csv"
    manifest.write_text("company_id,company_name\n7,Seven\n3,Three\n")

    assert load_company_ids_file(manifest) == [7, 3]

    duplicate = tmp_path / "duplicate.txt"
    duplicate.write_text("7\n7\n")
    with pytest.raises(ValueError, match="duplicates"):
        load_company_ids_file(duplicate)


def test_resolver_uses_identity_matched_glassdoor_overview_and_employer_id():
    target = CompanyTarget(1, "Amazon", "AI Engineer", 3)
    results = [
        {
            "title": "Working at Amazon",
            "link": (
                "https://www.glassdoor.com/Overview/"
                "Working-at-Amazon-EI_IE6036.11,17.htm"
            ),
        },
        {
            "title": "Working at Amazon Web Services",
            "link": (
                "https://www.glassdoor.com/Overview/"
                "Working-at-Amazon-Web-Services-EI_IE7470741.11,30.htm"
            ),
        },
    ]

    resolution = resolve_company_sources(target, lambda _query: results)

    assert resolution.glassdoor_source_id == "6036"
    assert resolution.source_urls["glassdoor"]["company_name"] == "Amazon"
    assert "EI_IE6036" in resolution.source_urls["glassdoor"]["overview"]
    assert resolution.source_urls["ambitionbox"]["slug"] == "amazon"
    assert set(resolution.source_urls["ambitionbox"]) == {
        "company_name",
        "slug",
        "salaries",
    }
    assert resolution.source_urls["levels_fyi"]["company_slug"] == "amazon"


def test_resolver_uses_search_rank_for_duplicate_exact_name_profiles():
    target = CompanyTarget(1, "Example", "Data Scientist", 1)
    results = [
        {
            "title": "Working at Example",
            "link": "https://www.glassdoor.com/Overview/a-EI_IE10.1,2.htm",
        },
        {
            "title": "Working at Example",
            "link": "https://www.glassdoor.com/Overview/b-EI_IE20.1,2.htm",
        },
    ]

    resolution = resolve_company_sources(target, lambda _query: results)

    assert resolution.glassdoor_source_id == "10"
    assert resolution.source_urls["glassdoor"]["overview"].endswith(
        "a-EI_IE10.1,2.htm"
    )


def test_reviewed_glassdoor_artifact_replaces_runtime_serp_resolution(tmp_path):
    artifact = tmp_path / "glassdoor.json"
    artifact.write_text(
        '{"schema_version": 1, "resolutions": ['
        '{"company_id": 1, "company_name": "Amazon Science", '
        '"status": "accepted", "searched_name": "Amazon", '
        '"observed_name": "Amazon", "employer_id": "6036", '
        '"overview_url": "https://www.glassdoor.com/Overview/'
        'Working-at-Amazon-EI_IE6036.11,17.htm", "resolution_pass": "core"}'
        ']}'
    )

    decisions = load_glassdoor_resolution_artifact(artifact)

    assert decisions[1]["employer_id"] == "6036"
    assert decisions[1]["searched_name"] == "Amazon"


def test_resolved_glassdoor_employer_id_is_persisted_before_collection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        company = Company(name="Amazon", market_profile_status="in_progress")
        session.add(company)
        session.commit()
        company_id = company.id

    target = CompanyTarget(company_id, "Amazon", "AI Engineer", 1)
    resolution = resolve_company_sources(
        target,
        lambda _query: [{
            "title": "Working at Amazon",
            "link": "https://www.glassdoor.com/Overview/Amazon-EI_IE6036.1,7.htm",
        }],
    )

    _persist_resolved_glassdoor_ids(factory, [resolution])

    with factory() as session:
        assert session.get(Company, company_id).glassdoor_employer_id == "6036"


def test_glassdoor_only_batch_persists_reviewed_id_and_snapshot(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        company = Company(
            name="Amazon Science",
            market_profile_status="partial",
            market_profile_evidence={
                "sources": {
                    "glassdoor": {"status": "not_attempted"},
                    "ambitionbox": {"status": "missing"},
                    "levels_fyi": {"status": "missing"},
                },
            },
        )
        session.add(company)
        session.commit()
        company_id = company.id

    artifact = tmp_path / "glassdoor.json"
    artifact.write_text(
        '{"schema_version": 1, "resolutions": ['
        f'{{"company_id": {company_id}, "company_name": "Amazon Science", '
        '"status": "accepted", "searched_name": "Amazon", '
        '"observed_name": "Amazon", "employer_id": "6036", '
        '"overview_url": "https://www.glassdoor.com/Overview/'
        'Working-at-Amazon-EI_IE6036.11,17.htm", "resolution_pass": "core"}'
        ']}'
    )

    class Collector:
        def collect(self, resolutions):
            with factory() as session:
                assert session.get(Company, company_id).glassdoor_employer_id == "6036"
            assert [item.glassdoor_source_id for item in resolutions] == ["6036"]
            return {
                "6036": {
                    "id": "6036",
                    "company": "Amazon",
                    "url_overview": (
                        "https://www.glassdoor.com/Overview/"
                        "Working-at-Amazon-EI_IE6036.11,17.htm"
                    ),
                    "ratings_overall": 3.5,
                    "ratings_work_life_balance": 3.1,
                    "reviews_count": 500,
                    "details_size": "1,001 to 5,000 Employees",
                    "details_type": "Company - Public",
                    "details_revenue": "$10+ billion (USD)",
                },
            }

    results = run_glassdoor_batch(
        resolutions_path=str(artifact),
        session_factory=factory,
        client=Collector(),
    )

    assert results[0].source_statuses == {"glassdoor": "ok"}
    with factory() as session:
        saved = session.get(Company, company_id)
        assert saved.glassdoor_employer_id == "6036"
        assert saved.glassdoor_overall_rating == 3.5
        assert saved.glassdoor_wlb_rating == 3.1
        assert saved.glassdoor_review_count == 500
        assert saved.employee_count_range == "1,001 to 5,000 Employees"
        assert saved.ownership_type == "public"
        assert saved.revenue == "$10+ billion (USD)"
        evidence = saved.market_profile_evidence["sources"]["glassdoor"]
        assert evidence["resolution_source"] == "codex_internal_web_search"
        assert evidence["searched_name"] == "Amazon"


def test_glassdoor_collection_submits_at_most_one_record_per_employer_id():
    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.trigger_inputs = None

        def post(self, _endpoint, *, params, json):
            self.trigger_inputs = json
            return Response({"snapshot_id": "snapshot-1"})

        def get(self, endpoint, params=None):
            if "progress" in endpoint:
                return Response({"status": "ready"})
            return Response([{"id": "6036", "company": "Amazon"}])

    target_a = CompanyTarget(1, "Amazon", "AI Engineer", 1)
    target_b = CompanyTarget(2, "Amazon", "Data Scientist", 1)
    source_urls = {
        "glassdoor": {
            "overview": "https://www.glassdoor.com/Overview/Amazon-EI_IE6036.1,7.htm",
        },
    }
    resolutions = [
        SourceResolution(target_a, source_urls, "6036", {}),
        SourceResolution(target_b, source_urls, "6036", {}),
    ]
    client = Client()
    collector = BrightDataGlassdoorClient(api_key="key")
    collector._client.close()
    collector._client = client

    snapshots = []
    records = collector.collect(
        resolutions,
        snapshot_callback=lambda snapshot_id, count: snapshots.append(
            (snapshot_id, count),
        ),
        poll_seconds=0,
    )

    assert client.trigger_inputs == [{"url": source_urls["glassdoor"]["overview"]}]
    assert snapshots == [("snapshot-1", 1)]
    assert records == {"6036": {"id": "6036", "company": "Amazon"}}


def test_glassdoor_only_batch_reuses_verified_employer_id_for_alias(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        donor = Company(
            name="Amazon",
            market_profile_status="partial",
            glassdoor_employer_id="6036",
            glassdoor_overall_rating=3.5,
            glassdoor_wlb_rating=3.1,
            market_profile_evidence={
                "sources": {
                    "glassdoor": {
                        "status": "ok",
                        "source_id": "6036",
                        "url": (
                            "https://www.glassdoor.com/Overview/"
                            "Working-at-Amazon-EI_IE6036.11,17.htm"
                        ),
                    },
                    "ambitionbox": {"status": "missing"},
                },
            },
        )
        alias = Company(name="Amazon Science", market_profile_status="partial")
        session.add_all([donor, alias])
        session.commit()
        donor_id, alias_id = donor.id, alias.id

    artifact = tmp_path / "glassdoor.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "resolutions": [
                    {
                        "company_id": donor_id,
                        "company_name": "Amazon",
                        "status": "accepted",
                        "observed_name": "Amazon",
                        "employer_id": "6036",
                        "overview_url": (
                            "https://www.glassdoor.com/Overview/"
                            "Working-at-Amazon-EI_IE6036.11,17.htm"
                        ),
                    },
                    {
                        "company_id": alias_id,
                        "company_name": "Amazon Science",
                        "status": "accepted",
                        "observed_name": "Amazon",
                        "employer_id": "6036",
                        "overview_url": (
                            "https://www.glassdoor.com/Overview/"
                            "Working-at-Amazon-EI_IE6036.11,17.htm"
                        ),
                    },
                ],
            },
        ),
    )

    class Collector:
        def collect(self, _resolutions, **_kwargs):
            raise AssertionError("verified employer IDs must not be recollected")

    results = run_glassdoor_batch(
        resolutions_path=str(artifact),
        session_factory=factory,
        client=Collector(),
    )

    assert {item.company_id for item in results} == {donor_id, alias_id}
    with factory() as session:
        saved = session.get(Company, alias_id)
        assert saved.glassdoor_employer_id == "6036"
        assert saved.glassdoor_overall_rating == 3.5
        assert saved.glassdoor_wlb_rating == 3.1
        source = saved.market_profile_evidence["sources"]["glassdoor"]
        assert source["status"] == "ok"
        assert source["reused_from_company_id"] == donor_id


def test_glassdoor_collection_can_resume_snapshot_without_triggering_another():
    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def post(self, *_args, **_kwargs):
            raise AssertionError("resume must not trigger a paid snapshot")

        def get(self, endpoint, params=None):
            if "progress" in endpoint:
                assert endpoint.endswith("/existing-snapshot")
                return Response({"status": "ready"})
            assert endpoint.endswith("/existing-snapshot")
            assert params == {"format": "json"}
            return Response([{"id": "6036", "company": "Amazon"}])

    resolution = SourceResolution(
        CompanyTarget(1, "Amazon", "Data Scientist", 1),
        {
            "glassdoor": {
                "overview": (
                    "https://www.glassdoor.com/Overview/"
                    "Amazon-EI_IE6036.1,7.htm"
                ),
            },
        },
        "6036",
        {},
    )
    collector = BrightDataGlassdoorClient(api_key="key")
    collector._client.close()
    collector._client = Client()

    records = collector.collect(
        [resolution],
        snapshot_id="existing-snapshot",
        poll_seconds=0,
    )

    assert records == {"6036": {"id": "6036", "company": "Amazon"}}


def test_glassdoor_collection_retries_transient_progress_timeout():
    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.progress_calls = 0

        def post(self, *_args, **_kwargs):
            raise AssertionError("resume must not trigger a paid snapshot")

        def get(self, endpoint, params=None):
            if "progress" in endpoint:
                self.progress_calls += 1
                if self.progress_calls == 1:
                    raise httpx.ReadTimeout("transient timeout")
                return Response({"status": "ready"})
            return Response([{"id": "6036", "company": "Amazon"}])

    resolution = SourceResolution(
        CompanyTarget(1, "Amazon", "Data Scientist", 1),
        {
            "glassdoor": {
                "overview": (
                    "https://www.glassdoor.com/Overview/"
                    "Amazon-EI_IE6036.1,7.htm"
                ),
            },
        },
        "6036",
        {},
    )
    client = Client()
    collector = BrightDataGlassdoorClient(api_key="key")
    collector._client.close()
    collector._client = client

    records = collector.collect(
        [resolution],
        snapshot_id="existing-snapshot",
        poll_seconds=0,
    )

    assert client.progress_calls == 2
    assert records == {"6036": {"id": "6036", "company": "Amazon"}}


def test_glassdoor_collection_waits_when_snapshot_download_is_not_ready():
    class Response:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.progress_calls = 0
            self.download_calls = 0

        def post(self, *_args, **_kwargs):
            raise AssertionError("resume must not trigger a paid snapshot")

        def get(self, endpoint, params=None):
            if "progress" in endpoint:
                self.progress_calls += 1
                return Response({"status": "running"})
            self.download_calls += 1
            if self.download_calls == 1:
                return Response({"status": "building"}, status_code=202)
            return Response([{"id": "10436", "company": "Linde"}])

    resolution = SourceResolution(
        CompanyTarget(1, "Linde", "Data Scientist", 1),
        {
            "glassdoor": {
                "overview": (
                    "https://www.glassdoor.com/Overview/"
                    "Working-at-Linde-EI_IE10436.11,16.htm"
                ),
            },
        },
        "10436",
        {},
    )
    client = Client()
    collector = BrightDataGlassdoorClient(api_key="key")
    collector._client.close()
    collector._client = client

    records = collector.collect(
        [resolution],
        snapshot_id="existing-snapshot",
        poll_seconds=0,
    )

    assert client.progress_calls == 2
    assert client.download_calls == 2
    assert records == {"10436": {"id": "10436", "company": "Linde"}}


def test_target_selection_uses_registry_role_order_and_claims_rows():
    session = _session()
    company = Company(name="Example Co", market_profile_status="pending")
    session.add(company)
    session.flush()
    now = utcnow()
    session.add_all([
        Job(
            source="test",
            external_job_id="ai",
            company_id=company.id,
            title="GenAI Developer",
            is_remote=True,
            status="active",
            posted_at=now,
        ),
        Job(
            source="test",
            external_job_id="ds",
            company_id=company.id,
            title="Data Scientist",
            is_remote=True,
            status="active",
            posted_at=now - timedelta(days=1),
        ),
    ])
    session.flush()

    targets = select_market_profile_targets(session, limit=10)

    assert targets == [CompanyTarget(company.id, "Example Co", "Data Scientist", 2)]
    assert company.market_profile_status == "in_progress"


def test_target_selection_excludes_staffing_companies():
    session = _session()
    employer = Company(
        name="Product Employer",
        company_type="employer",
        market_profile_status="pending",
    )
    staffing = Company(
        name="Viraaj HR Solutions Private Limited",
        company_type="staffing",
        market_profile_status="pending",
    )
    session.add_all([employer, staffing])
    session.flush()
    for index, company in enumerate((employer, staffing)):
        session.add(
            Job(
                source="test",
                external_job_id=f"company-type-{index}",
                company_id=company.id,
                title="Data Scientist",
                is_remote=True,
                status="active",
                posted_at=utcnow(),
            ),
        )
    session.flush()

    targets = select_market_profile_targets(session, limit=None)

    assert targets == [
        CompanyTarget(employer.id, "Product Employer", "Data Scientist", 1),
    ]
    assert employer.market_profile_status == "in_progress"
    assert staffing.market_profile_status == "pending"


def test_exact_manifest_selection_does_not_apply_rolling_cutoff():
    session = _session()
    company = Company(
        name="Older Manifest Company",
        company_type="employer",
        market_profile_status="pending",
    )
    session.add(company)
    session.flush()
    session.add(
        Job(
            source="test",
            external_job_id="older-manifest-job",
            company_id=company.id,
            title="Data Scientist",
            status="active",
            posted_at=utcnow() - timedelta(days=30),
        ),
    )
    session.commit()

    targets = select_market_profile_targets(
        session,
        limit=None,
        company_ids=[company.id],
        claim=False,
    )

    assert [target.company_id for target in targets] == [company.id]


def test_target_selection_applies_remote_filter_only_when_requested():
    session = _session()
    onsite = Company(
        name="Onsite Employer",
        company_type="employer",
        market_profile_status="pending",
    )
    remote = Company(
        name="Remote Employer",
        company_type="employer",
        market_profile_status="pending",
    )
    session.add_all([onsite, remote])
    session.flush()
    now = utcnow()
    session.add_all([
        Job(
            source="test",
            external_job_id="onsite",
            company_id=onsite.id,
            title="Data Scientist",
            is_remote=False,
            status="active",
            posted_at=now,
        ),
        Job(
            source="test",
            external_job_id="remote",
            company_id=remote.id,
            title="Data Scientist",
            is_remote=True,
            status="active",
            posted_at=now,
        ),
    ])
    session.flush()

    all_targets = select_market_profile_targets(
        session,
        limit=None,
        claim=False,
    )
    remote_targets = select_market_profile_targets(
        session,
        limit=None,
        remote_only=True,
        claim=False,
    )

    assert all_targets == [
        CompanyTarget(onsite.id, "Onsite Employer", "Data Scientist", 1),
        CompanyTarget(remote.id, "Remote Employer", "Data Scientist", 1),
    ]
    assert remote_targets == [
        CompanyTarget(remote.id, "Remote Employer", "Data Scientist", 1),
    ]


def test_ambitionbox_targets_use_only_the_salary_page():
    target = CompanyTarget(7, "Example Co", "Data Scientist", 1)
    resolution = SourceResolution(
        target,
        {
            "ambitionbox": {
                "company_name": "Example Co",
                "slug": "example",
                "salaries": "https://www.ambitionbox.com/salaries/example-salaries",
            },
        },
        None,
        {},
    )

    assert _ambitionbox_targets([resolution]) == [
        market_profile_batch.AmbitionBoxTarget(
            company_id=7,
            company_name="Example Co",
            salary_role="Data Scientist",
            slug="example",
            salary_url="https://www.ambitionbox.com/salaries/example-salaries",
        ),
    ]


def test_ambitionbox_retry_selection_includes_unresolved_legacy_404_once():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        throttled = Company(
            name="Throttled Co",
            market_profile_status="partial",
            market_profile_evidence={
                "requested_salary_role": "Data Scientist",
                "sources": {
                    "ambitionbox": {
                        "status": "error",
                        "urls": {
                            "slug": "throttled",
                            "salaries": "https://example.test/throttled",
                        },
                        "error": "HTTPStatusError: 403 Forbidden",
                    },
                },
            },
        )
        missing = Company(
            name="Missing Co",
            market_profile_status="partial",
            market_profile_evidence={
                "sources": {
                    "ambitionbox": {
                        "status": "error",
                        "error": "HTTPStatusError: 404 Not Found",
                    },
                },
            },
        )
        deferred = Company(
            name="Deferred Co",
            market_profile_status="partial",
            market_profile_evidence={
                "sources": {"ambitionbox": {"status": "deferred"}},
            },
        )
        searched_missing = Company(
            name="Searched Missing Co",
            market_profile_status="done",
            market_profile_evidence={
                "sources": {
                    "ambitionbox": {
                        "status": "missing",
                        "reason": "AmbitionBox company search found no candidate",
                        "resolution": {
                            "status": "missing",
                            "search_attempts": [
                                {"query": "Searched Missing Co", "candidate_count": 0},
                            ],
                        },
                    },
                },
            },
        )
        session.add_all([throttled, missing, deferred, searched_missing])
        session.flush()
        now = utcnow()
        for index, company in enumerate(
            (throttled, missing, deferred, searched_missing),
        ):
            session.add(
                Job(
                    source="test",
                    external_job_id=str(index),
                    company_id=company.id,
                    title="Data Scientist",
                    is_remote=True,
                    status="active",
                    posted_at=now,
                ),
            )
        session.flush()

        selected = select_ambitionbox_retry_targets(session, limit=None)

    assert [item.company_name for item in selected] == [
        "Throttled Co",
        "Missing Co",
        "Deferred Co",
    ]
    assert selected[0].salary_url == "https://example.test/throttled"
    assert selected[1].salary_url.endswith("/missing-co-salaries")
    assert selected[2].salary_url.endswith("/deferred-co-salaries")


def test_ambitionbox_retry_selection_includes_legacy_ok_row_without_salary():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        legacy_gap = Company(
            name="Adobe",
            market_profile_status="done",
            ambitionbox_overall_rating=3.7,
            ambitionbox_wlb_rating=3.8,
            ambitionbox_estimated_salary_lpa=None,
            market_profile_evidence={
                "requested_salary_role": "Applied Scientist 4",
                "sources": {
                    "ambitionbox": {
                        "status": "ok",
                        "url": "https://www.ambitionbox.com/salaries/adobe-salaries",
                        "slug": "adobe",
                        "selected_role": None,
                    },
                },
            },
        )
        terminal_gap = Company(
            name="Terminal Gap",
            market_profile_status="done",
            ambitionbox_estimated_salary_lpa=None,
            market_profile_evidence={
                "requested_salary_role": "Data Scientist",
                "sources": {
                    "ambitionbox": {
                        "status": "ok",
                        "salary_lookup": {
                            "status": "missing",
                            "retryable": False,
                        },
                    },
                },
            },
        )
        session.add_all([legacy_gap, terminal_gap])
        session.flush()
        now = utcnow()
        for index, company in enumerate((legacy_gap, terminal_gap)):
            session.add(
                Job(
                    source="test",
                    external_job_id=f"legacy-{index}",
                    company_id=company.id,
                    title="Applied Scientist 4",
                    is_remote=True,
                    status="active",
                    posted_at=now,
                ),
            )
        session.flush()

        selected = select_ambitionbox_retry_targets(session, limit=None)

    assert [item.company_name for item in selected] == ["Adobe"]
    assert selected[0].salary_role == "Applied Scientist 4"
    assert selected[0].known_broad_observation is not None
    assert selected[0].known_broad_observation.status == "ok"
    assert selected[0].known_broad_observation.company_id == legacy_gap.id
    assert selected[0].known_broad_observation.overall_rating == 3.7
    assert selected[0].known_broad_observation.wlb_rating == 3.8
    assert selected[0].known_broad_observation.salary_lpa is None
    assert selected[0].known_broad_observation.evidence["url"] == (
        "https://www.ambitionbox.com/salaries/adobe-salaries"
    )


def test_ambitionbox_retry_selection_reuses_cached_canonical_resolution():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    resolved_url = "https://www.ambitionbox.com/salaries/unitedhealth-salaries"
    with factory() as session:
        company = Company(
            name="UnitedHealth Group",
            market_profile_status="partial",
            market_profile_evidence={
                "requested_salary_role": "Data Scientist",
                "sources": {
                    "ambitionbox": {
                        "status": "error",
                        "error": "HTTP 503",
                        "retryable": True,
                        "resolution": {
                            "status": "resolved",
                            "resolved_company": "UnitedHealth",
                            "resolved_slug": "unitedhealth",
                            "resolved_url": resolved_url,
                        },
                    },
                },
            },
        )
        session.add(company)
        session.flush()
        session.add(
            Job(
                source="test",
                external_job_id="resolved-company",
                company_id=company.id,
                title="Data Scientist",
                is_remote=True,
                status="active",
                posted_at=utcnow(),
            ),
        )
        session.flush()

        selected = select_ambitionbox_retry_targets(session, limit=None)

    assert len(selected) == 1
    assert selected[0].company_name == "UnitedHealth Group"
    assert selected[0].slug == "unitedhealth"
    assert selected[0].salary_url == resolved_url


def test_legacy_ambitionbox_404_is_normalized_to_terminal_missing():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        company = Company(
            name="Missing Co",
            market_profile_status="partial",
            market_profile_evidence={
                "sources": {
                    "glassdoor": {"status": "ok"},
                    "ambitionbox": {
                        "status": "error",
                        "urls": {"slug": "missing", "salaries": "https://x/missing"},
                        "error": "HTTPStatusError: 404 Not Found",
                    },
                    "levels_fyi": {"status": "missing"},
                },
            },
        )
        session.add(company)
        session.flush()
        session.add(
            Job(
                source="test",
                external_job_id="missing",
                company_id=company.id,
                title="Data Scientist",
                is_remote=True,
                status="active",
                posted_at=utcnow(),
            ),
        )
        session.commit()
        company_id = company.id

    assert normalize_legacy_ambitionbox_404s(factory) == 1

    with factory() as session:
        saved = session.get(Company, company_id)
        evidence = saved.market_profile_evidence["sources"]["ambitionbox"]
        assert evidence["status"] == "missing"
        assert evidence["url"] == "https://x/missing"
        assert "404" in evidence["legacy_error"]
        assert saved.market_profile_status == "done"
        retry_targets = select_ambitionbox_retry_targets(session, limit=None)
        assert [target.company_id for target in retry_targets] == [company_id]


def test_ambitionbox_only_batch_updates_no_other_source():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        company = Company(
            name="Example Co",
            market_profile_status="partial",
            glassdoor_overall_rating=4.2,
            glassdoor_wlb_rating=4.0,
            levels_fyi_estimated_salary_lpa=20.0,
            market_profile_evidence={
                "requested_salary_role": "Data Scientist",
                "sources": {
                    "glassdoor": {"status": "ok"},
                    "ambitionbox": {"status": "error", "error": "HTTP 403"},
                    "levels_fyi": {"status": "ok", "selected_role": "Data Scientist"},
                },
            },
        )
        session.add(company)
        session.flush()
        session.add(
            Job(
                source="test",
                external_job_id="one",
                company_id=company.id,
                title="Data Scientist",
                is_remote=True,
                status="active",
                posted_at=utcnow(),
            ),
        )
        session.commit()
        company_id = company.id

    class Collector:
        def collect_salaries(self, targets):
            assert [item.company_id for item in targets] == [company_id]
            yield AmbitionBoxObservation(
                company_id=company_id,
                status="ok",
                overall_rating=3.9,
                wlb_rating=3.7,
                salary_lpa=12.0,
                evidence={"status": "ok", "selected_role": "Data Scientist"},
            )

    results = run_ambitionbox_batch(
        limit=None,
        session_factory=factory,
        collector=Collector(),
    )

    assert len(results) == 1
    with factory() as session:
        saved = session.get(Company, company_id)
        assert saved.glassdoor_overall_rating == 4.2
        assert saved.glassdoor_wlb_rating == 4.0
        assert saved.levels_fyi_estimated_salary_lpa == 20.0
        assert saved.ambitionbox_overall_rating == 3.9
        assert saved.ambitionbox_wlb_rating == 3.7
        assert saved.ambitionbox_estimated_salary_lpa == 12.0
        assert saved.overall_rating == 4.0
        assert saved.wlb_rating == 4.0
        assert saved.estimated_salary_lpa == 12.0


def test_ambitionbox_only_batch_reuses_successful_broad_observation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    broad_url = "https://www.ambitionbox.com/salaries/adobe-salaries"
    with factory() as session:
        company = Company(
            name="Adobe",
            market_profile_status="done",
            ambitionbox_overall_rating=3.7,
            ambitionbox_wlb_rating=3.8,
            ambitionbox_estimated_salary_lpa=None,
            market_profile_evidence={
                "requested_salary_role": "Applied Scientist 4",
                "sources": {
                    "glassdoor": {"status": "missing"},
                    "ambitionbox": {
                        "status": "ok",
                        "company": "Adobe",
                        "url": broad_url,
                        "slug": "adobe",
                        "selected_role": None,
                    },
                    "levels_fyi": {"status": "missing"},
                },
            },
        )
        session.add(company)
        session.flush()
        session.add(
            Job(
                source="test",
                external_job_id="adobe-gap",
                company_id=company.id,
                title="Applied Scientist 4",
                is_remote=True,
                status="active",
                posted_at=utcnow(),
            ),
        )
        session.commit()
        company_id = company.id

    class Collector:
        def collect_salaries(self, targets):
            assert len(targets) == 1
            known = targets[0].known_broad_observation
            assert known is not None
            assert known.overall_rating == 3.7
            assert known.wlb_rating == 3.8
            assert known.evidence["url"] == broad_url
            yield AmbitionBoxObservation(
                company_id=company_id,
                status="ok",
                overall_rating=known.overall_rating,
                wlb_rating=known.wlb_rating,
                salary_lpa=17.95,
                evidence={
                    **known.evidence,
                    "selected_role": "Data Scientist",
                    "salary_lookup": {
                        "status": "ok",
                        "url": f"{broad_url}/data-scientist",
                    },
                },
            )

    results = run_ambitionbox_batch(
        limit=None,
        session_factory=factory,
        collector=Collector(),
    )

    assert len(results) == 1
    with factory() as session:
        saved = session.get(Company, company_id)
        assert saved.ambitionbox_overall_rating == 3.7
        assert saved.ambitionbox_wlb_rating == 3.8
        assert saved.ambitionbox_estimated_salary_lpa == 17.95
        assert saved.market_profile_evidence["sources"]["ambitionbox"][
            "salary_lookup"
        ]["status"] == "ok"


def test_levels_fyi_retry_selection_includes_done_missing_but_not_ok():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        missing = Company(
            name="Missing Co",
            market_profile_status="done",
            market_profile_evidence={
                "sources": {
                    "levels_fyi": {
                        "status": "missing",
                        "slug": "canonical-missing",
                    },
                },
            },
        )
        failed = Company(
            name="Failed Co",
            market_profile_status="partial",
            market_profile_evidence={
                "sources": {"levels_fyi": {"status": "error"}},
            },
        )
        complete = Company(
            name="Complete Co",
            market_profile_status="partial",
            market_profile_evidence={
                "sources": {"levels_fyi": {"status": "ok"}},
            },
        )
        session.add_all([missing, failed, complete])
        session.flush()
        for index, company in enumerate((missing, failed, complete)):
            session.add(
                Job(
                    source="test",
                    external_job_id=f"levels-{index}",
                    company_id=company.id,
                    title="Data Scientist",
                    is_remote=True,
                    status="active",
                    posted_at=utcnow(),
                ),
            )
        session.flush()

        selected = select_levels_fyi_retry_targets(session, limit=None)

    assert selected == [
        LevelsFyiTarget(
            missing.id,
            "Missing Co",
            "Data Scientist",
            1,
            "canonical-missing",
        ),
        LevelsFyiTarget(
            failed.id,
            "Failed Co",
            "Data Scientist",
            1,
            "failed-co",
        ),
    ]


def test_levels_fyi_only_batch_preserves_other_sources_and_uses_range(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'levels-batch.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        company = Company(
            name="UST",
            market_profile_status="done",
            glassdoor_overall_rating=4.2,
            glassdoor_wlb_rating=4.0,
            ambitionbox_overall_rating=3.9,
            ambitionbox_wlb_rating=3.7,
            market_profile_evidence={
                "requested_salary_role": "Data Scientist",
                "sources": {
                    "glassdoor": {"status": "ok", "source_id": "123"},
                    "ambitionbox": {"status": "ok", "slug": "ust"},
                    "levels_fyi": {"status": "missing", "slug": "ust"},
                },
            },
        )
        session.add(company)
        session.flush()
        session.add(
            Job(
                source="test",
                external_job_id="levels-ust",
                company_id=company.id,
                title="Data Scientist",
                is_remote=True,
                status="active",
                posted_at=utcnow(),
            ),
        )
        session.commit()
        company_id = company.id

    broad = (FIXTURES / "company_profile_levels_ust_broad.html").read_text()
    india = (FIXTURES / "company_profile_levels_ust_range_india.html").read_text()

    class Loader:
        def __init__(self):
            self.calls = []

        def __call__(self, source, url):
            self.calls.append((source, url))
            return broad if url.endswith("/salaries") else india

    loader = Loader()
    results = run_levels_fyi_batch(
        limit=1,
        session_factory=factory,
        page_loader=loader,
    )

    assert len(results) == 1
    assert results[0].source_statuses == {"levels_fyi": "ok"}
    assert loader.calls == [
        ("levels_fyi", "https://www.levels.fyi/companies/ust/salaries"),
        (
            "levels_fyi",
            "https://www.levels.fyi/companies/ust/salaries/data-scientist/locations/india",
        ),
    ]
    with factory() as session:
        saved = session.get(Company, company_id)
        assert saved.glassdoor_overall_rating == 4.2
        assert saved.glassdoor_wlb_rating == 4.0
        assert saved.ambitionbox_overall_rating == 3.9
        assert saved.ambitionbox_wlb_rating == 3.7
        assert saved.levels_fyi_estimated_salary_lpa == pytest.approx(30.05)
        assert saved.wlb_rating == 4.0
        assert saved.estimated_salary_lpa is None
        sources = saved.market_profile_evidence["sources"]
        assert sources["glassdoor"] == {"status": "ok", "source_id": "123"}
        assert sources["ambitionbox"] == {"status": "ok", "slug": "ust"}
        assert sources["levels_fyi"]["estimation_method"] == (
            "displayed_range_midpoint"
        )
