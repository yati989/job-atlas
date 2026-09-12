from app.contacts import pipeline
from app.contacts.people_search import ContactCandidate
from app.contacts.search_source import RawProfile
from app.contacts.upsert import upsert_contact
from app.models.orm import Base, Company
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_contact_pipeline_uses_public_profile_search_only(monkeypatch):
    company = Company(name="Acme", canonical_domain="acme.com")
    ladder = [{"tier": "hiring_manager", "titles": ["Head of Data"]}]
    seen_queries = []

    monkeypatch.setattr(pipeline, "_ladders_for_company", lambda _session, _company: [("data_ai", ladder)])

    def fake_search(company_name, title_terms):
        seen_queries.append((company_name, title_terms))
        return [RawProfile("Ada Lovelace", "Head of Data", "https://www.linkedin.com/in/ada")]

    monkeypatch.setattr(pipeline, "search_profiles", fake_search)
    monkeypatch.setattr(pipeline, "_gate_by_deliverability", lambda people, _company: people)

    result = pipeline.find_contacts_for_company(company, session=None)

    assert result.status == pipeline.STATUS_DONE
    assert [person.full_name for person in result.contacts] == ["Ada Lovelace"]
    assert seen_queries == [("Acme", ["Head of Data"])]


def test_contact_pipeline_skips_companies_without_a_matching_category(monkeypatch):
    monkeypatch.setattr(pipeline, "_ladders_for_company", lambda _session, _company: [])
    monkeypatch.setattr(
        pipeline,
        "search_profiles",
        lambda *_args: (_ for _ in ()).throw(AssertionError("search should not run")),
    )

    result = pipeline.find_contacts_for_company(Company(name="Acme"), session=None)

    assert result.status == pipeline.STATUS_NO_CATEGORY_MATCH
    assert result.contacts == []


def test_contact_upsert_records_public_search_provenance():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    company = Company(name="Acme")
    session.add(company)
    session.flush()

    row = upsert_contact(
        session,
        company.id,
        ContactCandidate(
            "Ada Lovelace",
            "Head of Data",
            "https://www.linkedin.com/in/ada",
            "hiring_manager",
        ),
    )

    assert row.source == "linkedin_public_search"
