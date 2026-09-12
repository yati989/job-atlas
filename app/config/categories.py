"""
Skill/category keyword config: the single place that defines what "relevant
to me" means, so connectors and filters don't each hardcode their own search
terms. Add a new category or keyword here and every connector that uses
`all_keywords()` or a specific category picks it up automatically.
"""

from app.config.settings import RECENCY_WINDOW_DAYS

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "data_science": [
        "data scientist", "data science", "machine learning",
        "deep learning", "nlp", "natural language processing",
        "applied scientist",
        # Variant/plural forms seen in real Naukri titles (coverage test,
        # issue #59): "Specialist - Data Sciences" failed on the plural.
        "data sciences", "ml scientist", "machine learning scientist",
    ],
    "analytics": [
        "data analyst", "business analyst", "analytics", "data analytics",
        "bi analyst", "business intelligence",
        "insights analyst", "insight analyst",
    ],
    "credit_risk": [
        "credit risk", "risk analyst", "risk analytics", "underwriting",
        "underwriter", "quantitative risk", "model risk", "fraud risk",
        "credit analyst", "fraud analyst",
        # Role-audit additions (2026-08-24). Keep these as occupational or
        # lending-adjacent phrases rather than the bare word "risk", which
        # would also admit legal, security, and generic governance work.
        "risk manager", "consumer risk", "portfolio risk",
        "associate risk", "risk associate", "risk officer", "risk specialist",
    ],
    "ai_ml_engineering": [
        "machine learning engineer", "ml engineer", "ai engineer",
        "artificial intelligence", "mlops engineer", "applied ai", "ml ops",
        "generative ai", "genai", "gen ai", "llm", "ai/ml", "ml/ai",
        # "ai ml" also covers "AI-ML"/"AI_ML" via _word_match's separator
        # normalization; "engr" abbreviations from issue #59's coverage test
        # ("Principal AI Engr" failed role).
        "ai ml", "ai engr", "ml engr", "machine learning engr",
        "agentic ai", "computer vision",
        # Explicit AI occupation variants found in the role-drop audit. Avoid
        # bare "ai", "engineer", and "developer": those tokens alone are too
        # broad for a title-only relevance gate.
        "ai developer", "ai software engineer", "ai enabled software engineer",
        "ai product engineer", "ai research engineer", "ai solution engineer",
        "ai solutions engineer", "ai application engineer",
        "ai applications engineer", "ai platform engineer", "ai native engineer",
        "ai agent developer", "ai agent engineer", "ai agentic engineer",
        "agentic engineer", "agentic software engineer", "agentic developer",
        "ai specialist", "ai researcher", "ai research scientist", "ai scientist",
        "artificial intelligence specialist",
        "artificial intelligence researcher",
        "artificial intelligence scientist",
        "ai engineering", "ai lead", "ai architect", "aiml",
        "founding engineer ai",
        # ML engineering variants and the customer-embedded engineering title
        # explicitly approved from the audit.
        "ml engineering", "ml developer", "ml software developer",
        "ml software engineer", "forward deployed", "forward deployment engineer",
    ],
    "quant_decision_science": [
        "quant", "quantitative analyst", "quantitative researcher",
        "decision scientist", "decision science",
    ],
    "data_engineering": [
        "data engineer", "data engineering", "analytics engineer", "mlops",
        "machine learning ops",
        # Database/tool-specific titles that perform data-engineering work but
        # do not literally contain "data engineer".
        "sql developer", "sql engineer", "database developer",
        "database engineer", "etl developer", "data warehouse developer",
        "warehouse developer", "snowflake developer", "databricks developer",
        "databricks engineer", "pyspark developer", "pyspark engineer",
        "data platform engineer", "data integration engineer", "data architect",
        "data modeler", "data modeller",
    ],
}


# --- Search vocabulary (fetch-side) -----------------------------------------
# The lean set of terms used to *drive* native site searches — one search pass
# per term, for sources that support it. Distinct from the relevance (gate)
# vocabulary below: this list stays narrow/high-precision so it doesn't burn
# search quota on terms that only marginally match; the gate vocabulary below
# is deliberately broader so it doesn't drop a job this list's own searches
# fetched. "business intelligence" and "quantitative analyst" are deliberately
# absent here — they remain gate-only. Wiring connectors to this list is a
# later ticket; nothing here is consumed yet.
SEARCH_TERMS: list[str] = [
    "data scientist",
    "data analyst",
    "data engineer",
    "machine learning engineer",
    "ai engineer",
    "credit risk",
]

# Search term -> CATEGORY_KEYWORDS family, for the relevance-drift stopping
# criterion (issue #60): a native-search connector classifies each fetched
# job against the family owned by the term it searched for, to decide
# whether results are still "on-slice" for that query.
TERM_TO_FAMILY: dict[str, str] = {
    "data scientist": "data_science",
    "data analyst": "analytics",
    "data engineer": "data_engineering",
    "machine learning engineer": "ai_ml_engineering",
    "ai engineer": "ai_ml_engineering",
    "credit risk": "credit_risk",
}


def classify_title(title: str) -> str | None:
    """Return the CATEGORY_KEYWORDS family with a keyword matching `title`,
    or None if no family matches. A title can match more than one family
    (families are not mutually exclusive at the title level, only each
    keyword's family assignment is unambiguous) — this returns the first
    match in CATEGORY_KEYWORDS' declaration order. Callers that care about a
    specific search term's family should use `is_on_slice` instead.
    """
    from app.pipeline.relevance import _word_match  # local: avoid import cycle

    title_lower = title.lower()
    for family, keywords in CATEGORY_KEYWORDS.items():
        if any(_word_match(kw, title_lower) for kw in keywords):
            return family
    return None


def is_on_slice(title: str, search_term: str) -> bool:
    """True if `title` contains any keyword from `search_term`'s owning
    family (decision 7 of the search-coverage redesign: "any keyword from
    the searched term's family" — no single-best-family tiebreak, so a
    dual-role title is on-slice for each family it touches).
    """
    family = TERM_TO_FAMILY.get(search_term)
    if family is None:
        return False

    from app.pipeline.relevance import _word_match  # local: avoid import cycle

    title_lower = title.lower()
    return any(_word_match(kw, title_lower) for kw in CATEGORY_KEYWORDS[family])


def all_keywords() -> list[str]:
    """Flatten every category's keywords into one deduplicated list."""
    seen: dict[str, None] = {}
    for keywords in CATEGORY_KEYWORDS.values():
        for kw in keywords:
            seen[kw] = None
    return list(seen.keys())


# --- Central relevance gate config (app/pipeline/relevance.py) -------------
# The three active axes are tunable here: role keywords/excludes, location
# scope, and seniority band markers. Recency is collection metadata, not a gate.

ROLE_TITLE_KEYWORDS: list[str] = all_keywords()

# Strong, explicit target occupations that remain relevant when an off-role
# word merely describes their business domain. For example, "AI Native
# Engineer, Growth Marketing" is an AI engineering job, not a marketing job.
# Keep this list narrow: its matches intentionally take precedence over the
# generic OFF_ROLE_TITLE_MARKERS veto in role_ok().
ROLE_TITLE_OVERRIDE_KEYWORDS: list[str] = [
    "ai native engineer",
    "ai data engineer",
]

OFF_ROLE_TITLE_MARKERS: list[str] = [
    "sales", "marketing", "front-end", "frontend", "front end", "devops",
    "solution architect", "solutions architect", "recruiter",
    "talent acquisition", "account executive", "customer success",
    "sales development", "designer", "ui/ux", "product manager",
    "project manager", "qa engineer", "network engineer",
    "security engineer", "sales engineer", "teacher", "nurse", "driver",
    "accountant",
]

BANGALORE_MARKERS: list[str] = ["bangalore", "bengaluru", "blr"]

# Shared India-eligibility marker list — canonical source for the relevance
# gate and any connector that needs the same classification. Do not maintain a
# second copy of this list.
# NB: a bare "remote" token is deliberately NOT here — it is not evidence of
# India-eligibility, and treating it as such would let "Remote - United States"
# short-circuit past the foreign-only exclusion. Bare/empty-location remote is
# handled separately as the ADR-0002 recall exception in location_ok().
# NB: "ind" is the ISO country code, not a prefix of "india" — Built In and
# Talent.com write locations as "IND" / "Bengaluru, Karnataka, IND", which
# `"india"` does not match. It is safe against the US false positives that
# make short codes dangerous, because _word_match applies word boundaries:
# "Indiana", "Indianapolis", "Indeed", "Indore" and "independent" all fail to
# match, while "IND", "IND-only" and "Bengaluru, Karnataka, IND" all match
# (verified live, issue #76). This only matters on the remote path where the
# listing ALSO names a foreign location — "Remote - IND or New York" was
# being dropped as foreign, since the India-eligible check ran first and
# missed the code. A bare "IND" was already kept by the ambiguous
# fallthrough, so this is not a change to that case.
INDIA_ELIGIBLE_MARKERS: list[str] = [
    "india", "ind", "worldwide", "anywhere", "global", "apac", "asia",
]

# Explicit non-India country names commonly emitted by job boards. Keep this
# separate from regional aliases and subnational markers so country coverage
# can be audited without mistaking "LATAM" or "New York" for a country.
FOREIGN_COUNTRY_MARKERS: list[str] = [
    "afghanistan", "albania", "algeria", "andorra", "angola",
    "antigua and barbuda", "argentina", "armenia", "australia", "austria",
    "azerbaijan", "bahamas", "bahrain", "bangladesh", "barbados",
    "belarus", "belgium", "belize", "benin", "bhutan", "bolivia",
    "bosnia and herzegovina", "botswana", "brazil", "brunei", "bulgaria",
    "burkina faso", "burundi", "cabo verde", "cape verde", "cambodia",
    "cameroon", "canada", "central african republic", "chad", "chile",
    "china", "colombia", "comoros", "congo", "costa rica", "croatia",
    "cuba", "cyprus", "czech republic", "czechia", "democratic republic of the congo",
    "denmark", "djibouti", "dominica", "dominican republic", "east timor",
    "ecuador", "egypt", "el salvador", "equatorial guinea", "eritrea",
    "estonia", "eswatini", "ethiopia", "fiji", "finland", "france",
    "gabon", "gambia", "georgia", "germany", "ghana", "greece",
    "grenada", "guatemala", "guinea", "guinea bissau", "guyana", "haiti",
    "honduras", "hong kong", "hungary", "iceland", "indonesia", "iran",
    "iraq", "ireland", "israel", "italy", "ivory coast", "jamaica",
    "japan", "jordan", "kazakhstan", "kenya", "kiribati", "kosovo",
    "kuwait", "kyrgyzstan", "laos", "latvia", "lebanon", "lesotho",
    "liberia", "libya", "liechtenstein", "lithuania", "luxembourg",
    "macau", "madagascar", "malawi", "malaysia", "maldives", "mali",
    "malta", "marshall islands", "mauritania", "mauritius", "mexico",
    "micronesia", "moldova", "monaco", "mongolia", "montenegro", "morocco",
    "mozambique", "myanmar", "namibia", "nauru", "nepal", "netherlands",
    "new zealand", "nicaragua", "niger", "nigeria", "north korea",
    "north macedonia", "norway", "oman", "pakistan", "palau", "palestine",
    "panama", "papua new guinea", "paraguay", "peru", "philippines",
    "poland", "portugal", "qatar", "romania", "russia", "rwanda",
    "saint kitts and nevis", "saint lucia", "saint vincent and the grenadines",
    "samoa", "san marino", "sao tome and principe", "saudi arabia",
    "senegal", "serbia", "seychelles", "sierra leone", "singapore",
    "slovakia", "slovenia", "solomon islands", "somalia", "south africa",
    "south korea", "south sudan", "spain", "sri lanka", "sudan", "suriname",
    "swaziland", "sweden", "switzerland", "syria", "taiwan", "tajikistan",
    "tanzania", "thailand", "timor leste", "togo", "tonga",
    "trinidad and tobago", "tunisia", "turkey", "türkiye", "turkmenistan",
    "tuvalu", "uganda", "ukraine", "united arab emirates", "united kingdom",
    "united states", "uruguay", "uzbekistan", "vanuatu", "vatican city",
    "venezuela", "vietnam", "yemen", "zambia", "zimbabwe",
]

# Foreign-only location markers. Matched with word boundaries (see
# _word_match) and only AFTER the India-eligible check, so an India-inclusive
# listing (e.g. "Remote - India or New York") is kept regardless. Country
# spellings alone under-catch — US roles are usually pinned by city/state, or
# the bare "US" abbreviation — so those are enumerated too. Not exhaustive by
# design; widen here as new leak shapes surface (the audit script flags them).
FOREIGN_ONLY_MARKERS: list[str] = FOREIGN_COUNTRY_MARKERS + [
    # Countries / regions
    "united states", "usa", "us", "us-only", "us only", "uk",
    "united kingdom", "eu", "europe", "emea", "canada", "latam",
    "americas",
    "foreign only",
    # US states (full names — 2-letter codes like OR/IN/CA collide with
    # common words, so they are intentionally excluded)
    "california", "new york", "texas", "washington", "massachusetts",
    "illinois", "colorado", "oregon", "florida", "georgia", "virginia",
    "pennsylvania", "michigan", "arizona", "ohio", "north carolina",
    "new jersey", "minnesota",
    # Major US metros
    "san francisco", "new york city", "boston", "austin", "dallas",
    "los angeles", "seattle", "chicago", "denver", "atlanta", "houston",
    "san diego", "san jose", "philadelphia", "phoenix", "miami",
]

JUNIOR_TITLE_MARKERS: list[str] = [
    "intern", "internship", "trainee", "apprentice",
]

EXEC_TITLE_MARKERS: list[str] = [
    "head of", "vp", "vice president", "director", "chief", "cxo",
    "president", "svp", "evp", "c-level",
]

# Titles that CONTAIN an exec marker but are actually in-band and must not be
# excluded. "Associate/Assistant Vice President" (AVP) sits below VP — a
# common mid-level banking/credit-risk rung, squarely in the candidate's
# target band — so it overrides the "vice president"/"vp" exec match.
SENIORITY_BAND_OVERRIDES: list[str] = [
    "associate vice president", "assistant vice president", "avp",
]


# --- Contact-finding config (app/contacts/) --------------------------------

# Contact-finding role ladder: per category, the titles worth searching for
# at a company, in priority order (rung 0 = search first). A company's
# ladder is chosen from the categories its own job-posting history has
# matched (see app/contacts/company_categories.py), not user-specified.
# "exec_fallback" exists for small companies with no dedicated functional
# lead — searching it last means it only fires once the earlier rungs come
# up empty and the fetch cap hasn't been hit yet.
ROLE_LADDER: dict[str, list[dict]] = {
    "data_science": [
        {
            "tier": "hiring_manager",
            "titles": [
                "Head of Data Science", "Data Science Manager",
                "Director of Data Science", "VP of Data Science",
                "Head of Machine Learning", "Director of Analytics",
            ],
        },
        {
            "tier": "ic",
            "titles": ["Senior Data Scientist", "Data Scientist", "Machine Learning Engineer"],
        },
        {
            "tier": "talent_acquisition",
            "titles": ["Technical Recruiter", "Talent Acquisition Partner", "Recruiter"],
        },
        {
            "tier": "exec_fallback",
            "titles": ["CEO", "Founder", "Co-Founder"],
        },
    ],
    "analytics": [
        {
            "tier": "hiring_manager",
            "titles": [
                "Head of Analytics", "Analytics Manager", "Director of Analytics",
                "Head of Business Intelligence", "VP of Analytics",
            ],
        },
        {
            "tier": "ic",
            "titles": ["Senior Data Analyst", "Data Analyst", "Business Analyst", "BI Analyst"],
        },
        {
            "tier": "talent_acquisition",
            "titles": ["Technical Recruiter", "Talent Acquisition Partner", "Recruiter"],
        },
        {
            "tier": "exec_fallback",
            "titles": ["CEO", "Founder", "Co-Founder"],
        },
    ],
    "credit_risk": [
        {
            "tier": "hiring_manager",
            "titles": [
                "Head of Credit Risk", "Risk Manager", "Director of Risk",
                "VP of Risk", "Head of Underwriting", "Head of Model Risk",
            ],
        },
        {
            "tier": "ic",
            "titles": ["Senior Risk Analyst", "Risk Analyst", "Underwriter", "Quantitative Risk Analyst"],
        },
        {
            "tier": "talent_acquisition",
            "titles": ["Technical Recruiter", "Talent Acquisition Partner", "Recruiter"],
        },
        {
            "tier": "exec_fallback",
            "titles": ["CEO", "Founder", "Co-Founder"],
        },
    ],
}
