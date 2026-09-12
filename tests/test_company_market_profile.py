from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.companies.market_profile import (
    _company,
    calculate_market_profile,
    save_ambitionbox_market_profile,
    save_glassdoor_market_profile,
    save_source_market_profile,
)
from app.models.orm import Base, Company


def test_source_persistence_locks_and_refreshes_company_before_json_merge():
    company = Company(id=42, name="Example Co")
    captured = {}

    class Result:
        def scalar_one_or_none(self):
            return company

    class Session:
        def execute(self, statement):
            captured["statement"] = statement
            return Result()

    assert _company(Session(), 42) is company
    statement = captured["statement"]
    assert statement._for_update_arg is not None
    assert statement.get_execution_options()["populate_existing"] is True


def _company_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    company = Company(
        name="Example Co",
        overall_rating=4.9,
        wlb_rating=4.8,
        estimated_salary_lpa=99,
    )
    session.add(company)
    session.flush()
    return session, company


def test_source_values_are_saved_without_changing_combined_values():
    session, company = _company_session()
    evidence = {
        "sources": {
            "glassdoor": {"status": "ok", "source_id": "gd-1"},
            "ambitionbox": {"status": "ok"},
            "levels_fyi": {"status": "missing"},
        },
    }

    result = save_source_market_profile(
        session,
        company.id,
        glassdoor_overall_rating=3.8,
        glassdoor_wlb_rating=3.6,
        ambitionbox_overall_rating=4.0,
        ambitionbox_wlb_rating=3.4,
        ambitionbox_estimated_salary_lpa=12.6,
        levels_fyi_estimated_salary_lpa=None,
        evidence=evidence,
    )

    assert result.glassdoor_overall_rating == 3.8
    assert result.glassdoor_wlb_rating == 3.6
    assert result.ambitionbox_overall_rating == 4.0
    assert result.ambitionbox_wlb_rating == 3.4
    assert result.ambitionbox_estimated_salary_lpa == 12.6
    assert result.levels_fyi_estimated_salary_lpa is None
    assert (result.overall_rating, result.wlb_rating, result.estimated_salary_lpa) == (
        4.9, 4.8, 99,
    )
    assert result.market_profile_evidence == evidence
    assert result.market_profile_status == "done"
    assert isinstance(result.market_profile_updated_at, datetime)


def test_combination_is_a_separate_stage_with_an_explicit_overall_rule():
    session, company = _company_session()
    company.glassdoor_overall_rating = 3.8
    company.glassdoor_wlb_rating = 3.5
    company.ambitionbox_overall_rating = 4.2
    company.ambitionbox_wlb_rating = 4.0
    company.ambitionbox_estimated_salary_lpa = 12.6
    company.levels_fyi_estimated_salary_lpa = 17.0
    company.market_profile_evidence = {
        "requested_salary_role": "Data Scientist",
        "sources": {
            "ambitionbox": {"selected_role": "Business Analyst"},
            "levels_fyi": {"selected_role": "Data Scientist"},
        },
    }

    result = calculate_market_profile(session, company.id)
    assert result.overall_rating == 4.0
    assert result.wlb_rating == 3.5
    assert result.estimated_salary_lpa == 12.6
    assert result.market_profile_evidence["combined"]["salary"] == {
        "selected_role": "business analyst",
        "sources": ["ambitionbox"],
        "method": "single_source",
    }
    assert result.market_profile_evidence["combined"]["wlb"] == {
        "source": "glassdoor",
        "method": "single_source",
    }


def test_salary_uses_ambitionbox_even_when_levels_has_the_same_role():
    session, company = _company_session()
    company.ambitionbox_estimated_salary_lpa = 12.6
    company.levels_fyi_estimated_salary_lpa = 17.0
    company.market_profile_evidence = {
        "requested_salary_role": "Data Scientist",
        "sources": {
            "ambitionbox": {"selected_role": "Data Scientist"},
            "levels_fyi": {"selected_role": "Data Scientist"},
        },
    }

    result = calculate_market_profile(session, company.id)

    assert result.estimated_salary_lpa == 12.6
    assert result.market_profile_evidence["combined"]["salary"]["sources"] == [
        "ambitionbox",
    ]
    assert result.market_profile_evidence["combined"]["salary"]["method"] == "single_source"


def test_combined_salary_uses_updated_software_before_data_engineer_ranking():
    session, company = _company_session()
    company.ambitionbox_estimated_salary_lpa = 12.0
    company.levels_fyi_estimated_salary_lpa = 30.0
    company.market_profile_evidence = {
        "requested_salary_role": "Data Scientist",
        "sources": {
            "ambitionbox": {"selected_role": "Data Engineer"},
            "levels_fyi": {"selected_role": "Software Engineer"},
        },
    }

    result = calculate_market_profile(session, company.id)

    assert result.estimated_salary_lpa == 12.0
    assert result.market_profile_evidence["combined"]["salary"] == {
        "selected_role": "data engineer",
        "sources": ["ambitionbox"],
        "method": "single_source",
    }

def test_genai_variants_are_reconciled_as_ai_engineer():
    session, company = _company_session()
    company.ambitionbox_estimated_salary_lpa = 20.0
    company.levels_fyi_estimated_salary_lpa = 30.0
    company.market_profile_evidence = {
        "requested_salary_role": "GenAI Developer",
        "sources": {
            "ambitionbox": {"selected_role": "GenAI Engineer"},
            "levels_fyi": {"selected_role": "AI Engineer"},
        },
    }

    result = calculate_market_profile(session, company.id)

    assert result.estimated_salary_lpa == 20.0
    assert result.market_profile_evidence["combined"]["salary"] == {
        "selected_role": "ai engineer",
        "sources": ["ambitionbox"],
        "method": "single_source",
    }


def test_invalid_source_values_are_rejected():
    session, company = _company_session()
    with pytest.raises(ValueError, match="between 1 and 5"):
        save_source_market_profile(
            session,
            company.id,
            glassdoor_overall_rating=0,
            glassdoor_wlb_rating=None,
            ambitionbox_overall_rating=None,
            ambitionbox_wlb_rating=None,
            ambitionbox_estimated_salary_lpa=None,
            levels_fyi_estimated_salary_lpa=None,
            evidence={},
        )


def test_ambitionbox_only_save_preserves_other_sources():
    session, company = _company_session()
    company.glassdoor_overall_rating = 3.8
    company.glassdoor_wlb_rating = 3.6
    company.levels_fyi_estimated_salary_lpa = 17.0
    company.market_profile_evidence = {
        "requested_salary_role": "Data Scientist",
        "sources": {
            "glassdoor": {"status": "ok", "source_id": "gd-1"},
            "ambitionbox": {"status": "error", "error": "HTTP 403"},
            "levels_fyi": {"status": "ok", "selected_role": "Data Scientist"},
        },
    }

    result = save_ambitionbox_market_profile(
        session,
        company.id,
        overall_rating=3.9,
        wlb_rating=3.5,
        salary_lpa=12.6,
        evidence={"status": "ok", "selected_role": "Data Scientist"},
    )

    assert result.glassdoor_overall_rating == 3.8
    assert result.glassdoor_wlb_rating == 3.6
    assert result.levels_fyi_estimated_salary_lpa == 17.0
    assert result.ambitionbox_overall_rating == 3.9
    assert result.ambitionbox_wlb_rating == 3.5
    assert result.ambitionbox_estimated_salary_lpa == 12.6
    assert result.market_profile_evidence["sources"]["glassdoor"]["source_id"] == "gd-1"
    assert result.market_profile_evidence["sources"]["ambitionbox"]["status"] == "ok"
    assert result.market_profile_status == "done"


def test_glassdoor_failure_preserves_prior_company_metadata():
    session, company = _company_session()
    company.glassdoor_review_count = 500
    company.employee_count_range = "1,001 to 5,000 Employees"
    company.ownership_type = "private"
    company.revenue = "$1 to $5 billion (USD)"

    result = save_glassdoor_market_profile(
        session,
        company.id,
        overall_rating=None,
        wlb_rating=None,
        evidence={"status": "error", "reason": "snapshot unavailable"},
    )

    assert result.glassdoor_review_count == 500
    assert result.employee_count_range == "1,001 to 5,000 Employees"
    assert result.ownership_type == "private"
    assert result.revenue == "$1 to $5 billion (USD)"


def test_throttled_refresh_preserves_last_successful_ambitionbox_observation():
    session, company = _company_session()
    company.ambitionbox_overall_rating = 4.0
    company.ambitionbox_wlb_rating = 3.8
    company.ambitionbox_estimated_salary_lpa = 14.0
    company.market_profile_evidence = {
        "sources": {
            "glassdoor": {"status": "ok"},
            "ambitionbox": {
                "status": "ok",
                "selected_role": "Data Scientist",
                "url": "https://example.test/salary",
            },
            "levels_fyi": {"status": "missing"},
        },
    }

    result = save_ambitionbox_market_profile(
        session,
        company.id,
        overall_rating=None,
        wlb_rating=None,
        salary_lpa=None,
        evidence={"status": "deferred", "reason": "circuit open"},
    )

    assert result.ambitionbox_overall_rating == 4.0
    assert result.ambitionbox_wlb_rating == 3.8
    assert result.ambitionbox_estimated_salary_lpa == 14.0
    saved = result.market_profile_evidence["sources"]["ambitionbox"]
    assert saved["status"] == "ok"
    assert saved["last_refresh"]["status"] == "deferred"
    assert result.market_profile_status == "done"
