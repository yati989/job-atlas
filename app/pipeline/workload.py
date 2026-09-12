"""Fail fast on serial per-card hydration, not connector supply depth."""
from __future__ import annotations

MAX_SERIAL_DETAILS_PER_INSTANCE = 300


class WorkloadLimitError(RuntimeError):
    """Raised before fetching when connector configuration can run for hours."""


def _description_hydration_enabled(connector: object) -> bool:
    for name in ("fetch_descriptions", "fetch_details"):
        if hasattr(connector, name):
            return bool(getattr(connector, name))
    return False


def _detail_concurrency(connector: object) -> int:
    for name in ("detail_workers", "detail_batch_size"):
        value = getattr(connector, name, None)
        if isinstance(value, int) and value > 0:
            return value
    return 1


def enforce_workload_bounds(connectors: list) -> None:
    """Reject configurations matching known runaway workload shapes.

    Static supply depth is source-specific and remains unchanged for sources
    without verified newest-first ordering. This guard targets the actual
    TimesJobs failure shape: a large result set hydrated one card at a time.
    """
    violations: list[str] = []
    for connector in connectors:
        source = str(connector.source_name)
        max_results = getattr(connector, "max_results", None)
        if (
            _description_hydration_enabled(connector)
            and isinstance(max_results, int)
            and max_results > MAX_SERIAL_DETAILS_PER_INSTANCE
            and _detail_concurrency(connector) == 1
        ):
            violations.append(
                f"{source}: serial detail hydration for up to {max_results} cards"
            )
    violations = list(dict.fromkeys(violations))
    if violations:
        raise WorkloadLimitError(
            "Unsafe connector workload configuration:\n- "
            + "\n- ".join(violations)
        )
