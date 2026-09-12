"""Stable vocabulary for current versus pre-instrumentation Decision Runs."""

CURRENT_TELEMETRY_VERSION = "decision_funnels_v1"


def is_legacy_telemetry(version: str | None) -> bool:
    return version is None
