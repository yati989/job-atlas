"""
Mechanical support for the agentic /find-contacts skill (#73). The judgement
half is the agent's and is verified by the human trial gate (#75); what is
testable here is the bookkeeping around it — quota/status arithmetic, the
store-without-email reversal of #20, and dedup on re-run.

SQLite in-memory, same pattern as test_persistence.py / test_company_queue.py.
"""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.contacts import ladder as L
from app.contacts.agentic_batch import (
    CompanyContext, JudgedContact, _status_for, batch_report,
    context_for_company_id, record_company,
    STATUS_DONE, STATUS_PARTIAL, STATUS_NO_CATEGORY_MATCH,
)
from app.models.orm import Base, Company, Contact, Job


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _context(company_id=1, company_type="employer", groups=(L.DATA_AI,), name="Acme"):
    groups = list(groups)
    return CompanyContext(
        company_id=company_id, name=name, company_type=company_type,
        canonical_domain="acme.com", industry=None, hq_location=None,
        search_groups=groups,
        ladder={g: L.ladder_for(company_type, g) for g in groups},
        quotas=L.quotas_for(company_type),
    )


def _contact(tier, name="A Person", email="a@example.com", group=L.DATA_AI):
    return JudgedContact(
        full_name=name, title="T", profile_url=f"https://x/in/{name.replace(' ','-')}",
        tier=tier, search_group=group, tier_rationale="r", judged_text="t",
        query="q", email=email, email_confidence="verified" if email else None,
    )


def _fill_all_quotas(group=L.DATA_AI, company_type="employer"):
    """One contact per quota slot for every non-conditional tier."""
    out = []
    quotas = L.quotas_for(company_type)
    ladder = L.ladder_for(company_type, group)
    for tier, quota in quotas.items():
        if tier in L.CONDITIONAL_TIERS or tier not in ladder:
            continue
        for i in range(quota):
            out.append(_contact(tier, name=f"{tier}{i}", group=group))
    return out


def test_no_search_groups_is_no_category_match():
    context = _context(groups=())
    assert _status_for([], context) == STATUS_NO_CATEGORY_MATCH


def test_context_consumes_stored_search_groups_instead_of_rederiving_jobs():
    session = _session()
    company = Company(
        name="StoredProfileCo",
        company_type="employer",
        contact_search_groups=[L.CREDIT_RISK],
    )
    session.add(company)
    session.flush()
    session.add(Job(
        source="test",
        external_job_id="data-job",
        company_id=company.id,
        title="Data Scientist",
    ))
    session.flush()

    context = context_for_company_id(session, company.id)

    assert context.search_groups == [L.CREDIT_RISK]
    assert set(context.ladder) == {L.CREDIT_RISK}


def test_context_builds_a_generic_ladder_for_public_profession_group():
    session = _session()
    company = Company(
        name="DesignCo",
        company_type="employer",
        contact_search_groups=["product_design"],
    )
    session.add(company)
    session.flush()

    context = context_for_company_id(session, company.id)

    assert context.search_groups == ["product_design"]
    assert context.ladder["product_design"][L.HEAD][0] == "Head of Product Design"


def test_product_manager_group_uses_natural_product_leadership_titles():
    ladder = L.ladder_for("employer", "product_manager")

    assert ladder[L.HEAD] == ["Head of Product", "VP Product"]
    assert ladder[L.HIRING_MANAGER] == ["Director of Product", "Product Lead"]
    assert ladder[L.IC] == ["Senior Product Manager", "Product Manager"]
    assert ladder[L.TALENT_ACQUISITION] == [
        "Talent Acquisition OR Human Resources OR Recruiter",
        "Technical Recruiter OR Talent Recruiter",
    ]
    assert "Clinical Recruiter" not in ladder[L.TALENT_ACQUISITION]
    assert "Product Manager Manager" not in {
        title for titles in ladder.values() for title in titles
    }


def test_all_quotas_filled_is_done():
    context = _context()
    assert _status_for(_fill_all_quotas(), context) == STATUS_DONE


def test_short_of_quota_is_partial():
    context = _context()
    contacts = _fill_all_quotas()[:-1]
    assert _status_for(contacts, context) == STATUS_PARTIAL


def test_exec_fallback_is_not_required_for_done():
    """It's conditional — a company with no exec_fallback contact at all
    still reaches done once the four real tiers are filled."""
    context = _context()
    contacts = _fill_all_quotas()
    assert not any(c.tier == L.EXEC_FALLBACK for c in contacts)
    assert _status_for(contacts, context) == STATUS_DONE


def test_contact_without_email_does_not_count_toward_quota():
    """#68 user story 9: quota counts contacts that have an email."""
    context = _context()
    contacts = _fill_all_quotas()
    contacts[0].email = None
    contacts[0].email_confidence = None
    assert _status_for(contacts, context) == STATUS_PARTIAL


def test_staffing_company_only_needs_recruiters_for_done():
    """A staffing firm's other tiers are structurally unfillable, so its
    ladder is recruiter-only and done requires just that quota (#74)."""
    context = _context(company_type="staffing")
    contacts = _fill_all_quotas(company_type="staffing")
    assert {c.tier for c in contacts} == {L.TALENT_ACQUISITION}
    assert _status_for(contacts, context) == STATUS_DONE


def test_two_search_groups_need_both_filled():
    context = _context(groups=(L.DATA_AI, L.CREDIT_RISK))
    only_one = _fill_all_quotas(group=L.DATA_AI)
    assert _status_for(only_one, context) == STATUS_PARTIAL
    both = only_one + _fill_all_quotas(group=L.CREDIT_RISK)
    assert _status_for(both, context) == STATUS_DONE


def test_record_company_stores_contact_with_no_email():
    """The #20 reversal: an underivable address must not drop the contact."""
    session = _session()
    company = Company(name="Acme", contact_enrichment_status="pending")
    session.add(company)
    session.flush()

    context = _context(company_id=company.id)
    record_company(session, context, [_contact(L.HEAD, email=None)])
    session.commit()

    rows = session.execute(select(Contact)).scalars().all()
    assert len(rows) == 1
    assert rows[0].email_guess is None
    assert rows[0].seniority_tier == L.HEAD


def test_rerunning_a_company_updates_rather_than_duplicates():
    session = _session()
    company = Company(name="Acme", contact_enrichment_status="pending")
    session.add(company)
    session.flush()
    context = _context(company_id=company.id)

    record_company(session, context, [_contact(L.HEAD, email=None)])
    session.commit()
    record_company(session, context, [_contact(L.HEAD, email="h@example.com")])
    session.commit()

    rows = session.execute(select(Contact)).scalars().all()
    assert len(rows) == 1                       # deduped on (company, url)
    assert rows[0].email_guess == "h@example.com"  # and updated


def test_reviewed_rerun_can_replace_and_remove_rejected_contacts():
    session = _session()
    company = Company(name="Acme", contact_enrichment_status="pending")
    session.add(company)
    session.flush()
    context = _context(company_id=company.id)

    accepted = _contact(L.IC, name="Accepted Person")
    rejected = _contact(L.HIRING_MANAGER, name="Rejected Person")
    record_company(session, context, [accepted, rejected])
    session.commit()

    accepted.title = "Corrected title"
    record_company(session, context, [accepted], replace_existing=True)
    session.commit()

    rows = session.execute(select(Contact)).scalars().all()
    assert [(row.full_name, row.title) for row in rows] == [
        ("Accepted Person", "Corrected title"),
    ]


def test_record_company_sets_status_and_timestamp():
    session = _session()
    company = Company(name="Acme", contact_enrichment_status="pending")
    session.add(company)
    session.flush()
    context = _context(company_id=company.id)

    status = record_company(session, context, _fill_all_quotas())
    session.commit()
    assert status == STATUS_DONE
    assert company.contact_enrichment_status == STATUS_DONE
    assert company.contact_enriched_at is not None


def test_record_company_strict_raises_when_partial_and_undersearched(tmp_path, monkeypatch):
    """The Vinsari/Binance/Highbrow shape: a company reads `partial` while its
    call ledger shows a mandatory query was never issued. strict=True must
    refuse to write that status silently."""
    import app.contacts.search_source as ss
    from app.contacts.agentic_batch import UnderSearched

    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", tmp_path / "brightdata_calls.jsonl")

    session = _session()
    company = Company(name="Acme", contact_enrichment_status="pending")
    session.add(company)
    session.flush()
    context = _context(company_id=company.id)

    try:
        record_company(session, context, [], strict=True)
        assert False, "expected UnderSearched"
    except UnderSearched as exc:
        assert "Acme" in str(exc)


def test_record_company_strict_allows_partial_when_fully_covered(tmp_path, monkeypatch):
    """A company that genuinely exhausted its plan and still came up short
    must be allowed to record `partial` even under strict=True."""
    import json
    import app.contacts.search_source as ss
    from app.contacts.search_plan import mandatory_queries

    log = tmp_path / "brightdata_calls.jsonl"
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)

    session = _session()
    company = Company(name="Acme", contact_enrichment_status="pending")
    session.add(company)
    session.flush()
    context = _context(company_id=company.id)

    with log.open("w", encoding="utf-8") as fh:
        for q in mandatory_queries(context):
            fh.write(json.dumps({
                "ts": "2026-08-03T00:00:00+00:00", "query": q.query, "results": 0,
                "company_id": q.company_id, "search_group": q.search_group,
                "tier": q.tier, "attempt": q.attempt,
            }) + "\n")

    status = record_company(session, context, [], strict=True)
    session.commit()
    assert status == STATUS_PARTIAL
    assert company.contact_enrichment_status == STATUS_PARTIAL


def test_record_company_strict_allows_done_regardless_of_coverage(tmp_path, monkeypatch):
    """strict only guards the partial path — a company that filled every
    quota needs no coverage check at all."""
    import app.contacts.search_source as ss

    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", tmp_path / "brightdata_calls.jsonl")

    session = _session()
    company = Company(name="Acme", contact_enrichment_status="pending")
    session.add(company)
    session.flush()
    context = _context(company_id=company.id)

    status = record_company(session, context, _fill_all_quotas(), strict=True)
    session.commit()
    assert status == STATUS_DONE


def test_batch_report_surfaces_fill_rates_and_confidence_mix():
    context = _context()
    contacts = _fill_all_quotas()
    contacts[0].email = None          # one stored contact with no email
    report = batch_report([(context, contacts, STATUS_PARTIAL)])
    assert "Per-tier quota fill" in report
    assert "Email confidence mix" in report
    assert "(no email)" in report
    assert "partial=1" in report


def test_batch_report_flags_undersearched_partials(tmp_path, monkeypatch):
    import app.contacts.search_source as ss
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", tmp_path / "brightdata_calls.jsonl")

    context = _context(name="UnderSearchedCo")
    report = batch_report([(context, [], STATUS_PARTIAL)])
    assert "MANDATORY QUERIES NEVER ISSUED" in report
    assert "UnderSearchedCo" in report


def test_every_category_maps_to_a_search_group():
    """Regression guard for the gap found while rebasing: main's search
    overhaul added three categories that had no ladder, stranding ~34% of
    eligible companies as no_category_match."""
    from app.config.categories import CATEGORY_KEYWORDS
    unmapped = set(CATEGORY_KEYWORDS) - set(L.CATEGORY_TO_SEARCH_GROUP)
    assert not unmapped, f"categories with no search group: {unmapped}"


def test_every_search_group_has_a_full_employer_ladder():
    for group in set(L.CATEGORY_TO_SEARCH_GROUP.values()):
        ladder = L.EMPLOYER_LADDER[group]
        assert set(ladder) == set(L.QUOTAS), f"{group} ladder missing tiers"
        for tier, titles in ladder.items():
            assert 1 <= len(titles) <= 3, f"{group}/{tier} should seed <=3 terms"
