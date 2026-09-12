"""
Reconcile pushed outreach drafts against Gmail: has the candidate sent it?

For each `pushed` row, checks whether its Gmail draft still exists. If it's
gone and a message on that thread carries the SENT label, the row flips to
`sent` and the SENT message's actual body is pulled back — an edit made in
Gmail is the real message, so it overwrites what we drafted (ADR-0009). If
the draft is gone but no SENT message is found, the row is left `pushed`
and reported as `gone_unconfirmed` — discarded-without-sending is not
distinguishable from "sent from a different account", so this is never
guessed at.

Cron-able: no agent in the loop, requires a cached Gmail token.

Usage:
    python -m scripts.sync_outreach
"""
import sys

from app.db.session import get_session
from app.outreach import gmail as gmail_ops


def main() -> int:
    with get_session() as session:
        try:
            service = gmail_ops.get_service()
        except gmail_ops.GmailNotConfigured as exc:
            print(f"GMAIL NOT CONFIGURED: {exc}", file=sys.stderr)
            return 2

        counts = gmail_ops.sync_all(session, service)
        session.commit()

        for key, n in counts.items():
            print(f"{key}: {n}")
        if counts["gone_unconfirmed"]:
            print(
                f"\n{counts['gone_unconfirmed']} draft(s) disappeared from Gmail with no "
                "SENT message found on their thread — discarded, not confirmed sent. "
                "Still marked 'pushed'; check manually if this is unexpected.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
