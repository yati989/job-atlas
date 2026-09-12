"""
The seam between the `/find-prospects` skill and the database.

The skill does the searching and the judging in-session; this CLI is how it
writes anything down. Keeping persistence behind a CLI (rather than letting
the skill emit SQL) is what makes the budget cap, the dedup key and the
"qualified rows must carry evidence" rule enforceable instead of advisory —
the same reason `app/contacts/brightdata_query.py` exists.

Usage:
    python -m app.prospects.cli thesis-next
    python -m app.prospects.cli progress
    python -m app.prospects.cli dedup --thesis <slug> --names-file <path>
    python -m app.prospects.cli record --thesis <slug> --name "Acme" \
        --source-query "..." --source-url "https://..."
    python -m app.prospects.cli spend --name "Acme"
    python -m app.prospects.cli qualify --name "Acme" --band 1 \
        --function-evidence "..." --india-signal "..." [--industry ... --hq ...
        --remote-posture remote_first --domain acme.com --website ... --linkedin ...]
    python -m app.prospects.cli disqualify --name "Acme" --reason no_function
    python -m app.prospects.cli evidence --thesis <slug>
    python -m app.prospects.cli report --thesis <slug>
"""
import argparse
import sys

from sqlalchemy import select

from app.companies.naming import normalize
from app.config.prospect_theses import TARGET_QUALIFIED_PER_THESIS
from app.db.session import get_session
from app.models.orm import Prospect
from app.prospects import batch


def _safe_print(line: str) -> None:
    """Company names harvested off directory pages carry arbitrary Unicode
    (smart quotes, em dashes, non-Latin scripts) that a plain `print()`
    crashes on under Windows' cp1252 console encoding. Confirmed live in
    app/contacts/brightdata_query.py, mid-batch, where it lost the rest of an
    already-billed call's results to an uncaught UnicodeEncodeError. Display
    concern, not a data-integrity one — replace what the console can't
    render rather than raising."""
    encoding = sys.stdout.encoding or "utf-8"
    print(line.encode(encoding, errors="replace").decode(encoding))


def _load(session, name: str) -> Prospect:
    norm = normalize(name)
    prospect = session.execute(
        select(Prospect).where(Prospect.normalized_name == norm)
    ).scalar_one_or_none()
    if prospect is None:
        raise SystemExit(f"no prospect matching {name!r} (normalized: {norm!r}) — `record` it first")
    return prospect


def _cmd_thesis_next(args) -> None:
    with get_session() as session:
        thesis = batch.next_thesis(session)
        if thesis is None:
            _safe_print(
                "Every checked-in thesis has hit its qualified target. "
                "Extending the list in app/config/prospect_theses.py is a "
                "decision for the user, not something to improvise."
            )
            return
        done = batch.qualified_count(session, thesis.slug)
        _safe_print(f"thesis : {thesis.slug}")
        _safe_print(f"label  : {thesis.label}")
        _safe_print(f"progress: {done}/{TARGET_QUALIFIED_PER_THESIS} qualified")
        _safe_print(f"why    : {thesis.rationale}")
        _safe_print("seed queries (starting points — adapt them if one returns thin):")
        for q in thesis.seed_queries:
            _safe_print(f"  - {q}")


def _cmd_progress(args) -> None:
    with get_session() as session:
        for thesis, done in batch.thesis_progress(session):
            marker = "DONE" if done >= TARGET_QUALIFIED_PER_THESIS else "    "
            _safe_print(f"  [{marker}] {done:3d}/{TARGET_QUALIFIED_PER_THESIS}  {thesis.slug:34s} {thesis.label}")


def _cmd_dedup(args) -> None:
    names = [
        line.strip()
        for line in open(args.names_file, encoding="utf-8").read().splitlines()
        if line.strip()
    ]
    with get_session() as session:
        result = batch.dedup_candidates(session, names)
        _safe_print(f"{len(names)} harvested -> {result.summary()}")
        _safe_print("")
        _safe_print("NEW (worth qualification budget):")
        for n in result.new:
            _safe_print(f"  {n}")
        for label, bucket in (
            ("already in companies (we already scrape jobs here)", result.already_in_companies),
            ("already a prospect (researched under an earlier thesis)", result.already_a_prospect),
            ("placeholder / unusable", result.placeholder),
        ):
            if bucket:
                _safe_print("")
                _safe_print(f"{label}:")
                for n in bucket:
                    _safe_print(f"  {n}")

        if args.record_new:
            for n in result.new:
                batch.record_candidate(
                    session, n, thesis=args.thesis,
                    source_query=args.source_query, source_url=args.source_url,
                )
            written = batch.record_excluded(
                session, result.already_in_companies,
                thesis=args.thesis, reason=batch.REASON_ALREADY_IN_COMPANIES,
            )
            session.commit()
            _safe_print("")
            _safe_print(
                f"recorded {len(result.new)} candidates + {written} already-in-companies "
                f"exclusions under thesis {args.thesis!r}"
            )


def _cmd_record(args) -> None:
    with get_session() as session:
        prospect = batch.record_candidate(
            session, args.name, thesis=args.thesis,
            source_query=args.source_query, source_url=args.source_url,
        )
        session.commit()
        _safe_print(f"{prospect!r}")


def _cmd_spend(args) -> None:
    with get_session() as session:
        prospect = _load(session, args.name)
        try:
            spent = batch.spend_search(session, prospect)
        except batch.BudgetExceeded as exc:
            session.commit()
            _safe_print(f"BUDGET EXCEEDED: {exc}")
            raise SystemExit(2)
        session.commit()
        _safe_print(f"{prospect.name}: {spent}/{batch.MAX_SEARCHES_PER_CANDIDATE} searches spent")


def _cmd_qualify(args) -> None:
    with get_session() as session:
        prospect = _load(session, args.name)
        try:
            batch.qualify(
                session, prospect,
                rank_band=args.band,
                function_evidence=args.function_evidence,
                india_signal=args.india_signal,
                industry=args.industry,
                hq_location=args.hq,
                remote_posture=args.remote_posture,
                canonical_domain=args.domain,
                website=args.website,
                linkedin_company_url=args.linkedin,
            )
        except (batch.MissingEvidence, ValueError) as exc:
            _safe_print(f"REFUSED: {exc}")
            raise SystemExit(2)
        session.commit()
        _safe_print(f"{prospect!r}")


def _cmd_disqualify(args) -> None:
    with get_session() as session:
        prospect = _load(session, args.name)
        try:
            batch.disqualify(session, prospect, args.reason, note=args.note)
        except ValueError as exc:
            _safe_print(f"REFUSED: {exc}")
            raise SystemExit(2)
        session.commit()
        _safe_print(f"{prospect!r}")


def _cmd_evidence(args) -> None:
    with get_session() as session:
        path = batch.write_evidence(session, args.thesis)
        _safe_print(str(path))


def _cmd_report(args) -> None:
    with get_session() as session:
        _safe_print(batch.batch_report(session, args.thesis))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("thesis-next", help="the highest-priority unexhausted thesis").set_defaults(func=_cmd_thesis_next)
    sub.add_parser("progress", help="qualified count per thesis").set_defaults(func=_cmd_progress)

    p = sub.add_parser("dedup", help="split harvested names against companies + prospects")
    p.add_argument("--thesis", required=True)
    p.add_argument("--names-file", required=True, help="one harvested company name per line")
    p.add_argument("--record-new", action="store_true", help="persist the new names as candidates")
    p.add_argument("--source-query")
    p.add_argument("--source-url")
    p.set_defaults(func=_cmd_dedup)

    p = sub.add_parser("record", help="create one candidate row")
    p.add_argument("--thesis", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--source-query")
    p.add_argument("--source-url")
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser("spend", help="charge one search against a candidate's cap")
    p.add_argument("--name", required=True)
    p.set_defaults(func=_cmd_spend)

    p = sub.add_parser("qualify", help="mark a prospect qualified (evidence required)")
    p.add_argument("--name", required=True)
    p.add_argument("--band", type=int, required=True, choices=[1, 2, 3])
    p.add_argument("--function-evidence", required=True, help="VERBATIM text proving an in-scope data function")
    p.add_argument("--india-signal", required=True, help="VERBATIM text proving India eligibility")
    p.add_argument("--industry")
    p.add_argument("--hq")
    p.add_argument("--remote-posture", choices=["remote_first", "hybrid", "onsite", "unknown"])
    p.add_argument("--domain")
    p.add_argument("--website")
    p.add_argument("--linkedin")
    p.set_defaults(func=_cmd_qualify)

    p = sub.add_parser("disqualify", help="mark a prospect unqualified (the row is kept)")
    p.add_argument("--name", required=True)
    p.add_argument("--reason", required=True, choices=sorted(batch.VALID_REASONS))
    p.add_argument("--note")
    p.set_defaults(func=_cmd_disqualify)

    p = sub.add_parser("evidence", help="write the reviewable batch file")
    p.add_argument("--thesis", required=True)
    p.set_defaults(func=_cmd_evidence)

    p = sub.add_parser("report", help="terminal summary of a thesis run")
    p.add_argument("--thesis", required=True)
    p.set_defaults(func=_cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
