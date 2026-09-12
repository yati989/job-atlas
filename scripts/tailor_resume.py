"""
Deterministic entrypoint for the ATS Resume Builder.

The *tailoring judgment* (which true bullets to surface, how to reword them, the
score, the gap split) is the agent's own reasoning via the `tailor-resumes`
skill — it cannot be done by a plain script without an LLM API call, which this
project deliberately avoids. This script is the deterministic other half:
- `--requirements` resolves a job (by id, or by URL — existence-checked first,
  inserted via the same path every connector uses if it's genuinely new) and
  dumps its decomposed requirements for the agent to reason over. If the job
  isn't enriched yet, it says so — run `/enrich-jobs` on it before tailoring.
- The render step takes an agent-produced tailored master and renders +
  persists it (`--job-id`) or just renders it (`--out`, no DB).

Examples
--------
# Render the base master (a quick sanity check that the toolchain works):
python -m scripts.tailor_resume --out output/base

# Resolve a job by link — inserts it into Postgres if it doesn't exist yet,
# reuses the existing row if it does. Prints its id + decomposed requirements
# (or a note that it needs /enrich-jobs first).
python -m scripts.tailor_resume --requirements --url "https://.../jobs/view/123" \
    --jd-text-file jd.txt --title "Data Scientist" --company "Acme"

# Same, by an already-known job id:
python -m scripts.tailor_resume --requirements --job-id 31

# Render + persist the tailored_resumes row for that job (same for a link-
# sourced job or any other — it's a real jobs row either way):
python -m scripts.tailor_resume --tailored t31.yaml --job-id 31 \
    --score 77 --gap-report gap31.json

# Ad-hoc preview only (no DB write at all) — rare; prefer --job-id above:
python -m scripts.tailor_resume --tailored my_tailored.yaml --out output/adhoc/acme
"""
import argparse
import json
import sys
from pathlib import Path

from app.resume.render import RenderError
from app.resume.schema import MASTER_PATH, ResumeMaster, load_master
from app.resume.tailor import (
    compact_job_requirements,
    find_job_by_url,
    get_or_create_adhoc_job,
    job_requirements,
    render_adhoc,
    save_tailored,
)


def _load_tailored(path: str | None) -> ResumeMaster:
    if not path:
        return load_master()  # default: the base master
    return load_master(path)  # same schema/validation as the master


def _load_from_plan(path: str) -> ResumeMaster:
    from app.resume.plan import ResumeTailoringPlan, apply_tailoring_plan
    plan = ResumeTailoringPlan.model_validate_json(Path(path).read_text(encoding="utf-8"))
    return apply_tailoring_plan(load_master(), plan)


def _dump_requirements(args) -> int:
    from app.db.session import get_session
    from app.models.orm import Job

    with get_session() as session:
        if args.url:
            # Existence check always runs first (find_job_by_url, inside
            # get_or_create_adhoc_job); only inserts a new jobs row if
            # nothing matches. jd_text is only required on a genuine miss.
            jd_text = None
            if args.jd_text_file:
                jd_text = Path(args.jd_text_file).read_text(encoding="utf-8")
            elif args.jd_text:
                jd_text = args.jd_text
            try:
                job = get_or_create_adhoc_job(
                    session, url=args.url, jd_text=jd_text,
                    title=args.title, company=args.company,
                )
            except ValueError as e:
                print(f"{e}", file=sys.stderr)
                print(
                    "No stored job matched this link, and no JD text was given to "
                    "insert one. Re-run with --jd-text or --jd-text-file (fetch the "
                    "link yourself, or paste it, if it's bot-blocked).",
                    file=sys.stderr,
                )
                return 2
            session.commit()
            print(f"# job id: {job.id}  (source={job.source}, "
                  f"external_job_id={job.external_job_id})", file=sys.stderr)
        else:
            job = session.get(Job, args.job_id)
            if job is None:
                print(f"No job with id {args.job_id}", file=sys.stderr)
                return 2

        if job.enrichment_status != "done":
            print(
                f"# NOTE: job {job.id} is not enriched yet (status="
                f"{job.enrichment_status!r}) — its job_skills/experience_*/"
                f"education_requirement will be empty/null below. Run the "
                f"/enrich-jobs skill on this job before tailoring against it.",
                file=sys.stderr,
            )
        if args.run_id:
            from app.decision_runs.resume_funnel import (
                approved_version_for_job, freeze_resume_tailoring_manifest,
                job_requirements_for_approved_version,
            )
            freeze_resume_tailoring_manifest(session, args.run_id)
            version = approved_version_for_job(session, args.run_id, job.id)
            requirements = job_requirements_for_approved_version(session, args.run_id, version.id)
        else:
            requirements = job_requirements(session, job)
        if args.compact:
            requirements = compact_job_requirements(requirements)
        print(json.dumps(requirements, separators=(",", ":") if args.compact else None,
                         indent=None if args.compact else 2, default=str))
    return 0


def _render(args) -> int:
    if args.job_id is not None:
        # DB path: render + persist the tailored_resumes row.
        if args.score is None and not args.drop_reason:
            print("--score is required when persisting a DB job (--job-id).", file=sys.stderr)
            return 2
        if args.drop_reason and not args.run_id:
            print("--drop-reason requires --run-id and --job-id.", file=sys.stderr)
            return 2
        if args.run_id and not args.drop_reason and not args.failure_disposition:
            print("approved-run rendering requires --failure-disposition non_blocking|excluded.", file=sys.stderr)
            return 2
        from app.db.session import get_session

        failure = None
        with get_session() as session:
            from app.models.orm import Job

            job = session.get(Job, args.job_id)
            if job is None:
                print(f"No job with id {args.job_id}", file=sys.stderr)
                return 2
            if args.run_id:
                # The optional attended path freezes/loads its own immutable
                # approved scope before rendering.  Do not let a CLI caller
                # attribute arbitrary backlog work to a Decision Run.
                from app.decision_runs.resume_funnel import freeze_resume_tailoring_manifest
                manifest = freeze_resume_tailoring_manifest(session, args.run_id)
                if job.id not in {item.job_id for item in manifest.items}:
                    print(f"Job {job.id} is outside approved Decision Run {args.run_id}", file=sys.stderr)
                    return 2
                from app.decision_runs.resume_funnel import approved_version_for_job
                approved_version = approved_version_for_job(session, args.run_id, job.id)
                if args.drop_reason:
                    from app.decision_runs.resume_funnel import report_resume_tailoring_drop
                    report_resume_tailoring_drop(
                        session, args.run_id, approved_version.id, reason=args.drop_reason,
                    )
                    print(f"dropped posting_version={approved_version.id}: {args.drop_reason}")
                    return 0
            try:
                tailored = _load_from_plan(args.tailoring_plan) if args.tailoring_plan else _load_tailored(args.tailored)
                gap_report = (
                    json.loads(Path(args.gap_report).read_text(encoding="utf-8"))
                    if args.gap_report else {}
                )
                row, result = save_tailored(
                    session, job_id=args.job_id, tailored=tailored,
                    score=args.score, gap_report=gap_report,
                    out_root=args.out_root, jd_snapshot=job.description_raw,
                    decision_run_id=args.run_id,
                )
            except (RenderError, ValueError, OSError) as exc:
                if not args.run_id:
                    raise
                from app.decision_runs.resume_funnel import report_resume_tailoring_failure
                reason = "render_failed" if isinstance(exc, RenderError) else "validation_failed"
                report_resume_tailoring_failure(
                    session, args.run_id, approved_version.id, reason=reason,
                    disposition=args.failure_disposition,
                )
                failure = (reason, exc)
                row = result = None
            if failure is not None:
                # Leave the session normally so the terminal progress record
                # commits; surface the original error after the transaction.
                pass
            else:
                # Read ORM attributes before the session closes (expire_on_commit).
                row_id, row_job_id, row_score = row.id, row.job_id, row.score
        if failure is not None:
            reason, exc = failure
            print(f"{reason.upper()}: {exc}", file=sys.stderr)
            return 1
        print(f"persisted tailored_resumes row id={row_id} job_id={row_job_id} score={row_score}")
        print(f"pdf: {result.pdf_path}  (round-trip ok: {result.ok})")
        return 0

    tailored = _load_from_plan(args.tailoring_plan) if args.tailoring_plan else _load_tailored(args.tailored)
    # Ad-hoc path: render + return, no persistence.
    out_dir = args.out or "output/adhoc"
    result = render_adhoc(tailored, out_dir=out_dir, basename=args.basename)
    print(f"pdf: {result.pdf_path}  (round-trip ok: {result.ok})")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render / persist an ATS-safe tailored resume.")
    p.add_argument("--tailored", help="Agent-produced tailored master YAML (defaults to the base master).")
    p.add_argument("--tailoring-plan", help="Compact agent-produced resume edit plan JSON.")
    p.add_argument("--out", help="Output dir for ad-hoc render (default output/adhoc).")
    p.add_argument("--basename", default="resume", help="Output file basename (default 'resume').")
    p.add_argument("--job-id", type=int, help="Persist for this DB job (tailored_resumes upsert).")
    p.add_argument("--score", type=float, help="Match score to persist (required with --job-id).")
    p.add_argument("--gap-report", help="Path to gap-report JSON to persist.")
    p.add_argument("--out-root", default="output/tailored_resumes", help="Root dir for persisted DB artifacts.")
    p.add_argument("--run-id", help="Optional approved Decision Run to update after this exact-manifest resume saves.")
    p.add_argument("--failure-disposition", choices=("non_blocking", "excluded"),
                   help="Required with --run-id rendering; terminal disposition if validation/rendering fails.")
    p.add_argument("--drop-reason", choices=("approval_revoked_for_posting",),
                   help="Exceptional approved-scope drop; only an explicit post-approval revocation.")
    p.add_argument("--requirements", action="store_true", help="Resolve/dump a job's decomposed requirements and exit.")
    p.add_argument("--compact", action="store_true", help="Emit a bounded reusable job brief instead of the full raw JD.")
    p.add_argument("--url", help="With --requirements: existence-check this URL first, insert a new job only if it's genuinely missing.")
    p.add_argument("--jd-text", help="With --url on a genuine miss: the JD text to insert (inline).")
    p.add_argument("--jd-text-file", help="With --url on a genuine miss: path to a file containing the JD text.")
    p.add_argument("--title", help="With --url on a genuine miss: job title for the new row.")
    p.add_argument("--company", help="With --url on a genuine miss: company name for the new row.")
    args = p.parse_args(argv)
    if args.tailored and args.tailoring_plan:
        p.error("--tailored and --tailoring-plan are mutually exclusive")

    try:
        if args.requirements:
            if not args.url and args.job_id is None:
                p.error("--requirements needs --job-id or --url")
            return _dump_requirements(args)
        return _render(args)
    except RenderError as e:
        print(f"RENDER FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
