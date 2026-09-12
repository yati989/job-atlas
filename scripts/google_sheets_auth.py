"""One-time OAuth setup for unattended approval Sheet creation/readback."""

from app.decision_runs.google_sheet_approval import get_credentials


def main() -> None:
    credentials = get_credentials(force_reauth=True, interactive=True)
    if not credentials.valid:
        raise SystemExit("Google Sheets authorization did not produce a valid credential")
    print("Google Sheets authorization saved; unattended approval handoff is ready.")


if __name__ == "__main__":
    main()
