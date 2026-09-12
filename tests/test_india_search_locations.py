"""Public location choices must reach collection, not just configuration."""
import pytest

from app.pipeline.registry import SOURCE_CATALOG, connector_instances
from app.workflows.planning import SearchProfile, prepare_run


def profile(source, cities=(), arrangements=("onsite",)):
    return SearchProfile.model_validate({
        "version": 1, "name": "location-test", "professions": ["data analyst"],
        "search_terms": ["data analyst"], "relevance_terms": ["data analyst"],
        "countries": ["IN"], "cities": cities, "arrangements": arrangements,
        "seniority": ["individual_contributor"], "collection_window_days": 30,
        "source_selection": {"mode": "named", "sources": [source]},
    })


@pytest.mark.parametrize("source", [
    "builtin", "glassdoor", "indeed", "instahyre", "linkedin", "naukri",
    "shine", "talentcom", "timesjobs", "wellfound", "ziprecruiter", "efinancialcareers", "iimjobs",
])
@pytest.mark.parametrize("cities,arrangements,count", [
    (("Pune",), ("onsite",), 1),
    (("Pune", "Mumbai"), ("onsite", "hybrid", "remote"), 3),
    ((), ("onsite",), 1),
    ((), ("onsite", "remote"), 2),
    ((), ("remote",), 1),
])
def test_location_preview_matches_real_instances(source, cities, arrangements, count):
    requested = profile(source, cities, arrangements)
    preview = prepare_run(requested, SOURCE_CATALOG)
    assert not preview.unsupported_sources
    instances = connector_instances(source, requested)
    assert preview.query_instance_count == len(instances) == count
    local = [item for item in instances if item.location_mode in {"city", "india"}]
    if "onsite" in arrangements:
        assert [item.location for item in local] == (list(cities) if cities else [None])
        assert [item.location_mode for item in local] == (["city"] * len(cities) if cities else ["india"])
    else:
        assert not local


def test_foundit_india_and_remote_are_separate_queries():
    requested = profile("foundit", (), ("remote", "onsite"))
    preview = prepare_run(requested, SOURCE_CATALOG)
    instances = connector_instances("foundit", requested)
    assert preview.query_instance_count == len(instances) == 2
    assert {item.search for item in instances} == {"data analyst remote", "data analyst India"}


def test_city_is_part_of_recorded_connector_dimensions():
    from app.pipeline.runner import _safe_connector_dimensions
    from app.pipeline.progress import _dimensions
    instances = connector_instances("naukri", profile("naukri", ("Pune", "Mumbai")))
    for dimensions in (_safe_connector_dimensions, _dimensions):
        assert [dimensions(item)["location"] for item in instances] == ["Pune", "Mumbai"]


@pytest.mark.parametrize("source", ["foundit", "timesjobs"])
def test_detail_screening_receives_accepted_profile(source):
    instances = connector_instances(source, profile(source, ("Pune",)))
    assert all(item.relevance_policy.cities == ("Pune",) for item in instances)
    assert all(item.relevance_policy.role_terms == ("data analyst",) for item in instances)


@pytest.mark.parametrize("cities,arrangements,expected", [
    (("Pune",), ("onsite",), {"pune"}),
    ((), ("onsite",), {"pune", "mumbai"}),
    ((), ("remote",), {"remote"}),
    (("Pune",), ("onsite", "remote"), {"pune", "remote"}),
])
def test_profile_reaches_fetch_gate_storage_and_resume(monkeypatch, cities, arrangements, expected):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.collectors.html.naukri import NaukriConnector
    from app.models.orm import Base, Job
    from app.models.schemas import NormalizedJob
    from app.workflows.connector_factory import build_fetchers
    from app.workflows.guided_run import start_guided_run, collect_guided_run

    calls = []

    def fetch(connector):
        calls.append((connector.location_mode, connector.location))
        # Real sources can return off-location or promoted listings. The gate
        # must use the frozen profile, never the old Bengaluru policy.
        return [NormalizedJob(
            source="naukri", external_job_id=key, title="Data Analyst",
            company_name_raw="Example", location_raw=location, is_remote=remote,
            job_url=f"https://example.com/{key}",
        ) for key, location, remote in [
            ("pune", "Pune", False), ("mumbai", "Mumbai, India", False),
            ("remote", "Remote - India", True), ("foreign", "London, United Kingdom", False),
        ]]

    monkeypatch.setattr(NaukriConnector, "fetch", fetch)
    preview = prepare_run(profile("naukri", cities, arrangements), SOURCE_CATALOG)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        start_guided_run(session, preview=preview, profile_fingerprint="test", confirmed=True, run_id="locations")
        fetchers = build_fetchers(preview, allow_attended=False)
        result = collect_guided_run(session, run_id="locations", fetchers=fetchers)
        assert result.state == "collected"
        assert {job.external_job_id for job in session.scalars(select(Job))} == expected
        assert len(calls) == preview.query_instance_count
        collect_guided_run(session, run_id="locations", fetchers=fetchers)
        assert len(calls) == preview.query_instance_count
