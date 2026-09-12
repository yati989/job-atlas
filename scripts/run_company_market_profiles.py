"""Resolve and populate company market profiles without per-company LLM work."""

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from app.companies.market_profile_batch import (
    load_company_ids_file,
    run_ambitionbox_batch,
    run_glassdoor_batch,
    run_market_profile_batch,
)


def _write_checkpoint(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _snapshot_checkpoint_callback(path: str):
    checkpoint_path = Path(path)

    def record(snapshot_id: str, input_count: int) -> None:
        payload = (
            json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint_path.exists()
            else {}
        )
        glassdoor = dict(payload.get("glassdoor") or {})
        active = glassdoor.get("active_snapshot_id")
        if active not in {None, snapshot_id}:
            raise RuntimeError(
                f"checkpoint already owns active Glassdoor snapshot {active}"
            )
        snapshots = list(glassdoor.get("snapshots") or [])
        if not any(item.get("snapshot_id") == snapshot_id for item in snapshots):
            snapshots.append(
                {
                    "snapshot_id": snapshot_id,
                    "scope": "remaining accepted employer IDs after verified reuse",
                    "input_count": input_count,
                    "status": "triggered",
                    "triggered_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        glassdoor.update(
            {
                "active_snapshot_id": snapshot_id,
                "active_snapshot_input_count": input_count,
                "status": "snapshot_in_progress",
                "snapshots": snapshots,
            },
        )
        payload["glassdoor"] = glassdoor
        payload["next_safe_resume_step"] = (
            f"resume Glassdoor snapshot {snapshot_id}; never trigger a replacement"
        )
        _write_checkpoint(checkpoint_path, payload)

    return record


def _complete_snapshot_checkpoint(path: str, results) -> None:
    checkpoint_path = Path(path)
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    glassdoor = dict(payload.get("glassdoor") or {})
    active = glassdoor.get("active_snapshot_id")
    snapshots = list(glassdoor.get("snapshots") or [])
    for item in snapshots:
        if item.get("snapshot_id") == active:
            item["status"] = "completed"
            item["completed_at"] = datetime.now(timezone.utc).isoformat()
    glassdoor.update(
        {
            "status": "completed",
            "persisted_result_count": len(results),
            "persisted_ok_count": sum(
                item.source_statuses.get("glassdoor") == "ok" for item in results
            ),
            "persisted_error_count": sum(
                item.source_statuses.get("glassdoor") == "error" for item in results
            ),
            "snapshots": snapshots,
        },
    )
    payload["glassdoor"] = glassdoor
    payload.setdefault("phase_b", {})["glassdoor"] = "completed"
    payload["next_safe_resume_step"] = "run exact-manifest AmbitionBox collection"
    _write_checkpoint(checkpoint_path, payload)


def _ambitionbox_checkpoint_callback(path: str):
    checkpoint_path = Path(path)

    def record(result) -> None:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        ambitionbox = dict(payload.get("ambitionbox") or {})
        completed_ids = list(ambitionbox.get("completed_ids") or [])
        if result.company_id not in completed_ids:
            completed_ids.append(result.company_id)
        status_counts = dict(ambitionbox.get("status_counts") or {})
        source_status = result.source_statuses.get("ambitionbox", "error")
        status_counts[source_status] = status_counts.get(source_status, 0) + 1
        ambitionbox.update(
            {
                "status": "in_progress",
                "completed_ids": completed_ids,
                "completed_count": len(completed_ids),
                "status_counts": status_counts,
                "last_completed_company_id": result.company_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        payload["ambitionbox"] = ambitionbox
        payload["next_safe_resume_step"] = (
            "resume exact-manifest AmbitionBox collection; terminal completed "
            "companies will be skipped"
        )
        _write_checkpoint(checkpoint_path, payload)

    return record


def _complete_ambitionbox_checkpoint(path: str, results) -> None:
    checkpoint_path = Path(path)
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    ambitionbox = dict(payload.get("ambitionbox") or {})
    ambitionbox.update(
        {
            "status": "completed",
            "last_run_result_count": len(results),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    payload["ambitionbox"] = ambitionbox
    payload.setdefault("phase_b", {})["naukri"] = "completed"
    payload["next_safe_resume_step"] = "verify the completed exact-manifest wave"
    _write_checkpoint(checkpoint_path, payload)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    size = parser.add_mutually_exclusive_group()
    size.add_argument("--limit", type=int, default=100)
    size.add_argument("--all", action="store_true")
    parser.add_argument(
        "--resume-in-progress",
        action="store_true",
        help="Include rows left in_progress by an interrupted prior run.",
    )
    parser.add_argument(
        "--remote-only",
        action="store_true",
        help=(
            "Restrict the requested cohort to companies with a qualifying "
            "remote posting. By default both remote and non-remote companies "
            "are eligible."
        ),
    )
    source_mode = parser.add_mutually_exclusive_group()
    source_mode.add_argument(
        "--glassdoor-only",
        action="store_true",
        help=(
            "Persist reviewed internal-search IDs and collect only the "
            "deduplicated Glassdoor dataset snapshot"
        ),
    )
    source_mode.add_argument(
        "--ambitionbox-only",
        action="store_true",
        help=(
            "Recover only retryable AmbitionBox failures and successful "
            "broad-page salary gaps; never invoke Glassdoor discovery/collection "
            "or Levels.fyi"
        ),
    )
    source_mode.add_argument(
        "--levels-fyi-only",
        action="store_true",
        help=(
            "Retry only missing/failed Levels.fyi work; never invoke "
            "Glassdoor or AmbitionBox"
        ),
    )
    parser.add_argument("--resolution-workers", type=int, default=16)
    parser.add_argument("--company-workers", type=int, default=12)
    parser.add_argument("--levels-requests", type=int, default=8)
    parser.add_argument(
        "--glassdoor-resolutions",
        help=(
            "Reviewed internal-search Glassdoor resolution artifact. Required "
            "for Glassdoor collection; Bright Data SERP is not used."
        ),
    )
    parser.add_argument(
        "--glassdoor-snapshot-id",
        help=(
            "Resume an already-triggered Bright Data Glassdoor snapshot. "
            "This never submits a new paid snapshot."
        ),
    )
    parser.add_argument(
        "--wave-checkpoint",
        help=(
            "Wave checkpoint updated atomically as soon as a Glassdoor snapshot "
            "ID is returned, preventing duplicate paid triggers on restart."
        ),
    )
    parser.add_argument(
        "--ambitionbox-judgments",
        help=(
            "Reviewed Naukri candidate JSON. Without it, only exact/legal-name "
            "matches are allowed to reach AmbitionBox."
        ),
    )
    parser.add_argument(
        "--company-ids-file",
        help=(
            "Exact ordered company-ID manifest (one ID per line or CSV with "
            "company_id); prevents database cohort drift."
        ),
    )
    args = parser.parse_args()

    company_ids = load_company_ids_file(args.company_ids_file) if args.company_ids_file else None
    # An exact manifest always wins over the generic rolling selector.
    limit = None if args.all or company_ids is not None else args.limit
    if args.glassdoor_only:
        if not args.glassdoor_resolutions:
            parser.error("--glassdoor-only requires --glassdoor-resolutions")
        results = run_glassdoor_batch(
            resolutions_path=args.glassdoor_resolutions,
            snapshot_id=args.glassdoor_snapshot_id,
            company_ids=company_ids,
            snapshot_callback=(
                _snapshot_checkpoint_callback(args.wave_checkpoint)
                if args.wave_checkpoint
                else None
            ),
        )
        if args.wave_checkpoint:
            _complete_snapshot_checkpoint(args.wave_checkpoint, results)
    elif args.ambitionbox_only:
        results = run_ambitionbox_batch(
            limit=limit,
            remote_only=args.remote_only,
            judgments_path=args.ambitionbox_judgments,
            company_ids=company_ids,
            progress_callback=(
                _ambitionbox_checkpoint_callback(args.wave_checkpoint)
                if args.wave_checkpoint
                else None
            ),
        )
        if args.wave_checkpoint:
            _complete_ambitionbox_checkpoint(args.wave_checkpoint, results)
    elif args.levels_fyi_only:
        parser.error(
            "Levels.fyi is parked by source policy; use Glassdoor for WLB "
            "and AmbitionBox for salary estimates"
        )
    else:
        results = run_market_profile_batch(
            limit=limit,
            include_in_progress=args.resume_in_progress,
            resolution_workers=args.resolution_workers,
            company_workers=args.company_workers,
            levels_requests=args.levels_requests,
            remote_only=args.remote_only,
            ambitionbox_judgments_path=args.ambitionbox_judgments,
            glassdoor_resolutions_path=args.glassdoor_resolutions,
            company_ids=company_ids,
        )
    done = sum(item.status == "done" for item in results)
    partial = sum(item.status == "partial" for item in results)
    print(f"processed={len(results)} done={done} partial={partial}")
    for item in results:
        sources = ", ".join(
            f"{source}={status}" for source, status in sorted(item.source_statuses.items())
        )
        suffix = f" error={item.error}" if item.error else ""
        print(f"{item.company_id}\t{item.company_name}\t{item.status}\t{sources}{suffix}")


if __name__ == "__main__":
    main()
