"""
The ordered thesis list that drives prospect discovery.

A **thesis** is one sector-shaped bet about where companies worth
cold-emailing live — "India digital banking", "remote-first fintech". It is
the unit of work: one thesis per run, because a thesis is also the unit a
human can review coherently (twenty lending companies are comparable to each
other in a way twenty companies drawn from five sectors are not).

Why this list is checked in rather than invented per run:
  - Runs become reproducible. "Which thesis did that batch come from" has an
    answer that survives the session.
  - Progress is visible across sessions — `next_thesis()` walks down a fixed
    order instead of the agent drifting toward whatever it thought of last.
  - The agent physically cannot silently re-mine a directory it already
    consumed, because every Prospect row records its `source_query` and
    `source_url`.

This legacy list uses neutral example sectors and contains no candidate resume
history. Remote-first theses rank high because remote widens the location gate
rather than narrowing it — see ADR-0008 and the ranking bands in
`.claude/skills/find-prospects/`.

Seed queries are STARTING points, not a script. The skill is expected to
adapt them (add a year, swap a synonym, try a different aggregator) when one
returns thin — what it must not do is wander into a different thesis.
"""
from dataclasses import dataclass, field


# A thesis is done when it has produced this many `qualified` prospects.
# Sized so one run fits one sitting and one review.
TARGET_QUALIFIED_PER_THESIS = 20


@dataclass(frozen=True)
class Thesis:
    slug: str
    label: str
    # What makes a company in this thesis a strong match — shown to the agent
    # at the top of a run so the qualification judgement has the sector's own
    # vocabulary, not just generic "has a data team".
    rationale: str
    seed_queries: list[str] = field(default_factory=list)


# Ordered. `next_thesis()` takes the first that hasn't hit its target.
THESES: tuple[Thesis, ...] = (
    Thesis(
        slug="india-digital-banking",
        label="India digital banking & neobanking",
        rationale=(
            "Example banking-domain match. These companies run in-house credit "
            "scoring, onboarding risk models and transaction analytics — the "
            "exact stack in the resume master."
        ),
        seed_queries=[
            "top neobanking companies India 2026 list",
            "digital banking startups Bangalore data science team",
            "India neobank funded companies Tracxn",
        ],
    ),
    Thesis(
        slug="india-lending-bnpl",
        label="India lending, BNPL & NBFC-tech",
        rationale=(
            "Example lending-domain match. Underwriting, collections scoring and "
            "alternative-data credit models are the core data function, and "
            "they staff it in-house because it is the product."
        ),
        seed_queries=[
            "Bangalore fintech lending startups with data science teams 2026",
            "India BNPL companies list funded",
            "NBFC tech lending startups India underwriting machine learning",
        ],
    ),
    Thesis(
        slug="india-credit-bureau-underwriting",
        label="India credit bureau, scoring & risk infrastructure",
        rationale=(
            "The infrastructure layer under the lending thesis — bureaus, "
            "scoring-as-a-service, fraud and model-risk vendors. Small teams, "
            "high credit-risk density, and they rarely post publicly."
        ),
        seed_queries=[
            "credit bureau alternative credit scoring companies India",
            "India fraud detection risk analytics startups list",
            "model risk management vendors India Bangalore",
        ],
    ),
    Thesis(
        slug="india-insurtech",
        label="India insurtech",
        rationale=(
            "Adjacent to credit risk: pricing, claims fraud and actuarial "
            "modelling are the same toolkit under a different name."
        ),
        seed_queries=[
            "insurtech startups India list 2026",
            "India insurance claims fraud analytics companies",
            "Bangalore insurtech funded startups Tracxn",
        ],
    ),
    Thesis(
        slug="remote-first-fintech",
        label="Remote-first fintech hiring from India",
        rationale=(
            "Band 1 by construction — remote-first AND matching industry. "
            "Needs a positive India signal (an employee visibly in India, or "
            "a careers page naming India/APAC); a company that merely says "
            "'remote' does not qualify."
        ),
        seed_queries=[
            "remote-first fintech companies hiring India engineers",
            "fully remote fintech startups global team data science",
            "remote fintech companies APAC hiring handbook",
        ],
    ),
    Thesis(
        slug="india-logistics-supplychain",
        label="India logistics, supply chain & last-mile",
        rationale=(
            "Example logistics-domain match. Route optimisation, demand forecasting "
            "and ETA prediction are real in-house data functions here."
        ),
        seed_queries=[
            "India logistics tech startups data science route optimization",
            "last mile delivery companies India funded list",
            "supply chain analytics startups Bangalore",
        ],
    ),
    Thesis(
        slug="remote-first-data-ml",
        label="Remote-first companies with substantial data/ML orgs",
        rationale=(
            "Band 2 — industry is not a match, so the data org has to carry "
            "the whole case. Look for a public engineering/data blog or a "
            "named ML team, not just a job title."
        ),
        seed_queries=[
            "remote-first companies with large data science teams hiring globally",
            "fully distributed companies machine learning team engineering blog",
            "remote companies hiring data scientists India timezone",
        ],
    ),
    Thesis(
        slug="india-ai-ml-product",
        label="India AI/ML product companies",
        rationale=(
            "Band 2-3. Dense in-scope headcount and low competition for cold "
            "email, but weakest domain story — the pitch has to lean on ML "
            "engineering rather than credit risk."
        ),
        seed_queries=[
            "AI ML product startups Bangalore funded 2026",
            "India generative AI companies engineering team list",
            "applied AI startups India Tracxn Bengaluru",
        ],
    ),
)

THESES_BY_SLUG: dict[str, Thesis] = {t.slug: t for t in THESES}


def get_thesis(slug: str) -> Thesis:
    """Look up a thesis by slug, failing loudly with the valid set — a typo'd
    `--thesis` must not silently start an unnamed run whose rows can never be
    grouped with anything."""
    try:
        return THESES_BY_SLUG[slug]
    except KeyError:
        valid = ", ".join(t.slug for t in THESES)
        raise KeyError(f"unknown thesis {slug!r}; valid slugs: {valid}") from None
