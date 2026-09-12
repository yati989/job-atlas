"""
The seam between the /draft-outreach skill and the database + Gmail.

Mirrors app/contacts/brightdata_query.py and app/prospects/cli.py: the
skill writes the email in-session; this CLI is how it persists anything,
enforces the addressing/hook/collision invariants, and is the only thing
allowed to talk to Gmail.

For a tailored resume, this CLI does NOT do the tailoring itself — that is
`python -m scripts.tailor_resume --tailored t.yaml --job-id X --score S
--gap-report g.json` (the existing tool), whose printed tailored_resumes
row id and pdf path feed straight into `draft --resume-kind tailored`.

Usage:
    python -m app.outreach.cli pair --company-id 1 --recipient-tier ic --recipient-title "..."
    python -m app.outreach.cli pair --prospect-id 1 --recipient-tier hiring_manager --recipient-title "..."

    python -m app.outreach.cli draft --to-email x@example.com \
        [--company-id 1 | --prospect-id 1] \
        --to-name "..." --recipient-title "..." --recipient-tier ic \
        --resume-kind master --pairing-reason no_matching_job \
        --subject-file subj.txt --body-file body.txt \
        [--hook-url ... --hook-quote "..."]

    python -m app.outreach.cli draft --to-email x@example.com --company-id 1 \
        --to-name "..." --recipient-title "..." --recipient-tier hiring_manager \
        --resume-kind tailored --pairing-reason recipient_function_match \
        --tailored-resume-id 7 --resume-path output/tailored_resumes/job_42/resume.pdf --matched-job-id 42 \
        --subject-file subj.txt --body-file body.txt

    python -m app.outreach.cli push --to-email x@example.com
    python -m app.outreach.cli push --all
    python -m app.outreach.cli sync
    python -m app.outreach.cli report
"""
import argparse
import sys

from sqlalchemy import select

from app.db.session import get_session
from app.models.orm import Job, OutreachDraft, OutreachDeliveryAttempt, OutreachMessage
from app.outreach import drafts as draft_ops
from app.outreach import gmail as gmail_ops
from app.outreach import pairing
from app.resume.render import RenderError


def _safe_print(line: str) -> None:
    """Recipient names and hook quotes carry arbitrary Unicode that a plain
    print() crashes on under Windows' cp1252 console — same fix as
    app/contacts/brightdata_query.py and app/prospects/cli.py."""
    encoding = sys.stdout.encoding or "utf-8"
    print(line.encode(encoding, errors="replace").decode(encoding))


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _cmd_pair(args) -> None:
    with get_session() as session:
        if args.run_id:
            if args.contact_id is None:
                raise SystemExit("--run-id requires --contact-id; approved pairing is contact-specific")
            from app.decision_runs import pair_for_approved_scope
            result = pair_for_approved_scope(session, args.run_id, args.contact_id)
        else:
            result = pairing.pair(
                session, company_id=args.company_id,
                recipient_tier=args.recipient_tier, recipient_title=args.recipient_title,
            )
        _safe_print(f"resume_kind : {result.resume_kind}")
        _safe_print(f"reason      : {result.reason}")
        _safe_print(f"search_group: {result.search_group}")
        if result.candidate_job_ids:
            _safe_print("candidates  :")
            for job_id in result.candidate_job_ids:
                job = session.get(Job, job_id)
                domain = pairing.subject_domain_for_job(job)
                _safe_print(f"  [{job_id}] subject_domain={domain!r}  {job.title}")
        else:
            _safe_print("candidates  : []")


def _cmd_draft(args) -> None:
    with get_session() as session:
        try:
            target = draft_ops.resolve_recipient(
                session, to_email=args.to_email,
                company_id=args.company_id, prospect_id=args.prospect_id,
            )
        except draft_ops.RecipientResolutionError as exc:
            _safe_print(f"REFUSED: {exc}")
            raise SystemExit(2)

        resume_path = args.resume_path
        if args.resume_kind == "master" and resume_path is None:
            try:
                resume_path = draft_ops.render_master_resume()
            except RenderError as exc:
                _safe_print(f"REFUSED: could not render the master resume: {exc}")
                raise SystemExit(2)

        try:
            if args.decision_run_id is not None:
                from app.decision_runs import load_approved_scope
                scope = load_approved_scope(session, args.decision_run_id)
                if args.posting_version_id is not None and args.posting_version_id not in scope.posting_version_ids:
                    raise draft_ops.InvalidDraft("posting_version_id is outside this approved decision scope")
                if args.matched_job_id is not None:
                    from app.decision_runs.manifests import selected_jobs
                    if target.company_id is None:
                        raise draft_ops.InvalidDraft("approved outreach draft must resolve to a company")
                    approved = {job.id for job in selected_jobs(session, args.decision_run_id, target.company_id)}
                    if args.matched_job_id not in approved:
                        raise draft_ops.InvalidDraft("matched_job_id is outside this approved decision scope")
            row = draft_ops.save_draft(
                session, target,
                to_name=args.to_name, recipient_title=args.recipient_title,
                recipient_tier=args.recipient_tier,
                resume_kind=args.resume_kind, pairing_reason=args.pairing_reason,
                subject=_read(args.subject_file), body=_read(args.body_file),
                tailored_resume_id=args.tailored_resume_id, resume_path=resume_path,
                matched_job_id=args.matched_job_id,
                hook_url=args.hook_url, hook_quote=args.hook_quote,
                decision_run_id=args.decision_run_id,
                posting_version_id=args.posting_version_id,
                calibration=args.calibration,
                recipient_evidence=(
                    {"source_url": args.recipient_source_url, "verification": args.recipient_verification}
                    if args.recipient_source_url or args.recipient_verification else None
                ),
            )
        except draft_ops.InvalidDraft as exc:
            _safe_print(f"REFUSED: {exc}")
            raise SystemExit(2)

        session.commit()
        _safe_print(f"{row!r}  status={row.status}")
        if row.collision_risk:
            _safe_print(
                "  name-collision risk on this contact — drafted and will be pushed "
                "like any other; flagged for review (ADR-0012), not blocked."
            )


def _cmd_backfill_company_inbox(args) -> None:
    with get_session() as session:
        try:
            row = draft_ops.backfill_company_inbox_ledger(
                session, draft_id=args.draft_id, decision_run_id=args.decision_run_id,
                posting_version_id=args.posting_version_id,
                recipient_evidence={
                    "source_url": args.recipient_source_url,
                    "verification": args.recipient_verification,
                },
            )
        except draft_ops.InvalidDraft as exc:
            _safe_print(f"REFUSED: {exc}")
            raise SystemExit(2)
        session.commit()
        _safe_print(f"Backfilled immutable company-inbox ledger for draft {row.id}: {row.to_email}")


def _cmd_push(args) -> None:
    with get_session() as session:
        frozen = None
        targets = None
        if args.run_id:
            from app.decision_runs import freeze_gmail_draft_manifest
            frozen = freeze_gmail_draft_manifest(session, args.run_id)
            from app.decision_runs.outreach_funnel import prepare_gmail_retry
            prepare_gmail_retry(session, args.run_id)
            attempt_ids = [item.delivery_attempt_id for item in frozen if item.outcome is None]
            targets = list(session.execute(select(OutreachDeliveryAttempt).join(OutreachMessage).where(
                OutreachDeliveryAttempt.id.in_(attempt_ids), OutreachDeliveryAttempt.state == "held",
            )).scalars()) if attempt_ids else []
        try:
            service = gmail_ops.get_service()
        except gmail_ops.GmailNotConfigured as exc:
            _safe_print(f"GMAIL NOT CONFIGURED: {exc}")
            # No API call happened: preserve pending item state but durably
            # fail this stage attempt so an explicit retry is legal.
            if args.run_id:
                from app.decision_runs.progress import fail_stage, StageName
                fail_stage(session, args.run_id, StageName.GMAIL_DRAFT_POSTING, reason="gmail_not_configured_before_provider_call")
            session.commit()
            raise SystemExit(2)

        if args.run_id:
            assert targets is not None
        elif args.all:
            targets = draft_ops.pending_for_push(session)
        else:
            row = session.execute(
                select(OutreachDraft).where(OutreachDraft.to_email == draft_ops.normalize_email(args.to_email))
            ).scalar_one_or_none()
            targets = [row] if row else []

        if not targets:
            _safe_print("nothing to push")
            return

        for draft in targets:
            try:
                if args.run_id:
                    from app.outreach.state import push_attempt
                    push_attempt(session, gmail_ops.GmailMailboxAdapter(service), draft)
                else:
                    gmail_ops.push_draft(session, service, draft)
            except Exception as exc:
                _safe_print(f"FAILED {draft.to_email}: {exc}")
                session.commit()
                continue
            session.commit()
            _safe_print(f"pushed {draft.to_email} -> gmail draft {draft.gmail_draft_id}")


def _cmd_push_ledger(args) -> None:
    """Push every held attempt in the immutable approved-run manifest."""
    with get_session() as session:
        try:
            mailbox = gmail_ops.GmailMailboxAdapter(gmail_ops.get_service())
        except gmail_ops.GmailNotConfigured as exc:
            _safe_print(f"GMAIL NOT CONFIGURED: {exc}")
            raise SystemExit(2)
        from app.decision_runs import freeze_gmail_draft_manifest
        frozen = freeze_gmail_draft_manifest(session, args.run_id)
        attempt_ids = [item.delivery_attempt_id for item in frozen if item.outcome is None]
        attempts = list(session.execute(select(OutreachDeliveryAttempt).join(OutreachMessage).where(
            OutreachMessage.run_id == args.run_id,
            OutreachDeliveryAttempt.id.in_(attempt_ids),
            OutreachDeliveryAttempt.state == "held")).scalars()) if attempt_ids else []
        from app.outreach.state import push_attempt
        for attempt in attempts:
            push_attempt(session, mailbox, attempt)
            session.commit()
            _safe_print(f"pushed approved draft {attempt.to_email}")
        if not attempts:
            _safe_print("no held approved draft attempts")


def _cmd_sync(args) -> None:
    with get_session() as session:
        try:
            service = gmail_ops.get_service()
        except gmail_ops.GmailNotConfigured as exc:
            _safe_print(f"GMAIL NOT CONFIGURED: {exc}")
            raise SystemExit(2)
        counts = gmail_ops.sync_all(session, service)
        session.commit()
        for key, n in counts.items():
            _safe_print(f"{key}: {n}")


def _cmd_reconcile_indeterminate(args) -> None:
    with get_session() as session:
        from app.decision_runs.outreach_funnel import reconcile_indeterminate_gmail
        reconcile_indeterminate_gmail(session, args.run_id, delivery_attempt_id=args.delivery_attempt_id,
            provider_draft_id=args.provider_draft_id, provider_thread_id=args.provider_thread_id,
            confirmed_not_created=args.confirmed_not_created)
        session.commit()


def _cmd_report(args) -> None:
    with get_session() as session:
        rows = session.execute(select(OutreachDraft)).scalars().all()
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
            _safe_print(f"{status:24s} {n}")

        flagged = draft_ops.collision_risk_drafts(session)
        if flagged:
            _safe_print("")
            _safe_print("collision-risk (pushed like any other, flagged for review — ADR-0012):")
            for r in flagged:
                _safe_print(f"  {r.to_email}  ({r.to_name or 'unknown name'})  status={r.status}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pair", help="preview the resume-kind/job pairing decision")
    p.add_argument("--company-id", type=int)
    p.add_argument("--run-id", help="Use immutable approved-scope pairing.")
    p.add_argument("--contact-id", type=int)
    p.add_argument("--recipient-tier")
    p.add_argument("--recipient-title")
    p.set_defaults(func=_cmd_pair)

    p = sub.add_parser("draft", help="create or update one recipient's draft")
    p.add_argument("--to-email", required=True)
    p.add_argument("--company-id", type=int)
    p.add_argument("--prospect-id", type=int)
    p.add_argument("--to-name")
    p.add_argument("--recipient-title")
    p.add_argument("--recipient-tier")
    p.add_argument("--resume-kind", required=True, choices=["tailored", "master"])
    p.add_argument("--pairing-reason", required=True, choices=sorted(pairing.VALID_PAIRING_REASONS))
    p.add_argument("--tailored-resume-id", type=int)
    p.add_argument("--resume-path")
    p.add_argument("--matched-job-id", type=int)
    p.add_argument("--subject-file", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--hook-url")
    p.add_argument("--hook-quote")
    p.add_argument("--decision-run-id", help="Dual-write this approved draft into the outreach ledger.")
    p.add_argument("--posting-version-id", type=int)
    p.add_argument("--calibration", action="store_true")
    p.add_argument("--recipient-source-url", help="Published page that names a company inbox recipient.")
    p.add_argument("--recipient-verification", help="Why that published company inbox is usable.")
    p.set_defaults(func=_cmd_draft)

    p = sub.add_parser("backfill-company-inbox", help="add a pre-ledger company inbox draft to its immutable run record")
    p.add_argument("--draft-id", required=True, type=int)
    p.add_argument("--decision-run-id", required=True)
    p.add_argument("--posting-version-id", type=int)
    p.add_argument("--recipient-source-url", required=True)
    p.add_argument("--recipient-verification", required=True)
    p.set_defaults(func=_cmd_backfill_company_inbox)

    p = sub.add_parser("push", help="push drafted rows to Gmail Drafts")
    p.add_argument("--to-email")
    p.add_argument("--all", action="store_true")
    p.add_argument("--run-id", help="Push only the immutable approved-run Gmail Draft manifest.")
    p.set_defaults(func=_cmd_push)

    p = sub.add_parser("push-ledger", help="push every held draft attempt for one approved run")
    p.add_argument("--run-id", required=True)
    p.set_defaults(func=_cmd_push_ledger)

    sub.add_parser("sync", help="reconcile pushed drafts against Gmail").set_defaults(func=_cmd_sync)
    p = sub.add_parser("reconcile-indeterminate", help="Operator reconciliation for one ambiguous Gmail Draft call")
    p.add_argument("--run-id", required=True); p.add_argument("--delivery-attempt-id", required=True, type=int)
    p.add_argument("--provider-draft-id"); p.add_argument("--provider-thread-id"); p.add_argument("--confirmed-not-created", action="store_true")
    p.set_defaults(func=_cmd_reconcile_indeterminate)
    sub.add_parser("report", help="status counts + blocked recipients").set_defaults(func=_cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
