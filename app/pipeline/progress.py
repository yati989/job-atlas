"""Atomic live and historical progress snapshots for connector runs.

The runner publishes through :class:`ConnectorProgress`; the dashboard reads
through :func:`load_progress_history` and :func:`source_rows`. JSON is
deliberately used as the seam so the UI remains useful while the pipeline owns
database connections. ``latest.json`` remains the compatibility/live
projection, while one atomic file per run preserves completed and interrupted
runs for dashboard selection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.process_status import process_id_is_alive


DEFAULT_PROGRESS_PATH = (
    Path(__file__).resolve().parents[2] / "logs" / "pipeline_progress" / "latest.json"
)
DEFAULT_PROGRESS_HISTORY_DIR = DEFAULT_PROGRESS_PATH.parent / "runs"
_SAFE_DIMENSION_NAMES = ("search", "search_terms", "location_mode", "location", "country")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dimensions(connector: object) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in _SAFE_DIMENSION_NAMES:
        value = getattr(connector, name, None)
        if isinstance(value, (str, int, float, bool)):
            result[name] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, int, float, bool)) for item in value
        ):
            result[name] = list(value)
    return result


def _attach_gate_summary(snapshot: dict, summaries: list[object]) -> None:
    """Mutate a snapshot with source-level relevance-gate outcomes."""
    by_source = {str(summary.source): summary for summary in summaries}
    for source in snapshot.get("sources", []):
        summary = by_source.get(source.get("source"))
        if summary is None:
            continue
        source.update(
            gate_fetched=int(summary.fetched),
            kept=int(summary.kept),
            drop_role=int(summary.drop_counts.get("role", 0)),
            drop_seniority=int(summary.drop_counts.get("seniority", 0)),
            drop_location=int(summary.drop_counts.get("location", 0)),
            drop_recency=int(summary.drop_counts.get("recency", 0)),
        )


def _write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _history_path(history_dir: Path, run_id: str) -> Path:
    safe_run_id = re.sub(r"[^A-Za-z0-9._-]+", "_", run_id).strip("._-")
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10]
    return history_dir / f"{(safe_run_id or 'run')[:100]}-{digest}.json"


def _default_history_dir(path: Path) -> Path | None:
    return DEFAULT_PROGRESS_HISTORY_DIR if path == DEFAULT_PROGRESS_PATH else None


def _write_progress_snapshot(
    path: Path,
    snapshot: dict,
    *,
    history_dir: Path | None,
) -> None:
    _write_snapshot(path, snapshot)
    if history_dir is not None:
        _write_snapshot(
            _history_path(history_dir, str(snapshot.get("run_id") or "run")),
            snapshot,
        )


class ConnectorProgress:
    """Publish one atomic progress document for a connector collection run."""

    def __init__(
        self,
        connectors: list,
        *,
        path: Path = DEFAULT_PROGRESS_PATH,
        run_id: str | None = None,
        command: str | None = None,
        history_dir: Path | None = None,
        now: Callable[[], datetime] = _utc_now,
        process_id: int | None = None,
    ) -> None:
        self.path = path
        self.history_dir = history_dir or _default_history_dir(path)
        self._now = now
        self._lock = threading.RLock()
        self._instance_keys: dict[int, str] = {}
        started_at = now()
        resolved_run_id = run_id or started_at.strftime("%Y%m%dT%H%M%SZ")
        existing_latest = load_progress(path)
        if (
            self.history_dir is not None
            and existing_latest is not None
            and str(existing_latest.get("run_id") or "") != resolved_run_id
        ):
            _write_snapshot(
                _history_path(
                    self.history_dir,
                    str(existing_latest.get("run_id") or "legacy-run"),
                ),
                existing_latest,
            )
        source_counts: dict[str, int] = {}
        instances: list[dict] = []
        for connector in connectors:
            source = str(connector.source_name)
            source_counts[source] = source_counts.get(source, 0) + 1
            key = f"{source}-{source_counts[source]:03d}"
            self._instance_keys[id(connector)] = key
            instances.append(
                {
                    "id": key,
                    "source": source,
                    "connector_class": type(connector).__name__,
                    "dimensions": _dimensions(connector),
                    "status": "pending",
                    "attempt": 0,
                    "jobs_finished": 0,
                    "started_at": None,
                    "ended_at": None,
                    "error_type": None,
                }
            )
        self.snapshot: dict = {
            "schema_version": 1,
            "run_id": resolved_run_id,
            "command": command,
            "process_id": process_id if process_id is not None else os.getpid(),
            "status": "running",
            "started_at": _iso(started_at),
            "updated_at": _iso(started_at),
            "ended_at": None,
            "sources": [],
            "instances": instances,
        }
        self._rebuild_sources()
        self._write()

    def _instance(self, connector: object) -> dict:
        key = self._instance_keys[id(connector)]
        return next(item for item in self.snapshot["instances"] if item["id"] == key)

    def instance_started(self, connector: object, *, attempt: int) -> None:
        with self._lock:
            timestamp = self._now()
            instance = self._instance(connector)
            if instance["started_at"] is None:
                instance["started_at"] = _iso(timestamp)
            instance.update(
                status="running",
                attempt=attempt,
                ended_at=None,
                error_type=None,
            )
            self.snapshot["status"] = "running"
            self.snapshot["ended_at"] = None
            self.snapshot["updated_at"] = _iso(timestamp)
            self._rebuild_sources()
            self._write()

    def instance_finished(self, result: object) -> None:
        with self._lock:
            timestamp = self._now()
            instance = self._instance(result.connector)
            jobs = result.jobs or []
            instance.update(
                status="failed" if result.error is not None else "completed",
                jobs_finished=len(jobs),
                ended_at=_iso(timestamp),
                error_type=(type(result.error).__name__ if result.error is not None else None),
            )
            self.snapshot["updated_at"] = _iso(timestamp)
            self._rebuild_sources()
            self._write()

    def finish(self) -> None:
        with self._lock:
            timestamp = self._now()
            failed = sum(
                item["status"] == "failed" for item in self.snapshot["instances"]
            )
            self.snapshot["status"] = "completed" if failed == 0 else "partial"
            self.snapshot["ended_at"] = _iso(timestamp)
            self.snapshot["updated_at"] = _iso(timestamp)
            self._rebuild_sources()
            self._write()

    def complete(self, summaries: list[object]) -> None:
        """Finish after gating, hydration, and upserts have completed.

        Connector instances finish when collection ends, but that is not the
        end of the pipeline.  The terminal run and source timestamps belong
        here so dashboard elapsed time includes post-collection work.
        """
        with self._lock:
            timestamp = self._now()
            failed = sum(
                item["status"] == "failed" for item in self.snapshot["instances"]
            )
            self.snapshot["status"] = "completed" if failed == 0 else "partial"
            self.snapshot["ended_at"] = _iso(timestamp)
            self.snapshot["updated_at"] = _iso(timestamp)
            self._rebuild_sources()
            _attach_gate_summary(self.snapshot, summaries)
            for source in self.snapshot["sources"]:
                source["ended_at"] = _iso(timestamp)
            self._write()

    def record_gate_summary(self, summaries: list[object]) -> None:
        """Attach the final relevance-gate outcomes to this run's snapshot.

        Collection progress is intentionally published while connectors are
        still running.  The relevance gate happens afterwards, so its counts
        must be added separately before the terminal snapshot is rendered by
        the dashboard or completion email.
        """
        with self._lock:
            _attach_gate_summary(self.snapshot, summaries)
            timestamp = self._now()
            self.snapshot["updated_at"] = _iso(timestamp)
            self._write()

    def _rebuild_sources(self) -> None:
        sources: list[dict] = []
        source_names = list(dict.fromkeys(item["source"] for item in self.snapshot["instances"]))
        for source in source_names:
            instances = [
                item for item in self.snapshot["instances"] if item["source"] == source
            ]
            succeeded = sum(item["status"] == "completed" for item in instances)
            failed = sum(item["status"] == "failed" for item in instances)
            finished = succeeded + failed
            if any(item["status"] == "running" for item in instances) or finished:
                status = "running" if finished < len(instances) else (
                    "failed" if failed == len(instances) else "partial" if failed else "completed"
                )
            else:
                status = "pending"
            starts = [item["started_at"] for item in instances if item["started_at"]]
            ends = [item["ended_at"] for item in instances if item["ended_at"]]
            sources.append(
                {
                    "source": source,
                    "status": status,
                    "jobs_finished": sum(item["jobs_finished"] for item in instances),
                    "instances_finished": finished,
                    "instances_total": len(instances),
                    "instances_succeeded": succeeded,
                    "instances_failed": failed,
                    "started_at": min(starts) if starts else None,
                    "ended_at": max(ends) if finished == len(instances) and ends else None,
                }
            )
        self.snapshot["sources"] = sources

    def _write(self) -> None:
        _write_progress_snapshot(
            self.path,
            self.snapshot,
            history_dir=self.history_dir,
        )


def load_progress(path: Path = DEFAULT_PROGRESS_PATH) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_progress_history(
    path: Path = DEFAULT_PROGRESS_PATH,
    *,
    history_dir: Path | None = None,
) -> list[dict]:
    """Return newest-first durable runs, including a legacy latest snapshot.

    Files are keyed by ``run_id`` and updated in place while a run advances.
    Malformed or partially unavailable history files are ignored so one bad
    artifact cannot make the dashboard unusable.
    """
    resolved_history_dir = history_dir or _default_history_dir(path)
    snapshots: dict[str, dict] = {}
    candidates: list[dict] = []
    latest = load_progress(path)
    if latest is not None:
        candidates.append(latest)
    if resolved_history_dir is not None:
        try:
            history_paths = list(resolved_history_dir.glob("*.json"))
        except OSError:
            history_paths = []
        for history_path in history_paths:
            snapshot = load_progress(history_path)
            if snapshot is not None:
                candidates.append(snapshot)

    for snapshot in candidates:
        run_id = str(snapshot.get("run_id") or "")
        if not run_id:
            continue
        existing = snapshots.get(run_id)
        if existing is None or (snapshot.get("updated_at") or "") > (
            existing.get("updated_at") or ""
        ):
            snapshots[run_id] = snapshot

    return sorted(
        snapshots.values(),
        key=lambda snapshot: (
            snapshot.get("started_at") or "",
            snapshot.get("updated_at") or "",
        ),
        reverse=True,
    )


def default_progress_run_id(snapshots: list[dict]) -> str | None:
    """Choose the newest stored run; older interrupted runs remain selectable."""
    if not snapshots:
        return None
    return str(snapshots[0].get("run_id"))


def record_gate_summary_for_snapshot(
    summaries: list[object], *, path: Path = DEFAULT_PROGRESS_PATH,
    history_dir: Path | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict | None:
    """Backfill a completed run's gate outcomes without re-collecting jobs."""
    snapshot = load_progress(path)
    if snapshot is None:
        return None
    _attach_gate_summary(snapshot, summaries)
    snapshot["updated_at"] = _iso(now())
    _write_progress_snapshot(
        path,
        snapshot,
        history_dir=history_dir or _default_history_dir(path),
    )
    return snapshot


def process_is_alive(snapshot: dict) -> bool:
    return process_id_is_alive(snapshot.get("process_id"))


def run_elapsed_seconds(
    snapshot: dict | None,
    *,
    now: datetime | None = None,
    interrupted: bool = False,
) -> float:
    if not snapshot:
        return 0.0
    started = _parse(snapshot.get("started_at"))
    ended = _parse(snapshot.get("ended_at"))
    if ended is None and interrupted:
        # An interrupted process cannot publish its terminal timestamp.  Its
        # most recent atomic snapshot is the truthful endpoint, rather than
        # the dashboard refresh time hours later.
        ended = _parse(snapshot.get("updated_at"))
    if started is None:
        return 0.0
    return round(max(0.0, ((ended or now or _utc_now()) - started).total_seconds()), 1)


def source_rows(
    snapshot: dict | None,
    *,
    now: datetime | None = None,
    interrupted: bool = False,
) -> list[dict]:
    if not snapshot:
        return []
    current = now or _utc_now()
    if interrupted:
        current = _parse(snapshot.get("updated_at")) or current
    rows: list[dict] = []
    for source in snapshot.get("sources", []):
        started = _parse(source.get("started_at"))
        ended = _parse(source.get("ended_at"))
        elapsed = 0.0
        if started is not None:
            elapsed = max(0.0, ((ended or current) - started).total_seconds())
        rows.append(
            {
                "source": source["source"],
                "status": source["status"],
                "elapsed_seconds": round(elapsed, 1),
                "jobs_finished": source["jobs_finished"],
                # ``gate_fetched`` is the de-duplicated input to the
                # relevance gate.  Before that stage completes, use the live
                # raw collection count and leave gate outcomes unavailable.
                "fetched": source.get("gate_fetched", source["jobs_finished"]),
                "kept": source.get("kept"),
                "kept_pct": (
                    round(100 * source["kept"] / source["gate_fetched"], 1)
                    if source.get("gate_fetched")
                    and source.get("kept") is not None
                    else None
                ),
                "drop_role": source.get("drop_role"),
                "drop_seniority": source.get("drop_seniority"),
                "drop_location": source.get("drop_location"),
                "drop_recency": source.get("drop_recency"),
                "instances_finished": source["instances_finished"],
                "instances_total": source["instances_total"],
                "instances_succeeded": source["instances_succeeded"],
                "instances_failed": source["instances_failed"],
            }
        )
    return rows


def total_source_row(
    rows: list[dict], *, status: str, elapsed_seconds: float
) -> dict:
    """Return the shared total row for connector status views.

    Gate outcomes remain unavailable until every source has published them;
    mixing a partial gate total with live raw fetched counts would mislead.
    """
    gate_complete = bool(rows) and all(row["kept"] is not None for row in rows)

    def gate_sum(field: str) -> int | None:
        return sum(int(row[field]) for row in rows) if gate_complete else None

    fetched = sum(int(row["fetched"]) for row in rows)
    kept = gate_sum("kept")
    return {
        "source": "Total",
        "status": status,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "jobs_finished": sum(int(row["jobs_finished"]) for row in rows),
        "fetched": fetched,
        "kept": kept,
        "kept_pct": round(100 * kept / fetched, 1) if kept is not None and fetched else None,
        "drop_role": gate_sum("drop_role"),
        "drop_seniority": gate_sum("drop_seniority"),
        "drop_location": gate_sum("drop_location"),
        "drop_recency": gate_sum("drop_recency"),
        "instances_finished": sum(int(row["instances_finished"]) for row in rows),
        "instances_total": sum(int(row["instances_total"]) for row in rows),
        "instances_succeeded": sum(int(row["instances_succeeded"]) for row in rows),
        "instances_failed": sum(int(row["instances_failed"]) for row in rows),
    }
