"""Canonical connector registration and construction.

Historical and inactive connectors are recoverable from Git rather than
retained as commented imports or dead list entries. Profile-driven runs use
``connector_instances`` below so connector ownership remains in this module.
"""

from __future__ import annotations

from app.collectors.api.himalayas import HimalayasConnector
from app.collectors.api.workingnomads import WorkingNomadsConnector
from app.collectors.browser.indeed import IndeedConnector
from app.collectors.browser.zip_recruiter import ZipRecruiterConnector
from app.collectors.feed.hn_hiring import HNHiringConnector
from app.collectors.feed.weworkremotely import WeWorkRemotelyConnector
from app.collectors.html.builtin import BuiltInConnector
from app.collectors.html.cutshort import CutshortConnector
from app.collectors.html.efinancialcareers import EFinancialCareersConnector
from app.collectors.html.foundit import FounditConnector
from app.collectors.html.glassdoor import GlassdoorConnector
from app.collectors.html.iimjobs import IIMJobsConnector
from app.collectors.html.instahyre import InstahyreConnector
from app.collectors.html.linkedin import LinkedInConnector
from app.collectors.html.naukri import NaukriConnector
from app.collectors.html.shine import ShineConnector
from app.collectors.html.talentcom import TalentComConnector
from app.collectors.html.timesjobs import TimesJobsConnector
from app.collectors.html.wellfound import WellfoundConnector
from app.config.categories import SEARCH_TERMS
from app.workflows.planning import Runtime, SearchProfile, SourceCapability
from app.pipeline.relevance import RelevancePolicy


# Instahyre's anonymous API has a source-wide rate budget. Twelve focused
# role/location queries at 100 rows retain useful breadth without allowing one
# query to starve the remaining matrix.
INSTAHYRE_MAX_RESULTS_PER_QUERY = 100

INDIA = frozenset({"IN"})
REMOTE = frozenset({"remote"})
INDIA_ARRANGEMENTS = frozenset({"remote", "hybrid", "onsite"})


def _india_source(
    name: str,
    *,
    runtime: Runtime = "http",
    prerequisites: tuple[str, ...] = (),
    rationale: str,
    locations_per_term: bool = True,
    instances_per_city: bool = True,
    instances_per_term: bool = True,
    local_cities: frozenset[str] | None = None,
) -> SourceCapability:
    return SourceCapability(
        name=name,
        runtime=runtime,
        countries=INDIA,
        arrangements=INDIA_ARRANGEMENTS,
        prerequisites=prerequisites,
        instances_per_term=instances_per_term,
        instances_per_country=True,
        instances_per_location=locations_per_term,
        instances_per_city=instances_per_city,
        local_cities=local_cities,
        coverage="India queries; exact board support is checked in the run plan",
        rationale=rationale,
    )


SOURCE_CATALOG: tuple[SourceCapability, ...] = (
    _india_source(name="builtin", rationale="Direct HTTP job search."),
    SourceCapability(
        name="cutshort", runtime="http", countries=INDIA,
        arrangements=INDIA_ARRANGEMENTS,
        prerequisites=("saved candidate session",),
        coverage="India-oriented saved-session feed",
        rationale="Use only after the user confirms the saved-session prerequisite.",
    ),
    _india_source(
        name="efinancialcareers",
        rationale="Finance-oriented HTTP source with query filtering.",
        locations_per_term=False,
        instances_per_term=False,
        local_cities=None,
    ),
    _india_source(
        name="foundit", rationale="Direct HTTP role and location search.",
        instances_per_city=True,
        local_cities=None,
    ),
    _india_source(name="glassdoor", rationale="Anonymous HTTP job search."),
    SourceCapability(
        name="himalayas", runtime="http", countries=frozenset({"*"}),
        arrangements=REMOTE, instances_per_term=True,
        coverage="remote jobs with a native country-code filter",
        rationale="Remote API source; the requested country is explicit in each query.",
    ),
    SourceCapability(
        name="hn_hiring", runtime="http", countries=frozenset({"*"}),
        arrangements=INDIA_ARRANGEMENTS,
        coverage="global monthly feed; country is listing evidence",
        rationale="Broad feed with central profession and geography screening.",
    ),
    _india_source(
        name="iimjobs", rationale="India-focused HTTP source with role terms.",
        locations_per_term=False,
        local_cities=None,
    ),
    _india_source(
        name="indeed", runtime="attended_browser",
        prerequisites=("desktop session",),
        rationale="Visible browser is required; it cannot run unattended.",
    ),
    _india_source(name="instahyre", rationale="Anonymous API role/location search."),
    _india_source(name="linkedin", rationale="Logged-out HTTP jobs surface."),
    _india_source(name="naukri", rationale="Anonymous HTTP role/location search."),
    _india_source(name="shine", rationale="HTTP role/location search."),
    _india_source(
        name="talentcom", runtime="headless_browser", prerequisites=("Chromium",),
        rationale="HTTP listings with a background-browser detail fallback.",
    ),
    _india_source(name="timesjobs", rationale="HTTP role/location search."),
    SourceCapability(
        name="wellfound", runtime="http", countries=INDIA,
        arrangements=INDIA_ARRANGEMENTS, instances_per_term=True,
        instances_per_location=True,
        instances_per_city=True,
        supported_search_terms=frozenset({
            "data analyst", "data scientist", "business analyst",
            "risk analyst", "machine learning engineer", "data engineer",
        }),
        coverage="India startup search with a closed role taxonomy",
        rationale="Selected only when every requested term has a native role route.",
    ),
    SourceCapability(
        name="weworkremotely", runtime="http", countries=frozenset({"*"}),
        arrangements=REMOTE,
        coverage="global remote feed; country is listing evidence",
        rationale="Broad remote feed screened centrally for profession and geography.",
    ),
    SourceCapability(
        name="workingnomads", runtime="http", countries=INDIA,
        arrangements=REMOTE, instances_per_term=True,
        coverage="remote listings eligible for India/APAC",
        rationale="Remote API source with native title and location filters.",
    ),
    _india_source(
        name="ziprecruiter", runtime="headless_browser",
        prerequisites=("Chromium",),
        rationale="Background-browser India job search.",
    ),
)


def _profile_locations(profile: SearchProfile, remote: str) -> list[tuple[str, str | None]]:
    """One remote route plus each local city, or the whole country if omitted."""
    modes: list[tuple[str, str | None]] = []
    if "remote" in profile.arrangements:
        modes.append((remote, None))
    if {"hybrid", "onsite"} & set(profile.arrangements):
        modes.extend(("city", city) for city in profile.cities)
        if not profile.cities:
            modes.append(("india", None))
    return modes


def connector_instances(source: str, profile: SearchProfile):
    """Construct one frozen profile's instances for a registered source."""
    terms = list(profile.search_terms)
    days = profile.collection_window_days
    if source == "himalayas":
        return [HimalayasConnector(search=term, country=country, max_age_days=days)
                for term in terms for country in profile.countries]
    if source == "weworkremotely":
        return [WeWorkRemotelyConnector(search_terms=terms, max_age_days=days)]
    if source == "hn_hiring":
        return [HNHiringConnector(max_age_days=days, india_or_remote_only=False)]
    if source == "workingnomads":
        return [WorkingNomadsConnector(search=term, max_age_days=days) for term in terms]
    if source == "cutshort":
        return [CutshortConnector(date_filter_days=days)]
    if source == "efinancialcareers":
        return [EFinancialCareersConnector(
            search_terms=terms, freshness_days=days, location_mode=mode, location=location,
        ) for mode, location in _profile_locations(profile, "remote")]
    if source == "iimjobs":
        return [IIMJobsConnector(
            search=term, freshness_days=days, location_mode=mode, location=location,
        ) for term in terms for mode, location in _profile_locations(profile, "remote")]
    if source == "foundit":
        locations = [location or ("remote" if mode == "remote" else "India")
                     for mode, location in _profile_locations(profile, "remote")]
        return [FounditConnector(search=f"{term} {location}", freshness_days=days,
                                 relevance_policy=RelevancePolicy.from_profile(profile))
                for term in terms for location in dict.fromkeys(locations)]
    constructors = {
        "shine": (ShineConnector, "remote", "freshness_days"),
        "builtin": (BuiltInConnector, "remote_india", "days_since_updated"),
        "glassdoor": (GlassdoorConnector, "remote_india", "date_filter_days"),
        "indeed": (IndeedConnector, "remote_india", "date_filter_days"),
        "instahyre": (InstahyreConnector, "remote", "date_filter_days"),
        "linkedin": (LinkedInConnector, "remote_india", "date_filter_days"),
        "naukri": (NaukriConnector, "remote", "date_filter_days"),
        "talentcom": (TalentComConnector, "remote", "date_filter_days"),
        "timesjobs": (TimesJobsConnector, "remote", "date_filter_days"),
        "wellfound": (WellfoundConnector, "remote_india", "recency_window_days"),
        "ziprecruiter": (ZipRecruiterConnector, "remote_india", None),
    }
    if source not in constructors:
        raise ValueError(f"no public connector adapter for source {source}")
    constructor, remote_mode, date_argument = constructors[source]
    instances = []
    for term in terms:
        for mode, location in _profile_locations(profile, remote_mode):
            kwargs = {"search": term, "location_mode": mode, "location": location}
            if date_argument:
                kwargs[date_argument] = days
            if source == "timesjobs":
                kwargs["relevance_policy"] = RelevancePolicy.from_profile(profile)
            instances.append(constructor(**kwargs))
    return instances


ACTIVE_CONNECTORS = [
    CutshortConnector(),
    *[
        WellfoundConnector(search=term, location_mode=mode)
        for term in SEARCH_TERMS
        for mode in ("bengaluru", "remote_india")
    ],
    *[
        LinkedInConnector(search=term, location_mode=mode)
        for term in SEARCH_TERMS
        for mode in ("remote_india", "bengaluru")
    ],
    *[
        ZipRecruiterConnector(search=term, location_mode=mode)
        for term in SEARCH_TERMS
        for mode in ("bengaluru", "remote_india")
    ],
    *[HimalayasConnector(search=term) for term in SEARCH_TERMS],
    WeWorkRemotelyConnector(search_terms=SEARCH_TERMS),
    HNHiringConnector(),
    *[
        BuiltInConnector(search=term, location_mode=mode)
        for term in SEARCH_TERMS
        for mode in ("bengaluru", "remote_india")
    ],
    *[
        ShineConnector(search=term, location_mode="bangalore")
        for term in SEARCH_TERMS
    ],
    *[
        TalentComConnector(search=term, location_mode=mode)
        for term in SEARCH_TERMS
        for mode in ("remote", "bengaluru")
    ],
    *[WorkingNomadsConnector(search=term) for term in SEARCH_TERMS],
    *[
        InstahyreConnector(
            search=term,
            location_mode=mode,
            max_results=INSTAHYRE_MAX_RESULTS_PER_QUERY,
        )
        for term in SEARCH_TERMS
        for mode in ("bangalore", "remote")
    ],
    EFinancialCareersConnector(search_terms=SEARCH_TERMS),
    *[
        TimesJobsConnector(search=term, location_mode=mode)
        for term in SEARCH_TERMS
        for mode in ("bengaluru", "remote")
    ],
    *[
        NaukriConnector(search=term, location_mode=mode)
        for term in SEARCH_TERMS
        for mode in ("bengaluru", "remote")
    ],
    *[
        GlassdoorConnector(search=term, location_mode=mode)
        for term in SEARCH_TERMS
        for mode in ("bengaluru", "remote_india")
    ],
    *[IIMJobsConnector(search=term) for term in SEARCH_TERMS],
    *[
        FounditConnector(search=f"{term} {location}")
        for term in SEARCH_TERMS
        for location in ("bangalore", "remote")
    ],
]
