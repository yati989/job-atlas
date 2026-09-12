"""Mark an interrupted relevance audit run with its terminal state."""
from __future__ import annotations

import argparse

from app.pipeline.relevance_audit import finish_audit_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--state", choices=["completed", "partial", "failed", "interrupted"], required=True)
    parser.add_argument("--failure-detail", default=None)
    args = parser.parse_args()
    finish_audit_run(
        run_id=args.run_id, state=args.state, failure_detail=args.failure_detail
    )


if __name__ == "__main__":
    main()
