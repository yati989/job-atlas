"""
Scripted half of the full-pipeline orchestration (issue #82, #88). Owns
the run window, the run directory, and every stage that's a plain script
— fetch, gate/upsert, job dedup, company dedup, skills dedup, and the
final workbook + self-send. Everything else (enrich, tailor, find
contacts, draft+push) is agent reasoning per this project's convention
(no billed LLM API — see CLAUDE.md's "Enrichment"/"Contact-finding"
sections) and is driven by the `full-pipeline` skill (#89), not here.

Because agent stages sit BETWEEN this script's stages (enrich runs after
company dedup but before skills dedup; tailor/contacts/draft run after
skills dedup but before the report), this can't be one `run()` call like
`run_daily_pipeline.py`'s — it's three CLI entry points the skill invokes
in sequence, with its own reasoning work happening between them, all
sharing one run's state on disk (a fresh `python -m` process per call has
no memory of the last one):

    python -m scripts.run_full_pipeline ingest --run-id <id>  # stages 1-4;
    # LinkedIn fetches alongside the other sources, then receives its required
    # deterministic location pass before the shared relevance gate.
    # ... skill does stage 5 (enrich) here ...
    python -m scripts.run_full_pipeline skills-dedup    # stage 6
    # ... skill does stages 7-9 (tailor/contacts/draft) here ...
    python -m scripts.run_full_pipeline report          # stage 10

`record-stage` lets the skill log its own (agent) stages into the same
run's stage_status list, so the workbook's Run summary sheet shows all
ten stages, not just the six this script drives directly:

    python -m scripts.run_full_pipeline record-stage \
        --stage enrich --status ok --detail "42 jobs enriched"

## The run window

Canonical runs are created first with `start`. Ingestion requires that
Decision Run ID and uses its immutable `since_at`/`cutoff_at` window. The
older date-folder commands remain temporarily available for legacy runs, but
they do not define the identity or window of new dashboard-visible runs.
"""
import argparse
import json
import logging
import time
from datetime import date as date_cls, datetime
from pathlib import Path

from app.pipeline.notifications import send_pipeline_notification
from app.decision_runs.connector_progress import DecisionRunConnectorProgress
from app.pipeline.progress import ConnectorProgress
from app.decision_runs.ingestion_funnel import (
    load_canonical_job_manifest,
    report_canonical_jobs,
    report_ingestion_funnel,
)
from app.decision_runs.progress import StageName, read_run_progress
from app.db.session import get_session
from app.pipeline.window import apply_sync_window
from app.pipeline.workload import enforce_workload_bounds
from app.pipeline.registry import ACTIVE_CONNECTORS
from app.pipeline.runner import (
    IncrementalRunPersister,
    fetch_all,
    gate_and_upsert,
    log_summary,
    retry_failed,
)
from app.pipeline.linkedin_location_stage import (
    apply_stage_results,
    load_fetch_results,
    write_stage as write_linkedin_location_stage,
)
from scripts.dedup_jobs import run as run_job_dedup
from scripts.dedup_companies import run as run_company_dedup
from scripts.normalize_skill_casing import run as run_skills_dedup_script

LOG_ROOT = Path(__file__).resolve().parent.parent / "logs" / "daily_runs"
AGENT_RESULT_ROOT = Path(__file__).resolve().parent.parent / "outputs" / "pipeline-context"


def _run_dir(run_date: str) -> Path:
    d = LOG_ROOT / run_date
    d.mkdir(parents=True, exist_ok=True)
    return d


def _canonical_run_dir(run_date: str, run_id: str) -> Path:
    d = _run_dir(run_date) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _today_run_dir() -> Path:
    return _run_dir(date_cls.today().isoformat())


def _resume_run_dir(args: argparse.Namespace) -> Path:
    """Keep downstream commands attached to the run that ingest created."""
    run_date = getattr(args, "run_date", None)
    return _run_dir(run_date) if run_date else _today_run_dir()


def _meta_path(run_dir: Path) -> Path:
    return run_dir / "full_pipeline_meta.json"


def _load_meta(run_dir: Path) -> dict:
    path = _meta_path(run_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"No full-pipeline run found at {path} — run `ingest` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _save_meta(run_dir: Path, meta: dict) -> None:
    _meta_path(run_dir).write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")


def _send_connector_completion_report(run_dir: Path, meta: dict) -> None:
    """Send the completed live connector table once, before later stages."""
    snapshot = meta.get("connector_progress")
    if not isinstance(snapshot, dict) or snapshot.get("status") not in {
        "completed", "partial", "failed",
    }:
        raise RuntimeError("completed connector progress is unavailable for email")

    delivery = meta.setdefault("connector_completion_email", {})
    state = delivery.get("state")
    if state == "delivered":
        return
    if state in {"in_progress", "indeterminate"}:
        raise RuntimeError(
            "connector completion email delivery is indeterminate; "
            "reconcile its Gmail receipt before any resend"
        )

    from app.outreach.gmail import get_service, send_self_report
    from app.pipeline.completion_email import render_completion_email

    report = render_completion_email(snapshot)
    delivery.update({"state": "in_progress", "subject": report.subject})
    _save_meta(run_dir, meta)
    send_started = False
    try:
        service = get_service()
        send_started = True
        receipt = send_self_report(
            service,
            subject=report.subject,
            body=report.plain_text,
            html_body=report.html,
        )
    except Exception as exc:
        delivery.update({
            "state": "indeterminate" if send_started else "failed",
            "error": f"{type(exc).__name__}: {exc}",
        })
        _save_meta(run_dir, meta)
        raise
    delivery.update({
        "state": "delivered",
        "receipt": str(
            receipt.get("id") or receipt.get("message_id") or "provider_accepted"
        ),
    })
    delivery.pop("error", None)
    _save_meta(run_dir, meta)


def _setup_logging(run_dir: Path) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = logging.FileHandler(run_dir / "full_pipeline.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    # Full detail remains available in full_pipeline.log.  Keeping routine
    # request/collector chatter off stdout prevents detached/agent-driven runs
    # from feeding megabytes of log text back into model context.
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _emit_agent_result(label: str, payload: dict, *, run_id: str) -> Path:
    """Persist the complete result and print only a context-sized receipt."""
    out_dir = AGENT_RESULT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{label}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    receipt = {"run_id": run_id, "artifact": str(path)}
    for key, value in payload.items():
        if key == "run_id":
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            receipt[key] = value
        elif isinstance(value, (list, tuple)):
            receipt[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            receipt[f"{key}_counts"] = {
                str(name): len(items) if isinstance(items, (list, tuple, dict)) else items
                for name, items in value.items()
            }
    print(json.dumps(receipt, separators=(",", ":"), default=str))
    return path


def _record_stage(meta: dict, stage: str, status: str, detail: str) -> None:
    meta.setdefault("stage_status", []).append({"stage": stage, "status": status, "detail": detail})


def _has_unresolved_failures(meta: dict) -> bool:
    latest = {}
    for item in meta.get("stage_status", []):
        latest[item["stage"]] = item["status"]
    return any(status == "failed" for status in latest.values())


def _record_result(run_dir: Path, meta: dict, stage: str, status: str, detail: str) -> bool:
    """Persist a result first, then alert from this process on failure."""
    _record_stage(meta, stage, status, detail)
    meta["outcome"] = "failed" if _has_unresolved_failures(meta) else "running"
    _save_meta(run_dir, meta)
    if status != "failed":
        return True

    delivered = send_pipeline_notification(
        title=f"job_agent pipeline failed: {stage}",
        message=detail,
        tags=["rotating_light"],
        priority="high",
    )
    meta.setdefault("notification_attempts", []).append(
        {"stage": stage, "kind": "failure", "delivered": delivered}
    )
    _save_meta(run_dir, meta)
    return delivered


def _reconcile_attended_progress(run_id: str) -> int:
    """Persist stale attempts before any attended command starts new work."""
    from app.db.session import get_session
    from app.decision_runs.lifecycle import reconcile_attended_run
    with get_session() as session:
        return reconcile_attended_run(session, run_id)


def cmd_ingest(args: argparse.Namespace) -> None:
    """Stages 1-4: fetch (both tiers) -> gate/upsert -> job dedup ->
    company dedup (Tier 1 only). Fail-fast per stage: if fetch/upsert
    raises structurally (not the existing per-item try/except inside
    gate_and_upsert, which already isolates one bad job), the run stops
    here rather than handing dedup a broken DB state."""
    _reconcile_attended_progress(args.run_id)
    since, started_at = _decision_run_window(args.run_id)
    run_date = started_at.date().isoformat()
    run_dir = _canonical_run_dir(run_date, args.run_id)
    _setup_logging(run_dir)
    logger = logging.getLogger("full_pipeline")

    if _meta_path(run_dir).exists():
        meta = _load_meta(run_dir)
        if meta.get("run_id") != args.run_id:
            raise RuntimeError("full-pipeline metadata belongs to another run")
    else:
        meta = {
            "run_id": args.run_id,
            "run_date": run_date,
            "since": since.isoformat(),
            "started_at": started_at.isoformat(),
            "stage_status": [],
        }
        _save_meta(run_dir, meta)
    logger.info("full-pipeline ingest: window since %s", since.isoformat())

    collection_scope = _resolve_collection_scope(
        args, _decision_run_policy_snapshot(args.run_id),
    )
    meta["collection_scope"] = collection_scope
    _save_meta(run_dir, meta)

    t0 = time.monotonic()
    with get_session() as session:
        durable_stages = {
            stage.name: stage
            for stage in read_run_progress(
                session, args.run_id, project_interruptions=False,
            ).stages
        }
    terminal = {"completed", "completed_with_errors"}
    fetch_done = (
        durable_stages.get(StageName.FETCH_JOBS.value) is not None
        and durable_stages[StageName.FETCH_JOBS.value].status in terminal
    )
    funnel_done = all(
        durable_stages.get(stage.value) is not None
        and durable_stages[stage.value].status in terminal
        for stage in (
            StageName.COLLECTION_DEDUPLICATION,
            StageName.RELEVANCE_STORAGE,
        )
    )

    connectors = list(ACTIVE_CONNECTORS)
    if args.headless_only:
        logger.info("--headless-only: skipping the headed tier")
    else:
        from scripts.run_headed_sources import CONNECTORS as HEADED_CONNECTORS
        connectors = connectors + list(HEADED_CONNECTORS)
    if (
        collection_scope["source"]
        or collection_scope["one_per_source"]
        or collection_scope["location_mode"]
    ):
        from app.pipeline.run_all import select_connectors
        connectors = select_connectors(
            connectors,
            source=collection_scope["source"],
            one_per_source=collection_scope["one_per_source"],
            sample_search=collection_scope["sample_search"],
            sample_location=collection_scope["sample_location"],
            location_mode=collection_scope["location_mode"],
        )
        if not connectors:
            raise SystemExit("connector selection is empty")
        logger.info(
            "Immutable collection scope selected %s source instance(s): search=%s location=%s",
            len(connectors), collection_scope["sample_search"],
            collection_scope["sample_location"] or collection_scope["location_mode"],
        )

    if fetch_done and funnel_done:
        with get_session() as session:
            canonical_manifest = load_canonical_job_manifest(session, args.run_id)
        logger.info("Reusing completed fetch and ingestion funnel for run %s", args.run_id)
        job_dedup_stage = durable_stages.get(StageName.JOB_DEDUPLICATION.value)
        if (
            job_dedup_stage is not None
            and job_dedup_stage.status == "completed_with_errors"
        ):
            linkedin_connectors = [
                connector for connector in connectors
                if connector.source_name == "linkedin"
            ]
            if not linkedin_connectors:
                raise RuntimeError(
                    "recovering isolated upsert failures requires the frozen source selection"
                )
            snapshot_path = run_dir / "linkedin-location-fetch.json"
            input_path = run_dir / "linkedin-location-input.jsonl"
            result_path = run_dir / "linkedin-location-results.jsonl"
            staged_linkedin_results = load_fetch_results(
                snapshot_path, linkedin_connectors,
            )
            apply_stage_results(
                results=staged_linkedin_results,
                input_path=input_path,
                result_path=result_path,
            )
            recovery_summary, recovered, recovery_errors = gate_and_upsert(
                staged_linkedin_results,
                cutoff_at=since,
            )
            logger.info(
                "Replayed frozen LinkedIn snapshot for upsert recovery: "
                "upserted=%s errors=%s",
                recovered, recovery_errors,
            )
            if recovery_errors:
                log_summary(recovery_summary)
        _record_stage(
            meta, "fetch_gate_upsert", "ok",
            "reused completed durable fetch and ingestion funnel",
        )
    else:
        linkedin_connectors = [
            connector for connector in connectors
            if connector.source_name == "linkedin"
        ]
        staged_linkedin_results = []
        linkedin_checkpoint_exists = False
        if linkedin_connectors:
            snapshot_path = run_dir / "linkedin-location-fetch.json"
            input_path = run_dir / "linkedin-location-input.jsonl"
            result_path = run_dir / "linkedin-location-results.jsonl"
            checkpoint_parts = (
                snapshot_path.exists(), input_path.exists(), result_path.exists(),
            )
            if any(checkpoint_parts) and not all(checkpoint_parts):
                raise RuntimeError(
                    "LinkedIn location checkpoint is incomplete; expected fetch, "
                    "input, and result artifacts together"
                )
            linkedin_checkpoint_exists = all(checkpoint_parts)
            if linkedin_checkpoint_exists:
                staged_linkedin_results = load_fetch_results(
                    snapshot_path, linkedin_connectors
                )
                enriched = apply_stage_results(
                    results=staged_linkedin_results,
                    input_path=input_path,
                    result_path=result_path,
                )
                logger.info(
                    "Reused %s deterministic LinkedIn location enrichments before relevance",
                    enriched,
                )

        # A frozen LinkedIn checkpoint is replayed on resume.  On a new run,
        # LinkedIn stays in this complete connector set so its network work
        # overlaps the non-browser pool and serial browser lane in fetch_all.
        live_connectors = (
            [connector for connector in connectors if connector.source_name != "linkedin"]
            if linkedin_checkpoint_exists
            else connectors
        )
        progress = DecisionRunConnectorProgress(
            connectors,
            run_id=args.run_id,
            command="scripts.run_full_pipeline ingest",
        )
        window = apply_sync_window(connectors, since_at=since, cutoff_at=started_at)
        enforce_workload_bounds(connectors)
        logger.info(
            "Sync window native prefilter: %s day(s), exact cutoff=%s",
            window.native_days,
            window.since_at.isoformat(),
        )
        try:
            for staged in staged_linkedin_results:
                progress.instance_started(staged.connector, attempt=staged.attempts)
                progress.instance_finished(staged)
            persister = IncrementalRunPersister(cutoff_at=window.since_at)
            live_results = fetch_all(
                live_connectors,
                progress=progress,
                # LinkedIn's location policy must be applied before its first
                # relevance decision.  Other sources remain crash-safe while
                # collection is still running.
                on_result=lambda result: (
                    None if result.source == "linkedin" else persister.persist(result)
                ),
            )
            retry_failed(live_results, progress=progress)
            if linkedin_connectors and not linkedin_checkpoint_exists:
                staged_linkedin_results = [
                    result for result in live_results if result.source == "linkedin"
                ]
                counts = write_linkedin_location_stage(
                    snapshot_path=snapshot_path,
                    input_path=input_path,
                    result_path=result_path,
                    results=staged_linkedin_results,
                    connectors=linkedin_connectors,
                    cutoff_at=window.since_at,
                    full_job_enrichment_requested=False,
                )
                enriched = apply_stage_results(
                    results=staged_linkedin_results,
                    input_path=input_path,
                    result_path=result_path,
                )
                logger.info(
                    "Applied %s deterministic LinkedIn location enrichments after "
                    "parallel collection (%s candidates, %s hydrated, %s failed)",
                    enriched,
                    counts.get("eligible", 0),
                    counts.get("hydrated", 0),
                    counts.get("failed", 0),
                )
                results = live_results
            else:
                results = [*staged_linkedin_results, *live_results]
            persister.persist_remaining(results)
            gate_facts = []
            summary, total_upserted, total_errors = gate_and_upsert(
                results, cutoff_at=window.since_at, outcome_sink=gate_facts.append,
            )
            canonical_manifests = []

            def persist_funnel(session):
                canonical_manifests.append(report_ingestion_funnel(
                    session, args.run_id, gate_facts[0],
                ))

            progress.complete(summary, after_finish=persist_funnel)
            canonical_manifest = canonical_manifests[0]
        except Exception as exc:
            logger.error("Fetch/gate/upsert/funnel failed structurally: %s", exc)
            progress.fail(str(exc))
            _record_result(run_dir, meta, "fetch_gate_upsert", "failed", str(exc))
            raise
        log_summary(summary)
        meta["connector_progress"] = progress.snapshot
        _record_stage(
            meta, "fetch_gate_upsert", "ok",
            f"{len(connectors)} connector instances, upserted={total_upserted} errors={total_errors}",
        )

    # First pipeline email: the dashboard projection is complete and durable,
    # while decision preparation and its approval email have not started yet.
    _save_meta(run_dir, meta)
    _send_connector_completion_report(run_dir, meta)

    job_dedup = durable_stages.get(StageName.JOB_DEDUPLICATION.value)
    if job_dedup is not None and job_dedup.status == "completed":
        _record_stage(
            meta, "job_dedup", "ok",
            "reused completed durable job deduplication",
        )
    else:
        try:
            job_dedup_stats = run_job_dedup(dry_run=False)
            with get_session() as session:
                report_canonical_jobs(session, args.run_id, canonical_manifest)
            _record_stage(
                meta, "job_dedup", "ok",
                f"scanned={job_dedup_stats.get('scanned')} flagged={job_dedup_stats.get('flagged')}",
            )
        except Exception as exc:
            logger.error("Job dedup failed: %s", exc)
            _record_result(run_dir, meta, "job_dedup", "failed", str(exc))
            raise

    try:
        company_dedup_stats = run_company_dedup(apply=True)
        meta["dedup_stats"] = company_dedup_stats
        _record_stage(
            meta, "company_dedup", "ok",
            f"tier1_merged={company_dedup_stats.get('tier1_merged')} "
            f"tier2_groups={company_dedup_stats.get('tier2_groups')} (not auto-merged)",
        )
    except Exception as exc:
        logger.error("Company dedup failed: %s", exc)
        _record_result(run_dir, meta, "company_dedup", "failed", str(exc))
        raise

    meta["ingest_elapsed_s"] = time.monotonic() - t0
    _save_meta(run_dir, meta)
    logger.info("ingest complete, meta written to %s", _meta_path(run_dir))


def cmd_linkedin_location_input(args: argparse.Namespace) -> None:
    """Fetch/hydrate LinkedIn and write deterministic pre-gate location results."""
    _reconcile_attended_progress(args.run_id)
    since, started_at = _decision_run_window(args.run_id)
    run_dir = _canonical_run_dir(started_at.date().isoformat(), args.run_id)
    _setup_logging(run_dir)
    snapshot_path = run_dir / "linkedin-location-fetch.json"
    input_path = run_dir / "linkedin-location-input.jsonl"
    result_path = run_dir / "linkedin-location-results.jsonl"

    connectors = [
        connector for connector in ACTIVE_CONNECTORS
        if connector.source_name == "linkedin"
    ]
    if getattr(args, "location_mode", None):
        connectors = [
            connector for connector in connectors
            if getattr(connector, "location_mode", None) == args.location_mode
        ]
    if getattr(args, "one_instance", False):
        from app.pipeline.run_all import select_connectors
        connectors = select_connectors(
            connectors,
            source="linkedin",
            one_per_source=True,
            sample_search=args.sample_search,
            sample_location=args.sample_location,
        )
    if not connectors:
        raise RuntimeError("no active LinkedIn connectors are registered")

    # This is a prerequisite snapshot, not the Decision Run's all-source
    # fetch stage.  The latter owns one immutable expected count across every
    # configured source and is started by ``ingest`` below.
    progress = ConnectorProgress(
        connectors,
        run_id=args.run_id,
        command="scripts.run_full_pipeline linkedin-location-input",
    )

    if snapshot_path.exists() and input_path.exists():
        staged_results = load_fetch_results(snapshot_path, connectors)
        for staged in staged_results:
            progress.instance_started(
                staged.connector, attempt=max(1, staged.attempts),
            )
            progress.instance_finished(staged)
        progress.finish()
        count = sum(
            bool(line.strip())
            for line in input_path.read_text(encoding="utf-8").splitlines()
        )
        if not result_path.exists():
            from app.pipeline.work_location_enrichment import (
                WorkLocationEnrichmentInput,
                write_deterministic_results,
            )
            packets = tuple(
                WorkLocationEnrichmentInput.model_validate_json(line)
                for line in input_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            write_deterministic_results(result_path, packets)
        _emit_agent_result("linkedin-location-input", {
            "run_id": args.run_id,
            "input": str(input_path),
            "result": str(result_path),
            "candidates": count,
            "reused": True,
            "location_method": "deterministic_regex",
        }, run_id=args.run_id)
        return

    window = apply_sync_window(connectors, since_at=since, cutoff_at=started_at)
    enforce_workload_bounds(connectors)
    try:
        results = fetch_all(connectors, progress=progress)
        retry_failed(results, progress=progress)
        counts = write_linkedin_location_stage(
            snapshot_path=snapshot_path,
            input_path=input_path,
            result_path=result_path,
            results=results,
            connectors=connectors,
            cutoff_at=window.since_at,
            full_job_enrichment_requested=False,
        )
        progress.finish()
    except Exception as exc:
        progress.fail(str(exc))
        raise
    _emit_agent_result("linkedin-location-input", {
        "run_id": args.run_id,
        "input": str(input_path),
        "result": str(result_path),
        "snapshot": str(snapshot_path),
        "location_method": "deterministic_regex",
        **counts,
    }, run_id=args.run_id)


def _decision_run_window(run_id: str) -> tuple[datetime, datetime]:
    """Read the immutable window owned by the named durable Decision Run."""
    from app.db.session import get_session
    from app.models.orm import DecisionRun

    with get_session() as session:
        run = session.get(DecisionRun, run_id)
        if run is None:
            raise SystemExit(f"unknown decision run {run_id}")
        if run.state != "preparing":
            raise SystemExit(f"decision run {run_id} is {run.state}; ingestion requires preparing")
        return run.since_at, run.cutoff_at


def _decision_run_policy_snapshot(run_id: str) -> dict:
    from app.db.session import get_session
    from app.models.orm import DecisionRun
    with get_session() as session:
        run = session.get(DecisionRun, run_id)
        if run is None:
            raise SystemExit(f"unknown decision run {run_id}")
        return dict(run.policy_snapshot or {})


def _decision_policy_for_start(args: argparse.Namespace):
    """Build the immutable policy, including an optional connector instance."""
    from app.decision_runs.types import DecisionPolicy
    from app.pipeline.run_all import normalize_sample_location, normalize_sample_search

    raw_search = getattr(args, "search", None)
    raw_location = getattr(args, "location", None)
    if bool(raw_search) != bool(raw_location):
        raise SystemExit("--search and --location must be supplied together")
    if not raw_search:
        return DecisionPolicy()
    try:
        search = normalize_sample_search(raw_search)
        location = normalize_sample_location(raw_location)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return DecisionPolicy(
        collection_search=search,
        collection_location=location,
    )


def _resolve_collection_scope(args: argparse.Namespace, policy_snapshot: dict) -> dict:
    """Resolve CLI selection against the Decision Run's immutable scope."""
    durable_search = policy_snapshot.get("collection_search")
    durable_location = policy_snapshot.get("collection_location")
    if durable_search or durable_location:
        if not (durable_search and durable_location):
            raise SystemExit("decision run has an incomplete immutable collection scope")
        explicit_override = bool(
            getattr(args, "one_per_source", False)
            or getattr(args, "sample_location", None)
            or getattr(args, "location_mode", None)
        )
        if explicit_override:
            from app.pipeline.run_all import normalize_sample_location, normalize_sample_search
            try:
                requested_search = normalize_sample_search(
                    getattr(args, "sample_search", durable_search)
                )
                requested_location = normalize_sample_location(
                    getattr(args, "sample_location", None) or durable_location
                )
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            if (
                getattr(args, "location_mode", None)
                or requested_search != durable_search
                or requested_location != durable_location
            ):
                raise SystemExit(
                    "ingest CLI options cannot override the Decision Run's immutable collection scope"
                )
        return {
            "source": getattr(args, "source", None),
            "one_per_source": True,
            "sample_search": durable_search,
            "sample_location": durable_location,
            "location_mode": None,
        }
    return {
        "source": getattr(args, "source", None),
        "one_per_source": getattr(args, "one_per_source", False),
        "sample_search": getattr(args, "sample_search", "data scientist"),
        "sample_location": getattr(args, "sample_location", None),
        "location_mode": getattr(args, "location_mode", None),
    }


def cmd_start(args: argparse.Namespace) -> None:
    """Create the durable run before any mutable external work starts."""
    from app.db.session import get_session
    from app.decision_runs import create_decision_run
    policy = _decision_policy_for_start(args)
    with get_session() as session:
        run = create_decision_run(session, since=args.since, policy=policy)
        scope = (
            f" search={policy.collection_search!r} location={policy.collection_location!r}"
            if policy.collection_search else ""
        )
        print(
            f"{run.id} window={run.since_at.isoformat()}..{run.cutoff_at.isoformat()} "
            f"timezone={run.input_timezone}{scope}"
        )


def cmd_prepare_decision(args: argparse.Namespace) -> None:
    from app.db.session import get_session
    from app.decision_runs import prepare_decision_run
    from app.models.orm import DecisionRun
    from app.decision_runs.lifecycle import approval_artifact_ready, reconcile_attended_run
    with get_session() as session:
        reconcile_attended_run(session, args.run_id)
        summary = prepare_decision_run(session, args.run_id, defer_approval_wait=True)
        run = session.get(DecisionRun, args.run_id)
        delivery_receipt = None
        approval_sheet = None
        if args.output:
            from app.reporting.decision_report import build_decision_workbook
            build_decision_workbook(session, args.run_id, args.output)
            if args.send_self_report and run.state != "awaiting_approval":
                from app.decision_runs.google_sheet_approval import (
                    create_approval_spreadsheet,
                    get_service as get_drive_service,
                )
                from app.reporting.approval_email import render_approval_email
                from app.outreach.gmail import get_service, send_self_report
                approval_sheet = create_approval_spreadsheet(
                    get_drive_service(), args.output,
                    title=f"job_agent decision approval — {args.run_id}",
                )
                approval_email = render_approval_email(
                    session, args.run_id, approval_sheet["spreadsheet_url"],
                )
                delivery_receipt = send_self_report(get_service(), subject=f"job_agent decision run — {args.run_id} — LATEST approval sheet",
                    body=approval_email.text_body, attachment_path=None,
                    html_body=approval_email.html_body)
                if not (delivery_receipt or {}).get("threadId"):
                    raise RuntimeError(
                        "approval workbook delivery returned no Gmail thread id"
                    )
        if run.state != "awaiting_approval":
            approval_artifact_ready(
                session, args.run_id,
                available_company_count=summary["available_company_count"],
                gmail_message_id=(delivery_receipt or {}).get("id"),
                gmail_thread_id=(delivery_receipt or {}).get("threadId"),
                google_spreadsheet_id=(approval_sheet or {}).get("spreadsheet_id"),
                google_spreadsheet_url=(approval_sheet or {}).get("spreadsheet_url"),
            )
        _emit_agent_result("prepare-decision", summary, run_id=args.run_id)


def cmd_screen_decision(args: argparse.Namespace) -> None:
    """Freeze exact eligible company scope before agent-driven enrichment."""
    from app.decision_runs import screen_decision_run
    from app.decision_runs.lifecycle import reconcile_attended_run
    with get_session() as session:
        reconcile_attended_run(session, args.run_id)
        _emit_agent_result(
            "screen-decision", screen_decision_run(session, args.run_id),
            run_id=args.run_id,
        )


def cmd_begin_company_phase_b(args: argparse.Namespace) -> None:
    """Record Phase B as running immediately before source-worker dispatch."""
    from app.decision_runs import begin_company_phase_b
    from app.decision_runs.lifecycle import reconcile_attended_run
    with get_session() as session:
        reconcile_attended_run(session, args.run_id)
        company_ids = begin_company_phase_b(session, args.run_id)
        _emit_agent_result(
            "begin-company-phase-b",
            {"company_ids": list(company_ids), "company_count": len(company_ids)},
            run_id=args.run_id,
        )


def cmd_screening_input(args: argparse.Namespace) -> None:
    """Write compact salary/experience evidence for the pre-approval agent pass."""
    from app.decision_runs import freeze_job_enrichment_manifest
    from app.jobs.screening_brief import screening_brief
    from app.models.orm import JobPostingVersion

    with get_session() as session:
        manifest = freeze_job_enrichment_manifest(session, args.run_id)
        rows = []
        for item in manifest.jobs:
            version = session.get(JobPostingVersion, item.posting_version_id)
            if version is None:
                continue
            brief = screening_brief(version.snapshot if isinstance(version.snapshot, dict) else {})
            rows.append({
                "posting_version_id": item.posting_version_id,
                "job_id": item.job_id,
                **brief,
            })
    out_dir = AGENT_RESULT_ROOT / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "screening-input.jsonl"
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":"), default=str) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(json.dumps({
        "run_id": args.run_id,
        "jobs_count": len(rows),
        "artifact": str(path),
        "evidence_chars": sum(row["screening_evidence_chars"] for row in rows),
    }, separators=(",", ":")))


def cmd_finalize_decision(args: argparse.Namespace) -> None:
    """Rank after enrich-companies reported terminal Phase B evidence."""
    from app.decision_runs import finalize_decision_run
    from app.decision_runs.lifecycle import reconcile_attended_run
    with get_session() as session:
        reconcile_attended_run(session, args.run_id)
        summary = finalize_decision_run(session, args.run_id, defer_approval_wait=True)
        _emit_agent_result("finalize-decision", summary, run_id=args.run_id)


def cmd_approve(args: argparse.Namespace) -> None:
    from app.db.session import get_session
    from app.decision_runs import approve_decision_run
    from app.decision_runs.lifecycle import (
        approval_input_received,
        approval_input_rejected,
        reconcile_attended_run,
    )
    approval_error = None
    scope = None
    with get_session() as session:
        reconcile_attended_run(session, args.run_id)
        from app.decision_runs.telemetry import is_legacy_telemetry
        from app.models.orm import DecisionRun
        run = session.get(DecisionRun, args.run_id)
        current_telemetry = run is not None and not is_legacy_telemetry(run.telemetry_version)
        if current_telemetry:
            approval_input_received(session, args.run_id)
        try:
            selections = None
            if args.workbook:
                from app.decision_runs.workbook_approval import read_contact_search_selections
                selections = read_contact_search_selections(args.workbook, args.run_id)
        except Exception as exc:
            if current_telemetry:
                approval_input_rejected(
                    session, args.run_id, reason="approval input rejected before persistence",
                )
            approval_error = exc
        if approval_error is None:
            try:
                scope = approve_decision_run(
                    session, args.run_id, args.selection,
                    contact_search_selections=selections,
                    input_received=current_telemetry,
                )
            except Exception as exc:
                # The service has already durably returned rejected input to
                # Waiting.  Delay propagation until the session commits it.
                approval_error = exc
    if approval_error is not None:
        raise approval_error
    assert scope is not None
    _emit_agent_result("approval", {
        "run_id": scope.run_id,
        "company_ids": scope.company_ids,
        "job_ids": scope.job_ids,
    }, run_id=scope.run_id)


def cmd_approve_email_reply(args: argparse.Namespace) -> None:
    """Read the recorded Google Sheet after a reply in its decision email thread."""
    import time
    import re
    from pathlib import Path
    from app.db.session import get_session
    from app.decision_runs import approve_decision_run_from_workbook
    from app.decision_runs.email_approval import (ApprovalReplyNotFound,
                                                   find_latest_approval_reply)
    from app.decision_runs.google_sheet_approval import (
        export_approval_spreadsheet,
        get_service as get_drive_service,
    )
    from app.decision_runs.lifecycle import (approval_email_thread_id,
                                             approval_google_spreadsheet,
                                             approval_input_received,
                                             approval_input_rejected,
                                             reconcile_attended_run)
    from app.decision_runs.workbook_approval import read_workbook_approval
    from app.outreach.gmail import get_service

    if args.poll_seconds < 5:
        raise SystemExit("--poll-seconds must be at least 5")
    if args.timeout_seconds < 0:
        raise SystemExit("--timeout-seconds cannot be negative")
    gmail_service = get_service()
    drive_service = get_drive_service()
    with get_session() as session:
        expected_thread_id = approval_email_thread_id(session, args.run_id)
        spreadsheet_id, spreadsheet_url = approval_google_spreadsheet(session, args.run_id)
    deadline = time.monotonic() + args.timeout_seconds if args.timeout_seconds else None
    rejected_message_ids: set[str] = set()
    rejected_after_internal_date: int | None = None
    while True:
        try:
            reply = find_latest_approval_reply(
                gmail_service, args.run_id,
                expected_thread_id=expected_thread_id,
                excluded_message_ids=rejected_message_ids,
                after_internal_date=rejected_after_internal_date,
            )
        except ApprovalReplyNotFound:
            if not args.wait:
                raise
            if deadline is not None and time.monotonic() >= deadline:
                raise SystemExit("timed out waiting for an approval email reply")
            time.sleep(args.poll_seconds)
            continue
        approval_error = None
        safe_message_id = re.sub(r"[^A-Za-z0-9_.-]", "_", reply.gmail_message_id)
        approval_path = (
            Path(args.output_dir) / args.run_id /
            f"{safe_message_id}-google-sheet.xlsx"
        )
        try:
            export_approval_spreadsheet(drive_service, spreadsheet_id, approval_path)
            workbook_approval = read_workbook_approval(approval_path, args.run_id)
        except Exception as exc:
            approval_error = exc
            with get_session() as session:
                reconcile_attended_run(session, args.run_id)
                approval_input_received(session, args.run_id)
                approval_input_rejected(
                    session, args.run_id,
                    reason="Google Sheet approval rejected before persistence",
                )
        if approval_error is None:
            with get_session() as session:
                reconcile_attended_run(session, args.run_id)
                try:
                    scope = approve_decision_run_from_workbook(
                        session, args.run_id,
                        workbook_approval.company_approvals,
                        workbook_approval.contact_search_selections,
                        approver=reply.approver,
                    )
                except Exception as exc:
                    # The service returns rejected input to Waiting. Keep the
                    # context successful so that state is committed before
                    # polling for a corrected reply.
                    approval_error = exc
        if approval_error is None:
            break
        rejected_message_ids.add(reply.gmail_message_id)
        rejected_after_internal_date = max(
            rejected_after_internal_date or 0, reply.internal_date,
        )
        logging.getLogger("full_pipeline").warning(
            "Rejected approval Sheet at reply %s: %s; waiting for a newer corrected reply",
            reply.gmail_message_id, approval_error,
        )
        if not args.wait:
            raise approval_error
        if deadline is not None and time.monotonic() >= deadline:
            raise SystemExit("timed out waiting for a valid approval Sheet reply")
    result = {
        "run_id": scope.run_id,
        "company_ids": scope.company_ids,
        "job_ids": scope.job_ids,
        "approval_workbook": str(approval_path),
        "approval_google_sheet": spreadsheet_url,
        "gmail_message_id": reply.gmail_message_id,
        "gmail_thread_id": reply.gmail_thread_id,
    }
    if getattr(args, "resume_agent", False):
        from app.pipeline.agent_continuation import launch_full_pipeline_continuation
        result["agent_continuation"] = launch_full_pipeline_continuation(scope.run_id)
    _emit_agent_result("approval-email-reply", result, run_id=scope.run_id)


def cmd_process_approved(args: argparse.Namespace) -> None:
    """Expose the durable manifest to the agent-driven stages.

    The agent skills perform tailoring/contact judgement/copy generation; this
    command intentionally does not call their persistence CLIs.  It records
    the lifecycle transition only after exact scope validation.
    """
    from app.db.session import get_session
    from app.decision_runs import load_approved_scope
    from app.decision_runs.lifecycle import reconcile_attended_run
    from app.models.orm import DecisionRun
    with get_session() as session:
        reconcile_attended_run(session, args.run_id)
        run = session.get(DecisionRun, args.run_id)
        if run is None or run.state not in ("approved", "processing", "completed"):
            raise SystemExit("run must be approved before processing")
        scope = load_approved_scope(session, args.run_id)
        if not scope.company_ids:
            raise SystemExit("approved run has no selected scope")
        if run.state != "completed":
            run.state = "processing"
        # Resume work can start with Phase A. Contact work is frozen by
        # process-contacts only after approved company profiles are terminal.
        from app.decision_runs import (
            freeze_resume_tailoring_manifest,
            scope_company_phase_a_to_approval,
        )
        phase_a_company_ids = scope_company_phase_a_to_approval(session, args.run_id)
        freeze_resume_tailoring_manifest(session, args.run_id)
        _emit_agent_result("process-approved", {
            "run_id": scope.run_id,
            "company_ids": scope.company_ids,
            "job_ids": scope.job_ids,
            "posting_version_ids": scope.posting_version_ids,
            "phase_a_company_ids": phase_a_company_ids,
        }, run_id=scope.run_id)


def cmd_process_contacts(args: argparse.Namespace) -> None:
    """Freeze approved contact work after approved-scope Phase A completes."""
    from app.decision_runs import freeze_contact_enrichment_manifest
    from app.decision_runs.progress import StageName, StageStatus, read_run_progress
    from app.decision_runs.lifecycle import reconcile_attended_run
    terminal = {StageStatus.COMPLETED.value, StageStatus.COMPLETED_WITH_ERRORS.value}
    with get_session() as session:
        reconcile_attended_run(session, args.run_id)
        stages = {stage.name: stage.status for stage in read_run_progress(
            session, args.run_id, project_interruptions=False,
        ).stages}
        if stages.get(StageName.COMPANY_PHASE_A.value) not in terminal:
            raise SystemExit("approved-scope company Phase A must be terminal before contacts")
        manifest = freeze_contact_enrichment_manifest(session, args.run_id)
        _emit_agent_result("process-contacts", {
            "run_id": args.run_id,
            "company_ids": sorted({row.company_id for row in manifest}),
            "obligations_count": len(manifest),
        }, run_id=args.run_id)


def cmd_final_report(args: argparse.Namespace) -> None:
    from app.db.session import get_session
    from app.models.orm import DecisionRun, utcnow
    from app.decision_runs.lifecycle import reconcile_attended_run
    from app.reporting.final_run_report import build_final_workbook
    with get_session() as session:
        reconcile_attended_run(session, args.run_id)
        run = session.get(DecisionRun, args.run_id)
        if run is None or run.state not in ("approved", "processing", "completed"):
            raise SystemExit("run must be approved or processing before final report")
        if run.state == "completed": print("already_complete"); return
        if run.final_report_delivery_state in ("in_progress", "indeterminate"):
            raise SystemExit("final self-report delivery is indeterminate; reconcile its receipt before any resend")
        from app.decision_runs import begin_final_report, report_final_report
        begin_final_report(session, args.run_id)
        delivered = False
        send_started = False
        try:
            build_final_workbook(session, args.run_id, args.output)
            run.final_report_path = args.output
            run.final_report_delivery_requested = True
            already_delivered = run.final_report_delivery_state == "delivered"
            run.final_report_delivery_state = "delivered" if already_delivered else "in_progress"
            # Commit the intent before the external call. A crash after Gmail
            # accepts leaves an explicit indeterminate state, never a blind retry.
            session.commit()
            if not already_delivered:
                from app.outreach.gmail import get_service, send_self_report
                from app.decision_runs.read_models import decision_run_progress
                from app.reporting.decision_run_completion_email import (
                    render_decision_run_completion_email,
                )
                service = get_service()
                progress = decision_run_progress(session, args.run_id)
                completion_email = render_decision_run_completion_email(
                    run, progress, project_delivery_success=True,
                )
                send_started = True
                receipt = send_self_report(
                    service,
                    subject=completion_email.subject,
                    body=completion_email.plain_text,
                    attachment_path=args.output,
                    html_body=completion_email.html,
                )
                delivered = True
                run.final_report_delivery_receipt = str(receipt.get("id") or receipt.get("message_id") or "provider_accepted")
                run.final_report_delivery_state = "delivered"
                session.commit()  # receipt is durable before stage completion
            report_final_report(session, args.run_id, output=args.output,
                                self_report_requested=True, self_report_delivered=delivered or already_delivered)
            session.commit()
        except Exception as exc:
            from app.decision_runs.progress import fail_stage, StageName
            # A workbook/build/configuration failure precedes any outbound
            # call and is safely failed. Once the send call has started, an
            # exception cannot prove Gmail did not accept it: require receipt
            # reconciliation and refuse automatic resend.
            if run.final_report_delivery_state != "delivered":
                run.final_report_delivery_state = "indeterminate" if send_started else "failed"
            fail_stage(session, args.run_id, StageName.FINAL_REPORT,
                       reason=("self_report_send_indeterminate:" if send_started else "final_report_pre_delivery_failed:") + str(exc))
            session.commit()
            raise
        run.state, run.completed_at = "completed", utcnow()
        print(args.output)


def cmd_reconcile_final_report(args: argparse.Namespace) -> None:
    from app.db.session import get_session
    from app.decision_runs.outreach_funnel import reconcile_final_report_delivery
    with get_session() as session:
        reconcile_final_report_delivery(session, args.run_id, receipt=args.receipt,
                                        confirmed_not_delivered=args.confirmed_not_delivered)
        session.commit()


def cmd_skills_dedup(args: argparse.Namespace) -> None:
    """Stage 6, run after the skill's own enrichment pass (stage 5) — skill
    rows only exist once enrichment has created them."""
    run_dir = _resume_run_dir(args)
    _setup_logging(run_dir)
    logger = logging.getLogger("full_pipeline")
    meta = _load_meta(run_dir)

    try:
        stats = run_skills_dedup_script(apply=True)
        meta["skills_dedup_stats"] = stats
        _record_stage(meta, "skills_dedup", "ok", f"groups={stats.get('groups')} renamed={stats.get('renamed')}")
    except Exception as exc:
        logger.error("Skills dedup failed: %s", exc)
        _record_result(run_dir, meta, "skills_dedup", "failed", str(exc))
        raise

    _save_meta(run_dir, meta)


def cmd_record_stage(args: argparse.Namespace) -> None:
    """Lets the skill log its own agent-driven stages (enrich, tailor,
    find-contacts, draft+push) into this run's stage_status, so the
    workbook's Run summary sheet reports all ten stages, not just the six
    this script drives directly."""
    run_dir = _resume_run_dir(args)
    meta = _load_meta(run_dir)
    _record_result(run_dir, meta, args.stage, args.status, args.detail)
    print(f"Recorded: {args.stage} = {args.status} ({args.detail})")
    if args.status == "failed":
        raise SystemExit(1)


def _build_and_send_report(run_dir: Path, meta: dict) -> Path:
    from app.db.session import get_session
    from app.reporting.workbook import build_workbook

    run_meta = {
        "since": datetime.fromisoformat(meta["since"]),
        "stage_status": meta.get("stage_status", []),
        "dedup_stats": meta.get("dedup_stats"),
        "skills_dedup_stats": meta.get("skills_dedup_stats"),
    }

    out_path = run_dir / "run_workbook.xlsx"
    with get_session() as session:
        build_workbook(session, run_meta, out_path)

    from app.outreach.gmail import get_service, send_self_report
    from app.pipeline.completion_email import render_completion_email
    service = get_service()
    connector_snapshot = meta.get("connector_progress")
    connector_report = render_completion_email(connector_snapshot) if connector_snapshot else None
    body = f"Run report attached. Window: since {meta['since']}.\n\nSee attached workbook for contacts, jobs, and dedup summary."
    html_body = "<p>Run report attached. See the workbook for contacts, jobs, and dedup summary.</p>"
    if connector_report:
        body += f"\n\n{connector_report.plain_text}"
        html_body += connector_report.html
    send_self_report(
        service,
        subject=f"job_agent full-pipeline run — {meta['run_date']}",
        body=body,
        attachment_path=str(out_path),
        html_body=html_body,
    )
    return out_path


def cmd_report(args: argparse.Namespace) -> None:
    """Stage 10: build the workbook, self-send, and emit terminal status."""
    run_dir = _resume_run_dir(args)
    _setup_logging(run_dir)
    logger = logging.getLogger("full_pipeline")
    meta = _load_meta(run_dir)

    try:
        out_path = _build_and_send_report(run_dir, meta)
    except Exception as exc:
        logger.error("Report/self-send failed: %s", exc)
        _record_result(run_dir, meta, "report_self_send", "failed", str(exc))
        raise

    logger.info("Workbook written and emailed to self: %s", out_path)
    _record_stage(meta, "report_self_send", "ok", f"sent to self, workbook={out_path}")
    meta["outcome"] = "failed" if _has_unresolved_failures(meta) else "completed"
    _save_meta(run_dir, meta)

    if meta["outcome"] == "completed":
        delivered = send_pipeline_notification(
            title="job_agent pipeline completed",
            message=f"Workbook emailed for run {meta['run_date']}.",
            tags=["white_check_mark"],
            priority="default",
        )
        meta.setdefault("notification_attempts", []).append(
            {"stage": "report_self_send", "kind": "completion", "delivered": delivered}
        )
        _save_meta(run_dir, meta)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Create an explicit posted-at decision window.")
    p_start.add_argument("--since", required=True, help="ISO-8601; naive input is Asia/Kolkata.")
    p_start.add_argument(
        "--search",
        help=("Choose one role instance per active source, e.g. 'data scientist' "
              "or 'machine learning engineer'. Requires --location."),
    )
    p_start.add_argument(
        "--location",
        help="Choose the matching location instance across sources: remote or Bengaluru. Requires --search.",
    )
    p_start.set_defaults(func=cmd_start)
    p_prepare = sub.add_parser("prepare-decision", help="Freeze the ranked decision snapshot.")
    p_prepare.add_argument("--run-id", required=True)
    p_prepare.add_argument("--output", help="Optional decision workbook path; report delivery remains self-only.")
    p_prepare.add_argument("--send-self-report", action="store_true", help="Send the decision workbook only to configured SELF_EMAIL.")
    p_prepare.set_defaults(func=cmd_prepare_decision)
    p_screen = sub.add_parser("screen-decision", help="Freeze eligible jobs and the exact company enrichment manifest.")
    p_screen.add_argument("--run-id", required=True)
    p_screen.set_defaults(func=cmd_screen_decision)
    p_begin_phase_b = sub.add_parser(
        "begin-company-phase-b",
        help="Mark company Phase B running immediately before source workers dispatch.",
    )
    p_begin_phase_b.add_argument("--run-id", required=True)
    p_begin_phase_b.set_defaults(func=cmd_begin_company_phase_b)
    p_screen_input = sub.add_parser(
        "screening-input",
        help="Write compact salary/experience evidence for Decision Run screening.",
    )
    p_screen_input.add_argument("--run-id", required=True)
    p_screen_input.set_defaults(func=cmd_screening_input)
    p_finalize = sub.add_parser("finalize-decision", help="Rank after terminal company Phase B market evidence.")
    p_finalize.add_argument("--run-id", required=True)
    p_finalize.set_defaults(func=cmd_finalize_decision)
    p_approve = sub.add_parser("approve", help="Persist exact group-cutoff approval.")
    p_approve.add_argument("--run-id", required=True)
    p_approve.add_argument("--selection", required=True)
    p_approve.add_argument("--workbook", help="Optional edited decision workbook with company contact-search selections.")
    p_approve.set_defaults(func=cmd_approve)
    p_approve_email = sub.add_parser(
        "approve-email-reply",
        help="Approve exact companies from the recorded Google Sheet after an email reply.",
    )
    p_approve_email.add_argument("--run-id", required=True)
    p_approve_email.add_argument(
        "--output-dir", default="outputs/approval-replies",
        help="Directory where the reviewed Google Sheet export is retained.",
    )
    p_approve_email.add_argument("--wait", action="store_true", help="Poll until a qualifying reply arrives.")
    p_approve_email.add_argument("--poll-seconds", type=int, default=30)
    p_approve_email.add_argument("--timeout-seconds", type=int, default=0, help="Zero waits indefinitely.")
    p_approve_email.add_argument(
        "--resume-agent", action="store_true",
        help="Launch a detached Codex full-pipeline continuation after approval succeeds.",
    )
    p_approve_email.set_defaults(func=cmd_approve_email_reply)
    p_process = sub.add_parser("process-approved", help="Validate and expose immutable approved scope to agent stages.")
    p_process.add_argument("--run-id", required=True)
    p_process.set_defaults(func=cmd_process_approved)
    p_contacts = sub.add_parser(
        "process-contacts",
        help="Freeze approved contact work after company Phase A completes.",
    )
    p_contacts.add_argument("--run-id", required=True)
    p_contacts.set_defaults(func=cmd_process_contacts)
    p_final = sub.add_parser("final-report", help="Render final report strictly from run snapshots and ledger.")
    p_final.add_argument("--run-id", required=True)
    p_final.add_argument("--output", required=True)
    p_final.set_defaults(func=cmd_final_report)
    p_final_reconcile = sub.add_parser("reconcile-final-report", help="Operator reconciliation for an indeterminate self report")
    p_final_reconcile.add_argument("--run-id", required=True); p_final_reconcile.add_argument("--receipt")
    p_final_reconcile.add_argument("--confirmed-not-delivered", action="store_true")
    p_final_reconcile.set_defaults(func=cmd_reconcile_final_report)

    p_ingest = sub.add_parser("ingest", help="Stages 1-4: fetch -> gate/upsert -> job dedup -> company dedup.")
    p_ingest.add_argument("--run-id", required=True, help="Decision Run created by `start`; owns the immutable ingest window.")
    p_ingest.add_argument("--headless-only", action="store_true", help="Skip the headed/anti-bot tier.")
    p_ingest.add_argument("--source", help="Run only one active source, e.g. linkedin.")
    p_ingest.add_argument(
        "--location-mode",
        help="Run only connector instances with this exact location mode.",
    )
    p_ingest.add_argument(
        "--one-per-source", action="store_true",
        help="Choose one representative connector instance for the selected source.",
    )
    p_ingest.add_argument(
        "--sample-search", default="data scientist",
        help="Preferred search term for --one-per-source.",
    )
    p_ingest.add_argument(
        "--sample-location",
        help="Preferred location mode for --one-per-source, e.g. remote_india.",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_linkedin_location = sub.add_parser(
        "linkedin-location-input",
        help="Fetch LinkedIn and freeze full descriptions for agent location enrichment.",
    )
    p_linkedin_location.add_argument("--run-id", required=True)
    p_linkedin_location.add_argument(
        "--location-mode",
        help="Freeze all LinkedIn instances with this exact location mode.",
    )
    p_linkedin_location.add_argument(
        "--one-instance", action="store_true",
        help="Use one representative LinkedIn instance for a focused run.",
    )
    p_linkedin_location.add_argument("--sample-search", default="data scientist")
    p_linkedin_location.add_argument("--sample-location", default="remote_india")
    p_linkedin_location.set_defaults(func=cmd_linkedin_location_input)

    p_skills = sub.add_parser("skills-dedup", help="Stage 6: skills casing dedup. Run after enrichment.")
    p_skills.add_argument("--run-date", help="Resume an existing run under logs/daily_runs/YYYY-MM-DD.")
    p_skills.set_defaults(func=cmd_skills_dedup)

    p_record = sub.add_parser("record-stage", help="Log an agent-driven stage into this run's status.")
    p_record.add_argument("--stage", required=True)
    p_record.add_argument("--status", required=True, choices=["ok", "failed", "skipped"])
    p_record.add_argument("--detail", required=True)
    p_record.add_argument("--run-date", help="Resume an existing run under logs/daily_runs/YYYY-MM-DD.")
    p_record.set_defaults(func=cmd_record_stage)

    p_report = sub.add_parser("report", help="Stage 10: build the workbook and self-send it.")
    p_report.add_argument("--run-date", help="Resume an existing run under logs/daily_runs/YYYY-MM-DD.")
    p_report.set_defaults(func=cmd_report)

    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    args.func(args)
