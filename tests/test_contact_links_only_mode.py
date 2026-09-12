from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.contacts.agentic_batch import CompanyContext, JudgedContact, record_profile_links
from app.models.orm import (
    Base, Company, DecisionRun, LinkedInProfileLink,
    ProfileDiscoveryAuthorization, PublicSelectionScope, PublicSelectionScopeItem,
)


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def test_links_only_reuses_judged_contacts_but_stops_before_email_storage():
    session = _session()
    company = Company(name="Example Co", contact_search_groups=["engineering"])
    session.add_all([company, DecisionRun(
        id="run-1", since_at=datetime.now(timezone.utc),
        cutoff_at=datetime.now(timezone.utc), input_timezone="UTC",
        state="optional_stages", policy_snapshot={}, target_count=0,
    )])
    session.flush()
    scope = PublicSelectionScope(
        run_id="run-1", revision=1, mode="all_eligible", fingerprint="scope",
        job_count=1, company_count=1,
    )
    session.add(scope)
    session.flush()
    session.add(PublicSelectionScopeItem(
        scope_id=scope.id, posting_version_id=999, company_id=company.id,
    ))
    authorization = ProfileDiscoveryAuthorization(
        run_id="run-1", scope_id=scope.id, plan_fingerprint="plan",
        maximum_calls=2, retry_allowance=1, planned_query_count=1,
    )
    session.add(authorization)
    session.flush()
    context = CompanyContext(
        company_id=company.id, name=company.name, company_type="employer",
        canonical_domain="example.com", industry=None, hq_location=None,
        search_groups=["engineering"], ladder={}, quotas={},
    )
    judged = JudgedContact(
        full_name="Person One", title="Engineering Manager person@example.com at Example Co",
        profile_url="https://in.linkedin.com/in/person-one/?trk=search", tier="head",
        search_group="engineering", tier_rationale="current relevant leader",
        judged_text="Person One person@example.com", query="provider query",
        email="person@example.com", email_confidence="mx_verified",
    )

    first = record_profile_links(
        session, authorization_id=authorization.id, context=context, contacts=[judged],
    )
    judged.profile_url = "https://www.linkedin.com/in/person-one/"
    repeated = record_profile_links(
        session, authorization_id=authorization.id, context=context, contacts=[judged],
    )
    revised_scope = PublicSelectionScope(
        run_id="run-1", revision=2, mode="manual", fingerprint="scope-2",
        job_count=1, company_count=1,
    )
    session.add(revised_scope); session.flush()
    session.add(PublicSelectionScopeItem(
        scope_id=revised_scope.id, posting_version_id=1000, company_id=company.id,
    ))
    revised_authorization = ProfileDiscoveryAuthorization(
        run_id="run-1", scope_id=revised_scope.id, plan_fingerprint="plan-2",
        query_plan=[], maximum_calls=0, retry_allowance=0, planned_query_count=0,
    )
    session.add(revised_authorization); session.flush()
    cross_scope = record_profile_links(
        session, authorization_id=revised_authorization.id,
        context=context, contacts=[judged],
    )

    assert repeated[0].id == first[0].id
    assert cross_scope[0].id == first[0].id
    row = session.scalar(select(LinkedInProfileLink))
    assert row.linkedin_url == "https://www.linkedin.com/in/person-one"
    assert "@" not in row.headline
    assert row.evidence == {
        "tier": "head", "search_group": "engineering",
        "tier_rationale": "current relevant leader",
    }
    assert "@" not in str(row.evidence)
    assert "email" not in set(LinkedInProfileLink.__table__.columns.keys())
