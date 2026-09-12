"""
Unit fixtures for the triage layer (ADR-0007). Every fixture below is either
a real title this project has actually seen (in a stored contact or a batch
this week) or one of the documented #75/judging-and-evidence.md failure
cases the design was built to keep escalating rather than mis-decide.
"""
import pytest

from app.contacts.title_classification import (
    classify_function, classify_seniority, resolve_headline,
)
from app.contacts.triage import AUTO_ACCEPT, AUTO_REJECT, ESCALATE, triage
from app.config.contact_function import L1_IC, L2_SENIOR_IC, L3_MANAGER, L4_HEAD, L5_EXEC


# --------------------------------------------------------------- resolve_headline

def test_resolve_headline_strips_name_and_truncates_at_company_boundary():
    hl = resolve_headline("Suman Pal - Manager, Data Science at Oracle Health")
    assert hl.text == "Manager, Data Science"
    assert hl.trusted is True


def test_resolve_headline_truncates_at_at_symbol():
    hl = resolve_headline("Nabanita Debnath - Talent Advisor @ Oracle | Tech Recruitment Certified")
    assert hl.text == "Talent Advisor"
    assert hl.trusted is True


def test_resolve_headline_no_name_boundary_is_untrusted():
    hl = resolve_headline("Just Some Title With No Dash")
    assert hl.trusted is False


def test_resolve_headline_never_uses_full_name():
    """The real call site (brightdata_query._print_rows_gated) constructs
    ProfileCandidate with full_name="" — resolve_headline must work from the
    title's own structure alone, never from an out-of-band name."""
    hl = resolve_headline("Vijayender Rathore - Director - PwC - AI Leader")
    assert hl.trusted is True
    assert "Rathore" not in hl.text


# --------------------------------------------------------------- classify_seniority

@pytest.mark.parametrize("title,expected_level", [
    ("Head of Data Science", L4_HEAD),
    ("Director - Risk Advisory", L4_HEAD),
    ("Managing Director at PwC", L4_HEAD),   # MD is L4 here, not L5 — see contact_function.py
    ("Data Science Manager", L3_MANAGER),
    # L2 modifiers ("senior"/"staff") co-occurring with an L1 anchor don't
    # change the reported level — best_level is taken from anchors only
    # (see classify_seniority's docstring); L1 and L2 map to the same tier
    # in triage._tier_for anyway, so nothing is lost.
    ("Senior Data Scientist", L1_IC),
    ("Staff Data Engineer", L1_IC),
    ("Data Scientist", L1_IC),
    ("Machine Learning Engineer", L1_IC),
])
def test_seniority_level(title, expected_level):
    v = classify_seniority(title)
    assert v.level == expected_level


def test_lead_as_head_noun_is_identified_but_never_armed():
    """'Analytics Lead'/'Team Lead' (head-NOUN position) measured only 45%
    single-tier purity in the stored corpus — must always escalate."""
    v = classify_seniority("Analytics Lead")
    assert v.ambiguous is True
    assert "lead_head_noun_unarmed" in v.notes


def test_lead_prefix_is_identified_but_never_armed():
    """PREFIX 'Lead X' measured 87% ic (n=15) — clears the general arming
    bar, but the backtest against all 782 stored contacts still found real
    anchor-co-occurring disagreement ('Lead Data Scientist/DATA & AI' stored
    hiring_manager despite an explicit 'scientist' IC-noun anchor present).
    Fully demoted: unlike senior/sr/principal/staff (which only fail to arm
    when they're the SOLE signal), 'lead' always forces ambiguous, even
    alongside a real anchor."""
    v = classify_seniority("Lead Data Scientist")
    assert v.ambiguous is True
    assert "lead_prefix_unarmed" in v.notes


def test_vp_family_is_identified_but_never_armed():
    """Measured 46% purity across 13 real stored VP titles — genuinely
    undecidable from the word alone (Indian BFSI titling)."""
    v = classify_seniority("Vice President - Data Science")
    assert v.ambiguous is True
    assert "vp_family_unarmed" in v.notes


def test_l5_exec_never_armed():
    """0 of 782 stored contacts were classified from title alone into
    exec_fallback, and it's a batch-level fact — never row-decidable."""
    v = classify_seniority("Founder & CEO")
    assert v.level == L5_EXEC
    assert v.ambiguous is True


def test_associate_director_is_ambiguous_not_silently_downgraded():
    """Real stored counter-examples: 'Associate Director @ PwC' ->
    hiring_manager, 'Associate Director, KPMG India' -> hiring_manager. But
    only 60% purity (n=5) — too thin to arm at any level."""
    v = classify_seniority("Associate Director")
    assert v.ambiguous is True
    assert "downgrade_modifier" in v.notes


def test_assistant_manager_data_scientist_is_ambiguous():
    """Real stored contact: 'Assistant Manager - Data Scientist' -> ic, not
    hiring_manager. The downgrade modifier must force escalation, not a
    silent L1 guess either."""
    v = classify_seniority("Assistant Manager - Data Scientist")
    assert v.ambiguous is True


def test_delivery_manager_is_ambiguous_not_demoted():
    """Real, THREE-WAY stored contradiction if this were a demotion instead
    of an escalation: 'Delivery Manager - Data & Analytics' (EPAM) is
    hiring_manager; 'Delivery Head'/'Delivery Director' are head. A function-
    manager prefix must force ESCALATE, never guess either tier."""
    v = classify_seniority("Delivery Manager - Data & Analytics")
    assert v.ambiguous is True
    assert "function_manager_prefix" in v.notes


def test_global_program_manager_never_wins_head_or_hiring_manager():
    """The #75 human-review rule this whole layer must not repeat: 'Global
    Program Manager - Data, Analytics and AI' was wrongly stored as `head`
    on 'closest available' reasoning and rejected on review."""
    v = classify_seniority("Global Program Manager - Data, Analytics and AI")
    assert v.ambiguous is True
    assert "function_manager_prefix" in v.notes


def test_competing_levels_forces_ambiguous():
    v = classify_seniority("Director, Manager of Something")
    assert v.ambiguous is True
    assert "competing_levels" in v.notes


# --------------------------------------------------------------- classify_function

def test_bare_recruit_token_is_recruiting():
    """Real stored headline: 'Recruit[in]g | Empath' normalizes to tokens
    ['recruit','in','g'] — no marker containing the full word 'recruiting'
    fires on it, only the bare 'recruit' token does."""
    fn = classify_function("Recruit[in]g")
    assert fn.verdict == "recruiting"


def test_hr_is_recruiting_not_out_of_scope():
    """5 real stored contacts confirm: HR belongs in recruiting, never
    out-of-scope."""
    for title in ["Human Resources Executive", "HR Business Partner", "Senior HR Shared Services"]:
        fn = classify_function(title)
        assert fn.verdict == "recruiting", title


@pytest.mark.parametrize("title", [
    "Director, Financial Service",     # real stored `head` — bans "financial"
    "Banking Process Associate",       # real stored `ic` — bans "banking"
    "Associate Director - People Operations",  # real stored TA — bans "operations"
    "Director - Risk Advisory",        # real stored `head` — bans "advisory"
    "Patent Consultant | Analytics Specialist",  # real stored `ic` at a patent ANALYTICS firm
    "TA Manager - Fulfillment",        # real stored TA — bans "fulfillment"
])
def test_words_deliberately_excluded_from_out_of_scope(title):
    """Each of these real stored contacts vetoes a word that looks
    plausibly out-of-scope but isn't — see contact_function.py's
    FUNCTION_OUT_OF_SCOPE docstring for the full exclusion list."""
    fn = classify_function(title)
    assert fn.verdict != "out_of_scope", f"{title!r} wrongly flagged out_of_scope: {fn.out_of_scope}"


def test_absence_of_in_scope_marker_is_unknown_not_out_of_scope():
    """The structural encoding of 'domain is elastic' — a title with truly
    no recognized marker at all must never be treated as evidence of a
    wrong function."""
    fn = classify_function("Chief Storyteller")
    assert fn.verdict == "unknown"


def test_mixed_signal_is_escalated_not_decided():
    fn = classify_function("Marketing Analyst")  # in-scope 'analyst' + out-of-scope 'marketing'
    assert fn.verdict == "mixed"


# --------------------------------------------------------------- triage composition

def test_marketing_director_would_auto_reject_once_armed():
    """Real documented false-positive (judging-and-evidence.md, 'right
    company wrong function'): a Marketing Director at Prescience passed
    company identity cleanly and was correctly dropped by hand. Confirms the
    word fires — arming itself is a separate, evidence-gated step
    (OUT_OF_SCOPE_ARM_THRESHOLD), tested via monkeypatch here since
    OUT_OF_SCOPE_ARMED starts empty by design."""
    fn = classify_function("Marketing Director / Global Head of Marketing")
    assert "marketing" in fn.out_of_scope


def test_out_of_scope_unarmed_escalates_not_rejects():
    """Before a marker is armed, out-of-scope presence only escalates —
    OUT_OF_SCOPE_ARMED starts empty, so this must be true today."""
    v = triage("X - Marketing Director / Global Head of Marketing", company_type="employer", company_reason="clean")
    assert v.decision == ESCALATE


def test_out_of_scope_armed_auto_rejects(monkeypatch):
    import app.contacts.title_classification as tc
    monkeypatch.setattr(tc, "OUT_OF_SCOPE_ARMED", frozenset({"marketing"}))
    v = triage("X - Marketing Director / Global Head of Marketing", company_type="employer", company_reason="clean")
    assert v.decision == AUTO_REJECT


def test_recruiting_dominates_seniority_for_tier():
    """Real stored confirmation: 'Head of Talent Acquisition, India' is
    stored talent_acquisition, never head."""
    v = triage("X - Head of Talent Acquisition, India", company_type="employer", company_reason="clean")
    assert v.decision == AUTO_ACCEPT
    assert v.tier == "talent_acquisition"


def test_staffing_company_non_recruiting_hit_escalates():
    """STAFFING_LADDER (ladder.py) only ever plans talent_acquisition
    queries for a staffing firm — a non-recruiting hit there is
    structurally unexpected."""
    v = triage("X - Senior Data Scientist", company_type="staffing", company_reason="clean")
    assert v.decision == ESCALATE


def test_agency_marker_blocks_auto_accept_at_an_employer():
    v = triage("X - Recruiter | RPO Program", company_type="employer", company_reason="clean")
    assert v.decision == ESCALATE


def test_agency_marker_does_not_block_at_a_staffing_firm():
    """Real stored contact: 'Recruiter at TEKsystems | MSP Recruitment | US
    Staffing' (Steena Dsouza) — TEKsystems IS the staffing firm, so MSP/
    staffing language is expected there, not disqualifying."""
    v = triage("Steena Dsouza - Recruiter at TEKsystems | MSP Recruitment | US Staffing",
               company_type="staffing", company_reason="clean")
    assert v.decision == AUTO_ACCEPT


def test_non_clean_company_reason_never_auto_accepts():
    v = triage("X - Head of Data Science", company_type="employer", company_reason="sister_entity")
    assert v.decision != AUTO_ACCEPT


def test_untrusted_headline_never_auto_accepts_or_rejects():
    v = triage("Title With No Name Dash At All", company_type="employer", company_reason="clean")
    assert v.decision == ESCALATE


@pytest.mark.xfail(strict=True, reason=(
    "Known, documented hole (see plan): 'Business Unit Data Manager' is a "
    "real #75 human rejection (a BU data manager is not a function head) "
    "but is in-scope function + unambiguous L3 + clean company, so it WOULD "
    "auto-accept as hiring_manager. This is the strongest argument for "
    "stopping the rollout at stage 4 (draft, not auto-store) rather than "
    "ever auto-storing. If a future change fixes this, this test will fail "
    "and force the ADR to be updated, not silently pass."
))
def test_business_unit_data_manager_known_hole():
    v = triage("X - Business Unit Data Manager", company_type="employer", company_reason="clean")
    assert v.decision != AUTO_ACCEPT
