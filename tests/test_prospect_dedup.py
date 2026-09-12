"""
The dedup key shared by prospect discovery and company dedup.

Two things are worth a test here. First, that extracting `normalize()` out of
scripts/dedup_companies.py into app/companies/naming.py did not change its
behaviour — the extraction exists precisely so both sides apply ONE rule, and
a silent drift would re-introduce the duplicate class the dedup script exists
to collapse. Second, that a harvested name colliding with an existing Company
is rejected, since "not already in the companies table" is the user's own
first criterion for what makes something a prospect.

SQLite in-memory, same pattern as test_agentic_batch.py / test_persistence.py.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.companies.naming import is_placeholder, normalize
from app.models.orm import Base, Company, Prospect
from app.prospects import batch


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# ------------------------------------------------------------- normalize ---

def test_normalize_peels_trailing_legal_and_regional_suffixes():
    # The real variants that motivated the dedup script in the first place.
    assert normalize("NTT DATA Americas, Inc") == "ntt data"
    assert normalize("NTT DATA, Inc.") == "ntt data"
    assert normalize("ntt data north america") == "ntt data"
    assert normalize("Fractal Analytics Private Limited") == "fractal analytics"
    assert normalize("Fractal Analytics Ltd.") == "fractal analytics"


def test_normalize_never_strips_a_leading_qualifier():
    """"Global" is a qualifier only in trailing position — clipping it here
    would fuse "Global Logic" with any other company called "Logic"."""
    assert normalize("Global Logic") == "global logic"
    assert normalize("India Infoline") == "india infoline"


def test_normalize_never_empties_a_name_made_entirely_of_qualifiers():
    """A name stripped to nothing would collide with every other such name."""
    assert normalize("Services Global Ltd") != ""
    assert normalize("Solutions") == "solutions"


def test_normalize_scripts_import_is_the_same_function():
    """scripts/dedup_companies.py must not carry its own drifting copy."""
    from scripts.dedup_companies import normalize as script_normalize
    assert script_normalize is normalize


def test_placeholder_names_are_recognised():
    assert is_placeholder("Stealth Startup")
    assert is_placeholder("Confidential")
    assert is_placeholder("Various")
    assert not is_placeholder("Razorpay")


# ----------------------------------------------------------------- dedup ---

def test_name_matching_an_existing_company_is_not_a_new_prospect():
    session = _session()
    session.add(Company(name="Fractal Analytics Private Limited"))
    session.flush()

    result = batch.dedup_candidates(session, ["Fractal Analytics Ltd.", "Volt Money"])

    assert result.already_in_companies == ["Fractal Analytics Ltd."]
    assert result.new == ["Volt Money"]


def test_name_matching_an_existing_prospect_is_not_new_even_when_unqualified():
    """Unqualified rows are kept exactly so a later thesis doesn't re-research
    the same dead end — that only works if dedup counts them."""
    session = _session()
    p = batch.record_candidate(session, "PayCrunch", thesis="india-lending-bnpl")
    batch.disqualify(session, p, batch.REASON_NO_FUNCTION)
    session.flush()

    result = batch.dedup_candidates(session, ["PayCrunch Pvt Ltd", "Volt Money"])

    assert result.already_a_prospect == ["PayCrunch Pvt Ltd"]
    assert result.new == ["Volt Money"]


def test_variants_within_one_harvest_collapse_to_one():
    """Directory pages routinely list a company twice under two legal names."""
    session = _session()
    result = batch.dedup_candidates(
        session, ["Razorpay", "Razorpay Pvt Ltd", "Razorpay, Inc."]
    )
    assert result.new == ["Razorpay"]


def test_dedup_only_peels_known_qualifiers_and_stays_conservative():
    """"Software" is not in the trailing-qualifier vocabulary, so "Razorpay
    Software Pvt Ltd" does NOT collapse into "Razorpay". This is deliberate:
    every marker in that vocabulary is evidence-earned, and widening it to
    catch this case would also change company dedup's merge decisions. Two
    prospect rows for one company is a cheap, visible error; fusing two
    genuinely different companies is not."""
    session = _session()
    result = batch.dedup_candidates(session, ["Razorpay", "Razorpay Software Pvt Ltd"])
    assert result.new == ["Razorpay", "Razorpay Software Pvt Ltd"]


def test_placeholders_are_split_out_not_treated_as_new():
    session = _session()
    result = batch.dedup_candidates(session, ["Stealth Startup", "  ", "Volt Money"])
    assert result.new == ["Volt Money"]
    assert "Stealth Startup" in result.placeholder


def test_recording_a_placeholder_is_refused():
    session = _session()
    try:
        batch.record_candidate(session, "Stealth Startup", thesis="india-lending-bnpl")
    except ValueError:
        pass
    else:
        raise AssertionError("expected a placeholder name to be refused")


def test_excluded_names_are_persisted_so_the_next_run_skips_them():
    session = _session()
    written = batch.record_excluded(
        session, ["Acme Corp"], thesis="india-lending-bnpl",
        reason=batch.REASON_ALREADY_IN_COMPANIES,
    )
    session.flush()
    assert written == 1
    row = session.query(Prospect).filter_by(normalized_name="acme").one()
    assert row.status == batch.STATUS_UNQUALIFIED
    assert row.unqualified_reason == batch.REASON_ALREADY_IN_COMPANIES
