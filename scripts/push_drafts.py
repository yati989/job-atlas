"""
Push every drafted (unblocked) outreach row to Gmail Drafts.

Cron-able: no agent in the loop, requires a cached Gmail token
(`python -m scripts.gmail_auth` must have been run once already) — raises
GmailNotConfigured rather than opening a browser if the token is missing,
so an unattended run never hangs.

Usage:
    python -m scripts.push_drafts
"""
import sys

from app.db.session import get_session
from app.outreach import drafts as draft_ops
from app.outreach import gmail as gmail_ops


def main() -> int:
    with get_session() as session:
        try:
            service = gmail_ops.get_service()
        except gmail_ops.GmailNotConfigured as exc:
            print(f"GMAIL NOT CONFIGURED: {exc}", file=sys.stderr)
            return 2

        targets = draft_ops.pending_for_push(session)
        if not targets:
            print("nothing to push")
            return 0

        pushed, skipped = 0, 0
        for draft in targets:
            try:
                gmail_ops.push_draft(session, service, draft)
            except draft_ops.InvalidDraft as exc:
                print(f"SKIPPED {draft.to_email}: {exc}", file=sys.stderr)
                skipped += 1
                continue
            session.commit()
            print(f"pushed {draft.to_email} -> gmail draft {draft.gmail_draft_id}")
            pushed += 1

        print(f"\n{pushed} pushed, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
