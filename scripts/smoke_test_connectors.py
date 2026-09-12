"""Fetch-only smoke check for every currently active connector.

This command deliberately does not write to the database, run the relevance
gate, retry failures, or change connector configuration. Its connector list
comes from the runtime registries, so parked connectors are not imported or
run here.

The complete active inventory includes the headed tier (currently Indeed), so
this is an attended broad smoke test and may open a visible browser window.
Run only when a broad live check is explicitly intended:

    python -m scripts.smoke_test_connectors
"""
from app.pipeline.registry import ACTIVE_CONNECTORS
from scripts.run_headed_sources import CONNECTORS as HEADED_CONNECTORS


def active_connectors() -> list:
    """Return the exact active runtime inventory, including headed sources."""
    return [*ACTIVE_CONNECTORS, *HEADED_CONNECTORS]


def main() -> None:
    connectors = active_connectors()
    headed_count = len(HEADED_CONNECTORS)
    print(
        f"Smoke-checking {len(connectors)} active connector instance(s) "
        f"without database writes ({headed_count} headed/attended)."
    )

    for connector in connectors:
        print(f"--- {connector.source_name} ---")
        try:
            jobs = connector.fetch()
            print(f"Fetched {len(jobs)} jobs")
            for job in jobs[:3]:
                print(
                    f"  - [{job.source}] {job.title} @ {job.company_name_raw} "
                    f"({job.location_raw})"
                )
        except Exception as exc:
            print(f"  FAILED: {exc}")


if __name__ == "__main__":
    main()
