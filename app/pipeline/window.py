"""One explicit posting-time window shared by collectors, depth, and gate."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config.settings import PIPELINE_INPUT_TIMEZONE


_NATIVE_DAY_ATTRIBUTES = (
    "date_filter_days",
    "max_age_days",
    "freshness_days",
    "recency_window_days",
    "days_since_posted",
    "days_since_updated",
)
RECENCY_CAP_BASELINE_DAYS = 60


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(PIPELINE_INPUT_TIMEZONE))
    return value.astimezone(timezone.utc)


def parse_since(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value)
    return as_utc(datetime.fromisoformat(value))


@dataclass(frozen=True)
class SyncWindow:
    since_at: datetime
    cutoff_at: datetime
    native_days: int


def apply_sync_window(
    connectors: list,
    *,
    since_at: datetime,
    cutoff_at: datetime,
) -> SyncWindow:
    """Apply an exact gate cutoff and a safe whole-day native prefilter.

    Native job-board filters generally accept only an integer number of days.
    Rounding upward prevents them from hiding a posting that falls inside the
    exact timestamp window; the central gate then enforces ``since_at``.
    Connectors explicitly verified as newest-first may also declare one cap to
    scale from its 60-day baseline. Unsorted connectors are not changed.
    """
    since_at = as_utc(since_at)
    cutoff_at = as_utc(cutoff_at)
    if since_at > cutoff_at:
        raise ValueError("--since must not be after the run start")
    native_days = max(
        1, math.ceil((cutoff_at - since_at).total_seconds() / 86_400)
    )
    for connector in connectors:
        # Some connectors use listing metadata to decide whether a costly
        # per-job detail request is worthwhile. Give those connectors the
        # same exact boundary used by the central gate; the native day filter
        # is deliberately rounded and can otherwise hydrate out-of-window
        # rows.
        if hasattr(connector, "exact_recency_cutoff"):
            setattr(connector, "exact_recency_cutoff", since_at)
        for attribute in _NATIVE_DAY_ATTRIBUTES:
            if hasattr(connector, attribute):
                setattr(connector, attribute, native_days)
        cap_attribute = getattr(
            connector, "recency_scaled_cap_attribute", None
        )
        if getattr(connector, "verified_newest_first", False) and cap_attribute:
            baseline_caps = getattr(connector, "_sync_window_baseline_caps", None)
            if baseline_caps is None:
                baseline_caps = {}
                setattr(connector, "_sync_window_baseline_caps", baseline_caps)
            if cap_attribute not in baseline_caps:
                baseline_caps[cap_attribute] = getattr(connector, cap_attribute)
            baseline = baseline_caps[cap_attribute]
            if not isinstance(baseline, int) or baseline < 1:
                raise ValueError(
                    f"{connector.source_name}: recency-scaled cap "
                    f"{cap_attribute!r} must be a positive integer"
                )
            setattr(
                connector,
                cap_attribute,
                max(
                    1,
                    math.ceil(
                        baseline * native_days / RECENCY_CAP_BASELINE_DAYS
                    ),
                ),
            )
            setattr(connector, "_recency_cap_intentionally_scaled", True)
    return SyncWindow(
        since_at=since_at,
        cutoff_at=cutoff_at,
        native_days=native_days,
    )
