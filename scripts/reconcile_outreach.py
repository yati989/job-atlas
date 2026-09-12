"""Reconcile the outreach ledger (draft-only), optionally for one run."""
import argparse
from datetime import datetime, timezone
from app.db.session import get_session
from app.outreach.gmail import GmailMailboxAdapter, get_service
from app.outreach.state import reconcile_outreach

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", help="limit reconciliation to one decision run")
    p.add_argument("--read-only", action="store_true", help="observe and record events without creating released Gmail drafts")
    args = p.parse_args()
    with get_session() as session:
        print(reconcile_outreach(session, GmailMailboxAdapter(get_service()), now=datetime.now(timezone.utc),
                                 run_id=args.run_id, push_released=not args.read_only))
if __name__ == "__main__": main()
