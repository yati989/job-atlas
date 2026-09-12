"""
Per-source acceptance scorecard: fetch-only (NO DB writes), reports whether
a connector is pulling the *right* jobs (relevance/location/freshness) with
*complete* data (field non-null rates). This is the onboarding gate every
source must pass before its data is trusted — and re-runnable later to
detect site drift (DOM changes, filter params silently breaking).

Usage:
    python -m scripts.verify_source <source_name> [--max-instances N]
    python -m scripts.verify_source <source_name> --spec "module.path:ClassName:{json kwargs}" [--spec ...]

Default mode finds connector instances by source_name across both
registries (app/pipeline/registry.py ACTIVE_CONNECTORS and
scripts/run_headed_sources.py CONNECTORS). --spec bypasses the registries
and constructs the connector(s) directly — for verifying a changed
constructor signature without touching shared registry files (e.g. during
parallel source onboarding).
"""
import argparse
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone

from app.pipeline.relevance import filter_relevant
from app.pipeline.progress import ConnectorProgress, DEFAULT_PROGRESS_PATH
from app.pipeline.runner import _fetch_with_progress

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("verify_source")

SCORECARD_DIR = pathlib.Path(__file__).resolve().parent.parent / "scorecards"
# Acceptance diagnostics must never replace the dashboard's canonical
# connector-run snapshot.  The dashboard intentionally reads only
# ``latest.json``; verify_source publishes separately for operator debugging.
VERIFY_SOURCE_PROGRESS_PATH = DEFAULT_PROGRESS_PATH.with_name(
    "latest-verify-source.json"
)

# Descriptive location stats only — NOT a relevance verdict. The relevance
# gate (ADR-0002) is the sole authority on whether a job is India-eligible;
# these percentages just describe what the source's location text looks like,
# which is useful when diagnosing *why* the gate dropped rows. Do not gate the
# verdict on them, or a second relevance definition creeps back in.
INDIA_MARKERS = [
    "india", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad",
    "chennai", "pune", "kolkata", "gurugram", "gurgaon", "noida",
    "ahmedabad", "kochi", "jaipur", "chandigarh", "coimbatore", "indore",
]

FRESHNESS_WINDOW_DAYS = 180
POSTED_DATE_OPTIONAL_SOURCES = frozenset({"instahyre"})

COMPLETENESS_FIELDS = [
    "description_raw", "posted_at", "location_raw", "company_name_raw",
    "salary_raw", "employment_type", "seniority", "apply_url",
]


def _find_connectors(source_name: str) -> list:
    from app.pipeline.registry import ACTIVE_CONNECTORS
    matches = [c for c in ACTIVE_CONNECTORS if c.source_name == source_name]
    if not matches:
        # Headed connectors live in a separate list (they need a display);
        # import lazily so verifying an API source doesn't import patchright.
        from scripts.run_headed_sources import CONNECTORS as HEADED
        matches = [c for c in HEADED if c.source_name == source_name]
    return matches


def _build_from_spec(spec: str):
    """'module.path:ClassName:{json kwargs}' -> connector instance.
    The kwargs segment is optional; it's everything after the second
    colon so JSON containing colons parses fine."""
    module_path, _, rest = spec.partition(":")
    class_name, _, kwargs_json = rest.partition(":")
    import importlib
    cls = getattr(importlib.import_module(module_path), class_name)
    kwargs = json.loads(kwargs_json) if kwargs_json.strip() else {}
    return cls(**kwargs)


def _non_empty(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _is_fresh(posted_at) -> bool | None:
    """True/False if posted_at is parseable, None if absent/unparseable."""
    if posted_at is None:
        return None
    dt = posted_at
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= datetime.now(timezone.utc) - timedelta(days=FRESHNESS_WINDOW_DAYS)


def _request_observability_delta(before: dict, after: dict) -> dict:
    request_count = max(
        0,
        int(after["http_requests_started"])
        - int(before["http_requests_started"]),
    )
    cooldown_events = max(
        0, int(after["cooldown_events"]) - int(before["cooldown_events"])
    )
    cooldown_wait_s = max(
        0.0,
        float(after["cooldown_committed_s"])
        - float(before["cooldown_committed_s"]),
    )
    return {
        "http_request_count": request_count,
        "max_in_flight_limit": int(after["max_in_flight_limit"]),
        "max_in_flight_observed": int(after["max_in_flight_observed"]),
        "cooldown_events": cooldown_events,
        "cooldown_wait_s": round(cooldown_wait_s, 3),
        "cooldown_outcome": "observed" if cooldown_events else "none",
        "circuit_outcome": "open" if after["circuit_open"] else "closed",
    }


def score(
    source_name: str,
    max_instances: int,
    specs: list[str] | None = None,
    max_jobs: int | None = 100,
) -> dict:
    if specs:
        connectors = [_build_from_spec(s) for s in specs]
    else:
        connectors = _find_connectors(source_name)
    if not connectors:
        raise SystemExit(f"No connector registered with source_name={source_name!r}")

    observation_reader = getattr(connectors[0], "request_observability", None)
    observation_before = observation_reader() if callable(observation_reader) else None

    jobs = []
    instances_run = 0
    selected_connectors = connectors[:max_instances]
    progress = ConnectorProgress(
        selected_connectors,
        path=VERIFY_SOURCE_PROGRESS_PATH,
        command=f"scripts.verify_source {source_name}",
    )
    try:
        for connector in selected_connectors:
            logger.info("Fetching via %s instance %s...", source_name, instances_run + 1)
            result = _fetch_with_progress(connector, progress)
            if result.error is not None:
                raise result.error
            jobs.extend(result.jobs or [])
            instances_run += 1
            # Each instance's fetch() still runs its own full pagination/depth
            # cursor (this is fetch-only against the real connector, not a
            # truncated one) — this only stops us from starting further
            # instances once a scoring-sized sample already exists, so a
            # high-fan-out source (many search-term/location-mode instances)
            # doesn't pay for all of them just to produce a scorecard.
            if max_jobs is not None and len(jobs) >= max_jobs:
                break
    finally:
        progress.finish()

    if max_jobs is not None:
        jobs = jobs[:max_jobs]

    total = len(jobs)
    scorecard = {
        "source": source_name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "instances_run": instances_run,
        "instances_registered": len(connectors),
        "fetched": total,
    }
    if total == 0:
        if observation_before is not None:
            scorecard["request_observability"] = _request_observability_delta(
                observation_before, observation_reader()
            )
        scorecard["verdict"] = "FAIL: zero jobs fetched"
        return scorecard

    # Central relevance gate (ADR-0002) — same authority run_all.py enforces,
    # so a source's reported relevance matches what the pipeline actually
    # stores, and the per-axis breakdown shows *why* a source scores badly.
    relevant, drop_counts, _drop_details = filter_relevant(jobs)
    scorecard["relevance_pct"] = round(100 * len(relevant) / total, 1)
    scorecard["relevance_drop_counts"] = drop_counts

    # Match the pipeline's two-stage connector contract: collect listings,
    # gate them centrally, then spend public-detail requests only on kept
    # rows. This makes the scorecard's completeness evidence representative
    # without putting detail hydration back on the fetch path.
    hydrator = getattr(connectors[0], "hydrate_relevant_jobs", None)
    if callable(hydrator) and relevant:
        hydration = hydrator(relevant)
        scorecard["post_gate_hydration"] = hydration

    if observation_before is not None:
        scorecard["request_observability"] = _request_observability_delta(
            observation_before, observation_reader()
        )

    india = sum(
        1 for j in jobs
        if any(m in (f"{j.location_raw or ''} {j.remote_scope or ''}").lower() for m in INDIA_MARKERS)
    )
    remote = sum(1 for j in jobs if j.is_remote)
    scorecard["india_location_pct"] = round(100 * india / total, 1)
    scorecard["remote_pct"] = round(100 * remote / total, 1)
    scorecard["india_or_remote_pct"] = round(
        100 * sum(
            1 for j in jobs
            if j.is_remote or any(m in (f"{j.location_raw or ''} {j.remote_scope or ''}").lower() for m in INDIA_MARKERS)
        ) / total, 1,
    )

    fresh_flags = [_is_fresh(j.posted_at) for j in jobs]
    dated = [f for f in fresh_flags if f is not None]
    scorecard["posted_at_known_pct"] = round(100 * len(dated) / total, 1)
    scorecard["fresh_pct_of_dated"] = round(100 * sum(dated) / len(dated), 1) if dated else None

    completeness = {}
    for field in COMPLETENESS_FIELDS:
        have = sum(1 for j in jobs if _non_empty(getattr(j, field)))
        completeness[field] = round(100 * have / total, 1)
    scorecard["completeness_pct"] = completeness

    # Verdict thresholds — deliberately simple; the human-readable numbers
    # above are the real product, the verdict is just a first-glance flag.
    problems = []
    if scorecard["relevance_pct"] < 60:
        problems.append(f"relevance {scorecard['relevance_pct']}% < 60%")
    location_dropped = drop_counts["location"]
    if location_dropped > total / 2:
        problems.append(f"location-dropped {location_dropped}/{total} by the gate")
    if completeness["description_raw"] < 90:
        problems.append(f"description {completeness['description_raw']}% < 90%")
    if (source_name not in POSTED_DATE_OPTIONAL_SOURCES
            and scorecard["posted_at_known_pct"] < 100):
        problems.append(
            f"posted date {scorecard['posted_at_known_pct']}% < 100%"
        )
    scorecard["verdict"] = "PASS" if not problems else "FAIL: " + "; ".join(problems)
    return scorecard


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_name")
    parser.add_argument("--max-instances", type=int, default=2)
    parser.add_argument("--max-jobs", type=int, default=100,
                        help="stop after collecting this many jobs across instances "
                             "(0/negative disables the cap); default 100")
    parser.add_argument("--spec", action="append", default=None,
                        help="module.path:ClassName:{json kwargs} — bypass registries")
    args = parser.parse_args()

    max_jobs = args.max_jobs if args.max_jobs and args.max_jobs > 0 else None
    card = score(args.source_name, args.max_instances, specs=args.spec, max_jobs=max_jobs)

    SCORECARD_DIR.mkdir(exist_ok=True)
    out_path = SCORECARD_DIR / f"{args.source_name}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(card, f, indent=2, ensure_ascii=False)

    print(json.dumps(card, indent=2, ensure_ascii=False))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
