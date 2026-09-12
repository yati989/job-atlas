"""
Gating vocabulary for the function/seniority triage layer
(`app/contacts/title_classification.py` + `app/contacts/triage.py`, ADR-0007 —
amends ADR-0006, does not replace it).

DELIBERATELY A SEPARATE FILE FROM `contact_matching.py`: that file's docstring
says "none of these markers ever reject a candidate" (ADR-0006's line: company
identity may reject, tier/domain scoring never may). This file's vocabulary
DOES reject — one narrow list of it, under narrow conditions — so it needed
its own file rather than making that claim false.

WHY THIS ISN'T ADR-0005's MISTAKE, ARGUED, NOT ASSUMED: ADR-0005 killed a
matcher that "accepted a person at one bank for a search against a different
bank" on a single shared word. That failure was SIMILARITY doing the
accepting/rejecting. Nothing here scores or ranks — every check is exact
word-boundary membership in a small, evidence-earned list, the same
"structural, not scored" standard ADR-0006 already uses for company identity
(KNOWN_COMPANY_ALIASES). The thing that makes this safe is size discipline: a
marker only earns reject power after real batches show zero contradiction
(OUT_OF_SCOPE_ARMED, same evidence-earned pattern as KNOWN_COMPANY_ALIASES).

GROUND TRUTH IS MEASURED, NOT GUESSED. Every threshold below was checked
against all 782 contacts already stored in the `contacts` table (98.5%
internally consistent — 462 of 469 distinct normalized titles map to exactly
one tier), not written from intuition. Where the measurement contradicts this
project's own prior docs (SKILL.md step 4 calls "Team Lead" a hiring_manager;
the stored corpus says title-PREFIX "Lead X" is IC 75% of the time and
head-noun "X Lead" is too mixed to auto-decide at all), the measurement wins,
because it's what a reviewer actually judged, not what a doc predicted they
would.

WHY TIER/FUNCTION IS DIFFERENT FROM COMPANY IDENTITY, NOT THE SAME PROBLEM
SOLVED THE SAME WAY: company identity is a closed-form check — a string either
is the target's confirmed name or it isn't. Seniority and function are
open-ended semantic judgements about what someone actually does, which is
exactly why `judging-and-evidence.md` says seniority must be "strict, never
stretch" and function/domain must be "elastic, and being strict here destroys
value." This file encodes both halves of that asymmetry:
  - the seniority ladder only arms markers that were internally near-unanimous
    (>=85% single-tier purity on a real, multi-observation sample) — strict.
  - FUNCTION_OUT_OF_SCOPE stays small and NEGATIVE (absence of an in-scope
    marker is never evidence of anything) — elastic. The failure this project
    already lived through once (fraud-risk / O2C / collections managers
    wrongly dropped for not being narrowly "credit risk") is a rejection on
    ABSENCE from a narrow positive set. Nothing here does that: a title with
    zero function markers is always UNKNOWN, never OUT_OF_SCOPE — see
    `title_classification.classify_function`'s `unknown` branch.
"""

# ---------------------------------------------------------------------------
# Axis A: seniority — ordinal, highest matched level wins. Matched on the
# TITLE only, word-boundary substring, never fuzzy (rapidfuzz's
# token_set_ratio is what caused the false positives this file exists to
# fix — verified live: "Marketing Director" and "Head of Data Science" both
# score head:100, "Senior QA/QC Engineer" and "Senior Data Scientist" both
# score ic:100, because a 1-2 word marker's tokens are trivially a subset of
# almost any title).
# ---------------------------------------------------------------------------
L1_IC, L2_SENIOR_IC, L3_MANAGER, L4_HEAD, L5_EXEC = 1, 2, 3, 4, 5

# Only markers whose measured single-tier purity is HIGH are placed at a
# level that can drive AUTO_ACCEPT — see SENIORITY_ARMED below, which is the
# actual gate. Markers below can still be matched for *display*/escalation
# context even where their purity didn't clear the bar (e.g. plain "vp"),
# they just never win a level that AUTO_ACCEPT trusts.

SENIORITY_MARKERS: dict[int, list[str]] = {
    # 8 of 8 titles containing founder/ceo/cto/coo/cfo split 2 head / 4
    # exec_fallback / 2 talent_acquisition — genuinely undecidable from the
    # word alone, AND exec_fallback is a BATCH-level fact (only searched when
    # head+hiring_manager are both empty for that company) a row-level
    # classifier cannot see. L5 NEVER drives an auto-decision — see
    # `classify_seniority`, which always returns `ambiguous=True` for L5.
    L5_EXEC: [
        "founder", "co founder", "cofounder", "ceo", "cto", "coo", "cfo",
        "chief executive officer", "chief technology officer",
        "chief data officer", "chief analytics officer", "chief risk officer",
        "managing partner",
    ],
    # Measured against all 782 stored contacts, matching the FULL de-named
    # headline (strip leading "Name - ", truncate only at a company boundary
    # " at "/"@" — never at internal dashes, which is what an earlier,
    # cruder measurement pass got wrong and had to be re-run).
    #
    # "director" (bare word), non-recruiting: 43 occurrences, 40 head / 2
    # hiring_manager / 1 talent_acquisition = 93% head. "head"/"head of": 13
    # occurrences, 13/13 = 100%. Both clear the arming bar. "managing
    # director" is INSIDE this bucket (contains "director") on purpose —
    # two real stored contacts (Abhishek Debnath @ PwC, Sharang Goyal @ NTT
    # DATA) confirm MD is `head` in Indian consulting/IT-services, a
    # partner grade, NOT an L5 CEO-equivalent.
    #
    # "vp"/"vice president" is DELIBERATELY EXCLUDED from the armed set even
    # though it looks head-shaped: measured 46% purity (6 head / 3
    # hiring_manager / 2 talent_acquisition / 2 ic across 13 real stored VP
    # titles) — Indian banking/BFSI titles use VP as a genuinely mid-level
    # rank, and human reviewers judged it inconsistently even for near-
    # identical strings ("Vice President - Data Science" stored as `head`
    # once and `hiring_manager` once). It stays in VP_MARKERS below purely
    # so its presence forces escalation, never a level.
    L4_HEAD: [
        "head of", "head", "global head", "practice head", "delivery head",
        "branch head", "vertical head", "country head",
        "director", "senior director", "executive director",
        "managing director",
    ],
    # "manager" (bare word), non-recruiting: 85 occurrences, 77
    # hiring_manager / 7 talent_acquisition / 1 ic = 91%. Clears the bar
    # directly (the 7 remaining talent_acquisition instances are titles
    # like "Deputy Manager" with the recruiting word past where an earlier,
    # cruder measurement pass truncated — the actual FUNCTION_RECRUITING
    # check in triage.py scans the whole headline and intercepts them
    # before seniority is consulted at all).
    #
    # "associate director" measured SEPARATELY at 60% (3 head / 2
    # hiring_manager, n=5) — too mixed and too small a sample to arm at
    # either L3 or L4. It is deliberately absent from every level's list;
    # `SENIORITY_DOWNGRADE_MODIFIERS` catches the "associate"/"assistant"
    # prefix and forces escalation rather than guessing which side it's on.
    L3_MANAGER: ["manager", "team manager", "group manager"],
    # NOTE: "Lead X" (prefix) and "senior"/"sr"/"principal"/"staff" are NOT
    # dict entries here even though they look seniority-shaped — both are
    # L2 MODIFIERS (see L2_MODIFIER_MARKERS below), demoted out of this
    # anchor dict entirely after the backtest found real disagreement when
    # either armed independently. See that list's docstring for the
    # measured numbers and the two real stored contradictions that forced
    # the change.
    #
    # Bare function nouns with no seniority modifier at all default here —
    # scientist/engineer/analyst/architect/developer measured 96% ic (202
    # of 210 non-recruiting occurrences) -- the strongest single signal in
    # the whole vocabulary. EXPLICIT list, not a "nothing else matched"
    # default: the backtest against all 782 stored contacts caught real
    # contradictions when "no seniority marker matched" was silently
    # trusted as confident L1 ("Credit Underwriter" stored hiring_manager,
    # "Chief Data Scientist - Weather" stored head -- both have zero words
    # from this list, and a title with truly NO recognized seniority signal
    # must escalate, the same "absence is not evidence" rule
    # FUNCTION_IN_SCOPE already applies to axis B. See
    # `title_classification.classify_seniority`'s zero-signal branch.
    L1_IC: [
        "scientist", "engineer", "analyst", "architect", "developer",
    ],
}

# L2 markers are MODIFIERS, not independently arming -- see
# `classify_seniority`: a title matching ONLY an L2 marker (no L1 IC noun,
# no L3/L4/L5 word) always escalates. Two real stored contradictions forced
# this, both cases where "senior" was the sole matched word and the title's
# true seniority driver was a word this vocabulary doesn't recognize at all:
# "Senior AVP, Fraud & Credit Risk" (head -- driven by "AVP", not in
# VP_MARKERS) and "Senior Technical Product Owner (Data & AI)"
# (hiring_manager). Letting L2 arm on its own would have silently produced
# `ic` for both. When L2 co-occurs with an L1 noun ("Senior Data
# Scientist"), the tier answer is the same either way (L1 and L2 both map
# to `ic` in triage._tier_for), so nothing is lost by this restriction --
# L2 becomes purely a modifier/refinement signal, never a sole basis for a
# decision.
#
# PREFIX "Lead X" (Lead Data Scientist) measured 87% ic (n=15) -- clears
# the general arming bar but, per the same backtest, still produced 3 real
# disagreements on its own (credit-risk-domain "Lead" titles the sample
# happened not to cover). Demoted to modifier-only rather than
# independently-arming for the same reason "senior" was: on 3/15 real
# disagreement, a HARD zero-tolerance backtest invariant cannot coexist
# with an intentionally-probabilistic single-marker rule. `classify_seniority`
# still records prefix-position specially (see LEAD_PREFIX handling there)
# so this can be re-armed later once shadow-mode evidence at a larger
# sample supports it.
L2_MODIFIER_MARKERS: list[str] = ["senior", "sr", "staff"]

# Present for IDENTIFICATION/escalation only — matching one of these NEVER
# lets `classify_seniority` return an armed level (see the note under
# L4_HEAD above for the measured 46% purity that disqualifies it). "avp"
# added after the backtest: "Senior AVP, Fraud & Credit Risk" (stored head)
# had no recognized L4 marker at all without it.
VP_MARKERS: list[str] = ["vp", "vice president", "svp", "evp", "avp"]

# Bare "chief"/"cio" not paired with one of SENIORITY_MARKERS[L5_EXEC]'s
# full CxO phrases -- same "identified but never armed" treatment as
# VP_MARKERS, added after the backtest caught "Chief Data Scientist -
# Weather" (stored head, zero recognized markers before this) and "Data,
# API & AI Platform Leader, CIO" (stored head, "cio" unrecognized).
UNQUALIFIED_CHIEF_MARKERS: list[str] = ["chief", "cio"]

# "Principal" moved OUT of L2_MODIFIER_MARKERS after the backtest: unlike
# "senior"/"sr"/"staff" (which only fail to arm when they're the SOLE
# signal, and agree with a co-occurring L1 anchor every time they were
# checked), "principal" disagreed even WITH an anchor present --
# "Senior Principal Consultant / Principal Data Scientist" (stored `head`)
# has an explicit "scientist" L1 anchor and would still have silently
# resolved to `ic`. "Principal" evidently means something more senior than
# a plain IC title at some orgs in this corpus (unlike "Staff", which
# didn't show this pattern) -- same never-arms treatment as VP/chief rather
# than a plain modifier.
PRINCIPAL_MARKERS: list[str] = ["principal"]

# A level match immediately preceded by one of these is downgraded to
# "ambiguous" rather than trusted at face value. Two independent stored
# confirmations for "associate director" -> hiring_manager, not head
# (Abhishek Das @ PwC, Rakesh Paul @ KPMG); "assistant manager" + an IC noun
# -> ic, not hiring_manager, in all 3 stored occurrences (e.g. "Assistant
# Manager - Data Scientist"). Rather than encode a second guess for each
# combination on a 3-9 sample, every downgrade-modifier match just forces
# escalation -- the modifier is a genuine "read this one" signal.
SENIORITY_DOWNGRADE_MODIFIERS: list[str] = [
    "associate", "assistant", "deputy", "acting", "interim", "junior",
]

# "<X> manager" names a function called "X management", not seniority over
# the data/credit function -- the insight TIER_DEMOTION_MARKERS already
# encodes for the ADVISORY scorer (contact_matching.py), generalized here.
# CRITICAL, and confirmed by real contradictions: this forces ESCALATE, NOT
# a demotion to a lower level. "Delivery Manager - Data & Analytics" (EPAM)
# is stored `hiring_manager`; "Delivery Head"/"Delivery Director" are stored
# `head`. A silent demotion on "delivery" would contradict three live
# judgements. TIER_DEMOTION_MARKERS' floor-to-0 behaviour in the advisory
# scorer is untouched by this file.
FUNCTION_MANAGER_PREFIXES: list[str] = [
    "program", "project", "product", "delivery", "account", "engagement",
    "vendor", "category", "community",
]

# ---------------------------------------------------------------------------
# Axis B: function -- THREE positive/negative buckets plus an implicit
# fourth (unknown = none matched). Matched on the TITLE only, never the
# snippet -- four real stored contacts have their only function marker in
# the SNIPPET, not the title (Ankit Prakash, ic, "...retail & supply chain
# consulting..."; Hitesh Jain, talent_acquisition, "...IT, Finance, Supply
# Chain..."; Mehak Yadav, talent_acquisition, "...brand management...";
# Krishna Parikh, hiring_manager, "...SAP ERP (FI module)..."). Matching the
# snippet would have pushed all four toward FUNCTION_OUT_OF_SCOPE.
# ---------------------------------------------------------------------------

# Generous by design -- "domain is elastic" (judging-and-evidence.md).
# Membership here only makes a row ELIGIBLE for AUTO_ACCEPT; absence NEVER
# rejects (see classify_function's `unknown` branch and
# test_absence_of_in_scope_marker_never_rejects). Includes the full
# credit_risk neighbourhood on purpose, per the documented over-correction:
# fraud risk / O2C / collections / receivables managers were wrongly DROPPED
# once for not being narrowly "credit risk".
FUNCTION_IN_SCOPE: list[str] = [
    "data", "data science", "data scientist", "data engineer",
    "data engineering", "data analyst", "data analytics", "data platform",
    "data governance", "data privacy", "data quality", "data architect",
    "analytics", "analyst", "business intelligence", "insights",
    "decision science", "machine learning", "ml", "mlops", "deep learning",
    "artificial intelligence", "ai", "gen ai", "genai", "generative ai",
    "aiml", "llm", "nlp", "computer vision",
    "risk", "credit", "credit risk", "fraud", "underwriting", "collections",
    "o2c", "receivables", "actuarial", "quant", "quantitative", "statistics",
    "statistical", "econometrics", "modelling", "modeling",
]

# Recruiting is its OWN positive value, not "out of scope" -- talent_
# acquisition is a real, quota-bearing tier (contact_matching.py /
# ladder.py). This bucket also DOMINATES seniority in composition (see
# triage._tier_for): "Head of Talent Acquisition, India", "Director, Talent
# Acquisition" (x2), "Branch Head - Recruitment" are all stored
# talent_acquisition, never head, despite carrying a strong L4 word.
#
# "hr"/"human resources" belong here on purpose: 13 stored contacts with
# "hr" are 100% talent_acquisition ("Human Resources Executive", "HR
# recruiter", "HR Business Partner", "Senior HR Shared Services", "Senior
# Human Resources Manager - Talent Acquisition"). Bare "it" was measured at
# 100% purity too (n=11) but EXCLUDED anyway -- every one of those 11 also
# contained "recruiter"/"recruitment" directly, so "it" added nothing, and
# "IT" alone is dangerously generic (matches "IT Consultant", "Head of IT",
# "AVP - IT" -- none of which are recruiting).
#
# Bare "recruit" is listed on purpose, not redundant with "recruiter"/
# "recruitment": a real stored contact's headline is "Recruit[in]g |
# Empath", which normalizes to tokens ["recruit","in","g"] -- no marker
# containing the full word "recruiting" would fire on it.
FUNCTION_RECRUITING: list[str] = [
    "recruit", "recruiter", "recruiting", "recruitment", "talent acquisition",
    "talent partner", "talent advisor", "talent manager", "talent associate",
    "sourcing", "sourcer", "staffing", "human resources", "hr", "hrbp",
    "people operations", "headhunting", "onboarding",
]

# Every candidate word below was checked against the corpus for false
# vetoes before inclusion (see title_classification tests +
# scripts/audit_triage.py). Words that DID false-veto a real stored contact
# are excluded and named here so the exclusion survives future edits:
#   financial / banking   -> "Director, Financial Service @ KPMG India |
#                             Risk" (head); "Banking Process Associate"
#                             (ic). The entire credit_risk group lives here.
#   operations             -> "Associate Director - People Operations",
#                             "Assistant Manager - People Operations" (TA)
#   consultant / advisory  -> "Director - Risk Advisory" (head);
#                             "Recruitment Consultant" (TA); "Associate
#                             Consultant" x2 (ic, TA)
#   hr / human resources   -> lives in FUNCTION_RECRUITING instead
#   patent / ip            -> "Patent Consultant | Analytics Specialist"
#                             (ic, Lumenci -- a patent ANALYTICS firm)
#   fulfillment             -> "TA Manager - Fulfillment" (TA)
#   bare "qa"               -> "Credit Risk Operations Associate / QA &
#                             Training Lead" (ic) -- QA means something
#                             different in a risk-ops context
#   bare "testing"          -> "AVP - ICAAP CCR Stress testing"
#                             (hiring_manager) -- stress TESTING is a real
#                             credit-risk discipline
#   admin (bare)            -> "Senior Executive - HR & Admin"
#                             (talent_acquisition, via "hr") -- kept out
#                             because the mixed-signal rule already handles
#                             this case correctly and a bare 4-letter token
#                             is too collision-prone to trust further
#   supply chain            -> "Supply Chain Risk Analytics Manager"
#                             (hiring_manager) -- a real risk/analytics
#                             sub-domain, not a logistics role
#   business development    -> "Business Development Manager" at The Avian
#                             Consulting LLC (talent_acquisition) -- at a
#                             small staffing firm, "BD" doing "executive and
#                             leadership hiring" IS the recruiting function,
#                             not a sales role; "sales"/"presales" alone
#                             stay armed since no stored contact vetoes them
# What's left is the defensible core: functions that are genuinely a
# different discipline with no observed overlap into data/analytics/risk/
# recruiting in this project's stored history.
FUNCTION_OUT_OF_SCOPE: list[str] = [
    "marketing", "brand", "branding", "advertising", "seo",
    "sales", "presales", "pre sales",
    "account executive", "inside sales", "field sales",
    "quality assurance", "qa engineer", "qc engineer", "sdet",
    "test engineer", "manual testing", "automation testing",
    "facilities", "facility", "housekeeping", "reception", "front office",
    "legal counsel", "counsel", "paralegal", "litigation",
    "logistics", "procurement", "warehouse", "transport",
    "payroll", "travel desk",
]

# THE ONLY SET THAT MAY ACTUALLY REJECT (via triage.triage). Starts EMPTY BY
# DESIGN -- a marker moves from FUNCTION_OUT_OF_SCOPE into here only after
# >= OUT_OF_SCOPE_ARM_THRESHOLD shadow-mode observations with zero human
# contradiction (scripts/audit_triage.py reports the tally). This is the
# same evidence-earned-exception pattern KNOWN_COMPANY_ALIASES already uses
# in contact_matching.py, applied to the axis where a false positive is more
# expensive (a wrongly-rejected contact never gets a second look, versus a
# wrongly-un-armed marker which just means one more escalation).
OUT_OF_SCOPE_ARMED: frozenset[str] = frozenset()
OUT_OF_SCOPE_ARM_THRESHOLD = 5

# Agency/RPO markers. These NEVER reject on their own -- they only block
# AUTO_ACCEPT, forcing escalation, because a real stored contact
# legitimately carries this language: "Recruiter at TEKsystems | MSP
# Recruitment | US Staffing" (Steena Dsouza) -- TEKsystems IS the staffing
# firm, so MSP/staffing language is EXPECTED there, not disqualifying.
# `triage.py` suppresses this check entirely when company_type == "staffing".
AGENCY_MARKERS: list[str] = ["rpo", "ams", "msp", " via ", "on behalf of", "c2c"]
