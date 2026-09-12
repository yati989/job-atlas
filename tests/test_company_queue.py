"""
The prioritised company queue (issue #72) — the one genuinely new piece of
logic in the agentic-search contact-finding pivot (#68). A good test here
exercises externally observable behaviour: given known companies/jobs, the
queue returns them in the specified precedence — remote before non-remote,
preferred roles before less-preferred, recent before stale, pinned before
everything — not internals.

Runs against SQLite in-memory, same pattern as test_persistence.py.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.contacts.company_queue import get_company_queue
from app.models.orm import Base, Company, Job


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _company(session, name, **kwargs):
    kwargs.setdefault("contact_enrichment_status", "pending")
    kwargs.setdefault("contact_search_groups", ["data_ai"])
    company = Company(name=name, **kwargs)
    session.add(company)
    session.flush()
    return company


def _job(session, company, title, is_remote=False, posted_at=None):
    session.add(Job(
        source="test", external_job_id=f"{company.id}-{title}",
        company_id=company.id, title=title, is_remote=is_remote,
        posted_at=posted_at,
    ))
    session.flush()


def test_remote_ranks_before_non_remote():
    session = _session()
    onsite = _company(session, "OnsiteCo")
    remote = _company(session, "RemoteCo")
    _job(session, onsite, "Data Scientist", is_remote=False)
    _job(session, remote, "Data Scientist", is_remote=True)
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert [c.name for c in queue] == ["RemoteCo", "OnsiteCo"]


def test_role_preference_ai_before_ml_before_data_scientist_before_credit_risk_before_analytics():
    session = _session()
    analytics = _company(session, "AnalyticsCo")
    credit = _company(session, "CreditCo")
    ds = _company(session, "DataScienceCo")
    ml = _company(session, "MLCo")
    ai = _company(session, "AICo")
    _job(session, analytics, "Data Analyst")
    _job(session, credit, "Credit Risk Analyst")
    _job(session, ds, "Data Scientist")
    _job(session, ml, "Machine Learning Engineer")
    _job(session, ai, "AI Engineer")
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert [c.name for c in queue] == [
        "AICo", "MLCo", "DataScienceCo", "CreditCo", "AnalyticsCo",
    ]


def test_company_ranks_by_its_best_job_not_worst():
    session = _session()
    company = _company(session, "MixedCo")
    _job(session, company, "Data Analyst")  # weakest match
    _job(session, company, "AI Engineer", is_remote=True)  # best match
    other = _company(session, "OnlyAnalyticsCo")
    _job(session, other, "Data Analyst", is_remote=True)
    session.commit()

    queue = get_company_queue(session, limit=10)
    # MixedCo's best job (remote AI Engineer) outranks OnlyAnalyticsCo's only
    # job (remote Data Analyst), even though MixedCo also has a weak match.
    assert [c.name for c in queue] == ["MixedCo", "OnlyAnalyticsCo"]


def test_recency_breaks_ties_within_same_remote_and_role_band():
    session = _session()
    older = _company(session, "OlderCo")
    newer = _company(session, "NewerCo")
    now = datetime.now(timezone.utc)
    _job(session, older, "Data Scientist", posted_at=now - timedelta(days=10))
    _job(session, newer, "Data Scientist", posted_at=now - timedelta(days=1))
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert [c.name for c in queue] == ["NewerCo", "OlderCo"]


def test_posting_count_breaks_ties_after_recency():
    session = _session()
    session.commit()
    fewer = _company(session, "FewerPostingsCo")
    more = _company(session, "MorePostingsCo")
    _job(session, fewer, "Data Scientist")
    _job(session, more, "Data Scientist")
    _job(session, more, "Senior Data Scientist")
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert [c.name for c in queue] == ["MorePostingsCo", "FewerPostingsCo"]


def test_pinned_company_jumps_to_front_regardless_of_heuristics():
    session = _session()
    remote_ai = _company(session, "RemoteAICo")
    onsite_analytics = _company(session, "OnsiteAnalyticsCo")
    _job(session, remote_ai, "AI Engineer", is_remote=True)
    _job(session, onsite_analytics, "Data Analyst", is_remote=False)
    session.commit()

    queue = get_company_queue(session, limit=10, pinned=["OnsiteAnalyticsCo"])
    assert [c.name for c in queue] == ["OnsiteAnalyticsCo", "RemoteAICo"]


def test_company_type_does_not_affect_ordering():
    session = _session()
    staffing = _company(session, "StaffingCo", company_type="staffing")
    employer = _company(session, "EmployerCo", company_type="employer")
    now = datetime.now(timezone.utc)
    # Give the staffing firm the objectively better job on every ranked
    # axis; if company_type leaked into ordering it would still lose.
    _job(session, staffing, "AI Engineer", is_remote=True, posted_at=now)
    _job(session, employer, "Data Analyst", is_remote=False, posted_at=now - timedelta(days=30))
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert [c.name for c in queue] == ["StaffingCo", "EmployerCo"]


def test_non_pending_company_excluded():
    session = _session()
    pending = _company(session, "PendingCo")
    done = _company(session, "DoneCo", contact_enrichment_status="done")
    _job(session, pending, "Data Scientist")
    _job(session, done, "Data Scientist")
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert [c.name for c in queue] == ["PendingCo"]


def test_company_without_contact_profile_excluded():
    session = _session()
    ready = _company(session, "ReadyCo")
    missing = _company(session, "MissingCo", contact_search_groups=None)
    _job(session, ready, "Data Scientist")
    _job(session, missing, "Data Scientist")
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert [c.name for c in queue] == ["ReadyCo"]


def test_company_with_no_target_category_job_excluded():
    session = _session()
    relevant = _company(session, "RelevantCo")
    irrelevant = _company(session, "IrrelevantCo")
    _job(session, relevant, "Data Scientist")
    _job(session, irrelevant, "Sales Executive")
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert [c.name for c in queue] == ["RelevantCo"]


def test_limit_caps_returned_companies():
    session = _session()
    for i in range(5):
        c = _company(session, f"Co{i}")
        _job(session, c, "Data Scientist")
    session.commit()

    queue = get_company_queue(session, limit=2)
    assert len(queue) == 2


def test_placeholder_named_companies_excluded():
    """Ingestion debt the queue surfaced: 'Unknown' and 'name' are merged
    buckets holding many different employers, and some historical sources truncated long
    names to 'X...'. None can be searched for, so none may consume a batch
    slot. A real short name like 'EY' must survive."""
    session = _session()
    for name in ["Unknown", "name", "H...", "R...", "EY", "RealCo"]:
        c = _company(session, name)
        _job(session, c, "Data Scientist")
    session.commit()

    queue = get_company_queue(session, limit=10)
    assert sorted(c.name for c in queue) == ["EY", "RealCo"]
