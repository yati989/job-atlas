"""Durable LinkedIn deterministic pre-gate location pass.

Inputs include the listing identity and complete description. Results are
generated locally, hash-pinned before the central relevance gate, and never
trigger company or careers-site research.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.models.schemas import NormalizedJob
from app.pipeline.relevance import role_ok, seniority_ok
from app.pipeline.runner import (
    FetchObservation,
    FetchResult,
    _reuse_stored_details,
    _safe_connector_dimensions,
)
from app.pipeline.work_location_enrichment import (
    WorkLocationEnrichmentInput,
    apply_results,
    load_results,
    write_deterministic_results,
    write_inputs,
)


def _is_remote_candidate(job: NormalizedJob) -> bool:
    return (
        job.source == "linkedin"
        and job.raw_payload.get("query_location_mode") == "remote_india"
        and role_ok(job)
        and seniority_ok(job)
    )


def hydrate_candidates(
    results: list[FetchResult], *, cutoff_at: datetime
) -> tuple[list[NormalizedJob], dict[str, int]]:
    """Hydrate each unique LinkedIn remote candidate before agent reasoning."""
    del cutoff_at  # Kept in the seam for compatibility; dates do not gate candidates.
    unique: dict[str, NormalizedJob] = {}
    for result in results:
        for job in result.jobs or []:
            if _is_remote_candidate(job):
                unique.setdefault(job.external_job_id, job)
    candidates = list(unique.values())
    if not candidates:
        return [], {"eligible": 0, "reused": 0, "hydrated": 0, "failed": 0}

    connector = next(
        result.connector
        for result in results
        if result.source == "linkedin"
        and callable(getattr(result.connector, "hydrate_relevant_jobs", None))
    )
    pending, reused = _reuse_stored_details("linkedin", candidates)
    hydration = (
        connector.hydrate_relevant_jobs(pending)
        if pending
        else {"eligible": 0, "hydrated": 0, "failed": 0}
    )

    canonical = {job.external_job_id: job for job in candidates}
    for result in results:
        for job in result.jobs or []:
            source = canonical.get(job.external_job_id)
            if source is None or source is job:
                continue
            job.description_raw = source.description_raw
            job.seniority = job.seniority or source.seniority
            job.employment_type = job.employment_type or source.employment_type
            for field in ("industry_scraped", "job_function"):
                if source.raw_payload.get(field) is not None:
                    job.raw_payload[field] = source.raw_payload[field]

    return candidates, {
        "eligible": len(candidates),
        "reused": reused,
        "hydrated": hydration.get("hydrated", 0),
        "failed": sum(not job.description_raw for job in candidates),
    }


def write_stage(
    *,
    snapshot_path: Path,
    input_path: Path,
    result_path: Path | None = None,
    results: list[FetchResult],
    connectors: list,
    cutoff_at: datetime,
    full_job_enrichment_requested: bool = False,
) -> dict[str, int]:
    candidates, counts = hydrate_candidates(results, cutoff_at=cutoff_at)
    save_fetch_results(snapshot_path, results, connectors)
    packets = write_inputs(
        input_path,
        candidates,
        full_job_enrichment_requested=full_job_enrichment_requested,
    )
    if result_path is not None:
        write_deterministic_results(result_path, packets)
    return counts


def apply_stage_results(
    *,
    results: list[FetchResult],
    input_path: Path,
    result_path: Path,
) -> int:
    expected = tuple(
        WorkLocationEnrichmentInput.model_validate_json(line)
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    judgments = load_results(result_path, expected)
    jobs = [job for result in results for job in (result.jobs or [])]
    applied = apply_results(jobs, judgments)
    expected_occurrences = sum(
        job.external_job_id in judgments for job in jobs if job.source == "linkedin"
    )
    if applied != expected_occurrences:
        raise RuntimeError(
            f"LinkedIn location result application mismatch: {applied}/{expected_occurrences}"
        )
    return len(judgments)


def save_fetch_results(path: Path, results: list[FetchResult], connectors: list) -> None:
    connector_indexes = {id(connector): index for index, connector in enumerate(connectors)}
    payload = []
    for result in results:
        jobs = list(result.jobs or [])
        job_indexes = {id(job): index for index, job in enumerate(jobs)}
        payload.append({
            "connector_index": connector_indexes[id(result.connector)],
            "source": result.source,
            "jobs": [job.model_dump(mode="json") for job in jobs],
            "error": str(result.error) if result.error is not None else None,
            "attempts": result.attempts,
            "retried": result.retried,
            "retry_succeeded": result.retry_succeeded,
            "elapsed_s": result.elapsed_s,
            "started_at": result.started_at.isoformat() if result.started_at else None,
            "ended_at": result.ended_at.isoformat() if result.ended_at else None,
            "dimensions": result.dimensions,
            "connector_class": result.connector_class,
            "observations": [
                {
                    "job_index": job_indexes[id(observation.job)],
                    "attempt": observation.attempt,
                    "ordinal": observation.ordinal,
                }
                for observation in result.observations
                if id(observation.job) in job_indexes
            ],
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_fetch_results(path: Path, connectors: list) -> list[FetchResult]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if len(payload) != len(connectors):
        raise ValueError(
            "frozen LinkedIn connector scope does not match ingest scope: "
            f"snapshot={len(payload)} ingest={len(connectors)}"
        )
    results = []
    for item in payload:
        connector = connectors[item["connector_index"]]
        if (
            connector.source_name != item["source"]
            or _safe_connector_dimensions(connector) != item["dimensions"]
        ):
            raise ValueError(
                "frozen LinkedIn connector identity does not match the current "
                f"ingest scope at index {item['connector_index']}"
            )
        jobs = [NormalizedJob.model_validate(job) for job in item["jobs"]]
        observations = [
            FetchObservation(
                jobs[observation["job_index"]],
                observation["attempt"],
                observation["ordinal"],
            )
            for observation in item["observations"]
        ]
        results.append(FetchResult(
            connector=connector,
            source=item["source"],
            jobs=jobs,
            error=RuntimeError(item["error"]) if item["error"] else None,
            attempts=item["attempts"],
            retried=item["retried"],
            retry_succeeded=item["retry_succeeded"],
            elapsed_s=item["elapsed_s"],
            started_at=datetime.fromisoformat(item["started_at"]) if item["started_at"] else None,
            ended_at=datetime.fromisoformat(item["ended_at"]) if item["ended_at"] else None,
            dimensions=item["dimensions"],
            connector_class=item["connector_class"],
            observations=observations,
        ))
    return results
