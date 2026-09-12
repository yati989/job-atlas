from __future__ import annotations

from app.models.schemas import NormalizedJob
from app.pipeline.relevance import (
    RelevancePolicy,
    evaluate_after_semantic_role,
    evaluate_relevance,
)
from app.workflows.planning import SearchProfile
import pytest


def _policy(**overrides) -> RelevancePolicy:
    data = {
        "version": 1,
        "name": "india-operations",
        "professions": ["operations analyst"],
        "search_terms": ["operations analyst"],
        "relevance_terms": [
            "operations analyst",
            "operations manager",
            "business operations",
        ],
        "title_exclusions": ["sales"],
        "countries": ["IN"],
        "cities": ["Bengaluru"],
        "arrangements": ["remote", "hybrid", "onsite"],
        "seniority": ["individual_contributor", "manager"],
        "collection_window_days": 30,
        "source_selection": {"mode": "all_compatible"},
    }
    data.update(overrides)
    return RelevancePolicy.from_profile(SearchProfile.model_validate(data))


def _job(
    title: str,
    location: str | None,
    *,
    is_remote: bool | None = None,
    employment_type: str | None = None,
    seniority: str | None = None,
):
    return NormalizedJob(
        source="fixture",
        external_job_id="1",
        title=title,
        company_name_raw="Example Co",
        location_raw=location,
        is_remote=is_remote,
        employment_type=employment_type,
        seniority=seniority,
    )


def test_non_default_profession_and_india_scope_drive_role_and_location():
    policy = _policy()

    assert evaluate_relevance(
        _job("Senior Operations Analyst", "Remote - India", is_remote=True), policy
    ).outcome == "kept"
    rejected = evaluate_relevance(
        _job("Data Scientist", "Remote - India", is_remote=True), policy
    )
    assert (rejected.outcome, rejected.axis) == ("rejected", "role")


@pytest.mark.parametrize("location", [
    "Pune", "Mumbai", "Bengaluru", "India", "Perundurai",
    "Coimbatore(Ganapathy)", "Ernakulam", "Kanyakumari", "Varanasi(Manduwadih)",
])
def test_india_wide_local_search_accepts_indian_locations(location):
    assert evaluate_relevance(
        _job("Operations Analyst", location, is_remote=False),
        _policy(cities=[], arrangements=["onsite", "remote"]),
    ).outcome == "kept"


def test_city_aliases_match_but_explicit_foreign_country_does_not():
    policy = _policy(cities=["Gurugram"], arrangements=["onsite"])
    assert evaluate_relevance(_job("Operations Analyst", "Gurgaon"), policy).outcome == "kept"
    policy = _policy(cities=["Delhi"], arrangements=["onsite"])
    assert evaluate_relevance(_job("Operations Analyst", "Delhi, Canada"), policy).outcome == "rejected"


def test_arbitrary_requested_city_remains_supported_without_a_closed_city_list():
    policy = _policy(cities=["Dibrugarh"], arrangements=["onsite"])
    assert evaluate_relevance(_job("Operations Analyst", "Dibrugarh, India"), policy).outcome == "kept"


def test_unknown_location_and_foreign_city_do_not_become_india_wide_matches():
    policy = _policy(cities=[], arrangements=["onsite"])
    assert evaluate_relevance(_job("Operations Analyst", "Unknown"), policy).outcome == "needs_review"
    assert evaluate_relevance(_job("Operations Analyst", "Pune, United States"), policy).outcome == "rejected"


def test_unclear_remote_location_is_review_not_confirmed_remote():
    decision = evaluate_relevance(
        _job("Operations Analyst", "Remote", is_remote=True), _policy()
    )

    assert (decision.outcome, decision.axis, decision.reason) == (
        "needs_review",
        "location",
        "remote country is unclear",
    )


def test_work_arrangement_and_city_are_independent_policy_axes():
    remote_only = _policy(arrangements=["remote"])
    onsite = _job("Operations Analyst", "Bengaluru, India", is_remote=False)
    assert evaluate_relevance(onsite, remote_only).reason == "onsite work is not selected"

    onsite_policy = _policy(arrangements=["onsite"])
    assert evaluate_relevance(onsite, onsite_policy).outcome == "kept"
    unclear_city = _job("Operations Analyst", "India", is_remote=False)
    assert evaluate_relevance(unclear_city, onsite_policy).outcome == "needs_review"


def test_saved_seniority_does_not_reject_management_roles():
    ic_only = _policy(seniority=["individual_contributor"])

    for title in ("Operations Manager", "Operations Director", "VP Operations"):
        assert evaluate_after_semantic_role(
            _job(title, "Remote - India", is_remote=True), ic_only
        ).outcome == "kept"


@pytest.mark.parametrize("job", [
    _job("Operations Intern", "Remote - India", is_remote=True),
    _job(
        "Operations Analyst", "Remote - India", is_remote=True,
        employment_type="Internship",
    ),
    _job(
        "Operations Analyst", "Remote - India", is_remote=True,
        employment_type="Part-time",
    ),
])
def test_internships_and_part_time_are_excluded_by_default(job):
    decision = evaluate_relevance(job, _policy())

    assert (decision.outcome, decision.axis) == ("rejected", "job_type")


def test_internships_and_part_time_require_explicit_opt_in():
    policy = _policy(include_internships=True, include_part_time=True)

    assert evaluate_after_semantic_role(
        _job("Operations Intern", "Remote - India", is_remote=True), policy
    ).outcome == "kept"
    assert evaluate_relevance(
        _job(
            "Operations Analyst", "Remote - India", is_remote=True,
            employment_type="Part-time",
        ),
        policy,
    ).outcome == "kept"


def test_exact_country_remote_fallback_is_explicit_opt_in():
    job = _job("Operations Analyst", "India", is_remote=None)
    base = _policy(
        countries=["IN"], cities=["Bengaluru"], arrangements=["remote"]
    )
    assert evaluate_relevance(job, base).outcome == "needs_review"

    opted_in = _policy(
        countries=["IN"],
        cities=["Bengaluru"],
        arrangements=["remote"],
        location_fallbacks={"exact_country_as_remote": True},
    )
    decision = evaluate_relevance(job, opted_in)
    assert (decision.outcome, decision.reason) == (
        "kept",
        "exact-country remote fallback",
    )


def test_explicit_hard_reject_uses_posting_evidence():
    policy = _policy(hard_rejects=["requires US work authorization"])
    job = _job("Operations Analyst", "Remote - India", is_remote=True)
    job.description_raw = "This position requires US work authorization."

    decision = evaluate_relevance(job, policy)

    assert (decision.outcome, decision.axis) == ("rejected", "hard_reject")


def test_experience_uses_extracted_requirement_and_unknown_is_reviewable():
    policy = _policy(experience={"minimum_years": 2, "maximum_years": 5})
    unknown = _job("Operations Analyst", "Remote - India", is_remote=True)
    excessive = _job("Operations Analyst", "Remote - India", is_remote=True)
    excessive.raw_payload["pre_gate_job_enrichment"] = {
        "experience_min_years": 8,
    }

    assert evaluate_relevance(unknown, policy).reason == "experience requirement is unknown"
    decision = evaluate_relevance(excessive, policy)
    assert (decision.outcome, decision.axis) == ("rejected", "experience")
