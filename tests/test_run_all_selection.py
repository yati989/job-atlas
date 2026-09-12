from app.pipeline.run_all import (
    normalize_sample_location,
    normalize_sample_search,
    select_connectors,
)
from app.pipeline.registry import ACTIVE_CONNECTORS
from scripts.run_headed_sources import CONNECTORS as HEADED_CONNECTORS


def test_source_slice_selects_exactly_the_ten_challenged_indeed_instances():
    selected = select_connectors(
        list(HEADED_CONNECTORS),
        source="indeed",
        index_range="2:12",
    )

    assert [
        (connector.search, connector.location_mode) for connector in selected
    ] == [
        ("data analyst", "bengaluru"),
        ("data analyst", "remote_india"),
        ("data engineer", "bengaluru"),
        ("data engineer", "remote_india"),
        ("machine learning engineer", "bengaluru"),
        ("machine learning engineer", "remote_india"),
        ("ai engineer", "bengaluru"),
        ("ai engineer", "remote_india"),
        ("credit risk", "bengaluru"),
        ("credit risk", "remote_india"),
    ]


def test_one_per_source_prefers_data_scientist_remote_without_mutating_registry():
    connectors = [*ACTIVE_CONNECTORS, *HEADED_CONNECTORS]
    original_wwr_terms = next(
        connector for connector in ACTIVE_CONNECTORS
        if connector.source_name == "weworkremotely"
    ).search_terms.copy()

    selected = select_connectors(
        connectors,
        one_per_source=True,
        sample_search="data scientist",
    )

    assert len(selected) == len({connector.source_name for connector in connectors}) == 19
    assert len({connector.source_name for connector in selected}) == len(selected)
    by_source = {connector.source_name: connector for connector in selected}
    for source in ("linkedin", "ziprecruiter", "wellfound", "instahyre",
                   "timesjobs", "naukri", "glassdoor", "indeed"):
        assert "remote" in str(by_source[source].location_mode)
        assert by_source[source].search == "data scientist"
    assert by_source["foundit"].search == "data scientist remote"
    assert by_source["weworkremotely"].search_terms == ["data scientist"]
    assert by_source["efinancialcareers"].search_terms == ["data scientist"]
    assert original_wwr_terms == next(
        connector for connector in ACTIVE_CONNECTORS
        if connector.source_name == "weworkremotely"
    ).search_terms


def test_collection_scope_aliases_are_canonical_and_remote_matches_every_spelling():
    assert normalize_sample_search("data science") == "data scientist"
    assert normalize_sample_search("machine learning") == "machine learning engineer"
    assert normalize_sample_location("remote India") == "remote"

    connectors = [*ACTIVE_CONNECTORS, *HEADED_CONNECTORS]
    selected = select_connectors(
        connectors,
        one_per_source=True,
        sample_search=normalize_sample_search("machine learning"),
        sample_location=normalize_sample_location("remote India"),
    )
    by_source = {connector.source_name: connector for connector in selected}
    for source in (
        "linkedin", "ziprecruiter", "wellfound", "instahyre",
        "timesjobs", "naukri", "glassdoor", "indeed",
    ):
        assert "remote" in str(by_source[source].location_mode)
        assert by_source[source].search == "machine learning engineer"


def test_one_per_source_can_prefer_bengaluru_and_exclude_a_source():
    connectors = [*ACTIVE_CONNECTORS, *HEADED_CONNECTORS]

    selected = select_connectors(
        connectors,
        one_per_source=True,
        sample_search="data scientist",
        sample_location="bengaluru",
        exclude_sources={"indeed"},
    )

    by_source = {connector.source_name: connector for connector in selected}
    assert len(selected) == 18
    assert "indeed" not in by_source
    assert by_source["linkedin"].location_mode == "bengaluru"
    assert by_source["ziprecruiter"].location_mode == "bengaluru"
    assert by_source["wellfound"].location_mode == "bengaluru"
    assert by_source["shine"].location_mode == "bangalore"


def test_location_mode_selects_all_linkedin_remote_instances():
    selected = select_connectors(
        list(ACTIVE_CONNECTORS),
        source="linkedin",
        location_mode="remote_india",
    )

    assert len(selected) == 6
    assert {connector.search for connector in selected} == {
        "data scientist",
        "data analyst",
        "data engineer",
        "machine learning engineer",
        "ai engineer",
        "credit risk",
    }
    assert {connector.location_mode for connector in selected} == {"remote_india"}


def test_per_source_index_range_selects_the_remaining_ten_for_every_source():
    connectors = [*ACTIVE_CONNECTORS, *HEADED_CONNECTORS]

    selected = select_connectors(
        connectors,
        per_source_index_range="2:12",
    )

    all_by_source = {}
    selected_by_source = {}
    for connector in connectors:
        all_by_source.setdefault(connector.source_name, []).append(connector)
    for connector in selected:
        selected_by_source.setdefault(connector.source_name, []).append(connector)

    assert selected_by_source == {
        source: instances[2:12]
        for source, instances in all_by_source.items()
        if instances[2:12]
    }
