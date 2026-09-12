"""Decision-run adapter for the connector JSON compatibility projection."""
from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.session import get_session
from app.pipeline.progress import DEFAULT_PROGRESS_PATH, ConnectorProgress

from .progress import (
    FailureDisposition,
    FailedRecordDisposition,
    StageCounts,
    StageRecord,
    complete_with_errors_stage,
    fail_stage,
    finish_stage,
    heartbeat_stage,
    start_stage,
    update_stage,
)


class DecisionRunConnectorProgress(ConnectorProgress):
    """Publish connector detail to JSON and canonical stage state to Postgres."""

    stage_name = "fetch_jobs"

    def __init__(
        self,
        connectors: list,
        *,
        run_id: str,
        path: Path = DEFAULT_PROGRESS_PATH,
        command: str | None = None,
        process_id: int | None = None,
        session_factory: Callable[[], AbstractContextManager[Session]] = get_session,
        heartbeat_interval_s: float | None = 30.0,
    ) -> None:
        self._session_factory = session_factory
        self._durable_lock = threading.RLock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        with self._session_factory() as session:
            self._counts_floor = start_stage(
                session,
                run_id,
                self.stage_name,
                expected_count=len(connectors),
                process_id=process_id,
                reason="collecting connector instances",
            )
        super().__init__(
            connectors,
            path=path,
            run_id=run_id,
            command=command,
            process_id=process_id,
        )
        if heartbeat_interval_s is not None:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(heartbeat_interval_s,),
                name=f"decision-run-heartbeat-{run_id}",
                daemon=True,
            )
            self._heartbeat_thread.start()

    def _snapshot_outcomes(self) -> tuple[StageCounts, dict[str, int], tuple[StageRecord, ...]]:
        instances = self.snapshot["instances"]
        advanced = sum(row["status"] == "completed" for row in instances)
        failed = sum(row["status"] == "failed" for row in instances)
        counts = StageCounts(len(instances), advanced, 0, failed, len(instances) - advanced - failed)
        reason_counts: dict[str, int] = {}
        records = []
        for row in instances:
            if row["status"] not in {"completed", "failed"}:
                continue
            outcome = "advanced" if row["status"] == "completed" else "failed"
            reason = (row.get("error_type") or "connector_error") if outcome == "failed" else None
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            records.append(StageRecord("connector_instance", row["id"], outcome, reason))
        return counts, reason_counts, tuple(records)

    def _update_durable(self, reason: str | None = None) -> None:
        with self._durable_lock, self._session_factory() as session:
            counts, reason_counts, records = self._snapshot_outcomes()
            if counts.processed < self._counts_floor.processed:
                heartbeat_stage(session, self.snapshot["run_id"], self.stage_name, reason=reason)
                return
            update_stage(
                session,
                self.snapshot["run_id"],
                self.stage_name,
                counts=counts,
                reason_counts=reason_counts,
                records=records,
                reason=reason,
            )

    def _heartbeat_loop(self, interval_s: float) -> None:
        while not self._heartbeat_stop.wait(interval_s):
            self._update_durable()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def instance_started(self, connector: object, *, attempt: int) -> None:
        super().instance_started(connector, attempt=attempt)
        self._update_durable()

    def instance_finished(self, result: object) -> None:
        super().instance_finished(result)
        self._update_durable()

    def complete(
        self,
        summaries: list[object],
        *,
        after_finish: Callable[[Session], None] | None = None,
    ) -> None:
        """Finish fetch and its immediate durable handoff in one transaction."""
        self._stop_heartbeat()
        super().complete(summaries)
        with self._durable_lock, self._session_factory() as session:
            counts, reason_counts, records = self._snapshot_outcomes()
            complete = complete_with_errors_stage if counts.failed else finish_stage
            complete(
                session,
                self.snapshot["run_id"],
                self.stage_name,
                counts=counts,
                reason_counts=reason_counts,
                records=records,
                **({
                    "failure_dispositions": tuple(
                        FailedRecordDisposition(
                            record.record_type, record.record_id,
                            FailureDisposition.NON_BLOCKING,
                        )
                        for record in records if record.outcome == "failed"
                    )
                } if counts.failed else {}),
                reason=f"jobs_fetched={sum(int(row.get('jobs_finished', 0)) for row in self.snapshot['sources'])}",
            )
            if after_finish is not None:
                after_finish(session)

    def fail(self, reason: str) -> None:
        """End both projections after a structural ingestion failure."""
        self._stop_heartbeat()
        with self._lock:
            timestamp = self._now()
            self.snapshot["status"] = "failed"
            self.snapshot["ended_at"] = self.snapshot["updated_at"] = timestamp.isoformat()
            self._rebuild_sources()
            self._write()
        with self._durable_lock, self._session_factory() as session:
            counts, reason_counts, records = self._snapshot_outcomes()
            fail_stage(
                session,
                self.snapshot["run_id"],
                self.stage_name,
                counts=counts,
                reason_counts=reason_counts,
                records=records,
                reason=reason,
            )
