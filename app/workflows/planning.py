"""Validated search configuration and deterministic source/workload previews."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Runtime = Literal["http", "headless_browser", "attended_browser"]
Arrangement = Literal["remote", "hybrid", "onsite"]
Seniority = Literal[
    "intern",
    "entry",
    "individual_contributor",
    "manager",
    "director",
    "executive",
]


class ExperienceCriteria(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_years: float | None = Field(default=None, ge=0)
    maximum_years: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered_range(self) -> "ExperienceCriteria":
        if (
            self.minimum_years is not None
            and self.maximum_years is not None
            and self.minimum_years > self.maximum_years
        ):
            raise ValueError("minimum_years cannot exceed maximum_years")
        return self


class LocationFallbacks(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exact_country_as_remote: bool = False
    unspecified_remote: bool = False


class SourceSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["named", "count", "all_compatible"]
    sources: tuple[str, ...] = ()
    count: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def matching_arguments(self) -> "SourceSelection":
        if self.mode == "named" and not self.sources:
            raise ValueError("named source selection requires sources")
        if self.mode != "named" and self.sources:
            raise ValueError("sources are only valid for named selection")
        if self.mode == "count" and self.count is None:
            raise ValueError("count source selection requires count")
        if self.mode != "count" and self.count is not None:
            raise ValueError("count is only valid for count selection")
        return self


class SearchProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    professions: tuple[str, ...] = Field(min_length=1)
    search_terms: tuple[str, ...] = Field(min_length=1)
    relevance_terms: tuple[str, ...] = Field(min_length=1)
    title_exclusions: tuple[str, ...] = ()
    countries: tuple[str, ...] = Field(min_length=1)
    cities: tuple[str, ...] = ()
    arrangements: tuple[Arrangement, ...] = Field(min_length=1)
    # Accepted for compatibility with saved beta profiles; public eligibility
    # no longer rejects ordinary jobs by seniority.
    seniority: tuple[Seniority, ...] = ()
    include_internships: bool = False
    include_part_time: bool = False
    experience: ExperienceCriteria = Field(default_factory=ExperienceCriteria)
    location_fallbacks: LocationFallbacks = Field(default_factory=LocationFallbacks)
    hard_rejects: tuple[str, ...] = ()
    collection_window_days: int = Field(default=14, gt=0, le=30)
    source_selection: SourceSelection

    @field_validator(
        "professions",
        "search_terms",
        "relevance_terms",
        "title_exclusions",
        "cities",
        "hard_rejects",
    )
    @classmethod
    def nonblank_unique_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("values cannot be blank")
        if len(set(value.casefold() for value in normalized)) != len(normalized):
            raise ValueError("values must be unique")
        return normalized

    @field_validator("countries")
    @classmethod
    def country_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().upper() for value in values)
        if any(len(value) != 2 or not value.isalpha() for value in normalized):
            raise ValueError("countries must use two-letter ISO codes")
        if len(set(normalized)) != len(normalized):
            raise ValueError("countries must be unique")
        if normalized != ("IN",):
            raise ValueError("public beta supports only country code IN")
        return normalized


@dataclass(frozen=True)
class SourceCapability:
    name: str
    runtime: Runtime
    countries: frozenset[str]
    arrangements: frozenset[Arrangement]
    rationale: str
    coverage: str = ""
    prerequisites: tuple[str, ...] = ()
    instances_per_term: bool = False
    instances_per_location: bool = False
    instances_per_city: bool = False
    instances_per_country: bool = False
    supported_search_terms: frozenset[str] | None = None
    # None represents a city-agnostic query or a broad feed screened after
    # collection. A non-empty set is the closed set of local query cities.
    local_cities: frozenset[str] | None = None


@dataclass(frozen=True)
class PlannedSource:
    name: str
    runtime: Runtime
    prerequisites: tuple[str, ...]
    rationale: str
    coverage: str
    query_instance_count: int

    @property
    def attendance_required(self) -> bool:
        return self.runtime == "attended_browser"


@dataclass(frozen=True)
class UnsupportedSource:
    name: str
    reason: str


@dataclass(frozen=True)
class RunPreview:
    profile: SearchProfile
    selected_sources: tuple[PlannedSource, ...]
    unsupported_sources: tuple[UnsupportedSource, ...]
    source_count: int
    query_instance_count: int
    requested_source_count: int
    source_shortfall: int
    requires_confirmation: bool = True


ConfigurationInput = SearchProfile | Mapping[str, object] | str | Path


def _load_configuration(value: ConfigurationInput) -> Mapping[str, object]:
    if isinstance(value, SearchProfile):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return value
    path = Path(value).expanduser()
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, Mapping):
        raise ValueError(f"{path} must contain a configuration mapping")
    return data


def _incompatibility(profile: SearchProfile, source: SourceCapability) -> str | None:
    if source.supported_search_terms is not None:
        unsupported_term = next(
            (
                term for term in profile.search_terms
                if term.casefold() not in {item.casefold() for item in source.supported_search_terms}
            ),
            None,
        )
        if unsupported_term:
            return f"does not support search term {unsupported_term!r}"
    if "*" not in source.countries:
        unsupported_country = next(
            (country for country in profile.countries if country not in source.countries),
            None,
        )
        if unsupported_country:
            return f"does not support country {unsupported_country}"
    arrangements = set(profile.arrangements) & source.arrangements
    if not arrangements:
        return f"does not support {'/'.join(profile.arrangements)} work"
    if {"hybrid", "onsite"} & arrangements and source.local_cities is not None:
        supported = ", ".join(sorted(source.local_cities))
        if not profile.cities:
            return f"requires an explicit local city; supports {supported}"
        supported_cities = {city.casefold() for city in source.local_cities}
        unsupported_city = next(
            (city for city in profile.cities if city.casefold() not in supported_cities),
            None,
        )
        if unsupported_city:
            return (
                f"does not support local city {unsupported_city!r}; "
                f"supports {supported}"
            )
    return None


def _instances(profile: SearchProfile, source: SourceCapability) -> int:
    term_count = len(profile.search_terms) if source.instances_per_term else 1
    arrangements = set(profile.arrangements) & source.arrangements
    location_count = 1
    if source.instances_per_city:
        location_count = (
            int("remote" in arrangements)
            + (
                max(1, len(profile.cities))
                if {"hybrid", "onsite"} & arrangements
                else 0
            )
        ) or 1
    elif source.instances_per_location:
        location_count = int("remote" in arrangements) + int(
            bool({"hybrid", "onsite"} & arrangements)
        )
    country_count = len(profile.countries) if source.instances_per_country else 1
    return term_count * location_count * country_count


def prepare_run(
    configuration: ConfigurationInput,
    source_catalog: Sequence[SourceCapability],
) -> RunPreview:
    """Validate configuration and return the exact, unexecuted run preview."""
    profile = SearchProfile.model_validate(_load_configuration(configuration))
    catalog = {source.name: source for source in source_catalog}
    incompatibilities = {
        source.name: reason
        for source in source_catalog
        if (reason := _incompatibility(profile, source)) is not None
    }
    compatible = [
        source for source in source_catalog if source.name not in incompatibilities
    ]
    unsupported: list[UnsupportedSource] = []

    selection = profile.source_selection
    if selection.mode == "named":
        candidates: list[SourceCapability] = []
        for name in selection.sources:
            source = catalog.get(name)
            if source is None:
                unsupported.append(UnsupportedSource(name, "unknown source"))
                continue
            reason = _incompatibility(profile, source)
            if reason:
                unsupported.append(UnsupportedSource(name, reason))
            else:
                candidates.append(source)
        requested_count = len(selection.sources)
    elif selection.mode == "count":
        requested_count = selection.count or 0
        candidates = compatible[:requested_count]
        unsupported.extend(
            UnsupportedSource(source.name, incompatibilities[source.name])
            for source in source_catalog
            if source.name in incompatibilities
        )
    else:
        candidates = compatible
        requested_count = len(candidates)
        unsupported.extend(
            UnsupportedSource(source.name, incompatibilities[source.name])
            for source in source_catalog
            if source.name in incompatibilities
        )

    selected = tuple(
        PlannedSource(
            name=source.name,
            runtime=source.runtime,
            prerequisites=source.prerequisites,
            rationale=source.rationale,
            coverage=source.coverage,
            query_instance_count=_instances(profile, source),
        )
        for source in candidates
    )
    source_count = len(selected)
    return RunPreview(
        profile=profile,
        selected_sources=selected,
        unsupported_sources=tuple(unsupported),
        source_count=source_count,
        query_instance_count=sum(source.query_instance_count for source in selected),
        requested_source_count=requested_count,
        source_shortfall=max(0, requested_count - source_count),
    )
