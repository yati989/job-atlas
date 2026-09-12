"""
The five-tier contact ladder, search groups, and quotas for the agentic
contact-finding skill (issue #73, spec #68).

This replaces `categories.ROLE_LADDER`'s three-tier structure for the
agentic path. `ROLE_LADDER` itself is left in place, dormant, serving the
retained heuristic path (ADR 0005) — it is not imported here.

Two things this module owns and the skill does not re-decide:
  - which target categories collapse into one *search group* (quotas apply
    per group, so this controls how much searching a company gets), and
  - the tier ladder + quota for each company type.

The titles below are query *seeds*, not a matching vocabulary. Nothing here
accepts or rejects a candidate — that is the agent's judgement per #68. They
exist only to shape the search query, which is why each tier carries about
three of them (ADR 0001's finding: an 8-term disjunctive query returned zero
usable profiles, buried under aggregator pages, where the same intent in
3 terms surfaced real people).
"""

# --- Tiers -----------------------------------------------------------------
# "head" is split out of what was previously a single hiring_manager tier
# (#68): the function owner and the person you would actually report to are
# different outreach targets and deserve separate quota.
HEAD = "head"
HIRING_MANAGER = "hiring_manager"
IC = "ic"
TALENT_ACQUISITION = "talent_acquisition"
EXEC_FALLBACK = "exec_fallback"

# --- Search groups ---------------------------------------------------------
# Quotas apply per search group, so this mapping decides how much search
# budget a company gets. Most companies match one group; a company whose job
# history spans both may yield up to double.
#
# SPEC NOTE (flagged on #73 for review): #68 names only data_science,
# analytics and credit_risk, because it was written before `main`'s
# search-coverage overhaul (#47/#60) grew CATEGORY_KEYWORDS from three
# categories to six. Applying #68's own stated rule — merge where the ladders
# "substantially duplicate each other", separate where the function is
# "genuinely different with different people" — puts all five data-flavoured
# categories in one group and leaves credit risk alone: a Head of Data
# Science, a Head of ML and a Head of Data Engineering are the same person at
# most companies, whereas a Head of Credit Risk is not.
#
# Leaving the three new categories unmapped was measured as stranding 1,133
# of 3,351 eligible companies (34%) with no ladder at all — they would be
# marked no_category_match and never searched.
DATA_AI = "data_ai"
CREDIT_RISK = "credit_risk"

CATEGORY_TO_SEARCH_GROUP: dict[str, str] = {
    "data_science": DATA_AI,
    "analytics": DATA_AI,
    "ai_ml_engineering": DATA_AI,
    "data_engineering": DATA_AI,
    "quant_decision_science": DATA_AI,
    "credit_risk": CREDIT_RISK,
}

# --- Quotas ----------------------------------------------------------------
# Quota counts contacts that HAVE an email, at any confidence (#68):
# requiring positive confirmation would make a large share of companies
# structurally unsatisfiable, since catch-all servers accept every address and
# can never confirm a specific one.
QUOTAS: dict[str, int] = {
    HEAD: 2,
    HIRING_MANAGER: 2,
    IC: 3,
    TALENT_ACQUISITION: 3,
    EXEC_FALLBACK: 2,
}

# Tiers whose quota counts ONCE PER COMPANY rather than once per search group
# (2026-08-02). Everything not listed here is per-group.
#
# A dual-group company (data_ai + credit_risk) gets extra search budget only
# for its leadership tiers — a credit-risk lead is genuinely a different
# person from a data-science lead, worth its own query — while ICs and
# recruiters are searched once and assigned to whichever group fits. Quota
# shape has to mirror that budget, or dual-group companies could never fill
# ic/talent_acquisition and would read `partial` forever no matter how well
# they actually did. ~20% of the queue is dual-group, so this is not an edge
# case.
PER_COMPANY_TIERS: frozenset[str] = frozenset({IC, TALENT_ACQUISITION})

# Hard cap of two BILLED Bright Data calls per tier: an initial query plus at
# most one top-up, stopping early the moment the tier's quota fills. The
# top-up uses the tier's SECOND seed title (see EMPLOYER_LADDER below), never
# a re-run and never page 2 of the same query.
MAX_SEARCHES_PER_TIER = 2

# exec_fallback gets ONE call, not two. It only fires for small companies
# where head and hiring_manager both came back empty, and a small company has
# no title variance for a second query to catch — it has a CEO and a CTO, both
# of which the first seed already ranks.
MAX_SEARCHES_EXEC_FALLBACK = 1

# exec_fallback is conditional: searched only when BOTH head and
# hiring_manager come back empty, so effort isn't wasted hunting founders at
# large companies where they are irrelevant and unreachable.
CONDITIONAL_TIERS = {EXEC_FALLBACK}

# --- Employer ladder -------------------------------------------------------
# EXACTLY TWO SEEDS PER TIER, and the order is load-bearing (2026-08-02).
# The search step spends at most two billed Bright Data calls per tier —
# seeds[0] for the first query, seeds[1] for the second, and it stops there.
# A third seed would never be reached, so the lists are trimmed to two rather
# than carrying dead entries that imply a query budget that does not exist.
#
# seeds[0] is the most likely title; seeds[1] must be the most *dissimilar*
# one, not a rephrasing. A second query that merely restates the first buys
# nothing but a billed call — which is the whole reason the second call
# exists (catching title variance), and why e.g. "Head of Data" was dropped
# as a near-duplicate of "Head of Data Science".
EMPLOYER_LADDER: dict[str, dict[str, list[str]]] = {
    DATA_AI: {
        HEAD: ["Head of Data Science", "Head of AI"],
        HIRING_MANAGER: ["Data Science Manager", "Principal Data Scientist"],
        # Confirmed live: the "Machine Learning Engineer" query surfaced two
        # Weave ICs that the "Data Scientist" query did not.
        IC: ["Senior Data Scientist", "Machine Learning Engineer"],
        TALENT_ACQUISITION: ["Technical Recruiter", "Talent Acquisition Manager"],
        EXEC_FALLBACK: ["CTO", "Founder"],
    },
    CREDIT_RISK: {
        HEAD: ["Head of Credit Risk", "Risk Head"],
        HIRING_MANAGER: ["Credit Risk Manager", "Underwriting Manager"],
        IC: ["Senior Credit Risk Analyst", "Data Scientist Credit Risk"],
        TALENT_ACQUISITION: ["Technical Recruiter", "Talent Acquisition Manager"],
        EXEC_FALLBACK: ["Chief Risk Officer", "Founder"],
    },
}

PUBLIC_PROFESSION_LADDERS: dict[str, dict[str, list[str]]] = {
    "product_manager": {
        HEAD: ["Head of Product", "VP Product"],
        # These levels are deliberately distinct: a Product Lead is not
        # folded into a senior-IC query, and a Senior Product Manager is not
        # folded into the manager/lead query.
        HIRING_MANAGER: ["Director of Product", "Product Lead"],
        IC: ["Senior Product Manager", "Product Manager"],
        # Start with broad in-house people functions; technical and talent
        # recruiter variants are a narrower second attempt. The planner
        # expands each alternative into a complete '<title> at <company>'
        # clause.
        TALENT_ACQUISITION: [
            "Talent Acquisition OR Human Resources OR Recruiter",
            "Technical Recruiter OR Talent Recruiter",
        ],
        EXEC_FALLBACK: ["Chief Product Officer", "Founder"],
    },
}

# --- Staffing ladder (#74) -------------------------------------------------
# A staffing firm has no in-house data function, so every tier except
# recruiting is structurally unfillable — but its recruiter is among the most
# valuable contacts available. Defined here so both ladders live together;
# #74 owns wiring and verifying it.
STAFFING_LADDER: dict[str, list[str]] = {
    TALENT_ACQUISITION: ["Technical Recruiter", "Talent Acquisition"],
}
STAFFING_QUOTAS: dict[str, int] = {TALENT_ACQUISITION: 3}

# Tier order is the search order: head first, then the manager, then ICs,
# then recruiters. exec_fallback is absent because it is conditional.
TIER_SEARCH_ORDER: list[str] = [HEAD, HIRING_MANAGER, IC, TALENT_ACQUISITION]


def search_groups_for_categories(categories: list[str]) -> list[str]:
    """Distinct search groups a company's matched categories map to,
    in a stable order. A company matching only unmapped categories returns
    empty — the caller's signal for no_category_match."""
    groups = []
    for category in categories:
        group = CATEGORY_TO_SEARCH_GROUP.get(category)
        if group and group not in groups:
            groups.append(group)
    return groups


def ladder_for(company_type: str, search_group: str) -> dict[str, list[str]]:
    """The tier -> seed-titles ladder for this company. Staffing firms get the
    recruiter-only ladder regardless of search group (#74)."""
    if company_type == "staffing":
        return dict(STAFFING_LADDER)
    if search_group in EMPLOYER_LADDER:
        return dict(EMPLOYER_LADDER[search_group])
    if search_group in PUBLIC_PROFESSION_LADDERS:
        return dict(PUBLIC_PROFESSION_LADDERS[search_group])
    label = search_group.replace("_", " ").title()
    return {
        HEAD: [f"Head of {label}", f"VP {label}"],
        HIRING_MANAGER: [f"{label} Manager", f"{label} Lead"],
        IC: [f"Senior {label}", f"{label} Specialist"],
        TALENT_ACQUISITION: ["Technical Recruiter", "Talent Acquisition Manager"],
        EXEC_FALLBACK: ["CTO", "Founder"],
    }


def quotas_for(company_type: str) -> dict[str, int]:
    return dict(STAFFING_QUOTAS) if company_type == "staffing" else dict(QUOTAS)
