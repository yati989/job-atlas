from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.contacts.company_profile import (
    ContactCompanyProfileMissing,
    InvalidContactCompanyProfile,
    record_contact_company_profile,
    record_profile_discovery_company_profile,
    record_resolved_canonical_domain,
    stored_search_groups,
    search_group_for_profession,
    suggested_search_groups,
)
from app.models.orm import Base, Company, Job


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_suggested_groups_collapse_data_categories_and_keep_credit_separate():
    session = _session()
    company = Company(name="DualCo")
    session.add(company)
    session.flush()
    session.add_all([
        Job(source="test", external_job_id="ds", company_id=company.id, title="Data Scientist"),
        Job(source="test", external_job_id="risk", company_id=company.id, title="Credit Risk Manager"),
    ])
    session.flush()

    assert suggested_search_groups(session, company.id) == ["data_ai", "credit_risk"]


def test_record_profile_persists_all_contact_prerequisites_atomically():
    session = _session()
    company = Company(name="Acme")
    session.add(company)
    session.flush()

    record_contact_company_profile(
        session,
        company.id,
        canonical_domain=" ACME.COM ",
        domain_resolution_status="done",
        company_type="employer",
        search_groups=["credit_risk", "data_ai", "credit_risk"],
    )
    session.commit()

    assert company.canonical_domain == "acme.com"
    assert company.domain_resolution_status == "done"
    assert company.company_type == "employer"
    assert company.contact_search_groups == ["data_ai", "credit_risk"]
    assert company.contact_profile_enriched_at is not None


def test_unresolvable_profile_keeps_classification_without_a_domain():
    session = _session()
    company = Company(name="NoDomainCo")
    session.add(company)
    session.flush()

    record_contact_company_profile(
        session,
        company.id,
        canonical_domain=None,
        domain_resolution_status="unresolvable",
        company_type="staffing",
        search_groups=["data_ai"],
    )

    assert stored_search_groups(company) == ["data_ai"]
    assert company.company_type == "staffing"


def test_missing_or_invalid_profiles_fail_before_contact_search():
    company = Company(name="Missing")
    try:
        stored_search_groups(company)
        assert False, "expected ContactCompanyProfileMissing"
    except ContactCompanyProfileMissing:
        pass

    session = _session()
    session.add(company)
    session.flush()
    try:
        record_contact_company_profile(
            session,
            company.id,
            canonical_domain=None,
            domain_resolution_status="done",
            company_type="employer",
            search_groups=["data_ai"],
        )
        assert False, "expected InvalidContactCompanyProfile"
    except InvalidContactCompanyProfile:
        pass


def test_record_resolved_domain_does_not_complete_or_clobber_contact_profile():
    session = _session()
    company = Company(name="Acme")
    session.add(company)
    session.flush()

    assert record_resolved_canonical_domain(
        session,
        company.id,
        canonical_domain=" ACME.COM. ",
        mx_host=" MX.ACME.COM. ",
    ) == "saved"
    assert company.canonical_domain == "acme.com"
    assert company.domain_resolution_status == "done"
    assert company.contact_search_groups is None
    assert company.contact_profile_enriched_at is None

    assert record_resolved_canonical_domain(
        session,
        company.id,
        canonical_domain="acme.com",
        mx_host="mx.acme.com",
    ) == "unchanged"
    assert record_resolved_canonical_domain(
        session,
        company.id,
        canonical_domain="different.example",
        mx_host="mx.different.example",
    ) == "conflict"
    assert company.canonical_domain == "acme.com"


def test_links_only_profile_accepts_profession_group_without_mx_gate():
    session = _session()
    company = Company(name="DesignCo")
    session.add(company)
    session.flush()

    group = search_group_for_profession("Product Design")
    record_profile_discovery_company_profile(
        session,
        company.id,
        company_type="employer",
        search_groups=[group],
        canonical_domain="design.example",
    )

    assert stored_search_groups(company) == ["product_design"]
    assert company.canonical_domain == "design.example"
    assert company.domain_resolution_status is None
