"""
Case-only dedup for `job_skills.skill` (dashboard "Top skills" chart request,
2026-08-07: "PyTorch"/"pytorch"/"Pytorch" etc. showing as separate bars).

Groups rows by (skill.lower(), skill_type) and, within each group with more
than one casing variant, picks the highest-count variant as canonical
(ties broken alphabetically for determinism). Every other variant is merged
into it:
  - if the job already has a row with the canonical (skill, skill_type), the
    loser-variant row for that job is deleted (keeping the canonical one) —
    required because (job_id, skill, skill_type) is a unique constraint, so
    a blind rename would collide.
  - otherwise the loser row's `skill` text is renamed to the canonical
    casing in place.

No skill_type reassignment, no fuzzy/synonym merging (e.g. "ML" vs "Machine
Learning") — casing only, same scope as the request.

Usage:
    python -m scripts.normalize_skill_casing            # preview only, no writes
    python -m scripts.normalize_skill_casing --apply     # actually merge
"""
import argparse
from collections import defaultdict

from sqlalchemy import func

from app.db.session import get_session
from app.models.orm import JobSkill


def find_groups(session) -> dict[tuple[str, str], list[tuple[str, int]]]:
    rows = (
        session.query(JobSkill.skill, JobSkill.skill_type, func.count(JobSkill.id))
        .group_by(JobSkill.skill, JobSkill.skill_type)
        .all()
    )
    groups: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    for skill, skill_type, count in rows:
        groups[(skill.lower(), skill_type)].append((skill, count))
    return {k: v for k, v in groups.items() if len(v) > 1}


def pick_canonical(variants: list[tuple[str, int]]) -> str:
    """Highest total count wins; ties broken alphabetically for determinism."""
    return sorted(variants, key=lambda sc: (-sc[1], sc[0]))[0][0]


def run(apply: bool) -> dict:
    """Case-only skill dedup, callable from the pipeline as well as the CLI."""
    with get_session() as session:
        groups = find_groups(session)
        print(f"=== {len(groups)} case-variant groups ===")

        total_renamed = 0
        total_deleted = 0
        for (lower_skill, skill_type), variants in sorted(
            groups.items(), key=lambda kv: -sum(c for _, c in kv[1])
        ):
            canonical = pick_canonical(variants)
            losers = [v for v, _ in variants if v != canonical]
            print(f"  [{skill_type}] {canonical!r} <- {losers}  (counts: {variants})")

            if not apply:
                continue

            existing_job_ids = {
                jid
                for (jid,) in session.query(JobSkill.job_id)
                .filter(JobSkill.skill == canonical, JobSkill.skill_type == skill_type)
                .all()
            }
            for loser in losers:
                loser_rows = (
                    session.query(JobSkill)
                    .filter(JobSkill.skill == loser, JobSkill.skill_type == skill_type)
                    .all()
                )
                for row in loser_rows:
                    if row.job_id in existing_job_ids:
                        session.delete(row)
                        total_deleted += 1
                    else:
                        row.skill = canonical
                        existing_job_ids.add(row.job_id)
                        total_renamed += 1

        if apply:
            session.commit()
            print(f"\n--apply: {total_renamed} row(s) renamed to canonical casing, "
                  f"{total_deleted} duplicate row(s) deleted. Committed.")
        else:
            print(f"\nPreview only — rerun with --apply to commit.")

        return {
            "groups": len(groups),
            "renamed": total_renamed if apply else 0,
            "deleted": total_deleted if apply else 0,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually merge (default: preview only)")
    args = parser.parse_args()
    run(apply=args.apply)


if __name__ == "__main__":
    main()
