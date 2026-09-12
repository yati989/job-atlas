"""
Recipient <-> job pairing (app/outreach/pairing.py).

The one property worth protecting here is the refusal: a resume tailored
for one function must never be handed to a recipient in a different
function, so "no matching job" has to fall back to master rather than
picking the company's best job regardless of fit.

SQLite in-memory, same pattern as test_agentic_batch.py / test_prospect_batch.py.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.contacts.ladder import TALENT_ACQUISITION
from app.models.orm import Base, Company, Job
from app.outreach import pairing


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(company_id, title, *, enriched=True):
    return Job(
        source="test", external_job_id=title, company_id=company_id, title=title,
        enrichment_status="done" if enriched else "pending",
    )


# ------------------------------------------------------- recipient title ---

def test_recipient_search_group_classifies_a_real_title():
    assert pairing.recipient_search_group("Senior Data Scientist") == "data_ai"
    assert pairing.recipient_search_group("Credit Risk Manager") == "credit_risk"


def test_recipient_search_group_none_for_unclassifiable_or_empty():
    assert pairing.recipient_search_group("Software Engineer") is None
    assert pairing.recipient_search_group(None) is None
    assert pairing.recipient_search_group("  ") is None


# ------------------------------------------------------------------ pair ---

def test_no_company_is_always_master():
    session = _session()
    result = pairing.pair(session, company_id=None, recipient_tier="ic", recipient_title="Data Scientist")
    assert result.resume_kind == "master"
    assert result.reason == pairing.REASON_COMPANY_IS_PROSPECT


def test_company_with_no_jobs_is_master():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()

    result = pairing.pair(session, company_id=c.id, recipient_tier="ic", recipient_title="Data Scientist")
    assert result.resume_kind == "master"
    assert result.reason == pairing.REASON_NO_JOBS_AT_COMPANY


def test_recipient_function_matches_only_jobs_in_that_function():
    """The core refusal: a credit-risk job must never be offered as a
    candidate for a data-science recipient, even when it's the only job the
    company has open."""
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    ds_job = _job(c.id, "Senior Data Scientist")
    risk_job = _job(c.id, "Credit Risk Manager")
    session.add_all([ds_job, risk_job])
    session.flush()

    result = pairing.pair(session, company_id=c.id, recipient_tier="ic", recipient_title="Senior Data Scientist")
    assert result.resume_kind == "tailored"
    assert result.reason == pairing.REASON_RECIPIENT_FUNCTION_MATCH
    assert result.candidate_job_ids == [ds_job.id]
    assert risk_job.id not in result.candidate_job_ids


def test_no_job_in_recipients_function_falls_back_to_master():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    session.add(_job(c.id, "Credit Risk Manager"))
    session.flush()

    result = pairing.pair(session, company_id=c.id, recipient_tier="hiring_manager", recipient_title="Data Scientist")
    assert result.resume_kind == "master"
    assert result.reason == pairing.REASON_NO_MATCHING_JOB


def test_unclassifiable_recipient_title_falls_back_to_master():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    session.add(_job(c.id, "Data Scientist"))
    session.flush()

    result = pairing.pair(session, company_id=c.id, recipient_tier="ic", recipient_title="Software Engineer")
    assert result.resume_kind == "master"
    assert result.reason == pairing.REASON_RECIPIENT_FUNCTION_UNKNOWN


def test_recruiter_gets_every_enriched_job_regardless_of_function():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    ds_job = _job(c.id, "Data Scientist")
    risk_job = _job(c.id, "Credit Risk Manager")
    unenriched = _job(c.id, "ML Engineer", enriched=False)
    session.add_all([ds_job, risk_job, unenriched])
    session.flush()

    result = pairing.pair(session, company_id=c.id, recipient_tier=TALENT_ACQUISITION, recipient_title="Talent Acquisition Partner")
    assert result.resume_kind == "tailored"
    assert result.reason == pairing.REASON_RECRUITER_BEST_MATCH
    assert set(result.candidate_job_ids) == {ds_job.id, risk_job.id}
    assert unenriched.id not in result.candidate_job_ids


def test_recruiter_falls_back_to_unenriched_jobs_if_none_are_enriched():
    """Better to offer an unenriched job as a candidate than to wrongly
    report no_jobs_at_company when jobs genuinely exist."""
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    unenriched = _job(c.id, "Data Scientist", enriched=False)
    session.add(unenriched)
    session.flush()

    result = pairing.pair(session, company_id=c.id, recipient_tier=TALENT_ACQUISITION, recipient_title="Recruiter")
    assert result.resume_kind == "tailored"
    assert result.candidate_job_ids == [unenriched.id]


# --------------------------------------------------------- duplicate jobs ---

def test_duplicate_jobs_are_excluded_from_candidates():
    session = _session()
    c = Company(name="Acme")
    session.add(c)
    session.flush()
    canonical = _job(c.id, "Data Scientist")
    session.add(canonical)
    session.flush()
    dup = _job(c.id, "Data Scientist (dup)")
    dup.duplicate_of_job_id = canonical.id
    session.add(dup)
    session.flush()

    result = pairing.pair(session, company_id=c.id, recipient_tier="ic", recipient_title="Data Scientist")
    assert result.candidate_job_ids == [canonical.id]


# ------------------------------------------------------ PairingResult ---

def test_pairing_result_rejects_inconsistent_kind_and_reason():
    with pytest.raises(ValueError):
        pairing.PairingResult(resume_kind="master", reason=pairing.REASON_RECIPIENT_FUNCTION_MATCH)
    with pytest.raises(ValueError):
        pairing.PairingResult(resume_kind="tailored", reason=pairing.REASON_NO_JOBS_AT_COMPANY)


def test_pairing_result_rejects_unknown_reason():
    with pytest.raises(ValueError):
        pairing.PairingResult(resume_kind="master", reason="vibes")


# ------------------------------------------------- subject_domain_for_job ---

def test_subject_domain_credit_risk():
    job = _job(1, "Credit Risk Strategy")
    assert pairing.subject_domain_for_job(job) == "Credit Risk"


def test_subject_domain_analytics():
    job = _job(1, "Data Analyst")
    assert pairing.subject_domain_for_job(job) == "Analytics"


def test_subject_domain_data_engineering():
    job = _job(1, "Data Engineer")
    assert pairing.subject_domain_for_job(job) == "Data Engineering"


def test_subject_domain_quant_decision_science():
    job = _job(1, "Quantitative Researcher")
    assert pairing.subject_domain_for_job(job) == "Decision Science"


def test_subject_domain_none_for_plain_data_science():
    """"Data Scientist" needs no suffix — the base subject already says it."""
    job = _job(1, "Senior Data Scientist")
    assert pairing.subject_domain_for_job(job) is None


def test_subject_domain_none_for_unclassifiable_title():
    job = _job(1, "Software Engineer")
    assert pairing.subject_domain_for_job(job) is None


def test_subject_domain_inherits_classify_titles_first_match_order():
    """Known, accepted ambiguity: "Machine Learning Engineer" contains
    data_science's "machine learning" keyword, which is declared before
    ai_ml_engineering, so classify_title returns data_science (-> no
    suffix) rather than the more specific ai_ml_engineering. This function
    must agree with classify_title, not second-guess it."""
    job = _job(1, "Machine Learning Engineer")
    assert pairing.subject_domain_for_job(job) is None
