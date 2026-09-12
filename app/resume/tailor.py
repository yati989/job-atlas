"""
Tailoring core — the deterministic scaffolding around the agent's reasoning.

The *judgment* (which true bullets to surface, how to reword them to the JD's
vocabulary, the score, the gap split) is done by the agent in-session via the
`tailor-resumes` skill — no external LLM API call, same contract as
`enrich-jobs`. This module provides everything around that judgment that should
be code, not reasoning:

- `select_jobs_needing_resume` — the frontier query (relevant jobs with no
  current tailored resume).
- `job_requirements` — assemble a job's already-decomposed requirements
  (job_skills + experience_* + education_requirement + qualification_other) for
  the agent to reason over, so it never re-extracts what enrichment produced.
- `get_or_create_adhoc_job` — the single entry point for a link or pasted JD:
  reuses a matching stored job if one exists, otherwise inserts one via the
  ingestion pipeline's own `upsert_job`. Every ad-hoc input becomes a real
  `jobs` row — there is no separate no-persist ad-hoc path.
- `save_tailored` — render a tailored master to an ATS-safe PDF (with the
  round-trip gate) and upsert its `tailored_resumes` row. Used for both the
  DB-batch path and every ad-hoc input, since both now resolve to a real job.
- `render_adhoc` — a lower-level pure-render primitive (no DB at all), kept
  for previewing a tailored master without touching Postgres; not part of the
  standard ad-hoc flow (see `tailor-resumes/SKILL.md`).

Integrity boundary (hard invariant): a tailored master may only reorder/
re-emphasize/reword content already present in the resume master. This module
does not enforce truthfulness (it cannot judge it) — the skill's instructions
and the QA step do — but it keeps the true base master available for comparison.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.orm import Job, JobSkill, TailoredResume
from app.models.schemas import NormalizedJob
from app.pipeline.upsert import upsert_job
from app.resume.persistence import upsert_tailored_resume
from app.resume.render import RenderResult, render_resume
from app.resume.schema import ResumeMaster

DEFAULT_BATCH = 25


def select_jobs_needing_resume(
    session: Session, limit: int = DEFAULT_BATCH, since: datetime | None = None,
) -> list[Job]:
    """Relevant jobs (all stored jobs are relevant) lacking a tailored resume.

    Only jobs that have been enriched (so their requirements are decomposed) and
    have no current `tailored_resumes` row are returned — the tailoring frontier.

    `since`, if given, additionally requires `first_seen_at >= since` — used
    by the full-pipeline orchestration (issue #82) to scope tailoring to one
    run's ingestion batch rather than the whole backlog. Standalone
    `tailor-resumes` usage leaves this unset by design, since it exists to
    work down the backlog across runs.
    """
    stmt = (
        select(Job)
        .outerjoin(TailoredResume, TailoredResume.job_id == Job.id)
        .where(TailoredResume.id.is_(None))
        .where(Job.enrichment_status == "done")
        .order_by(Job.id)
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(Job.first_seen_at >= since)
    return list(session.scalars(stmt))


def job_requirements(session: Session, job: Job) -> dict[str, Any]:
    """The decomposed requirements the agent scores the master against."""
    skills = session.scalars(
        select(JobSkill).where(JobSkill.job_id == job.id)
    ).all()
    return {
        "job_id": job.id,
        "title": job.title,
        "company": job.company_name_raw,
        "location": job.location_raw,
        "seniority": job.seniority,
        "employment_type": job.employment_type,
        "experience_min_years": job.experience_min_years,
        "experience_max_years": job.experience_max_years,
        "education_requirement": job.education_requirement,
        "qualification_other": job.qualification_other,
        "hard_skills": [s.skill for s in skills if s.skill_type == "hard"],
        "soft_skills": [s.skill for s in skills if s.skill_type == "soft"],
        "description_raw": job.description_raw,
        "job_url": job.job_url or job.apply_url,
    }


def compact_job_requirements(requirements: dict[str, Any], *, max_description_chars: int = 4000) -> dict[str, Any]:
    """Return a reusable job brief without replaying the whole raw JD.

    Structured enrichment remains intact.  The raw description is replaced by
    a bounded set of requirement/responsibility-heavy paragraphs; full text
    stays in the immutable posting snapshot for targeted follow-up.
    """
    result = {key: value for key, value in requirements.items() if key != "description_raw"}
    raw = str(requirements.get("description_raw") or "")
    result["description_chars"] = len(raw)
    if not raw:
        result["description_excerpt"] = ""
        return result
    lines = [" ".join(line.split()) for line in re.split(r"[\r\n]+", raw) if line.strip()]
    markers = re.compile(
        r"(?i)\b(require|required|must|qualification|experience|responsibilit|"
        r"skill|role|build|develop|design|lead|own|deliver|python|sql|machine learning|data)\b"
    )
    ranked = sorted(enumerate(lines), key=lambda item: (not bool(markers.search(item[1])), item[0]))
    selected: list[tuple[int, str]] = []
    used = 0
    for index, line in ranked:
        remaining = max_description_chars - used
        if remaining <= 0:
            break
        piece = line[:remaining]
        selected.append((index, piece))
        used += len(piece) + 1
    result["description_excerpt"] = "\n".join(line for _, line in sorted(selected))
    result["description_truncated"] = len(result["description_excerpt"]) < len(raw)
    return result


# LinkedIn hands out multiple URL shapes for the same posting depending on
# where you copy the link from — a canonical "/jobs/view/<slug>-<id>" page vs.
# a "search-results?currentJobId=<id>&..." share link. An exact-string match
# misses the second shape entirely even though it's the same job. Extract the
# numeric LinkedIn job id from either shape and fall back to matching on it.
_LINKEDIN_JOB_ID_RE = re.compile(r"(?:currentJobId=|/jobs/view/[^?]*-)(\d{6,})")


def _extract_linkedin_job_id(url: str) -> Optional[str]:
    m = _LINKEDIN_JOB_ID_RE.search(url)
    return m.group(1) if m else None


def find_job_by_url(session: Session, url: str) -> Optional[Job]:
    """Return a stored job whose posting/apply URL matches, else None.

    Lets the ad-hoc path reuse a job's existing enrichment when the pasted link
    is one the pipeline already scraped — the cheapest correct behavior. Tries
    an exact URL match first, then falls back to matching by the embedded
    LinkedIn numeric job id (see `_LINKEDIN_JOB_ID_RE`) so a share-link/search-
    results URL still resolves to the same stored posting.
    """
    url = (url or "").strip()
    if not url:
        return None

    exact = session.scalars(
        select(Job).where((Job.job_url == url) | (Job.apply_url == url)).limit(1)
    ).first()
    if exact is not None:
        return exact

    li_id = _extract_linkedin_job_id(url)
    if li_id is None:
        return None
    return session.scalars(
        select(Job)
        .where(or_(Job.job_url.contains(li_id), Job.apply_url.contains(li_id)))
        .limit(1)
    ).first()


def jd_hash(text: Optional[str]) -> Optional[str]:
    """Stable hash of a JD, to detect re-runs of the same posting."""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_or_create_adhoc_job(
    session: Session,
    *,
    url: Optional[str] = None,
    jd_text: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
) -> Job:
    """The single entry point for every ad-hoc input (a pasted link or raw JD
    text, with or without a URL) — always resolves to a real `jobs` row.

    **Existence check always runs first.** If `url` is given, `find_job_by_url`
    (exact match, then LinkedIn-job-id fallback) is tried before anything else;
    a match is returned as-is — no re-insert, no duplicate. Only when nothing
    matches is a new row created, via the ingestion pipeline's own
    `app.pipeline.upsert.upsert_job` (same `NormalizedJob` contract every
    connector uses, so this shares dedup semantics rather than inventing a
    separate insert path):

    - A LinkedIn `url` uses `source="linkedin"` and the embedded numeric job id
      as `external_job_id` — so if the real LinkedIn connector later scrapes
      this same posting during a normal pipeline run, it updates this exact
      row instead of creating a duplicate.
    - Anything else uses `source="adhoc"` with a hash of the URL (or, if there
      is no URL at all, of the JD text itself) as a stable external id — so
      re-submitting the same link/pasted text is idempotent too.

    Raises `ValueError` if no stored job matches and no `jd_text` was given to
    create one from (nothing to insert).
    """
    if url:
        existing = find_job_by_url(session, url)
        if existing is not None:
            return existing

    if not jd_text:
        raise ValueError(
            "No stored job matched this input, and no JD text was provided to create one."
        )

    li_id = _extract_linkedin_job_id(url) if url else None
    if li_id:
        source, external_id = "linkedin", li_id
    else:
        source = "adhoc"
        external_id = jd_hash(url or jd_text)[:32]

    item = NormalizedJob(
        source=source,
        external_job_id=external_id,
        title=title or "Untitled (ad-hoc)",
        company_name_raw=company or "Unknown",
        description_raw=jd_text,
        job_url=url,
        apply_url=url,
    )
    job = upsert_job(session, item)
    session.flush()
    return job


def _coerce_master(tailored: Union[ResumeMaster, dict]) -> ResumeMaster:
    return tailored if isinstance(tailored, ResumeMaster) else ResumeMaster.model_validate(tailored)


class GapReportError(ValueError):
    """Raised when a gap_report doesn't exhaustively/exactly account for every
    requirement — e.g. a `covered: false` item missing from the gap lists, or
    present under a paraphrased name instead of its exact requirement string.
    """


def validate_gap_report(gap_report: dict[str, Any]) -> None:
    """Enforce gap-report completeness and the surfaceable/real distinction.

    - Every requirement marked `covered: false` must appear in `real_gaps`
      under its EXACT requirement string — no merging two requirements into
      one paraphrased entry (e.g. "Communication" and "Stakeholder
      management" are two separate entries, never combined).
    - `real_gaps` must contain only currently-uncovered, known requirements
      (never a covered=true item, never a made-up name).
    - `surfaceable_gaps` documents requirements tailoring *closed* — so its
      entries must reference currently-**covered** (true), known
      requirements, not uncovered ones (that would falsely claim a gap was
      closed when it wasn't).
    - The same requirement cannot appear in both buckets at once.

    Raises `GapReportError` listing every violation found (not just the
    first) if the report is incomplete or inconsistent.
    """
    items = [
        (item["requirement"], bool(item.get("covered", False)))
        for bucket in ("must_haves", "nice_to_haves")
        for item in gap_report.get(bucket, [])
    ]
    uncovered = {name for name, covered in items if not covered}
    covered = {name for name, covered in items if covered}

    real_gap_names = {item["requirement"] for item in gap_report.get("real_gaps", [])}
    surfaceable_names = {item["requirement"] for item in gap_report.get("surfaceable_gaps", [])}

    missing = uncovered - real_gap_names               # uncovered with no real_gaps entry
    bad_real = real_gap_names - uncovered              # real_gaps referencing a covered/unknown item
    bad_surfaceable = surfaceable_names - covered       # surfaceable_gaps referencing an uncovered/unknown item
    overlap = real_gap_names & surfaceable_names        # same requirement claimed in both buckets

    problems = []
    if missing:
        problems.append(
            f"uncovered requirement(s) with no real_gaps entry (add one under this exact name): {sorted(missing)}"
        )
    if bad_real:
        problems.append(
            f"real_gaps entry name(s) don't match a currently-uncovered requirement "
            f"(covered=true, or an unknown/paraphrased name): {sorted(bad_real)}"
        )
    if bad_surfaceable:
        problems.append(
            f"surfaceable_gaps entry name(s) don't match a currently-covered requirement "
            f"(still uncovered, or an unknown/paraphrased name): {sorted(bad_surfaceable)}"
        )
    if overlap:
        problems.append(f"requirement(s) listed in both real_gaps and surfaceable_gaps: {sorted(overlap)}")
    if problems:
        raise GapReportError("; ".join(problems))


def save_tailored(
    session: Session,
    *,
    job_id: int,
    tailored: Union[ResumeMaster, dict],
    score: float,
    gap_report: dict[str, Any],
    out_root: Union[str, Path],
    jd_snapshot: Optional[str] = None,
    decision_run_id: str | None = None,
) -> tuple[TailoredResume, RenderResult]:
    """Render a DB job's tailored resume and upsert its row (idempotent).

    Fails loudly if the PDF does not round-trip (the ATS gate) or if the gap
    report is incomplete (see `validate_gap_report`). Caller owns the
    transaction (mirrors `app.pipeline.upsert`).
    """
    jd_identity_hash = jd_hash(jd_snapshot)
    if decision_run_id is not None:
        from app.decision_runs.resume_funnel import approved_version_for_job
        version = approved_version_for_job(session, decision_run_id, job_id)
        snapshot = version.snapshot if isinstance(version.snapshot, dict) else {}
        jd_snapshot = snapshot.get("description_raw")
        jd_identity_hash = version.material_content_hash
    validate_gap_report(gap_report)
    master = _coerce_master(tailored)
    artifact_dir = Path(out_root) / f"job_{job_id}"
    result = render_resume(master, artifact_dir, basename="resume").raise_if_failed()

    row = upsert_tailored_resume(
        session,
        job_id=job_id,
        score=score,
        gap_report=gap_report,
        tailored_yaml=master.model_dump(),
        artifact_dir=str(artifact_dir),
        jd_hash=jd_identity_hash,
        jd_snapshot=jd_snapshot,
    )
    # This optional handoff is deliberately after the durable resume row is
    # present.  Ordinary backlog/ad-hoc tailoring supplies no run ID and
    # therefore cannot mutate a Decision Run's progress.
    if decision_run_id is not None:
        from app.decision_runs.resume_funnel import report_saved_tailored_resume
        report_saved_tailored_resume(session, run_id=decision_run_id, job_id=job_id)
    return row, result


def render_adhoc(
    tailored: Union[ResumeMaster, dict],
    out_dir: Union[str, Path],
    basename: str = "resume",
) -> RenderResult:
    """Render a tailored master to a PDF for an ad-hoc JD — no DB, no persistence."""
    master = _coerce_master(tailored)
    return render_resume(master, Path(out_dir), basename=basename).raise_if_failed()
