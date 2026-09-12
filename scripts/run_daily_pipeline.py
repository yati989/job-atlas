"""
Daily end-to-end ingestion entrypoint (2026-07-26): fetch -> gate -> upsert,
in one command, with a dated log + report saved under
logs/daily_runs/<date>/ so each day's run is independently inspectable
later — per-source fetch/kept counts, connector errors, and
platform-specific challenges (network failure, bot-detection challenge page,
rate-limiting) called out explicitly rather than looking identical to a
genuinely empty result in the raw log.

This script covers fetch/gate/upsert only. Enrichment is NOT scripted here
— per this project's convention (see CLAUDE.md's "Enrichment" section),
enrichment is done by the agent's own reasoning over description_raw, not
a billed API call, so it can't be driven end-to-end by a plain script. The
`daily-pipeline` skill (.claude/skills/daily-pipeline/SKILL.md) runs this
script for the ingestion half, then does the enrichment reasoning pass
itself and appends those results to the same dated report.

Usage:
    python -m scripts.run_daily_pipeline                  # both tiers (attended)
    python -m scripts.run_daily_pipeline --headless-only   # unattended-safe
"""
import argparse
import json
import logging
import re
import subprocess
import time
from dataclasses import asdict
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path

from app.config.settings import RECENCY_WINDOW_DAYS
from app.pipeline.registry import ACTIVE_CONNECTORS
from app.pipeline.progress import ConnectorProgress
from app.pipeline.completion_email import send_completion_email
from app.pipeline.window import apply_sync_window, parse_since
from app.pipeline.workload import enforce_workload_bounds
from app.pipeline.runner import (
    IncrementalRunPersister,
    fetch_all,
    gate_and_upsert,
    is_browser_connector,
    log_summary,
    retry_failed,
    sanitize_log_text,
)
from scripts.dedup_jobs import run as run_dedup

LOG_ROOT = Path(__file__).resolve().parent.parent / "logs" / "daily_runs"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Ordered so a message matching an earlier, more specific pattern (e.g. a
# 429 that also mentions "RetryError") gets the more useful label.
CHALLENGE_PATTERNS = [
    (re.compile(r"datadome|captcha|cloudflare|turnstile|attention required|just a moment", re.I), "bot_challenge"),
    (re.compile(r"suspiciously short page body", re.I), "empty_response"),
    (re.compile(r"429|too many requests", re.I), "rate_limited"),
    (re.compile(r"RetryError", re.I), "retry_exhausted"),
    (re.compile(r"ERR_TIMED_OUT|ERR_HTTP2|ERR_CONNECTION|ReadTimeout", re.I), "network_block"),
    (re.compile(r"not JSON serializable", re.I), "upsert_bug"),
]


class ChallengeCollector(logging.Handler):
    """Collects WARNING+ records during the run and classifies them against
    CHALLENGE_PATTERNS, so the daily report can show *why* a source
    underperformed instead of just a bare kept-count."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        for pattern, label in CHALLENGE_PATTERNS:
            if pattern.search(msg):
                self.records.append((label, msg))
                return
        if record.levelno >= logging.ERROR:
            self.records.append(("error", msg))


class SensitiveDataFilter(logging.Filter):
    """Redact secret-shaped text before any daily-run handler persists it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_log_text(record.getMessage())
        record.args = ()
        return True


def _setup_logging(run_dir: Path) -> ChallengeCollector:
    run_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.FileHandler(run_dir / "pipeline.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(SensitiveDataFilter())
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(SensitiveDataFilter())
    root.addHandler(stream_handler)

    collector = ChallengeCollector()
    collector.addFilter(SensitiveDataFilter())
    root.addHandler(collector)
    return collector


def _pct(count: int, total: int) -> float:
    return round(100 * count / total, 1) if total else 0.0


def _source_status(source) -> str:
    if source.errored:
        return "ERRORED"
    if source.instances_failed:
        return "partial-error"
    if source.kept == 0:
        return "zero-yield"
    return "ok"


def _source_payload(source) -> dict:
    unique = source.unique_count
    gate_drops = {
        axis: {"count": count, "pct": _pct(count, unique)}
        for axis, count in source.drop_counts.items()
    }
    date_warning = None
    unknown = source.date_quality.get("unknown", 0)
    if unique and unknown == unique:
        date_warning = (
            f"all {unique} posting dates are unknown; posting dates are not a relevance gate"
        )
    elif unknown:
        date_warning = (
            f"{unknown}/{unique} posting dates are unknown; recency is unverified "
            "for those rows"
        )

    return {
        "source": source.source,
        "status": _source_status(source),
        "instances_started": source.instances_started,
        "instances_succeeded": source.instances_succeeded,
        "instances_failed": source.instances_failed,
        "instances_retried": source.instances_retried,
        "raw_count": source.raw_count,
        "unique_count": unique,
        "duplicate_count": source.duplicate_count,
        "duplicate_rate_pct": source.duplicate_rate_pct,
        "kept_count": source.kept,
        "relevance_pct": _pct(source.kept, unique),
        "gate_drops": gate_drops,
        "location_drops": {
            "foreign": source.location_foreign,
            "no_signal": source.location_no_signal,
        },
        "source_wall_time_s": round(source.source_wall_time_s, 3),
        "cumulative_worker_time_s": round(source.fetch_time_s, 3),
        "date_quality": source.date_quality,
        "date_warning": date_warning,
        "field_completeness": source.field_completeness,
        "upsert_calls_completed": source.upsert_calls_completed,
        "upsert_errors": source.upsert_errors,
        "insert_update_unchanged_split_available": False,
    }


def _attempt_payload(attempt) -> dict:
    payload = asdict(attempt)
    payload["started_at"] = attempt.started_at.isoformat()
    payload["ended_at"] = attempt.ended_at.isoformat()
    payload["duration_s"] = round(attempt.duration_s, 3)
    return payload


def _instance_payload(result, instance_number: int) -> dict:
    attempts = [_attempt_payload(attempt) for attempt in result.attempt_details]
    final_attempt = attempts[-1] if attempts else None
    final_error = final_attempt.get("error") if final_attempt else (
        sanitize_log_text(result.error) if result.error is not None else None
    )
    fetched_count = final_attempt.get("fetched_count", 0) if final_attempt else len(result.jobs or [])
    return {
        "instance_id": f"{result.source}-{instance_number:03d}",
        "source": result.source,
        "connector_class": result.connector_class or type(result.connector).__name__,
        "dimensions": result.dimensions,
        "started_at": result.started_at.isoformat() if result.started_at else None,
        "ended_at": result.ended_at.isoformat() if result.ended_at else None,
        "cumulative_worker_time_s": round(result.elapsed_s, 3),
        "attempt_count": result.attempts,
        "retried": result.retried,
        "retry_succeeded": result.retry_succeeded,
        "fetched_count": fetched_count,
        "status": "error" if result.error is not None else "succeeded",
        "error": final_error,
        "attempts": attempts,
    }


def _markdown_text(value: object) -> str:
    return sanitize_log_text(value).replace("|", "\\|").replace("\n", " ")


def _format_dimensions(dimensions: dict[str, object]) -> str:
    if not dimensions:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in dimensions.items())


def _render_detailed_report(payload: dict) -> str:
    run = payload["run"]
    timings = payload["phase_timings_s"]
    lines = [
        f"# Pipeline run — {run['run_date']}",
        "",
        "## Run configuration",
        "",
        f"- Commit: `{run['commit']}`",
        f"- Configured collection window: {run['recency_window_days']} days",
        f"- Effective native collection window: {run.get('native_recency_days', run['recency_window_days'])} days",
        f"- Exact cutoff: {run['cutoff_at']}",
        f"- Started: {run['started_at']}",
        f"- Ended: {run.get('ended_at') or '-'}",
        f"- Active sources / instances: {run['active_sources']} / {run['active_instances']}",
        f"- Tier sizes: parallel={run['tier_sizes']['parallel']}, browser={run['tier_sizes']['browser']}",
        f"- Command options: `{json.dumps(run['options'], sort_keys=True)}`",
        f"- Upsert calls completed without exception: {payload['totals']['upsert_calls_completed']}",
        f"- Errors: {payload['totals']['errors']}",
        "",
        "## Phase timings",
        "",
        f"- Parallel fetch wall: {timings.get('parallel_fetch_wall_s', 0.0):.1f}s",
        f"- Browser fetch wall: {timings.get('browser_fetch_wall_s', 0.0):.1f}s",
        f"- Retry pass: {timings.get('retry_pass_wall_s', 0.0):.1f}s",
        f"- Gate and upsert: {timings.get('gate_upsert_wall_s', 0.0):.1f}s",
        f"- Cross-source dedup: {timings.get('dedup_wall_s', 0.0):.1f}s",
        f"- Report generation: {timings.get('report_generation_wall_s', 0.0):.1f}s",
        f"- Total wall: {timings.get('total_wall_s', 0.0):.1f}s",
        "",
        "## Per-source yield",
        "",
        "| Source | Instances ok/started | Raw | Unique | Duplicates | Kept | Relevance | Source wall | Cumulative worker | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for source in payload["sources"]:
        lines.append(
            f"| {source['source']} | {source['instances_succeeded']}/{source['instances_started']} | "
            f"{source['raw_count']} | {source['unique_count']} | "
            f"{source['duplicate_count']} ({source['duplicate_rate_pct']:.1f}%) | "
            f"{source['kept_count']} | {source['relevance_pct']:.1f}% | "
            f"{source['source_wall_time_s']:.1f}s | "
            f"{source['cumulative_worker_time_s']:.1f}s | {source['status']} |"
        )

    lines += ["", "## Source metrics", ""]
    for source in payload["sources"]:
        unique = source["unique_count"]
        gate = source["gate_drops"]
        date = source["date_quality"]
        completeness = ", ".join(
            f"{name} {metric['count']}/{unique} ({metric['pct']:.1f}%)"
            for name, metric in source["field_completeness"].items()
        )
        lines += [
            f"### {source['source']}",
            "",
            f"- Instances: started {source['instances_started']}; succeeded {source['instances_succeeded']}; "
            f"failed {source['instances_failed']}; retried {source['instances_retried']}",
            f"- Fetch timing: Source wall {source['source_wall_time_s']:.1f}s; "
            f"cumulative worker {source['cumulative_worker_time_s']:.1f}s",
            f"- Inventory: raw {source['raw_count']}; unique {unique}; duplicates "
            f"{source['duplicate_count']} ({source['duplicate_rate_pct']:.1f}%); kept "
            f"{source['kept_count']} ({source['relevance_pct']:.1f}%)",
            "- Gate drops: " + "; ".join(
                f"{axis} {metric['count']}/{unique} ({metric['pct']:.1f}%)"
                for axis, metric in gate.items()
            ),
            f"- Location drop detail: foreign {source['location_drops']['foreign']}; "
            f"no-signal {source['location_drops']['no_signal']}",
            f"- Date quality: known {date.get('known', 0)}; unknown {date.get('unknown', 0)}; "
            f"within cutoff {date.get('within_cutoff', 0)}; stale {date.get('stale', 0)}; "
            f"kept within cutoff {date.get('kept_within_cutoff', 0)}; "
            f"kept unknown {date.get('kept_unknown', 0)}",
            f"- Field completeness: {completeness}",
            f"- Database seam: upsert calls completed {source['upsert_calls_completed']}; "
            f"upsert errors {source['upsert_errors']}; inserted/updated/unchanged split unavailable",
        ]
        if source["date_warning"]:
            lines.append(f"- Date warning: **{source['date_warning']}**")
        lines.append("")

    lines += [
        "## Per-instance telemetry",
        "",
        "| Instance | Connector | Dimensions | Start | End | Worker time | Attempts | Retried | Fetched | Status | Error |",
        "|---|---|---|---|---|---:|---:|---|---:|---|---|",
    ]
    for instance in payload["instances"]:
        dimensions = _format_dimensions(instance["dimensions"])
        lines.append(
            f"| {instance['instance_id']} | {instance['connector_class']} | "
            f"`{_markdown_text(dimensions)}` | {instance['started_at'] or '-'} | "
            f"{instance['ended_at'] or '-'} | {instance['cumulative_worker_time_s']:.1f}s | "
            f"{instance['attempt_count']} | {'yes' if instance['retried'] else 'no'} | "
            f"{instance['fetched_count']} | {instance['status']} | "
            f"{_markdown_text(instance['error']) if instance['error'] else '-'} |"
        )

    lines += ["", "## Challenges detected", ""]
    if payload["challenges"]:
        for challenge in payload["challenges"]:
            lines.append(
                f"- **{_markdown_text(challenge['label'])}**: "
                f"{_markdown_text(challenge['message'])}"
            )
    else:
        lines.append("None detected.")

    dedup = payload["cross_source_dedup"]
    lines += [
        "",
        "## Duplicate detection (cross-source)",
        "",
        f"- Active jobs scanned: {dedup.get('scanned', '-')}",
        f"- Auto-flagged as duplicates this run: {dedup.get('flagged', '-')}",
        f"- Review-only candidates: {dedup.get('review_candidates', '-')}"
        + (f" — see `{dedup['review_path']}`" if dedup.get("review_path") else ""),
        "",
        "## Enrichment",
        "",
        "_Not run: this attended run is explicitly scoped to ingestion only._",
    ]
    return "\n".join(lines) + "\n"


def write_run_artifacts(
    *,
    run_dir: Path,
    metadata: dict,
    summary,
    results,
    total_upsert_calls_completed: int,
    total_errors: int,
    challenges: list[tuple[str, str]],
    phase_timings: dict[str, float],
    dedup_stats: dict | None = None,
) -> tuple[Path, Path]:
    """Write the human and machine-readable public artifacts for one run."""
    run_dir.mkdir(parents=True, exist_ok=True)
    instance_counts: dict[str, int] = {}
    instances = []
    for result in results:
        instance_counts[result.source] = instance_counts.get(result.source, 0) + 1
        instances.append(_instance_payload(result, instance_counts[result.source]))

    payload = {
        "schema_version": 1,
        "run": metadata,
        "totals": {
            "upsert_calls_completed": total_upsert_calls_completed,
            "errors": total_errors,
            "insert_update_unchanged_split_available": False,
        },
        "phase_timings_s": {
            key: round(value, 3) for key, value in phase_timings.items()
        },
        "sources": [_source_payload(source) for source in summary],
        "instances": instances,
        "challenges": [
            {"label": label, "message": sanitize_log_text(message)}
            for label, message in challenges
        ],
        "cross_source_dedup": dedup_stats or {},
    }
    report_path = run_dir / "report.md"
    metrics_path = run_dir / "source_metrics.json"
    report_path.write_text(_render_detailed_report(payload), encoding="utf-8")
    metrics_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report_path, metrics_path


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def run(headless_only: bool = False, since: str | None = None) -> Path:
    run_date = date_cls.today().isoformat()
    run_dir = LOG_ROOT / run_date
    collector = _setup_logging(run_dir)
    logger = logging.getLogger("daily_pipeline")

    total_start = time.monotonic()
    started_at = datetime.now(timezone.utc)
    cutoff_at = (
        parse_since(since)
        if since
        else started_at - timedelta(days=RECENCY_WINDOW_DAYS)
    )
    phase_timings: dict[str, float] = {}

    connectors = list(ACTIVE_CONNECTORS)
    if headless_only:
        logger.info("--headless-only: skipping the headed tier (needs a real display)")
    else:
        # Deferred import: constructing headed connectors launches real
        # browser sessions for some, so this cost is skipped entirely for
        # --headless-only runs.
        from scripts.run_headed_sources import CONNECTORS as HEADED_CONNECTORS

        connectors = connectors + list(HEADED_CONNECTORS)
    parallel_instances = sum(1 for connector in connectors if not is_browser_connector(connector))
    browser_instances = len(connectors) - parallel_instances
    metadata = {
        "run_date": run_date,
        "commit": _git_commit(),
        "recency_window_days": RECENCY_WINDOW_DAYS,
        "cutoff_at": cutoff_at.isoformat(),
        "started_at": started_at.isoformat(),
        "ended_at": None,
        "active_sources": len({connector.source_name for connector in connectors}),
        "active_instances": len(connectors),
        "tier_sizes": {
            "parallel": parallel_instances,
            "browser": browser_instances,
        },
        "options": {"headless_only": headless_only},
    }
    logger.info(
        "Run header: commit=%s window_days=%s cutoff=%s start=%s "
        "active_sources=%s active_instances=%s parallel_instances=%s "
        "browser_instances=%s options=%s",
        metadata["commit"],
        RECENCY_WINDOW_DAYS,
        metadata["cutoff_at"],
        metadata["started_at"],
        metadata["active_sources"],
        metadata["active_instances"],
        parallel_instances,
        browser_instances,
        json.dumps(metadata["options"], sort_keys=True),
    )

    progress = ConnectorProgress(
        connectors,
        run_id=started_at.strftime("%Y%m%dT%H%M%SZ"),
        command="scripts.run_daily_pipeline",
    )
    window = apply_sync_window(
        connectors, since_at=cutoff_at, cutoff_at=started_at
    )
    enforce_workload_bounds(connectors)
    metadata["native_recency_days"] = window.native_days
    metadata["sync_window_since"] = window.since_at.isoformat()
    logger.info(
        "Sync window native prefilter: %s day(s), exact cutoff=%s",
        window.native_days,
        window.since_at.isoformat(),
    )
    persister = IncrementalRunPersister(cutoff_at=cutoff_at)
    results = fetch_all(
        connectors,
        phase_timings=phase_timings,
        progress=progress,
        on_result=persister.persist,
    )
    retry_failed(results, phase_timings=phase_timings, progress=progress)
    persister.persist_remaining(results)

    gate_start = time.monotonic()
    summary, total_upserted, total_errors = gate_and_upsert(
        results,
        cutoff_at=cutoff_at,
    )
    progress.complete(summary)
    phase_timings["gate_upsert_wall_s"] = time.monotonic() - gate_start

    logger.info("Running cross-source duplicate detection")
    dedup_start = time.monotonic()
    dedup_stats = run_dedup(dry_run=False)
    phase_timings["dedup_wall_s"] = time.monotonic() - dedup_start

    logger.info(
        "Pipeline collection complete. Upsert_calls_completed=%s Errors=%s",
        total_upserted,
        total_errors,
    )
    log_summary(summary)

    artifact_start = time.monotonic()
    phase_timings["report_generation_wall_s"] = 0.0
    phase_timings["total_wall_s"] = time.monotonic() - total_start
    metadata["ended_at"] = datetime.now(timezone.utc).isoformat()
    report_path, metrics_path = write_run_artifacts(
        run_dir=run_dir,
        metadata=metadata,
        summary=summary,
        results=results,
        total_upsert_calls_completed=total_upserted,
        total_errors=total_errors,
        challenges=collector.records,
        phase_timings=phase_timings,
        dedup_stats=dedup_stats,
    )
    phase_timings["report_generation_wall_s"] = time.monotonic() - artifact_start
    phase_timings["total_wall_s"] = time.monotonic() - total_start
    metadata["ended_at"] = datetime.now(timezone.utc).isoformat()
    report_path, metrics_path = write_run_artifacts(
        run_dir=run_dir,
        metadata=metadata,
        summary=summary,
        results=results,
        total_upsert_calls_completed=total_upserted,
        total_errors=total_errors,
        challenges=collector.records,
        phase_timings=phase_timings,
        dedup_stats=dedup_stats,
    )
    logger.info(
        "Pipeline run complete at %s. Total_wall=%.3fs report=%s metrics=%s",
        metadata["ended_at"],
        phase_timings["total_wall_s"],
        report_path,
        metrics_path,
    )
    if send_completion_email(progress.snapshot):
        logger.info("Connector completion email sent to self")
    return report_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headless-only",
        action="store_true",
        help="Skip the headed/anti-bot tier (unattended-safe, no display required).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Exact ISO-8601 posting-time lower bound for collection and the central gate.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(headless_only=args.headless_only, since=args.since)
