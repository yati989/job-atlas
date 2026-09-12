"""Adapt a frozen public source plan to the existing connector interfaces."""
from __future__ import annotations

from collections.abc import Sequence

from app.models.schemas import NormalizedJob
from app.pipeline.registry import connector_instances
from app.workflows.guided_run import FetchSource
from app.workflows.planning import RunPreview, SearchProfile


def build_fetchers(preview: RunPreview, *, allow_attended: bool) -> dict[str, FetchSource]:
    """Build only selected sources; attended execution needs a separate opt-in."""
    fetchers: dict[str, FetchSource] = {}
    for planned in preview.selected_sources:
        if planned.attendance_required and not allow_attended:
            raise ValueError(
                f"{planned.name} requires a desktop session and --allow-attended"
            )

        def fetch(profile: SearchProfile, source=planned.name) -> Sequence[NormalizedJob]:
            jobs: list[NormalizedJob] = []
            for connector in connector_instances(source, profile):
                try:
                    jobs.extend(connector.fetch())
                finally:
                    connector.close_client()
            return jobs

        fetchers[planned.name] = fetch
    return fetchers
