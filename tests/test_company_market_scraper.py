from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.companies.ambitionbox import AmbitionBoxTarget, parse_salary_page
from app.companies.market_scraper import (
    _closest_role,
    _identity_matches,
    _levels_fyi,
    _role_family,
    scrape_levels_fyi_market_profile,
    scrape_market_profile,
)
from app.config.categories import SEARCH_TERMS
from app.models.orm import Base, Company


FIXTURES = Path(__file__).parent / "fixtures"


def test_source_identity_accepts_brand_spacing_only():
    assert _identity_matches("JPMorganChase", "JPMorgan Chase")
    assert not _identity_matches("JPMorganChase", "Morgan Chase")


def _session(company_name: str):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    company = Company(
        name=company_name,
        overall_rating=4.9,
        wlb_rating=4.8,
        estimated_salary_lpa=99,
    )
    session.add(company)
    session.flush()
    return session, company


def _scrape_parked_levels(
    session,
    company_id,
    *,
    salary_role,
    source_urls,
    glassdoor_record=None,
    page_loader,
):
    """Exercise the retained collector directly; production leaves it parked."""
    return scrape_levels_fyi_market_profile(
        session,
        company_id,
        salary_role=salary_role,
        levels_config=source_urls["levels_fyi"],
        page_loader=page_loader,
    )


def test_three_sources_are_persisted_independently_without_combining():
    session, company = _session("Ecolab India")
    ambitionbox_url = "https://www.ambitionbox.com/salaries/ecolab-salaries"
    ambitionbox = parse_salary_page(
        (FIXTURES / "company_profile_ambitionbox_salaries.html").read_text(),
        target=AmbitionBoxTarget(
            company_id=company.id,
            company_name="Ecolab",
            salary_role="Data Scientist",
            slug="ecolab",
            salary_url=ambitionbox_url,
        ),
    )

    result = scrape_market_profile(
        session,
        company.id,
        salary_role="Data Scientist",
        source_urls={
            "glassdoor": {
                "overview": "https://glassdoor.example/ecolab",
                "company_name": "Ecolab",
            },
            "ambitionbox": {
                "slug": "ecolab",
                "company_name": "Ecolab",
                "salaries": ambitionbox_url,
            },
        },
        glassdoor_record={
            "id": "gd-ecolab",
            "company": "Ecolab",
            "url_overview": "https://glassdoor.example/ecolab",
            "ratings_overall": 3.8,
            "ratings_work_life_balance": 3.6,
            "reviews_count": "1,234",
            "details_size": "10,001+ Employees",
            "details_type": "Company - Public",
            "details_revenue": "$10+ billion (USD)",
        },
        ambitionbox_record=ambitionbox.as_market_profile_record(),
        page_loader=lambda *_args: (_ for _ in ()).throw(
            AssertionError("parked Levels.fyi must not be requested")
        ),
    )

    assert result.glassdoor_overall_rating == 3.8
    assert result.glassdoor_wlb_rating == 3.6
    assert result.glassdoor_review_count == 1234
    assert result.employee_count_range == "10,001+ Employees"
    assert result.ownership_type == "public"
    assert result.revenue == "$10+ billion (USD)"
    assert result.company_type == "employer"
    assert result.ambitionbox_overall_rating == 3.9
    assert result.ambitionbox_wlb_rating == 3.5
    assert result.ambitionbox_estimated_salary_lpa == 12.6
    assert result.levels_fyi_estimated_salary_lpa is None
    assert (result.overall_rating, result.wlb_rating, result.estimated_salary_lpa) == (
        4.9, 4.8, 99,
    )
    sources = result.market_profile_evidence["sources"]
    assert sources["glassdoor"]["source_id"] == "gd-ecolab"
    assert sources["glassdoor"]["review_count"] == 1234
    assert sources["glassdoor"]["ownership_type_raw"] == "Company - Public"
    assert sources["ambitionbox"]["selected_role"] == "Data Scientist"
    assert sources["levels_fyi"]["status"] == "not_attempted"
    assert sources["levels_fyi"]["policy_status"] == "parked"
    assert result.market_profile_status == "done"


def test_a_source_identity_failure_does_not_discard_another_source():
    session, company = _session("Google India")
    levels_calls = []

    result = scrape_market_profile(
        session,
        company.id,
        salary_role="Data Scientist",
        source_urls={
            "glassdoor": {
                "overview": "https://glassdoor.example/google",
                "company_name": "Google",
            },
            "levels_fyi": {
                "company_slug": "google",
                "company_name": "Google",
            },
        },
        glassdoor_record={
            "id": "wrong-company",
            "company": "Alphabet Staffing",
            "ratings_overall": 4.8,
            "ratings_work_life_balance": 4.7,
        },
        page_loader=lambda *_args: levels_calls.append(_args),
    )

    assert result.glassdoor_overall_rating is None
    assert result.glassdoor_wlb_rating is None
    assert result.levels_fyi_estimated_salary_lpa is None
    assert levels_calls == []
    sources = result.market_profile_evidence["sources"]
    assert sources["glassdoor"]["status"] == "error"
    assert sources["levels_fyi"]["policy_status"] == "parked"
    assert result.market_profile_status == "partial"


def test_glassdoor_employer_id_must_match_resolved_overview_url():
    session, company = _session("Google India")

    result = scrape_market_profile(
        session,
        company.id,
        salary_role="Data Scientist",
        source_urls={
            "glassdoor": {
                "overview": (
                    "https://www.glassdoor.com/Overview/"
                    "Working-at-Google-EI_IE9079.11,17.htm"
                ),
                "company_name": "Google",
            },
        },
        glassdoor_record={
            "id": "6036",
            "company": "Google",
            "ratings_overall": 4.4,
            "ratings_work_life_balance": 4.2,
        },
        page_loader=lambda _source, _url: "",
    )

    assert result.glassdoor_overall_rating is None
    assert "employer ID mismatch" in (
        result.market_profile_evidence["sources"]["glassdoor"]["error"]
    )


def test_visible_levels_median_wins_and_role_fallback_preserves_seniority():
    html = (FIXTURES / "company_profile_levels_google.html").read_text()
    assert _levels_fyi(html, "Google", "Data Scientist")["salary_lpa"] == 82.5

    roles = [
        {"title": "Senior Data Scientist"},
        {"title": "Business Analyst"},
        {"title": "Data Analyst"},
    ]
    assert _closest_role(roles, "Data Scientist", "title")["title"] == "Data Analyst"
    assert (
        _closest_role(roles, "Senior Data Scientist", "title")["title"]
        == "Senior Data Scientist"
    )


def test_market_profile_roles_cover_registry_search_terms_and_genai_variants():
    assert all(_role_family(term) is not None for term in SEARCH_TERMS)
    assert _role_family("GenAI Developer") == "ai_engineer"
    assert _role_family("GenAI Engineer") == "ai_engineer"
    assert _role_family("ML Engineer") == "machine_learning_engineer"
    assert _role_family("Credit Risk Analyst") == "credit_risk"

def test_levels_discovers_exact_role_from_broad_page_before_fetching_india_salary():
    session, company = _session("UST")
    broad = (FIXTURES / "company_profile_levels_ust_broad.html").read_text()
    salary = (
        FIXTURES / "company_profile_levels_ust_data_scientist_india.html"
    ).read_text()
    calls = []

    def load(_source, url):
        calls.append(url)
        return broad if len(calls) == 1 else salary

    result = _scrape_parked_levels(
        session,
        company.id,
        salary_role="Data Scientist",
        source_urls={
            "levels_fyi": {
                "company_slug": "ust-guessed",
                "company_name": "UST",
            },
        },
        glassdoor_record=None,
        page_loader=load,
    )

    assert calls == [
        "https://www.levels.fyi/companies/ust-guessed/salaries",
        "https://www.levels.fyi/companies/ust/salaries/data-scientist/locations/india",
    ]
    assert result.levels_fyi_estimated_salary_lpa == pytest.approx(29.5)
    evidence = result.market_profile_evidence["sources"]["levels_fyi"]
    assert evidence["status"] == "ok"
    assert evidence["selected_role"] == "Data Scientist"
    assert evidence["slug"] == "ust"
    assert evidence["broad_url"] == "https://www.levels.fyi/companies/ust/salaries"
    assert [role["name"] for role in evidence["discovered_roles"]] == [
        "Software Engineer",
        "Data Scientist",
        "Business Analyst",
    ]


def test_levels_uses_midpoint_of_displayed_india_average_range():
    session, company = _session("UST")
    pages = iter(
        (
            (FIXTURES / "company_profile_levels_ust_broad.html").read_text(),
            (FIXTURES / "company_profile_levels_ust_range_india.html").read_text(),
        )
    )

    result = _scrape_parked_levels(
        session,
        company.id,
        salary_role="Data Scientist",
        source_urls={
            "levels_fyi": {"company_slug": "ust", "company_name": "UST"},
        },
        glassdoor_record=None,
        page_loader=lambda _source, _url: next(pages),
    )

    assert result.levels_fyi_estimated_salary_lpa == pytest.approx(30.05)
    evidence = result.market_profile_evidence["sources"]["levels_fyi"]
    assert evidence["status"] == "ok"
    assert evidence["estimation_method"] == "displayed_range_midpoint"
    assert evidence["observed_range"] == ["₹2.75M", "₹3.26M"]


def test_levels_uses_median_of_india_rows_when_aggregate_is_unavailable():
    session, company = _session("Fiserv")
    broad = (
        FIXTURES / "company_profile_levels_fiserv_exact_broad.html"
    ).read_text()
    salary = (
        FIXTURES / "company_profile_levels_fiserv_rows_india.html"
    ).read_text()
    rows = (FIXTURES / "company_profile_levels_fiserv_rows.json").read_text()
    calls = []

    def load(_source, url):
        calls.append(url)
        if "api.levels.fyi" in url:
            return rows
        return broad if len(calls) == 1 else salary

    result = _scrape_parked_levels(
        session,
        company.id,
        salary_role="Data Scientist",
        source_urls={
            "levels_fyi": {"company_slug": "fiserv", "company_name": "Fiserv"},
        },
        glassdoor_record=None,
        page_loader=load,
    )

    assert len(calls) == 3
    assert "countryIds%5B%5D=113" in calls[-1]
    assert result.levels_fyi_estimated_salary_lpa == pytest.approx(26.0)
    evidence = result.market_profile_evidence["sources"]["levels_fyi"]
    assert evidence["status"] == "ok"
    assert evidence["estimation_method"] == "india_rows_median"
    assert evidence["observed_row_salaries_lpa"] == pytest.approx([10.0, 42.0])
    assert evidence["row_count"] == 2


def test_levels_selects_best_discovered_fallback_role():
    session, company = _session("Fiserv")
    software_page = (
        FIXTURES / "company_profile_levels_fiserv_data_analyst_india.html"
    ).read_text().replace("Data Analyst", "Software Engineer")
    pages = {
        "https://www.levels.fyi/companies/fiserv/salaries": (
            FIXTURES / "company_profile_levels_fiserv_broad.html"
        ).read_text(),
        (
            "https://www.levels.fyi/companies/fiserv/salaries/"
            "software-engineer/locations/india"
        ): software_page,
    }
    calls = []

    result = _scrape_parked_levels(
        session,
        company.id,
        salary_role="Data Scientist",
        source_urls={
            "levels_fyi": {"company_slug": "fiserv", "company_name": "Fiserv"},
        },
        glassdoor_record=None,
        page_loader=lambda _source, url: calls.append(url) or pages[url],
    )

    assert calls == list(pages)
    assert result.levels_fyi_estimated_salary_lpa == 18.0
    evidence = result.market_profile_evidence["sources"]["levels_fyi"]
    assert evidence["selected_role"] == "Software Engineer"
    assert evidence["selected_role_slug"] == "software-engineer"


def test_parked_levels_records_missing_when_fallback_has_no_salary():
    session, company = _session("Example Co")
    broad = (
        FIXTURES / "company_profile_levels_no_compatible_role.html"
    ).read_text()
    calls = []

    def load(_source, url):
        calls.append(url)
        return '{"rows": [], "total": 0}' if "api.levels.fyi" in url else broad

    result = _scrape_parked_levels(
        session,
        company.id,
        salary_role="Credit Risk Analyst",
        source_urls={
            "levels_fyi": {
                "company_slug": "example-co",
                "company_name": "Example Co",
            },
        },
        glassdoor_record=None,
        page_loader=load,
    )

    assert calls[0] == "https://www.levels.fyi/companies/example-co/salaries"
    assert calls[1].endswith("/software-engineer/locations/india")
    assert calls[2].startswith("https://api.levels.fyi/v3/salary/search?")
    assert result.levels_fyi_estimated_salary_lpa is None
    evidence = result.market_profile_evidence["sources"]["levels_fyi"]
    assert evidence["status"] == "missing"
    assert evidence["broad_url"] == calls[0]
    assert evidence["discovered_roles"] == [
        {
            "name": "Software Engineer",
            "slug": "software-engineer",
            "url": (
                "https://www.levels.fyi/companies/example-co/salaries/"
                "software-engineer"
            ),
        },
        {
            "name": "Product Manager",
            "slug": "product-manager",
            "url": (
                "https://www.levels.fyi/companies/example-co/salaries/"
                "product-manager"
            ),
        },
    ]


def test_levels_route_http_failure_is_error_not_missing():
    session, company = _session("UST")
    broad = (FIXTURES / "company_profile_levels_ust_broad.html").read_text()

    def load(_source, url):
        if url.endswith("/salaries"):
            return broad
        raise RuntimeError("HTTP 405")

    result = _scrape_parked_levels(
        session,
        company.id,
        salary_role="Data Scientist",
        source_urls={
            "levels_fyi": {"company_slug": "ust", "company_name": "UST"},
        },
        glassdoor_record=None,
        page_loader=load,
    )

    evidence = result.market_profile_evidence["sources"]["levels_fyi"]
    assert evidence["status"] == "error"
    assert "HTTP 405" in evidence["error"]
    assert evidence["url"].endswith("/data-scientist/locations/india")
    assert evidence["selected_role"] == "Data Scientist"
    assert [role["name"] for role in evidence["discovered_roles"]] == [
        "Software Engineer",
        "Data Scientist",
        "Business Analyst",
    ]
