from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.workflows.planning import SourceCapability, prepare_run
from app.workflows.source_catalog import SOURCE_CATALOG


CATALOG = (
    SourceCapability(
        name="workingnomads",
        runtime="http",
        countries=frozenset({"*"}),
        arrangements=frozenset({"remote"}),
        instances_per_term=True,
        rationale="Global remote board with native title search.",
    ),
    SourceCapability(
        name="himalayas",
        runtime="http",
        countries=frozenset({"IN"}),
        arrangements=frozenset({"remote"}),
        instances_per_term=True,
        rationale="Remote board with a native country filter.",
    ),
    SourceCapability(
        name="indeed",
        runtime="attended_browser",
        countries=frozenset({"IN"}),
        arrangements=frozenset({"remote", "hybrid", "onsite"}),
        instances_per_term=True,
        instances_per_location=True,
        prerequisites=("desktop session",),
        rationale="Visible browser required; India edition is verified.",
    ),
)


def _profile(source_selection: dict | None = None) -> dict:
    return {
        "version": 1,
        "name": "india-operations",
        "professions": ["operations analyst"],
        "search_terms": ["operations analyst", "process analyst"],
        "relevance_terms": ["operations analyst", "business operations"],
        "title_exclusions": ["sales"],
        "countries": ["IN"],
        "cities": ["Bengaluru"],
        "arrangements": ["remote"],
        "seniority": ["individual_contributor", "manager"],
        "experience": {"minimum_years": 2, "maximum_years": 6},
        "hard_rejects": ["requires US work authorization"],
        "collection_window_days": 30,
        "source_selection": source_selection
        or {"mode": "named", "sources": ["workingnomads", "himalayas"]},
    }


def test_mapping_and_structured_file_produce_the_same_effective_preview(tmp_path: Path):
    profile = _profile()
    path = tmp_path / "search-profile.yaml"
    path.write_text(
        """\
version: 1
name: india-operations
professions: [operations analyst]
search_terms: [operations analyst, process analyst]
relevance_terms: [operations analyst, business operations]
title_exclusions: [sales]
countries: [IN]
cities: [Bengaluru]
arrangements: [remote]
seniority: [individual_contributor, manager]
experience: {minimum_years: 2, maximum_years: 6}
hard_rejects: [requires US work authorization]
collection_window_days: 30
source_selection:
  mode: named
  sources: [workingnomads, himalayas]
""",
        encoding="utf-8",
    )

    assert prepare_run(profile, CATALOG) == prepare_run(path, CATALOG)


def test_named_selection_keeps_compatible_india_sources():
    preview = prepare_run(
        _profile({"mode": "named", "sources": ["indeed", "workingnomads"]}),
        CATALOG,
    )

    assert [source.name for source in preview.selected_sources] == [
        "indeed", "workingnomads",
    ]
    assert preview.unsupported_sources == ()
    assert preview.source_count == 2
    assert preview.query_instance_count == 4


def test_count_selection_proposes_named_sources_and_reports_a_shortfall():
    preview = prepare_run(_profile({"mode": "count", "count": 4}), CATALOG)

    assert [source.name for source in preview.selected_sources] == [
        "workingnomads",
        "himalayas",
        "indeed",
    ]
    assert preview.requested_source_count == 4
    assert preview.source_shortfall == 1
    assert preview.requires_confirmation is True


def test_preview_distinguishes_attended_runtime_and_source_instance_counts():
    profile = _profile({"mode": "named", "sources": ["indeed"]})
    profile["arrangements"] = ["remote", "onsite"]

    preview = prepare_run(profile, CATALOG)

    assert preview.source_count == 1
    assert preview.query_instance_count == 4
    assert preview.selected_sources[0].attendance_required is True
    assert preview.selected_sources[0].prerequisites == ("desktop session",)


def test_profile_rejects_missing_relevance_policy_instead_of_inventing_it():
    profile = _profile()
    del profile["relevance_terms"]

    with pytest.raises(ValidationError):
        prepare_run(profile, CATALOG)


def test_public_beta_rejects_non_india_country_clearly():
    profile = _profile()
    profile["countries"] = ["CA"]

    with pytest.raises(ValidationError, match="supports only country code IN"):
        prepare_run(profile, CATALOG)


def test_public_collection_window_defaults_to_14_days_and_caps_at_30():
    defaulted = _profile()
    del defaulted["collection_window_days"]
    assert prepare_run(defaulted, CATALOG).profile.collection_window_days == 14

    maximum = _profile()
    maximum["collection_window_days"] = 30
    assert prepare_run(maximum, CATALOG).profile.collection_window_days == 30

    too_wide = _profile()
    too_wide["collection_window_days"] = 31
    with pytest.raises(ValidationError, match="less than or equal to 30"):
        prepare_run(too_wide, CATALOG)


def test_production_catalog_has_unique_names_and_reviewable_metadata():
    names = [source.name for source in SOURCE_CATALOG]
    assert names
    assert len(names) == len(set(names))
    assert all(source.rationale and source.coverage for source in SOURCE_CATALOG)
    indeed = next(source for source in SOURCE_CATALOG if source.name == "indeed")
    assert indeed.runtime == "attended_browser"
    assert indeed.prerequisites == ("desktop session",)


def test_broad_global_feed_discloses_lack_of_native_country_filter():
    preview = prepare_run(
        _profile({"mode": "named", "sources": ["weworkremotely"]}),
        SOURCE_CATALOG,
    )

    assert preview.selected_sources[0].coverage == "global remote feed; country is listing evidence"
    assert preview.selected_sources[0].query_instance_count == 1


def test_non_bengaluru_local_profile_keeps_city_capable_sources():
    profile = _profile({"mode": "named", "sources": ["builtin", "foundit", "shine"]})
    profile["cities"] = ["Pune"]
    profile["arrangements"] = ["onsite"]

    preview = prepare_run(profile, SOURCE_CATALOG)

    assert [source.name for source in preview.selected_sources] == ["builtin", "foundit", "shine"]
    assert preview.unsupported_sources == ()


def test_foundit_preview_counts_every_requested_city_and_remote_instance():
    profile = _profile({"mode": "named", "sources": ["foundit"]})
    profile["cities"] = ["Pune", "Mumbai"]
    profile["arrangements"] = ["remote", "onsite"]

    preview = prepare_run(profile, SOURCE_CATALOG)

    assert preview.query_instance_count == 6


def test_cityless_local_profile_selects_india_wide_source():
    profile = _profile({"mode": "named", "sources": ["builtin"]})
    profile["cities"] = []
    profile["arrangements"] = ["onsite"]

    preview = prepare_run(profile, SOURCE_CATALOG)

    assert [source.name for source in preview.selected_sources] == ["builtin"]
    assert preview.query_instance_count == 2


def test_mixed_city_or_remote_search_can_use_remote_only_boards():
    profile = _profile({"mode": "named", "sources": ["himalayas", "workingnomads"]})
    profile["cities"] = ["Pune", "Mumbai"]
    profile["arrangements"] = ["remote", "onsite"]
    preview = prepare_run(profile, SOURCE_CATALOG)
    assert preview.source_count == 2
    assert preview.query_instance_count == 4
    profile["arrangements"] = ["onsite"]
    assert prepare_run(profile, SOURCE_CATALOG).source_count == 0
