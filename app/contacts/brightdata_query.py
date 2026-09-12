"""
CLI entrypoint the find-contacts skill invokes (via Bash) in place of the
native WebSearch tool call — see docs/adr/0005 addendum (2026-08-02).

**2026-08-03: every search call must now be labelled.** Before this change
the CLI was a stateless `python -m ... "<query>"` shell with no company/tier
identity attached to a call, which is exactly what let the per-tier search
budget be silently under-spent across a full batch (see search_plan.py's
docstring for the measured drift). Three subcommands now exist, and `search`
refuses to run unless it can key the call:

    python -m app.contacts.brightdata_query plan --company-id 3030
        Print every query the ladder authorises for this company (MANDATORY
        vs optional), in issue order. Run this FIRST for a new company.

    python -m app.contacts.brightdata_query coverage --company-id 3030
        Print mandatory queries not yet reflected in the call ledger. Empty
        output means the company was searched to plan; a non-empty list means
        any `partial` status it carries is not yet trustworthy.

    python -m app.contacts.brightdata_query search --company-id 3030 \\
        --group data_ai --tier head --attempt 1 "<query>"
        Run one query, billed against that (company, group, tier) triple.
        Raises BudgetExceeded BEFORE billing if the triple already spent its
        cap — see `search_source.bright_data_profiles`.

**2026-08-03, second change: `search` now runs the profile relevance gate
(ADR-0006) before printing.** Structurally wrong-company / former-employer
rows are cut from the KEPT list and instead summarized in a separate
"structurally dropped" section — never silently vanished, since the agent
must be able to overrule the gate. Every kept row gets a tier suggestion and
a company-match flag when one applies (sister_entity / no_signal); neither
can remove a row — see profile_relevance.py's module docstring for why that
boundary is load-bearing. Pass `--raw` to bypass the gate entirely and see
exactly what `bright_data_profiles` returned, unfiltered — the old behaviour,
kept as an escape hatch since judgement must remain overridable per ADR-0005.

Prints each `/in/` profile result as one line of
`title | url | snippet [tier-suggestion] [flag]`; the agent reads stdout and
judges each row exactly as it judged WebSearch results. Not `search_source.py`'s
own __main__ block, which exercises the full classify-and-print pipeline
(RawProfile + `_mentions_company`) for smoke tests of that dormant module —
this script is deliberately thinner.
"""
import argparse
import re
import sys
from pathlib import Path

from app.contacts.search_source import BudgetExceeded, bright_data_profiles


_EMAIL_TEXT_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def _clean(text: str) -> str:
    return text.replace("\n", " ").strip()


def _safe_print(line: str) -> None:
    """Real Bright Data snippets carry arbitrary Unicode (smart quotes, the
    U+2011 non-breaking hyphen, etc.) that a plain `print()` crashes on under
    Windows' cp1252 console encoding — confirmed live, 2026-08-05, mid-batch,
    losing the rest of a call's already-billed results to an uncaught
    UnicodeEncodeError. Same class of bug as resolve_company_domains.py's
    `_printable()` fix; replace what the console can't render rather than
    raising, since this is a display concern, not a data-integrity one."""
    encoding = sys.stdout.encoding or "utf-8"
    print(line.encode(encoding, errors="replace").decode(encoding))


def _public_safe(text: str) -> str:
    return _EMAIL_TEXT_RE.sub("[email removed]", _clean(text))


def _bounded(text: str, limit: int, *, redact_emails: bool = False) -> str:
    clean = _public_safe(text) if redact_emails else _clean(text)
    return clean if len(clean) <= limit else clean[: max(0, limit - 1)].rstrip() + "…"


def _print_rows_raw(rows) -> None:
    if not rows:
        print("(no /in/ profile results)")
        return
    for r in rows:
        _safe_print(f"{_clean(r.title)} | {r.url} | {_clean(r.snippet)}")


def _print_rows_gated(
    rows,
    target_company: str,
    *,
    company_id: int | None = None,
    search_group: str | None = None,
    tier: str | None = None,
    attempt: int | None = None,
    company_type: str = "employer",
    show_dropped: bool = False,
    snippet_chars: int = 320,
    redact_emails: bool = False,
) -> None:
    from app.contacts.profile_relevance import (
        ProfileCandidate, _company_match_reason, filter_relevant, log_gate_verdict, suggest_tier,
    )
    from app.contacts.triage import AUTO_REJECT, log_triage_shadow, triage
    from app.config.settings import TRIAGE_MODE

    if not rows:
        print("(no /in/ profile results)")
        return

    candidates = [
        ProfileCandidate(full_name="", title=r.title, url=r.url, snippet=r.snippet, target_company=target_company)
        for r in rows
    ]
    kept, drop_counts, drop_details, flags = filter_relevant(candidates)
    log_gate_verdict(
        kept, drop_counts, flags,
        company_id=company_id, search_group=search_group, tier=tier, attempt=attempt,
    )
    flag_by_url = {f["candidate"].url: f["reason"] for f in flags}

    # ADR-0007's triage layer — always computed and shadow-logged (never
    # changes what the agent sees at TRIAGE_MODE=shadow, the default), only
    # printed/gated at TRIAGE_MODE=annotate/gate. See triage.py's module
    # docstring for why AUTO_ACCEPT never bypasses the agent even at 'gate'.
    company_reasons = {c.url: _company_match_reason(c) for c in kept}
    triage_verdicts = {
        c.url: triage(c.title, company_type=company_type, company_reason=company_reasons[c.url])
        for c in kept
    }
    log_triage_shadow(
        triage_verdicts, company_id=company_id, search_group=search_group,
        tier_searched=tier, attempt=attempt, company_reasons=company_reasons,
        redact_emails=redact_emails,
    )

    if not kept:
        print("(no /in/ profile results survived the gate; rejected counts follow)")
    seen_urls: set[str] = set()
    for c in kept:
        if c.url in seen_urls:
            continue
        seen_urls.add(c.url)
        suggestion = suggest_tier(c.title)
        tier_note = f" [tier~{suggestion.tier}:{suggestion.score:.0f}]" if suggestion.tier else ""
        flag_note = f" [FLAG:{flag_by_url[c.url]}]" if c.url in flag_by_url else ""
        triage_note = ""
        if TRIAGE_MODE in ("annotate", "gate"):
            tv = triage_verdicts[c.url]
            triage_note = f" [triage:{tv.decision}" + (f"->{tv.tier}]" if tv.tier else "]")
        if TRIAGE_MODE == "gate" and triage_verdicts[c.url].decision == AUTO_REJECT:
            continue  # moved to the structurally-dropped section below instead
        title = _public_safe(c.title) if redact_emails else _clean(c.title)
        snippet = _bounded(c.snippet, snippet_chars, redact_emails=redact_emails)
        _safe_print(f"{title} | {c.url} | {snippet}{tier_note}{flag_note}{triage_note}")

    if TRIAGE_MODE == "gate":
        triage_rejected = [c for c in kept if triage_verdicts[c.url].decision == AUTO_REJECT]
        if triage_rejected:
            print(f"\n-- {len(triage_rejected)} triage-rejected (out-of-scope function, armed marker) --")
            for c in triage_rejected if show_dropped else ():
                reasons = ",".join(triage_verdicts[c.url].reasons)
                _safe_print(f"  [{reasons}] {_clean(c.title)} | {c.url}")

    dropped = [d for d in drop_details]
    if dropped:
        print(f"\n-- {len(dropped)} structurally dropped (axis: shape={drop_counts['shape']} company={drop_counts['company']}) --")
        for d in dropped if show_dropped else ():
            c = d["candidate"]
            reason = d.get("company_reason", d["axis"])
            _safe_print(f"  [{reason}] {_clean(c.title)} | {c.url}")


def _cmd_plan(args: argparse.Namespace) -> int:
    from app.contacts.search_plan import format_plan, plan_for_company
    from app.db.session import get_session
    from app.contacts.agentic_batch import context_for_company_id

    with get_session() as session:
        ctx = context_for_company_id(session, args.company_id)
        planned = plan_for_company(ctx)
    print(f"# {ctx.name}  (id={ctx.company_id}, groups={ctx.search_groups})")
    print(format_plan(planned))
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    from app.contacts.search_plan import coverage_for_company, format_plan
    from app.db.session import get_session
    from app.contacts.agentic_batch import context_for_company_id

    with get_session() as session:
        ctx = context_for_company_id(session, args.company_id)
        outstanding = coverage_for_company(ctx)
    if not outstanding:
        print(f"# {ctx.name}: fully covered — every mandatory query has been issued")
        return 0
    print(f"# {ctx.name}: {len(outstanding)} mandatory quer{'y' if len(outstanding)==1 else 'ies'} NOT YET ISSUED")
    print(format_plan(outstanding))
    return 1


def _cmd_search(args: argparse.Namespace) -> int:
    reservation_id = None
    context = None
    if args.authorization_id is not None:
        if args.private_home is None:
            raise ValueError("--private-home is required with --authorization-id")
        if args.raw or args.show_dropped:
            raise ValueError(
                "authorized links-only search forbids --raw and --show-dropped"
            )
        from app.contacts.agentic_batch import context_for_company_id
        from app.contacts.profile_query_budget import (
            approved_query,
            complete_profile_query,
            reserve_profile_query,
        )
        from app.models.orm import ProfileDiscoveryAuthorization
        from app.workflows.public_database import public_session

        with public_session(args.private_home) as session:
            authorization = session.get(ProfileDiscoveryAuthorization, args.authorization_id)
            if authorization is None:
                raise ValueError(f"unknown profile authorization {args.authorization_id}")
            planned = approved_query(
                authorization,
                company_id=args.company_id,
                search_group=args.group,
                tier=args.tier,
                attempt=args.attempt,
                query_text=args.query,
            )
            reservation = reserve_profile_query(session, authorization.id, planned)
            reservation_id = reservation.call.id
            context = context_for_company_id(session, args.company_id)
            if not reservation.created:
                if reservation.call.status == "started":
                    complete_profile_query(
                        session, reservation.call.id,
                        error="previous process ended before recording a provider outcome",
                    )
                print(
                    "AUTHORIZED QUERY ALREADY RECORDED "
                    f"status={reservation.call.status}; provider call not repeated"
                )
                return 0
    try:
        rows = bright_data_profiles(
            args.query,
            company_id=args.company_id,
            search_group=args.group,
            tier=args.tier,
            attempt=args.attempt,
        )
    except BudgetExceeded as exc:
        if reservation_id is not None:
            from app.contacts.profile_query_budget import complete_profile_query
            from app.workflows.public_database import public_session
            with public_session(args.private_home) as session:
                complete_profile_query(session, reservation_id, error=str(exc))
        print(f"BUDGET EXCEEDED, call refused (nothing billed): {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if reservation_id is not None:
            from app.contacts.profile_query_budget import complete_profile_query
            from app.workflows.public_database import public_session
            with public_session(args.private_home) as session:
                complete_profile_query(session, reservation_id, error=str(exc))
        raise

    if reservation_id is not None:
        from app.contacts.profile_query_budget import complete_profile_query
        from app.workflows.public_database import public_session
        with public_session(args.private_home) as session:
            complete_profile_query(session, reservation_id, result_count=len(rows))

    if args.raw:
        _print_rows_raw(rows)
        return 0

    if context is None:
        from app.db.session import get_session
        from app.contacts.agentic_batch import context_for_company_id

        with get_session() as session:
            context = context_for_company_id(session, args.company_id)
    target_company = context.name
    company_type = context.company_type
    _print_rows_gated(
        rows, target_company,
        company_id=args.company_id, search_group=args.group, tier=args.tier, attempt=args.attempt,
        company_type=company_type,
        show_dropped=args.show_dropped,
        snippet_chars=args.snippet_chars,
        redact_emails=args.authorization_id is not None,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.contacts.brightdata_query")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="print the full query worklist for a company")
    p_plan.add_argument("--company-id", type=int, required=True)
    p_plan.set_defaults(func=_cmd_plan)

    p_cov = sub.add_parser("coverage", help="print mandatory queries not yet issued")
    p_cov.add_argument("--company-id", type=int, required=True)
    p_cov.set_defaults(func=_cmd_coverage)

    p_search = sub.add_parser("search", help="run one billed query, keyed to the budget ledger")
    p_search.add_argument("--company-id", type=int, required=True)
    p_search.add_argument("--group", dest="group", required=True, help="search_group, e.g. data_ai")
    p_search.add_argument("--tier", required=True, help="e.g. head, hiring_manager, ic, talent_acquisition, exec_fallback")
    p_search.add_argument("--attempt", type=int, default=1, choices=(1, 2))
    p_search.add_argument(
        "--authorization-id", type=int,
        help="approved public links-only global budget authorization",
    )
    p_search.add_argument(
        "--private-home", type=Path,
        help="private public data root (required with --authorization-id)",
    )
    p_search.add_argument("--raw", action="store_true", help="skip the profile relevance gate, print unfiltered rows")
    p_search.add_argument("--show-dropped", action="store_true", help="print rejected candidate rows; counts are always shown")
    p_search.add_argument("--snippet-chars", type=int, default=320, help="maximum kept-row snippet characters (default 320)")
    p_search.add_argument("query")
    p_search.set_defaults(func=_cmd_search)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
