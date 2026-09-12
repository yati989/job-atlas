"""
One-time interactive Gmail OAuth consent flow (ADR-0009).

Run this ONCE, by hand, after running ``job-atlas auth setup``. It opens
a browser for consent and caches the resulting token in the private application
home — every other outreach script (push_drafts.py,
sync_outreach.py, app.outreach.cli) then reuses that cached token and
never prompts.

Re-run this only if .gmail_token.json is deleted or the refresh token is
revoked; ordinary use never needs it again.

Usage:
    python -m scripts.gmail_auth
"""
from app.outreach.gmail import get_credentials, resolve_credential_paths


def main() -> None:
    # This is the explicit attended repair path.  It must bypass a revoked
    # cached refresh token and open fresh consent instead of retrying it.
    get_credentials(force_reauth=True)
    credentials_path, token_path = resolve_credential_paths()
    print(f"Gmail authorized. Token cached at {token_path}.")
    print(f"(OAuth client read from {credentials_path}.)")


if __name__ == "__main__":
    main()
