"""
Synthetic + real-case suite for the profile relevance gate (ADR-0006).

The real-case block is the important one: every case there is a candidate
this project's own human review already adjudicated during the 2026-08-03
pilot. The gate must reproduce those verdicts exactly on the structural
axes it owns (shape, company) and must never auto-reject on the axes it
doesn't own (tier, domain) — those stay advisory forever, see
profile_relevance.py's module docstring for why.
"""
from app.contacts.profile_relevance import (
    ProfileCandidate, TIER_TITLE_MARKERS, company_ok, filter_relevant,
    first_failing_axis, parse_structured_field, shape_ok, suggest_tier,
)


def _candidate(name, title, snippet, target, url="https://in.linkedin.com/in/x"):
    return ProfileCandidate(full_name=name, title=title, url=url, snippet=snippet, target_company=target)


# --------------------------------------------------------------- shape axis

def test_shape_rejects_non_profile_url():
    c = _candidate("A", "T", "s", "Acme", url="https://in.linkedin.com/company/acme")
    assert shape_ok(c) is False


def test_shape_accepts_in_profile_url():
    c = _candidate("A", "T", "s", "Acme", url="https://in.linkedin.com/in/a-person")
    assert shape_ok(c) is True


# ----------------------------------------------------- structured-field parse

def test_parse_structured_field_absent_returns_none():
    assert parse_structured_field("Lead Data Scientist at Turing") is None


def test_parse_structured_field_present():
    field = parse_structured_field("Bengaluru, Karnataka, India·Senior Director, Insights and Data·CapgeminiSenior Director...")
    assert field is not None
    assert field.location.startswith("Bengaluru")
    assert field.title == "Senior Director, Insights and Data"
    assert field.company_run_on.startswith("Capgemini")


# ------------------------------------------------------- real pilot cases

def test_bridgeweave_is_wrong_company_for_weave():
    """The exact 'Weave' search leak: 5 unrelated companies surfaced because
    their names contain 'weave' as a substring, not a boundary-aligned
    prefix. Bridgeweave, Powerweave, DataWeave must all be auto-dropped."""
    c = _candidate("Supriya Kumari", "Data Analyst", "Bengaluru·Data Analyst·Bridgeweave Ltd. some prose", "Weave")
    assert company_ok(c) is False
    assert first_failing_axis(c) == "company"


def test_dataweave_and_powerweave_are_wrong_company():
    for company in ("DataWeave", "Powerweave", "I-WEAVE SOLUTIONS"):
        c = _candidate("X", "Data Scientist", f"Bengaluru·Data Scientist·{company} more text", "Weave")
        assert company_ok(c) is False, f"{company} should be rejected as wrong_company"


def test_capgemini_invent_is_sister_entity_kept_and_flagged():
    """Real case, judged KEEP by human review: Capgemini Invent is the same
    real organisation's consulting arm."""
    c = _candidate("Gaurang Desai", "Director", "Mumbai·Director·Capgemini Invent - Director - Data Strategy", "Capgemini")
    assert company_ok(c) is True
    kept, drop_counts, drop_details, flags = filter_relevant([c])
    assert len(kept) == 1
    assert len(flags) == 1
    assert flags[0]["reason"] == "sister_entity"


def test_binance_us_is_sister_entity_not_auto_rejected():
    """Real case, judged DROP by human review (a separately regulated legal
    entity) — but the GATE must not make that call; it only flags. The
    2026-08-03 pilot dropped this by hand precisely because it required
    domain knowledge (regulatory separation) no string check can have."""
    c = _candidate("Hemanth Vegi", "Senior Data Scientist", "Hyderabad·Senior Data Scientist·Binance.US more text", "Binance")
    assert company_ok(c) is True  # gate does not reject
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags[0]["reason"] == "sister_entity"


def test_pwc_acceleration_center_is_known_alias_auto_clean():
    """Real case, 2026-08-05 batch: unlike Capgemini Invent/Binance.US
    (generic sister-suffix shapes the gate must always flag), PwC
    Acceleration Center is a CONFIRMED known alias (KNOWN_COMPANY_ALIASES) —
    the agent already verified it's PwC's own delivery-center brand, same
    real employer. Must resolve straight to 'clean', no flag."""
    c = _candidate("Vijayender Rathore", "Director - AI", "Bengaluru·Director - AI || Cloud, Engineering, Data and AI·PwC Acceleration Centers", "PWC")
    assert company_ok(c) is True
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags == []


def test_ecolab_digital_center_is_known_alias_auto_clean():
    c = _candidate("Ankit Prakash", "Senior Data Scientist", "Bengaluru·Senior Data Scientist·Ecolab Digital Center", "Ecolab")
    assert company_ok(c) is True
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags == []


def test_unknown_company_with_similar_suffix_shape_still_flags():
    """Known-alias matching must stay scoped to the target it was verified
    against — a DIFFERENT company's 'Acceleration Center' claim is not
    pre-verified and must still be flagged like any other sister-suffix
    shape, not silently auto-cleaned by coincidence."""
    c = _candidate("X", "Director", "Bengaluru·Director·Nomura Acceleration Center", "Nomura")
    assert company_ok(c) is True
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags[0]["reason"] == "sister_entity"


def test_short_company_mention_with_trailing_pipe_prose_is_not_wrong_company():
    """Real bug, found by the triage backtest crossing 100 stored contacts
    for the first time (2026-08-06): 'EPAM' (segment) against target 'Epam
    Systems' was silently auto-REJECTING a real, human-accepted contact
    (Ankur Kumar). Root cause was segment EXTRACTION, not matching:
    `_title_company_segments` ran an '@'/' at ' segment to the end of the
    title instead of stopping at the next '|', so a single-word company
    mention with unrelated pipe-separated prose glued after it ('EPAM |
    Ex-A23.com | Ex-Tiger Analytics') tokenized into 7 words and matched
    neither the forward nor reverse prefix check."""
    c = _candidate(
        "Ankur Kumar", "Senior Data Scientist @ EPAM | Ex-A23.com | Ex-Tiger Analytics",
        "Senior Data Scientist @ EPAM | Ex- A23.com | Ex-Tiger Analytics", "Epam Systems",
    )
    assert company_ok(c) is True
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags[0]["reason"] == "sister_entity"


def test_common_prefix_then_diverging_qualifier_is_flagged_not_rejected():
    """Real bug, same discovery as above: 'KPMG Global Services' (segment)
    against target 'KPMG India' was silently auto-REJECTING a real,
    human-accepted contact (Somil Jain) — neither side is a strict prefix
    of the other (both extend past the shared 'kpmg' root in different
    directions), so the existing forward/reverse checks both said nothing.
    This is exactly the 'ICICI Bank' vs 'ICICI Securities' shape ADR-0006
    names as the ambiguity the gate must FLAG, never silently decide either
    way — confirmed below with that literal case, which must stay flagged,
    not become 'clean'."""
    c = _candidate(
        "Somil Jain", "Data & Analytics - Manager at KPMG Global Services",
        "Data & Analytics - Manager at KPMG Global Services — ETL Developer, Informatica Data Quality, Powercenter",
        "KPMG India",
    )
    assert company_ok(c) is True
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags[0]["reason"] == "sister_entity"

    icici = _candidate("X", "Y", "Loc·Title·ICICI SecuritiesSome prose", "ICICI Bank")
    assert company_ok(icici) is True
    kept, _, _, flags = filter_relevant([icici])
    assert flags[0]["reason"] == "sister_entity"  # flagged, not silently accepted or rejected


def test_common_prefix_divergence_does_not_reopen_bridgeweave_class():
    """The rule above must not blur back into ADR-0005's exact failure —
    'Bridgeweave' shares NO whole-word prefix with 'Weave' (it's a
    substring, not a token), so this must stay a clean wrong_company
    rejection, not a new flagged path around it."""
    c = _candidate("X", "Data Analyst", "Bengaluru·Data Analyst·Bridgeweave Ltd. some prose", "Weave")
    assert company_ok(c) is False


def test_prosperity_travels_is_wrong_company_for_trail_blazer():
    """Real case: 'Samarth Bhatia - International Travel Consultant at
    Prosperity Travels', surfaced by a Trail Blazer Consulting query purely
    because his PAST role there matched. Structured field names an entirely
    unrelated company."""
    c = _candidate(
        "Samarth Bhatia", "International Travel Consultant",
        "Dehradun, Uttarakhand, India·International Travel Consultant·Prosperity TravelsTechnical IT Recruiter at Trail Blazer Consulting LLC",
        "Trail Blazer Consulting",
    )
    assert company_ok(c) is False


def test_nexgen_analytix_is_wrong_company_for_capgemini():
    """Real case: Himanshu Tyagi's CURRENT employer (structured field) is
    NexGen Analytix; Capgemini is only a past-employer mention in free text.
    Company axis alone rejects this without needing 'former' detection at
    all, since the structured field simply never names the target."""
    c = _candidate(
        "Himanshu Tyagi", "Manager",
        "Pune·Manager·NexGen AnalytixData Science & GenAI | Ex-PwC, Dunnhumby, Capgemini",
        "Capgemini",
    )
    assert company_ok(c) is False


def test_no_structured_field_is_no_signal_kept_and_flagged():
    """The nine-Madan-Kumars case: when a snippet carries no structured
    field at all, the gate must not silently drop it (that would repeat
    ADR-0005's pre-judgement-filtering mistake) — it keeps and flags,
    leaving the actual 'drop when unsure' call to the agent."""
    c = _candidate("Madan Kumar", "Director, Data Engineering", "some free-text bio mentioning nothing structured", "Zensar")
    assert company_ok(c) is True
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags[0]["reason"] == "no_signal"


def test_exact_match_is_clean_no_flag():
    c = _candidate("Jaya Srivastava", "Data Science Manager", "Gurgaon, Haryana, India·Data Science Manager·CapgeminiCurrently working in Capgemini", "Capgemini")
    assert company_ok(c) is True
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags == []


def test_explicit_former_marker_near_target_is_dropped():
    c = _candidate(
        "X", "Manager", "Bengaluru·Manager·CapgeminiFormer Capgemini employee, now at a startup",
        "Capgemini",
    )
    assert company_ok(c) is False


def test_alternate_snippet_shape_title_at_company_is_recognised():
    """Real case found live 2026-08-03: Bright Data does not always return
    the Location·Title·Company shape. 'Data Scientist at Highbrow Technology
    Inc· Experience: Highbrow Technology Inc ·Location: 752115...' put the
    company after ' at ' in segment 0 (no location segment at all) — a
    parser that only checked a fixed segment index misread three real
    Highbrow Technologies employees as wrong-company. Also exercises the
    singular/plural tolerance: the profile says 'Technology', the target is
    'Technologies'."""
    c = _candidate(
        "Santanu Dash", "Data Scientist at Highbrow Technology Inc",
        "Data Scientist at Highbrow Technology Inc· Experience: Highbrow Technology Inc · Location: 752115. View profile",
        "Highbrow Technologies",
    )
    assert company_ok(c) is True
    kept, _, _, flags = filter_relevant([c])
    assert len(kept) == 1
    assert flags == []  # trailing "Inc" is legal-suffix boilerplate, not a sister-entity flag


def test_at_extraction_does_not_leak_from_deep_prose():
    """The Prosperity-Travels/Trail-Blazer regression: restricting the
    ' at '-extraction marker to segment 0 only must not let an unrelated
    past-employer mention buried in segment 2's glued prose be read as the
    current company."""
    c = _candidate(
        "Samarth Bhatia", "International Travel Consultant",
        "Dehradun, Uttarakhand, India·International Travel Consultant·Prosperity TravelsTechnical IT Recruiter at Trail Blazer Consulting LLC",
        "Trail Blazer Consulting",
    )
    assert company_ok(c) is False


def test_current_employer_in_structured_field_beats_unrelated_past_mention():
    """A past mention of the target elsewhere in the snippet, with a
    DIFFERENT current employer structurally declared, must not be confused
    with the 'former' case above — the structured field for THIS candidate
    names a different company outright, so this is wrong_company, and the
    'former' marker (if any) is irrelevant to that verdict."""
    c = _candidate(
        "X", "Senior Manager", "Kolkata·Senior Manager·Some Other CoFormer Capgemini associate, joined Some Other Co in 2020",
        "Capgemini",
    )
    assert company_ok(c) is False


# --------------------------------------------------- filter_relevant shape

def test_filter_relevant_returns_four_tuple_with_preseeded_counts():
    kept, drop_counts, drop_details, flags = filter_relevant([])
    assert kept == []
    assert drop_counts == {"shape": 0, "company": 0}
    assert drop_details == []
    assert flags == []


def test_drop_details_carries_company_reason():
    c = _candidate("X", "T", "loc·title·Unrelated Corp free text", "Acme")
    kept, drop_counts, drop_details, flags = filter_relevant([c])
    assert kept == []
    assert drop_counts["company"] == 1
    assert drop_details[0]["axis"] == "company"
    assert drop_details[0]["company_reason"] == "wrong_company"


# ------------------------------------------------- tier scoring is advisory

def test_tier_scorer_never_appears_in_axis_checks():
    """Structural guarantee, not just a docstring claim: importing the
    scorer must not be reachable from AXIS_CHECKS."""
    from app.contacts.profile_relevance import AXIS_NAMES
    assert "tier" not in AXIS_NAMES
    assert "domain" not in AXIS_NAMES


def test_program_manager_is_floored_out_of_head_and_hiring_manager():
    """The #75 human-review rule, encoded: 'Global Program Manager - Data,
    Analytics and AI' must never win head/hiring_manager on a fuzzy
    near-miss, the exact wrong call that was rejected on review."""
    suggestion = suggest_tier("Global Program Manager - Data, Analytics and AI")
    scores = dict(suggestion.competing)
    assert scores["head"] == 0.0
    assert scores["hiring_manager"] == 0.0


def test_tier_suggestion_is_advisory_never_filters_candidates():
    """A low-confidence or absent tier suggestion must not remove a
    candidate from `kept` — suggest_tier is not part of filter_relevant."""
    c = _candidate("X", "Some Ambiguous Title Nobody Would Score Well", "loc·Some Ambiguous Title Nobody Would Score Well·Acme", "Acme")
    kept, _, _, _ = filter_relevant([c])
    assert len(kept) == 1  # tier score, whatever it is, never removed it


# --------------------------------------------------- backtest: never reject
# a human-accepted real contact from the pilot's evidence files.

_HUMAN_ACCEPTED_CASES = [
    # (title, snippet_with_structured_field, target_company)
    ("Senior Director, Insights & Data", "Bengaluru·Senior Director, Insights and Data·CapgeminiBusiness Intelligence Practice Head", "Capgemini"),
    ("Data Science Manager", "Gurgaon·Data Science Manager·CapgeminiCurrently working in Capgemini", "Capgemini"),
    ("Senior Director Data Science", "Noida·Senior Director Data Science·UnitedHealth GroupSenior Director Data Science", "UnitedHealth Group"),
    ("Director, Applied Data Science", "Bengaluru·Director, Applied Data Science·AirbnbData Science leader", "Airbnb"),
    ("Manager - Data Science", "Bengaluru, Karnataka, India·Manager - Data Science·dentsuManager - Data Science @ dentsu| building Marketing Effectiveness tools", "Dentsu"),
    ("Managing Director - Decision Science", "Mumbai·Managing Director - Revenue Management - DSML & Analytics·FedExManaging Director", "FedEx"),
]


def test_backtest_never_auto_rejects_a_human_accepted_contact():
    for title, snippet, target in _HUMAN_ACCEPTED_CASES:
        c = _candidate("Someone", title, snippet, target)
        assert company_ok(c) is True, f"gate would have wrongly rejected: {title} @ {target}"
        assert shape_ok(c) is True


# ------------------------------------------- title as company evidence (2026-08-04)
# The company axis used to read the snippet alone. The SERP title is the
# LinkedIn headline — structured, not prose — and often carries the employer
# when the snippet body doesn't. Every CrowdStrike profile in the 2026-08-03
# batch was dropped `wrong_company` while saying "at CrowdStrike" in the title.

def test_company_named_only_in_the_title_is_recognised():
    c = _candidate(
        "Evelyn Ryan", "Evelyn Ryan - Data Scientist at CrowdStrike",
        "Bengaluru, Karnataka, India·Data Scientist·Cybersecurity and threat detection.",
        "Crowdstrike",
    )
    assert company_ok(c) is True


def test_company_named_only_in_the_title_via_at_symbol():
    c = _candidate(
        "Dharti Patel", "Dharti Patel - Sr Talent Acquisition Partner @Crowdstrike",
        "Pune, Maharashtra, India·Sr Talent Acquisition Partner·Hiring across engineering.",
        "Crowdstrike",
    )
    assert company_ok(c) is True


def test_intercap_company_name_still_matches_its_db_spelling():
    """'CrowdStrike' splits to ['crowd','strike'] on the case boundary while
    the DB's 'Crowdstrike' stays one token — exact concatenation equality
    bridges that. Any intercap name (PowerToFly, DataKrew) hits this."""
    c = _candidate("Someone", "Data Scientist", "Bengaluru·Data Scientist·CrowdStrike", "Crowdstrike")
    assert company_ok(c) is True


def test_title_mining_still_catches_a_former_employer():
    """Taking every ' at '/'@' in the title must not let a trailing `Ex @Target`
    read as current — `_former_or_clean` is what holds this line."""
    c = _candidate(
        "Rupam Patil", "Rupam Patil - Data Scientist @AuthMind | Ex @CrowdStrike",
        "Pune·Data Scientist·AuthMind", "Crowdstrike",
    )
    assert company_ok(c) is False


def test_title_naming_a_different_company_is_still_wrong_company():
    c = _candidate(
        "Vinay Nimmala", "Vinay Nimmala - Technical Recruiter at CODEFORCE360",
        "Hyderabad·Technical Recruiter·CODEFORCE360", "Crowdstrike",
    )
    assert company_ok(c) is False
