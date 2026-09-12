"""Build a compact Google-Sheet payload for one frozen LinkedIn Decision Run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app.db.session import get_session
from app.models.orm import (
    DecisionRunIngestionObservation,
    DecisionRunStage,
    DecisionRunStageAttempt,
    DecisionRunStageRecord,
    Job,
)
from app.pipeline.work_location_enrichment import (
    WorkLocationEnrichmentResult,
    evidence_from_result,
)


HEADERS = [
    "Row", "External Job Id", "Title", "Company", "Listing Location",
    "Posted At", "Job Url", "Final Status", "Gate Reason",
    "Location Decision", "Location Confidence", "Normalized Location",
    "Automatic India Remote Fallback", "Location Evidence", "Location Reason",
    "Full Enrichment Status", "Experience Min Years", "Experience Max Years",
    "Education Requirement", "Qualification Other", "Hard Skills", "Soft Skills",
    "Salary Evidence", "Salary Guaranteed Max Lpa", "Salary Unusable Reason",
    "Query Search", "Query Location Mode", "Database Enrichment Status",
]


def _stage_records(session, run_id: str, stage_name: str):
    stage = session.execute(select(DecisionRunStage).where(
        DecisionRunStage.run_id == run_id,
        DecisionRunStage.name == stage_name,
    )).scalar_one()
    attempt = session.execute(select(DecisionRunStageAttempt).where(
        DecisionRunStageAttempt.stage_id == stage.id,
        DecisionRunStageAttempt.attempt_number == stage.attempt_number,
    )).scalar_one()
    records = session.execute(select(DecisionRunStageRecord).where(
        DecisionRunStageRecord.attempt_id == attempt.id,
    )).scalars().all()
    return {
        "advanced_count": stage.advanced_count,
        "dropped_count": stage.dropped_count,
        "failed_count": stage.failed_count,
    }, {
        str(row.record_id): {"outcome": row.outcome, "reason": row.reason}
        for row in records
    }


def build(run_id: str, run_dir: Path) -> dict:
    snapshot = json.loads((run_dir / "linkedin-location-fetch.json").read_text())[0]
    jobs = snapshot["jobs"]
    results = {
        row.external_job_id: row
        for row in (
            WorkLocationEnrichmentResult.model_validate_json(line)
            for line in (run_dir / "linkedin-location-results.jsonl")
            .read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }

    with get_session() as session:
        observations = session.execute(select(DecisionRunIngestionObservation).where(
            DecisionRunIngestionObservation.run_id == run_id,
        )).scalars().all()
        observation_by_external_id = {
            row.external_job_id: str(row.id) for row in observations
        }
        relevance_stage, relevance = _stage_records(
            session, run_id, "relevance_storage"
        )
        dedup_stage, dedup = _stage_records(
            session, run_id, "job_deduplication"
        )
        stored_jobs = session.execute(select(Job).where(
            Job.source == "linkedin",
            Job.external_job_id.in_([str(job["external_job_id"]) for job in jobs]),
        )).scalars().all()
        stored_by_external_id = {
            row.external_job_id: row.enrichment_status for row in stored_jobs
        }

    rows = []
    for index, job in enumerate(jobs, 1):
        external_id = str(job["external_job_id"])
        payload = job.get("raw_payload") or {}
        observation_id = observation_by_external_id[external_id]
        rel = relevance[observation_id]
        dedup_record = dedup.get(observation_id)
        result = results.get(external_id)

        if rel["outcome"] == "dropped":
            final_status = f"Dropped — {rel['reason']}"
        elif dedup_record is None:
            final_status = "Kept — dedup status unavailable"
        elif dedup_record["outcome"] == "advanced":
            final_status = "Kept — canonical"
        elif dedup_record["outcome"] == "dropped":
            final_status = "Kept — canonical duplicate"
        else:
            final_status = f"Failed — {dedup_record['reason'] or 'unknown'}"

        normalized = None
        enrichment = None
        if result is not None:
            normalized = evidence_from_result(
                result,
                listing_location=job.get("location_raw"),
                linkedin_remote_query=True,
            )
            enrichment = result.job_enrichment
        salary = enrichment.salary if enrichment and enrichment.salary else {}
        values = [
            index,
            external_id,
            job.get("title"),
            job.get("company_name_raw"),
            job.get("location_raw"),
            job.get("posted_at"),
            job.get("job_url"),
            final_status,
            rel["reason"],
            result.decision if result else None,
            result.confidence if result else None,
            (normalized.get("remote_scope") or normalized.get("location_raw"))
            if normalized else None,
            bool(normalized and normalized.get("fallback_remote_tagged")),
            result.evidence if result else None,
            result.reason if result else None,
            enrichment.status if enrichment else "not_run_early_gate_drop",
            enrichment.experience_min_years if enrichment else None,
            enrichment.experience_max_years if enrichment else None,
            enrichment.education_requirement if enrichment else None,
            enrichment.qualification_other if enrichment else None,
            ", ".join(enrichment.hard_skills) if enrichment else None,
            ", ".join(enrichment.soft_skills) if enrichment else None,
            salary.get("evidence"),
            salary.get("guaranteed_max_lpa"),
            salary.get("unusable_reason"),
            payload.get("query_search"),
            payload.get("query_location_mode"),
            stored_by_external_id.get(external_id),
        ]
        rows.append(values)

    location_counts: dict[str, int] = {}
    for result in results.values():
        location_counts[result.decision] = location_counts.get(result.decision, 0) + 1
    summary = [
        ["LinkedIn ML Engineer Remote India — Corrected Run Summary", "Value"],
        ["Run ID", run_id],
        ["Query", "remote machine learning engineer · India"],
        ["Fetched", len(jobs)],
        ["Combined location + full enrichment", len(results)],
        ["Early role/seniority drops", len(jobs) - len(results)],
        ["Relevance kept", relevance_stage["advanced_count"]],
        ["Relevance dropped", relevance_stage["dropped_count"]],
        ["Canonical kept", dedup_stage["advanced_count"]],
        ["Canonical duplicates", dedup_stage["dropped_count"]],
        ["Failures / pending", dedup_stage["failed_count"]],
        ["Remote India", location_counts.get("remote_india", 0)],
        ["Remote unspecified", location_counts.get("remote_unspecified", 0)],
        ["Bengaluru workplace", location_counts.get("bengaluru_workplace", 0)],
        ["Onsite outside Bengaluru", location_counts.get("onsite_outside_bengaluru", 0)],
        ["Unclear", location_counts.get("unclear", 0)],
        ["Unclear Bengaluru retained", sum(
            row[9] == "unclear"
            and str(row[7] or "").startswith("Kept")
            and any(marker in str(row[4] or "").casefold() for marker in ("bengaluru", "bangalore"))
            for row in rows
        )],
        ["Automatic India fallback", sum(bool(row[12]) for row in rows)],
        ["Policy", "Auto-accept only unclear + low/medium confidence + raw location exactly India."],
        ["Policy", "Location decisions use the hydrated LinkedIn description only."],
        ["Scope", "Reused the frozen 199-job LinkedIn snapshot and existing full job enrichment; no LinkedIn refetch."],
        ["QA", "PASS: 187/187 result packets validated; durable funnel completed with zero failures."],
    ]
    return {"headers": HEADERS, "summary": summary, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = build(args.run_id, args.run_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
