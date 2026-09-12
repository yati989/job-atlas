import pytest

from app.workflows.connector_factory import build_fetchers, connector_instances
from app.workflows.planning import prepare_run
from app.workflows.source_catalog import SOURCE_CATALOG


def _profile(sources):
    return {
        "version": 1,
        "name": "india-operations",
        "professions": ["operations analyst"],
        "search_terms": ["operations analyst", "process analyst"],
        "relevance_terms": ["operations analyst", "process analyst"],
        "countries": ["IN"],
        "arrangements": ["remote"],
        "seniority": ["individual_contributor"],
        "collection_window_days": 14,
        "source_selection": {"mode": "named", "sources": sources},
    }


def test_profile_terms_country_and_window_reach_himalayas_instances():
    preview = prepare_run(_profile(["himalayas"]), SOURCE_CATALOG)
    instances = connector_instances("himalayas", preview.profile)
    assert [(item.search, item.country, item.max_age_days) for item in instances] == [
        ("operations analyst", "IN", 14),
        ("process analyst", "IN", 14),
    ]
    assert preview.query_instance_count == 2


def test_only_selected_fetchers_are_built_and_attended_requires_opt_in():
    preview = prepare_run(_profile(["himalayas", "weworkremotely"]), SOURCE_CATALOG)
    assert set(build_fetchers(preview, allow_attended=False)) == {
        "himalayas", "weworkremotely",
    }

    attended = prepare_run(_profile(["indeed"]), SOURCE_CATALOG)
    with pytest.raises(ValueError, match="desktop session"):
        build_fetchers(attended, allow_attended=False)
