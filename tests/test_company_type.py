from app.contacts.company_type import EMPLOYER, STAFFING, classify


def test_verified_staffing_company_override_beats_bad_enrichment_text():
    assert classify(
        "Healthcare / Pharmaceuticals",
        company_name="Braintree Technology Solutions",
        description="Operates in healthcare and pharmaceuticals.",
    ) == STAFFING


def test_viraaj_staffing_override_beats_technology_enrichment_text():
    assert classify(
        "IT Consulting / Cloud Data & ML Engineering Services",
        company_name="Viraaj HR Solutions Private Limited",
        description=(
            "A technology services firm hiring an AI/ML Engineer to build "
            "production models for clients."
        ),
    ) == STAFFING


def test_recruitment_industry_is_staffing_without_an_override():
    assert classify("IT Staffing / Recruitment", company_name="Robert Half") == STAFFING


def test_real_employer_remains_employer():
    assert classify("Enterprise AI software", company_name="DataRobot") == EMPLOYER


def test_incidental_recruiting_language_does_not_turn_employer_into_staffing():
    assert classify(
        "IT Services / Consulting",
        company_name="ThoughtFocus",
        description="The company is recruiting data engineers and consultants.",
    ) == EMPLOYER


def test_explicit_agency_description_is_staffing():
    assert classify(
        "Software / Technology",
        company_name="Example Search",
        description="An executive search firm for technology roles.",
    ) == STAFFING
