"""Create or verify the dedicated local Indeed Chrome session."""

import argparse
from pathlib import Path

from app.collectors.browser.indeed_auth import check_session, login_headed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the saved profile instead of opening the login flow",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="override the dedicated profile directory (primarily for testing)",
    )
    args = parser.parse_args()
    kwargs = {"profile_dir": args.profile_dir.resolve()} if args.profile_dir else {}

    if args.check:
        if check_session(**kwargs):
            print("Indeed session is authenticated.")
            return
        raise SystemExit("Indeed session is not authenticated; run without --check.")

    login_headed(**kwargs)


if __name__ == "__main__":
    main()
