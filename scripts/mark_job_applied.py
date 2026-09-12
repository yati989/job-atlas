"""Record a manual job-board application against an immutable posting version."""
import argparse
from app.db.session import get_session
from app.models.orm import JobApplication, JobPostingVersion, utcnow

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--posting-version-id", type=int)
    p.add_argument("--tailored-resume-id", type=int)
    p.add_argument("--notes")
    p.add_argument("--list", action="store_true", help="List recorded applications.")
    args = p.parse_args()
    with get_session() as session:
        if args.list:
            for row in session.query(JobApplication).order_by(JobApplication.applied_at.desc()):
                print(f"{row.id}\t{row.posting_version_id}\t{row.applied_at.isoformat()}\t{row.source or 'manual'}")
            return
        if args.posting_version_id is None:
            p.error("--posting-version-id is required unless --list is used")
        version = session.get(JobPostingVersion, args.posting_version_id)
        if not version: raise SystemExit("unknown posting version")
        row = session.query(JobApplication).filter_by(posting_version_id=version.id).one_or_none()
        if row is None:
            row = JobApplication(posting_version_id=version.id, canonical_duplicate_root_id=version.canonical_duplicate_root_id,
                source="manual", job_url_snapshot=(version.snapshot or {}).get("job_url"), tailored_resume_id=args.tailored_resume_id, notes=args.notes, applied_at=utcnow())
            session.add(row)
        else:
            row.tailored_resume_id, row.notes = args.tailored_resume_id or row.tailored_resume_id, args.notes or row.notes
        session.flush(); print(row.id)
if __name__ == "__main__": main()
