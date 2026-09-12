"""Portable command-line entrypoint for the guided job-search pipeline."""

from __future__ import annotations

import argparse
import getpass
import json
import platform
from dataclasses import asdict
from pathlib import Path
import shutil
import sys
from sqlalchemy import select
import yaml

from app.models.orm import (
    GuidedRunPlan, GuidedSourceRun, Job, ProfiledSearchOutcome,
    PublicSelectionScope,
)
from app.workflows.connector_factory import build_fetchers
from app.workflows.guided_run import collect_guided_run, start_guided_run
from app.workflows.planning import ConfigurationInput, prepare_run
from app.workflows.profiles import ProfileStore, default_private_home
from app.workflows.public_database import initialize_public_database, public_session
from app.workflows.source_catalog import SOURCE_CATALOG


def _plan_payload(config: ConfigurationInput) -> dict[str, object]:
    preview = prepare_run(config, SOURCE_CATALOG)
    if preview.profile.include_internships and preview.profile.include_part_time:
        job_type_summary = (
            "Internships and part-time jobs are included because you explicitly requested them."
        )
    elif preview.profile.include_internships:
        job_type_summary = (
            "Part-time jobs are excluded. Internships are included because you explicitly "
            "requested them."
        )
    elif preview.profile.include_part_time:
        job_type_summary = (
            "Internships are excluded. Part-time jobs are included because you explicitly "
            "requested them."
        )
    else:
        job_type_summary = (
            "Internships and part-time jobs are excluded unless you explicitly ask to include them."
        )
    return {
        "profile": preview.profile.name,
        "selected_sources": [
            {
                "name": source.name,
                "runtime": source.runtime,
                "attendance_required": source.attendance_required,
                "prerequisites": list(source.prerequisites),
                "coverage": source.coverage,
                "query_instances": source.query_instance_count,
            }
            for source in preview.selected_sources
        ],
        "unsupported_sources": [
            asdict(source) for source in preview.unsupported_sources
        ],
        "source_count": preview.source_count,
        "query_instance_count": preview.query_instance_count,
        "collection_window_days": preview.profile.collection_window_days,
        "date_policy": {
            "source_search_window": "requested_where_supported",
            "known_older_jobs_returned_by_source": "kept_if_other_gates_pass",
            "unknown_posting_date": "kept_with_unknown_age",
        },
        "job_type_policy": {
            "summary": job_type_summary,
            "include_internships": preview.profile.include_internships,
            "include_part_time": preview.profile.include_part_time,
        },
        "requested_source_count": preview.requested_source_count,
        "source_shortfall": preview.source_shortfall,
        "requires_confirmation": preview.requires_confirmation,
    }


def _load_structured(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-atlas")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser(
        "setup", help="Initialize local storage and install the Job Atlas agent skills.",
    )
    setup.add_argument("--database-url")
    setup.add_argument("--private-home", type=Path, default=default_private_home())
    setup.add_argument("--skills-dir", type=Path)
    setup.add_argument("--force-skills", action="store_true")
    initialize = commands.add_parser(
        "init",
        help="Create tables in JOB_ATLAS_DATABASE_URL, or local SQLite if unset.",
    )
    initialize.add_argument("--database-url")
    initialize.add_argument("--private-home", type=Path, default=default_private_home())

    skills = commands.add_parser("skills", help="Install, inspect, or remove Codex skills.")
    skills_commands = skills.add_subparsers(dest="skills_command", required=True)
    for action in ("install", "status", "uninstall"):
        skill_action = skills_commands.add_parser(action)
        skill_action.add_argument("--skills-dir", type=Path)
        if action == "install":
            skill_action.add_argument("--force-skills", action="store_true")

    doctor = commands.add_parser(
        "doctor", help="Check the local runtime, storage, skills, and browser prerequisites.",
    )
    doctor.add_argument("--private-home", type=Path, default=default_private_home())
    doctor.add_argument("--skills-dir", type=Path)

    browser = commands.add_parser(
        "browser", help="Inspect or install the optional Chromium browser runtime.",
    )
    browser_commands = browser.add_subparsers(dest="browser_command", required=True)
    browser_commands.add_parser("status")
    browser_commands.add_parser("install")

    auth = commands.add_parser(
        "auth", help="Set up or inspect private provider authentication.",
    )
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_setup = auth_commands.add_parser(
        "setup", help="Store provider credentials for reuse without printing them.",
    )
    auth_setup.add_argument("--bright-data", action="store_true")
    auth_setup.add_argument("--google-client", type=Path)
    auth_setup.add_argument("--authorize-gmail", action="store_true")
    auth_setup.add_argument("--zone", default="serp_api1")
    auth_setup.add_argument("--private-home", type=Path, default=default_private_home())
    auth_status = auth_commands.add_parser(
        "status", help="Show which providers are configured without showing secrets.",
    )
    auth_status.add_argument("--private-home", type=Path, default=default_private_home())
    plan = commands.add_parser("plan", help="Validate configuration and preview exact work.")
    plan_input = plan.add_mutually_exclusive_group(required=True)
    plan_input.add_argument("--config", type=Path)
    plan_input.add_argument("--profile")
    plan.add_argument("--private-home", type=Path, default=default_private_home())

    accept = commands.add_parser(
        "accept-profile", help="Persist an explicitly accepted private profile revision."
    )
    accept.add_argument("--config", required=True, type=Path)
    accept.add_argument("--private-home", type=Path, default=default_private_home())
    accept.add_argument("--accept", action="store_true", required=True)

    start = commands.add_parser(
        "start-run", help="Freeze a reviewed profile/source plan without expanding it."
    )
    start.add_argument("--profile", required=True)
    start.add_argument("--private-home", type=Path, default=default_private_home())
    start.add_argument("--confirm", action="store_true", required=True)

    collect = commands.add_parser(
        "collect", help="Run or resume only the sources frozen for a guided run."
    )
    collect.add_argument("--run-id", required=True)
    collect.add_argument("--private-home", type=Path, default=default_private_home())
    collect.add_argument("--allow-attended", action="store_true")

    role_context = commands.add_parser(
        "role-review-context",
        help="Read compact titles or descriptions awaiting agent role judgment.",
    )
    role_context.add_argument("--run-id", required=True)
    role_context.add_argument(
        "--mode", choices=("titles", "details"), default="titles",
    )
    role_context.add_argument("--private-home", type=Path, default=default_private_home())

    role_apply = commands.add_parser(
        "apply-role-review",
        help="Validate and persist one complete agent role-judgment batch.",
    )
    role_apply.add_argument("--run-id", required=True)
    role_apply.add_argument("--input", required=True, type=Path)
    role_apply.add_argument("--private-home", type=Path, default=default_private_home())

    location_context = commands.add_parser(
        "location-review-context",
        help="Read relevant jobs whose remote-India eligibility needs evidence review.",
    )
    location_context.add_argument("--run-id", required=True)
    location_context.add_argument("--private-home", type=Path, default=default_private_home())

    location_apply = commands.add_parser(
        "apply-location-review",
        help="Persist a complete evidence-based location review batch.",
    )
    location_apply.add_argument("--run-id", required=True)
    location_apply.add_argument("--input", required=True, type=Path)
    location_apply.add_argument("--private-home", type=Path, default=default_private_home())

    status = commands.add_parser("status", help="Show durable source progress for a run.")
    status.add_argument("--run-id", required=True)
    status.add_argument("--private-home", type=Path, default=default_private_home())

    stage_status = commands.add_parser(
        "set-stage", help="Mark an optional public stage skipped or not requested.",
    )
    stage_status.add_argument("--run-id", required=True)
    stage_status.add_argument(
        "--stage", required=True,
        choices=("phase_b", "tailoring", "profile_links", "export"),
    )
    stage_status.add_argument(
        "--status", required=True, choices=("skipped", "not_requested"),
    )
    stage_status.add_argument("--confirm", action="store_true", required=True)
    stage_status.add_argument("--private-home", type=Path, default=default_private_home())

    interview = commands.add_parser(
        "interview-context",
        help="Read stored public job evidence for the mock-interview skill.",
    )
    interview_target = interview.add_mutually_exclusive_group(required=True)
    interview_target.add_argument("--job-id", type=int)
    interview_target.add_argument("--search")
    interview.add_argument("--limit", type=int, default=20)
    interview.add_argument("--private-home", type=Path, default=default_private_home())

    profile_plan = commands.add_parser(
        "profile-plan",
        help="Preview the existing contact-search plan for a links-only scope.",
    )
    profile_plan.add_argument("--scope-id", type=int, required=True)
    profile_plan.add_argument("--private-home", type=Path, default=default_private_home())

    profile_auth = commands.add_parser(
        "authorize-profile-search",
        help="Freeze a reviewed links-only query plan and global call ceiling.",
    )
    profile_auth.add_argument("--run-id", required=True)
    profile_auth.add_argument("--scope-id", type=int, required=True)
    profile_auth.add_argument("--maximum-calls", type=int, required=True)
    profile_auth.add_argument("--confirm", action="store_true", required=True)
    profile_auth.add_argument("--private-home", type=Path, default=default_private_home())

    phase_context = commands.add_parser(
        "phase-a-context", help="Read exact-run job and company evidence inputs.",
    )
    phase_context.add_argument("--run-id", required=True)
    phase_context.add_argument("--private-home", type=Path, default=default_private_home())

    phase_apply = commands.add_parser(
        "apply-phase-a", help="Validate and persist reviewed Phase A results.",
    )
    phase_apply.add_argument("--run-id", required=True)
    phase_apply.add_argument("--input", required=True, type=Path)
    phase_apply.add_argument("--private-home", type=Path, default=default_private_home())

    phase_b_plan = commands.add_parser(
        "phase-b-plan", help="Preview optional market-evidence calls for one scope.",
    )
    phase_b_plan.add_argument("--scope-id", required=True, type=int)
    phase_b_plan.add_argument(
        "--source", required=True, action="append",
        choices=("glassdoor", "ambitionbox"),
    )
    phase_b_plan.add_argument("--private-home", type=Path, default=default_private_home())

    phase_b_auth = commands.add_parser(
        "authorize-phase-b", help="Freeze Phase B plan and global provider-call ceiling.",
    )
    phase_b_auth.add_argument("--scope-id", required=True, type=int)
    phase_b_auth.add_argument(
        "--source", required=True, action="append",
        choices=("glassdoor", "ambitionbox"),
    )
    phase_b_auth.add_argument("--maximum-calls", required=True, type=int)
    phase_b_auth.add_argument("--confirm", action="store_true", required=True)
    phase_b_auth.add_argument("--private-home", type=Path, default=default_private_home())

    phase_b_reserve = commands.add_parser(
        "reserve-phase-b", help="Durably reserve one authorized call before provider I/O.",
    )
    phase_b_reserve.add_argument("--authorization-id", required=True, type=int)
    phase_b_reserve.add_argument("--company-id", required=True, type=int)
    phase_b_reserve.add_argument(
        "--source", required=True, choices=("glassdoor", "ambitionbox"),
    )
    phase_b_reserve.add_argument("--confirm", action="store_true", required=True)
    phase_b_reserve.add_argument("--private-home", type=Path, default=default_private_home())

    phase_b_apply = commands.add_parser(
        "apply-phase-b", help="Persist one reviewed terminal Phase B result.",
    )
    phase_b_apply.add_argument("--call-id", required=True, type=int)
    phase_b_apply.add_argument("--input", required=True, type=Path)
    phase_b_apply.add_argument("--private-home", type=Path, default=default_private_home())

    phase_b_glassdoor = commands.add_parser(
        "run-glassdoor-phase-b",
        help="Run one reviewed Glassdoor identity artifact as a resumable bulk snapshot.",
    )
    phase_b_glassdoor.add_argument("--run-id", required=True)
    phase_b_glassdoor.add_argument("--authorization-id", required=True, type=int)
    phase_b_glassdoor.add_argument("--reviewed-artifact", required=True, type=Path)
    phase_b_glassdoor.add_argument("--confirm", action="store_true", required=True)
    phase_b_glassdoor.add_argument("--private-home", type=Path, default=default_private_home())

    selection_preview = commands.add_parser(
        "selection-preview", help="Preview a validated saved filter against one run.",
    )
    selection_preview.add_argument("--run-id", required=True)
    selection_preview.add_argument("--config", required=True, type=Path)
    selection_preview.add_argument("--private-home", type=Path, default=default_private_home())

    selection_freeze = commands.add_parser(
        "freeze-selection", help="Freeze an all, manual, or filtered exact scope.",
    )
    selection_freeze.add_argument("--run-id", required=True)
    selection_freeze.add_argument(
        "--mode", required=True, choices=("all_eligible", "manual", "filtered"),
    )
    selection_freeze.add_argument("--config", type=Path)
    selection_freeze.add_argument("--posting-version-id", type=int, action="append", default=[])
    selection_freeze.add_argument(
        "--phase-b-research", action="store_true",
        help="Create an all-eligible Phase B research denominator, not a final selection.",
    )
    selection_freeze.add_argument("--confirm", action="store_true", required=True)
    selection_freeze.add_argument("--private-home", type=Path, default=default_private_home())

    tailoring = commands.add_parser(
        "tailoring-context", help="Read exact selected jobs and reusable resume status.",
    )
    tailoring.add_argument("--scope-id", required=True, type=int)
    tailoring.add_argument("--private-home", type=Path, default=default_private_home())

    save_resume = commands.add_parser(
        "save-tailored", help="Render and persist one reviewed exact-scope tailored resume.",
    )
    save_resume.add_argument("--scope-id", required=True, type=int)
    save_resume.add_argument("--posting-version-id", required=True, type=int)
    resume_input = save_resume.add_mutually_exclusive_group(required=True)
    resume_input.add_argument("--tailored", type=Path)
    resume_input.add_argument("--tailoring-plan", type=Path)
    save_resume.add_argument("--master", type=Path)
    save_resume.add_argument("--score", required=True, type=float)
    save_resume.add_argument("--gap-report", required=True, type=Path)
    save_resume.add_argument("--out-root", type=Path)
    save_resume.add_argument("--private-home", type=Path, default=default_private_home())

    export = commands.add_parser(
        "export", help="Create a private local Excel workbook for one run.",
    )
    export.add_argument("--run-id", required=True)
    export.add_argument("--scope-id", required=True, type=int)
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--private-home", type=Path, default=default_private_home())

    workbook_shortlist = commands.add_parser(
        "apply-workbook-shortlist",
        help="Freeze Approved rows from an exported Shortlist sheet.",
    )
    workbook_shortlist.add_argument("--run-id", required=True)
    workbook_shortlist.add_argument("--scope-id", required=True, type=int)
    workbook_shortlist.add_argument("--input", required=True, type=Path)
    workbook_shortlist.add_argument("--confirm", action="store_true", required=True)
    workbook_shortlist.add_argument(
        "--private-home", type=Path, default=default_private_home(),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "setup":
        from app.config.authentication import authentication_summary
        from app.packaging.skills import install_bundled_skills

        database = initialize_public_database(
            args.private_home, database_url=args.database_url,
        )
        installed_skills = install_bundled_skills(
            args.skills_dir, force=args.force_skills,
        )
        print(json.dumps({
            "status": "ready",
            "product": "Job Atlas",
            "private_home": str(args.private_home),
            "database": database,
            "skills": installed_skills,
            "authentication": authentication_summary(args.private_home),
            "next_steps": [
                "Restart your coding agent so it discovers the installed skills.",
                "Ask: $full-pipeline Find jobs for <role> in <city> or remote.",
                "Run `job-atlas-dashboard` to browse saved results.",
            ],
        }, indent=2))
        return 0
    if args.command == "init":
        from app.config.authentication import authentication_summary

        result = initialize_public_database(
            args.private_home, database_url=args.database_url,
        )
        print(json.dumps({
            "status": "ready",
            **result,
            "authentication": authentication_summary(args.private_home),
        }, indent=2))
        return 0
    if args.command == "skills":
        from app.packaging.skills import (
            bundled_skill_status,
            install_bundled_skills,
            uninstall_bundled_skills,
        )

        if args.skills_command == "install":
            result = install_bundled_skills(
                args.skills_dir, force=args.force_skills,
            )
        elif args.skills_command == "uninstall":
            result = uninstall_bundled_skills(args.skills_dir)
        else:
            result = bundled_skill_status(args.skills_dir)
        print(json.dumps({"skills": result}, indent=2))
        return 0
    if args.command == "doctor":
        from app.config.authentication import credential_status
        from app.packaging.browser import chromium_status
        from app.packaging.skills import bundled_skill_status
        from app.workflows.public_database import database_path

        browser_commands = {
            name: shutil.which(name)
            for name in ("google-chrome", "chrome", "chromium", "chromium-browser")
        }
        print(json.dumps({
            "product": "Job Atlas",
            "python": {
                "version": platform.python_version(),
                "supported": sys.version_info >= (3, 11),
            },
            "private_home": str(args.private_home),
            "database": {
                "path": str(database_path(args.private_home)),
                "exists": database_path(args.private_home).exists(),
            },
            "skills": bundled_skill_status(args.skills_dir),
            "authentication": credential_status(args.private_home),
            "system_browsers": browser_commands,
            "playwright_chromium": chromium_status(),
            "browser_note": (
                "Most sources use direct web requests. Attended sources may require "
                "Chrome or Chromium and a visible desktop session."
            ),
        }, indent=2))
        return 0
    if args.command == "browser":
        from app.packaging.browser import chromium_status, install_chromium

        result = (
            install_chromium()
            if args.browser_command == "install"
            else chromium_status()
        )
        print(json.dumps({"chromium": result}, indent=2))
        return 0
    if args.command == "auth":
        from app.config.authentication import (
            credential_status,
            save_bright_data_credentials,
            save_google_oauth_client,
        )

        if args.auth_command == "status":
            print(json.dumps(credential_status(args.private_home), indent=2))
            return 0

        interactive_all = not (
            args.bright_data or args.google_client or args.authorize_gmail
        )
        if args.bright_data or interactive_all:
            api_key = getpass.getpass(
                "Bright Data API key (leave blank to skip): "
            ).strip()
            if api_key:
                secondary = getpass.getpass(
                    "Secondary Bright Data API key (optional): "
                ).strip()
                save_bright_data_credentials(
                    args.private_home,
                    api_key=api_key,
                    secondary_api_key=secondary,
                    zone=args.zone,
                )
            elif args.bright_data:
                raise SystemExit("Bright Data API key cannot be empty")
        google_client = args.google_client
        if interactive_all:
            entered = input(
                "Google OAuth client JSON path (leave blank to skip): "
            ).strip()
            google_client = Path(entered).expanduser() if entered else None
        if google_client is not None:
            save_google_oauth_client(args.private_home, google_client)
        authorize_gmail = args.authorize_gmail
        if interactive_all and google_client is not None:
            authorize_gmail = input(
                "Open the browser to authorize Gmail drafts now? [Y/n]: "
            ).strip().lower() not in {"n", "no"}
        if authorize_gmail:
            from app.outreach.gmail import get_credentials

            get_credentials(force_reauth=True, private_home=args.private_home)
        print(json.dumps(credential_status(args.private_home), indent=2))
        return 0
    if args.command == "plan":
        configuration = (
            ProfileStore(args.private_home).load(args.profile)
            if args.profile
            else args.config
        )
        print(json.dumps(_plan_payload(configuration), indent=2))
        return 0
    if args.command == "accept-profile":
        accepted = ProfileStore(args.private_home).accept(args.config, SOURCE_CATALOG)
        print(
            json.dumps(
                {
                    "profile": accepted.profile.name,
                    "revision": accepted.revision,
                    "fingerprint": accepted.fingerprint,
                    "current_path": str(accepted.current_path),
                    "snapshot_path": str(accepted.snapshot_path),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "start-run":
        store = ProfileStore(args.private_home)
        profile = store.load(args.profile)
        accepted = store.accept(profile, SOURCE_CATALOG)
        preview = prepare_run(profile, SOURCE_CATALOG)
        with public_session(args.private_home) as session:
            started = start_guided_run(
                session,
                preview=preview,
                profile_fingerprint=accepted.fingerprint,
                confirmed=args.confirm,
            )
        print(json.dumps(asdict(started), indent=2))
        return 0
    if args.command == "collect":
        with public_session(args.private_home) as session:
            frozen = session.scalar(
                select(GuidedRunPlan).where(GuidedRunPlan.run_id == args.run_id)
            )
            if frozen is None:
                raise SystemExit(f"unknown guided run: {args.run_id}")
            preview = prepare_run(frozen.profile_snapshot, SOURCE_CATALOG)
            # Compare the same JSON-compatible representation that is loaded
            # from the database. JSON stores tuples such as prerequisites as
            # lists, even though dataclasses.asdict() preserves the tuple.
            reproduced_source_plan = json.loads(json.dumps(
                [asdict(item) for item in preview.selected_sources]
            ))
            if (
                [item.name for item in preview.selected_sources] != frozen.selected_sources
                or reproduced_source_plan != frozen.source_plan
                or preview.source_count != frozen.source_count
                or preview.query_instance_count != frozen.query_instance_count
            ):
                raise RuntimeError("maintained source catalog no longer reproduces the frozen plan")
            result = collect_guided_run(
                session,
                run_id=args.run_id,
                fetchers=build_fetchers(preview, allow_attended=args.allow_attended),
                semantic_role_review=True,
            )
        print(json.dumps(asdict(result), indent=2))
        return 0
    if args.command in {"role-review-context", "apply-role-review"}:
        from app.workflows.public_role_review import (
            apply_role_review,
            role_review_context,
        )

        with public_session(args.private_home) as session:
            if args.command == "role-review-context":
                payload = role_review_context(session, args.run_id, args.mode)
            else:
                payload = apply_role_review(
                    session, args.run_id, _load_structured(args.input),
                )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command in {"location-review-context", "apply-location-review"}:
        from app.workflows.public_location_review import (
            apply_location_review,
            location_review_context,
        )

        with public_session(args.private_home) as session:
            payload = (
                location_review_context(session, args.run_id)
                if args.command == "location-review-context"
                else apply_location_review(
                    session, args.run_id, _load_structured(args.input),
                )
            )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command == "status":
        with public_session(args.private_home) as session:
            rows = list(session.scalars(
                select(GuidedSourceRun)
                .where(GuidedSourceRun.run_id == args.run_id)
                .order_by(GuidedSourceRun.source)
            ))
            if not rows:
                raise SystemExit(f"unknown guided run: {args.run_id}")
            payload = {
                "run_id": args.run_id,
                "sources": [
                    {
                        "source": row.source,
                        "status": row.status,
                        "attempt_count": row.attempt_count,
                        "outcome_counts": row.outcome_counts,
                        "failure_detail": row.failure_detail,
                    }
                    for row in rows
                ],
            }
            from app.workflows.public_progress import public_stage_snapshot
            payload["stages"] = public_stage_snapshot(session, args.run_id)
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command == "set-stage":
        from app.workflows.public_progress import record_public_stage

        with public_session(args.private_home) as session:
            row = record_public_stage(
                session, args.run_id, args.stage, args.status,
            )
            payload = {"run_id": args.run_id, "stage": row.name, "status": row.status}
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "interview-context":
        from app.workflows.mock_interview import find_interview_jobs, load_interview_context

        with public_session(args.private_home) as session:
            payload = (
                load_interview_context(session, args.job_id)
                if args.job_id is not None
                else {"matches": find_interview_jobs(
                    session, args.search, limit=args.limit,
                )}
            )
        print(json.dumps(payload, indent=2))
        return 0
    if args.command in {"profile-plan", "authorize-profile-search"}:
        from app.contacts.agentic_batch import contexts_for_public_scope
        from app.contacts.profile_query_budget import (
            authorize_profile_queries,
            preview_profile_queries,
        )

        with public_session(args.private_home) as session:
            scope = session.get(PublicSelectionScope, args.scope_id)
            if scope is None:
                raise SystemExit(f"unknown public selection scope: {args.scope_id}")
            contexts = contexts_for_public_scope(session, scope)
            preview = preview_profile_queries(
                contexts, session=session, run_id=scope.run_id,
            )
            from app.models.orm import LinkedInProfileLink, ProfileDiscoveryAuthorization
            from sqlalchemy import func
            reusable_profile_count = session.scalar(
                select(func.count(LinkedInProfileLink.id))
                .join(
                    ProfileDiscoveryAuthorization,
                    LinkedInProfileLink.authorization_id
                    == ProfileDiscoveryAuthorization.id,
                )
                .where(
                    ProfileDiscoveryAuthorization.run_id == scope.run_id,
                    LinkedInProfileLink.company_id.in_(
                        [context.company_id for context in contexts]
                    ),
                )
            ) or 0
            if args.command == "profile-plan":
                from app.workflows.public_progress import record_public_stage
                record_public_stage(
                    session, scope.run_id, "profile_links", "awaiting_confirmation",
                    expected_count=preview.planned_query_count,
                    detail={"reused_query_count": preview.reused_query_count},
                )
                payload = {
                    "scope_id": scope.id,
                    "company_count": preview.company_count,
                    "initial_query_count": preview.initial_query_count,
                    "retry_allowance": preview.retry_allowance,
                    "planned_query_count": preview.planned_query_count,
                    "reused_query_count": preview.reused_query_count,
                    "reusable_profile_count": reusable_profile_count,
                    "queries": [query.as_dict() for query in preview.queries],
                }
            else:
                authorization = authorize_profile_queries(
                    session,
                    run_id=args.run_id,
                    scope_id=scope.id,
                    contexts=contexts,
                    maximum_calls=args.maximum_calls,
                )
                from app.workflows.public_progress import record_public_stage
                record_public_stage(
                    session, scope.run_id, "profile_links", "running",
                    expected_count=authorization.planned_query_count,
                    detail={
                        "maximum_calls": authorization.maximum_calls,
                        "reused_query_count": preview.reused_query_count,
                    },
                )
                payload = {
                    "authorization_id": authorization.id,
                    "scope_id": scope.id,
                    "company_count": preview.company_count,
                    "initial_query_count": preview.initial_query_count,
                    "retry_allowance": authorization.retry_allowance,
                    "planned_query_count": authorization.planned_query_count,
                    "maximum_calls": authorization.maximum_calls,
                    "reused_query_count": preview.reused_query_count,
                    "reusable_profile_count": reusable_profile_count,
                }
        print(json.dumps(payload, indent=2))
        return 0
    if args.command in {"phase-a-context", "apply-phase-a"}:
        from app.workflows.public_phase_a import apply_phase_a_results, phase_a_context

        with public_session(args.private_home) as session:
            from app.workflows.public_progress import record_public_stage
            if args.command == "phase-a-context":
                payload = phase_a_context(session, args.run_id)
                pending = len(payload["jobs"]) + len(payload["companies"])
                reused = len(payload["reused_jobs"]) + len(payload["reused_companies"])
                record_public_stage(
                    session, args.run_id, "phase_a", "running",
                    expected_count=pending + reused, completed_count=reused,
                    detail={"pending": pending, "reused": reused},
                )
            else:
                payload = apply_phase_a_results(
                    session, args.run_id, _load_structured(args.input),
                )
                final = phase_a_context(session, args.run_id)
                completed = len(final["reused_jobs"]) + len(final["reused_companies"])
                record_public_stage(
                    session, args.run_id, "phase_a", "completed",
                    expected_count=completed, completed_count=completed,
                )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command in {
        "phase-b-plan", "authorize-phase-b", "reserve-phase-b", "apply-phase-b",
    }:
        from app.models.orm import PublicPhaseBAuthorization, PublicPhaseBCall
        from app.workflows.public_phase_b import (
            authorize_phase_b, complete_phase_b_call, preview_phase_b,
            reserve_phase_b_call,
        )
        from app.workflows.public_progress import record_public_stage

        with public_session(args.private_home) as session:
            if args.command in {"phase-b-plan", "authorize-phase-b"}:
                scope = session.get(PublicSelectionScope, args.scope_id)
                if scope is None:
                    raise SystemExit(f"unknown public selection scope: {args.scope_id}")
                preview = preview_phase_b(
                    session, scope_id=scope.id, sources=args.source,
                )
                if args.command == "phase-b-plan":
                    record_public_stage(
                        session, scope.run_id, "phase_b", "awaiting_confirmation",
                        expected_count=preview.planned_call_count,
                        detail={"reused": preview.reusable_evidence_count},
                    )
                    payload = {
                        "scope_id": scope.id,
                        "company_count": preview.company_count,
                        "source_count": preview.source_count,
                        "planned_call_count": preview.planned_call_count,
                        "reusable_evidence_count": preview.reusable_evidence_count,
                        "items": [item.as_dict() for item in preview.items],
                    }
                else:
                    authorization = authorize_phase_b(
                        session, scope_id=scope.id, sources=args.source,
                        maximum_calls=args.maximum_calls,
                    )
                    record_public_stage(
                        session, scope.run_id, "phase_b", "running",
                        expected_count=authorization.planned_call_count,
                        detail={"maximum_calls": authorization.maximum_calls},
                    )
                    payload = {
                        "authorization_id": authorization.id,
                        "scope_id": scope.id,
                        "planned_call_count": authorization.planned_call_count,
                        "maximum_calls": authorization.maximum_calls,
                    }
            elif args.command == "reserve-phase-b":
                reservation = reserve_phase_b_call(
                    session, authorization_id=args.authorization_id,
                    company_id=args.company_id, source=args.source,
                )
                payload = {
                    "call_id": reservation.call.id,
                    "created": reservation.created,
                    "status": reservation.call.status,
                }
            else:
                call = complete_phase_b_call(
                    session, call_id=args.call_id,
                    result=_load_structured(args.input),
                )
                authorization = session.get(PublicPhaseBAuthorization, call.authorization_id)
                scope = session.get(PublicSelectionScope, authorization.scope_id)
                calls = list(session.scalars(select(PublicPhaseBCall).where(
                    PublicPhaseBCall.authorization_id == authorization.id,
                )))
                terminal = [item for item in calls if item.status != "reserved"]
                completed = sum(item.status in {"succeeded", "missing"} for item in terminal)
                failed = sum(item.status in {"failed", "ambiguous"} for item in terminal)
                if len(terminal) == authorization.planned_call_count and failed == 0:
                    status = "completed"
                elif len(calls) >= authorization.maximum_calls or failed:
                    status = "partial"
                else:
                    status = "running"
                record_public_stage(
                    session, scope.run_id, "phase_b", status,
                    expected_count=authorization.planned_call_count,
                    completed_count=completed, failed_count=failed,
                    detail={"maximum_calls": authorization.maximum_calls},
                )
                payload = {
                    "call_id": call.id, "status": call.status,
                    "stage_status": status,
                }
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command == "run-glassdoor-phase-b":
        from functools import partial

        from app.workflows.public_glassdoor_batch import run_reviewed_glassdoor_batch

        checkpoint_path = (
            args.private_home / "runs" / args.run_id / "phase-b-glassdoor-checkpoint.json"
        )
        payload = run_reviewed_glassdoor_batch(
            run_id=args.run_id,
            authorization_id=args.authorization_id,
            reviewed_path=args.reviewed_artifact,
            checkpoint_path=checkpoint_path,
            session_context=partial(public_session, args.private_home),
        )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command == "selection-preview":
        from app.workflows.selection import preview_filtered_selection

        with public_session(args.private_home) as session:
            preview = preview_filtered_selection(
                session,
                run_id=args.run_id,
                configuration=_load_structured(args.config),
            )
            from app.workflows.public_progress import record_public_stage
            record_public_stage(
                session, args.run_id, "selection", "awaiting_confirmation",
                expected_count=len(preview.items),
                detail={
                    "retained": preview.retained_job_count,
                    "unresolved": preview.unresolved_job_count,
                },
            )
            payload = {
                "run_id": args.run_id,
                "retained_jobs": preview.retained_job_count,
                "retained_companies": preview.retained_company_count,
                "excluded_jobs": preview.excluded_job_count,
                "unresolved_jobs": preview.unresolved_job_count,
                "items": [asdict(item) for item in preview.items],
            }
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command == "freeze-selection":
        from app.workflows.selection import (
            freeze_filtered_selection_scope, freeze_selection_scope,
        )

        with public_session(args.private_home) as session:
            purpose = "phase_b_research" if args.phase_b_research else "selection"
            if args.phase_b_research and args.mode != "all_eligible":
                raise SystemExit("--phase-b-research requires --mode all_eligible")
            if args.mode == "filtered":
                if args.config is None or args.posting_version_id:
                    raise SystemExit("filtered selection requires --config and no manual IDs")
                scope = freeze_filtered_selection_scope(
                    session,
                    run_id=args.run_id,
                    configuration=_load_structured(args.config),
                )
            else:
                if args.config is not None:
                    raise SystemExit("--config is only valid for filtered selection")
                kept = list(session.execute(
                    select(ProfiledSearchOutcome.posting_version_id, Job.company_id)
                    .join(Job, ProfiledSearchOutcome.job_id == Job.id)
                    .where(
                        ProfiledSearchOutcome.run_id == args.run_id,
                        ProfiledSearchOutcome.outcome == "kept",
                        Job.duplicate_of_job_id.is_(None),
                    )
                ))
                available = {posting_id: company_id for posting_id, company_id in kept}
                ids = tuple(available) if args.mode == "all_eligible" else tuple(args.posting_version_id)
                if args.mode == "manual" and not ids:
                    raise SystemExit("manual selection requires --posting-version-id")
                scope = freeze_selection_scope(
                    session,
                    run_id=args.run_id,
                    mode=args.mode,
                    posting_version_ids=ids,
                    company_ids={posting_id: available.get(posting_id) for posting_id in ids},
                    purpose=purpose,
                )
            payload = {
                "scope_id": scope.id,
                "revision": scope.revision,
                "mode": scope.mode,
                "purpose": scope.purpose,
                "job_count": scope.job_count,
                "company_count": scope.company_count,
            }
            if scope.purpose == "selection":
                from app.workflows.public_progress import record_public_stage
                record_public_stage(
                    session, args.run_id, "selection", "completed",
                    expected_count=scope.job_count, completed_count=scope.job_count,
                    detail={"scope_id": scope.id, "revision": scope.revision},
                )
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "tailoring-context":
        from app.workflows.public_tailoring import tailoring_context

        with public_session(args.private_home) as session:
            payload = tailoring_context(session, args.scope_id)
            scope = session.get(PublicSelectionScope, args.scope_id)
            if scope is None:
                raise SystemExit(f"unknown public selection scope: {args.scope_id}")
            from app.workflows.public_progress import record_public_stage
            record_public_stage(
                session, scope.run_id, "tailoring", "running",
                expected_count=len(payload["items"]), completed_count=len(payload["reused"]),
                detail={"scope_id": scope.id},
            )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if args.command == "apply-workbook-shortlist":
        from app.workflows.public_progress import record_public_stage
        from app.workflows.public_workbook_shortlist import apply_workbook_shortlist

        with public_session(args.private_home) as session:
            scope = apply_workbook_shortlist(
                session, run_id=args.run_id, scope_id=args.scope_id,
                workbook_path=args.input,
            )
            record_public_stage(
                session, args.run_id, "selection", "completed",
                expected_count=scope.job_count, completed_count=scope.job_count,
                detail={
                    "scope_id": scope.id, "revision": scope.revision,
                    "source": "workbook_shortlist",
                },
            )
            payload = {
                "scope_id": scope.id,
                "revision": scope.revision,
                "approved_job_count": scope.job_count,
                "company_count": scope.company_count,
            }
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "save-tailored":
        from app.resume.plan import ResumeTailoringPlan, apply_tailoring_plan
        from app.resume.schema import load_master
        from app.workflows.public_tailoring import save_public_tailored

        if args.tailoring_plan:
            plan = ResumeTailoringPlan.model_validate_json(
                args.tailoring_plan.read_text(encoding="utf-8")
            )
            tailored = apply_tailoring_plan(load_master(args.master), plan)
        else:
            tailored = load_master(args.tailored)
        gap_report = json.loads(args.gap_report.read_text(encoding="utf-8"))
        out_root = args.out_root or args.private_home / "resume-artifacts"
        with public_session(args.private_home) as session:
            row, rendered = save_public_tailored(
                session,
                scope_id=args.scope_id,
                posting_version_id=args.posting_version_id,
                tailored=tailored,
                score=args.score,
                gap_report=gap_report,
                out_root=out_root,
            )
            scope = session.get(PublicSelectionScope, args.scope_id)
            from app.models.orm import PublicTailoredResume, PublicSelectionScopeItem
            from sqlalchemy import func
            completed = session.scalar(
                select(func.count(PublicTailoredResume.id))
                .join(
                    PublicSelectionScopeItem,
                    PublicTailoredResume.posting_version_id
                    == PublicSelectionScopeItem.posting_version_id,
                )
                .where(PublicSelectionScopeItem.scope_id == args.scope_id)
            ) or 0
            from app.workflows.public_progress import record_public_stage
            record_public_stage(
                session, scope.run_id, "tailoring",
                "completed" if completed == scope.job_count else "partial",
                expected_count=scope.job_count, completed_count=completed,
                detail={"scope_id": scope.id},
            )
            payload = {
                "resume_id": row.id,
                "posting_version_id": row.posting_version_id,
                "score": row.score,
                "pdf": str(rendered.pdf_path),
                "round_trip_ok": rendered.ok,
            }
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "export":
        from app.reporting.public_export import export_public_workbook

        with public_session(args.private_home) as session:
            path = export_public_workbook(
                session, args.run_id, args.scope_id, args.out,
            )
            from app.workflows.public_progress import record_public_stage
            record_public_stage(
                session, args.run_id, "export", "completed",
                expected_count=1, completed_count=1,
                detail={"scope_id": args.scope_id, "workbook": str(path)},
            )
        print(json.dumps({"run_id": args.run_id, "workbook": str(path)}, indent=2))
        return 0
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
