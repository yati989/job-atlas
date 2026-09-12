"""Immutable value objects at the decision-run interface."""
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings


@dataclass(frozen=True)
class DecisionPolicy:
    version: int = settings.DECISION_POLICY_VERSION
    wlb_min_reviews: int = settings.GLASSDOOR_WLB_MIN_REVIEWS
    fresh_outreach_threshold: int = settings.FRESH_OUTREACH_SUCCESS_THRESHOLD
    fresh_company_target: int = settings.DAILY_FRESH_COMPANY_TARGET
    delivery_presumption_minutes: int = settings.DELIVERY_PRESUMPTION_MINUTES
    calibration_window_minutes: int = settings.CALIBRATION_WINDOW_MINUTES
    calibration_poll_minutes: int = settings.CALIBRATION_POLL_MINUTES
    input_timezone: str = settings.PIPELINE_INPUT_TIMEZONE
    # Optional immutable collection slice. When both are set, ingestion picks
    # one matching role/location connector per active source; broad-only
    # sources retain their single connector and still pass the central gate.
    collection_search: str | None = None
    collection_location: str | None = None

    def snapshot(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Approval:
    target: int
    group_limits: dict[int, int | None]
    original_text: str


@dataclass(frozen=True)
class ApprovedScope:
    run_id: str
    company_ids: tuple[int, ...]
    job_ids: tuple[int, ...]
    posting_version_ids: tuple[int, ...]


def parse_since(value: str, *, timezone_name: str = settings.PIPELINE_INPUT_TIMEZONE) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed
