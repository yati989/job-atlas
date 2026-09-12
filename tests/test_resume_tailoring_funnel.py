from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.dashboard.queries import (
    decision_run_resume_tailoring_drilldown, decision_run_resume_tailoring_funnel,
)
from app.decision_runs.progress import FailureDisposition, StageName, StageTransitionError, interrupt_stage, read_run_progress
from app.decision_runs.resume_funnel import (
    TailoringDropReason, TailoringOutcome, TailoringResult,
    freeze_resume_tailoring_manifest, job_requirements_for_approved_version,
    report_resume_tailoring, report_saved_tailored_resume, versions_needing_tailoring,
)
from app.resume.render import RenderResult
from app.resume.schema import EXAMPLE_MASTER_PATH, load_master
from app.resume.tailor import save_tailored
from app.models.orm import (
    Base, Company, DecisionRun, DecisionRunResumeTailoringManifest,
    DecisionRunSelectedJob, Job, JobPostingVersion, JobSkill, TailoredResume,
)
from tests.progress_support import complete_stage_chain


NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, future=True)()
    s.add(DecisionRun(id="run-1", since_at=NOW, cutoff_at=NOW, input_timezone="UTC",
                      state="approved", policy_snapshot={}, target_count=2,
                      telemetry_version="decision_funnels_v1"))
    s.flush()
    complete_stage_chain(s, "run-1", StageName.APPROVAL_GATE)
    return s


def _selected(s, key: str, *, add_resume=False):
    company = Company(name=f"Company {key}")
    s.add(company); s.flush()
    job = Job(source="board", external_job_id=key, company_id=company.id,
              company_name_raw=company.name, title="Data Engineer", job_url=f"https://jobs/{key}",
              description_raw="work", enrichment_status="done", posted_at=NOW)
    s.add(job); s.flush()
    version = JobPostingVersion(job_id=job.id, canonical_duplicate_root_id=job.id, posted_at=NOW,
                                material_content_hash=f"hash-{key}", posting_instance_key=f"version-{key}",
                                snapshot={"title": job.title, "description_raw": f"frozen JD {key}",
                                          "job_url": f"https://frozen.jobs/{key}",
                                          "apply_url": f"https://frozen.apply/{key}"})
    s.add(version); s.flush()
    s.add(DecisionRunSelectedJob(run_id="run-1", company_id=company.id, posting_version_id=version.id,
                                 company_order=job.id, selection_kind="approved"))
    if add_resume:
        s.add(TailoredResume(job_id=job.id, score=88, artifact_dir=f"output/{key}",
                             jd_hash=version.material_content_hash,
                             jd_snapshot=version.snapshot["description_raw"]))
    s.flush()
    return job, version


def test_freeze_uses_exact_approved_versions_and_combines_reused_with_new_tailored():
    s = _session()
    reused, reused_version = _selected(s, "reused", add_resume=True)
    new, new_version = _selected(s, "new")
    backlog, _ = _selected(s, "backlog")
    s.query(DecisionRunSelectedJob).filter_by(posting_version_id=s.query(JobPostingVersion).filter_by(job_id=backlog.id).one().id).delete()

    manifest = freeze_resume_tailoring_manifest(s, "run-1")
    assert manifest.posting_version_ids == (reused_version.id, new_version.id)
    assert [item.id for item in versions_needing_tailoring(s, "run-1")] == [new_version.id]
    report_resume_tailoring(s, "run-1", [TailoringResult(new_version.id, TailoringOutcome.TAILORED)])
    stage = next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value)
    assert (stage.status, stage.counts.input, stage.counts.advanced, stage.counts.pending) == ("completed", 2, 2, 0)
    assert s.query(DecisionRunResumeTailoringManifest).filter_by(job_id=backlog.id).count() == 0
    table = decision_run_resume_tailoring_funnel(s, "run-1")
    assert int(table.iloc[0]["tailored"]) == 2


def test_dropped_failed_and_pending_are_distinct_and_drillable_with_links():
    s = _session()
    dropped, dropped_version = _selected(s, "dropped")
    failed, failed_version = _selected(s, "failed")
    pending, _ = _selected(s, "pending")
    freeze_resume_tailoring_manifest(s, "run-1")
    report_resume_tailoring(s, "run-1", [
        TailoringResult(dropped_version.id, "dropped", TailoringDropReason.APPROVAL_REVOKED),
        TailoringResult(failed_version.id, "failed", "render_failed", FailureDisposition.NON_BLOCKING),
    ])
    stage = next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value)
    assert (stage.counts.advanced, stage.counts.dropped, stage.counts.failed, stage.counts.pending) == (0, 1, 1, 1)
    detail = decision_run_resume_tailoring_drilldown(s, "run-1", reason="render_failed")
    assert detail.iloc[0]["posting_version_id"] == failed_version.id
    assert detail.iloc[0]["job_url"] == "https://frozen.jobs/failed"
    assert pending.id not in set(detail["job_id"])


def test_saved_resume_updates_only_explicit_matching_run_and_rejects_outside_scope():
    s = _session()
    job, version = _selected(s, "in-scope")
    outside, _ = _selected(s, "outside")
    s.query(DecisionRunSelectedJob).filter_by(company_id=outside.company_id).delete()
    freeze_resume_tailoring_manifest(s, "run-1")
    report_saved_tailored_resume(s, run_id=None, job_id=job.id)
    stage = next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value)
    assert stage.counts.pending == 1
    report_saved_tailored_resume(s, run_id="run-1", job_id=job.id)
    assert next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value).status == "completed"
    with pytest.raises(StageTransitionError, match="outside"):
        report_saved_tailored_resume(s, run_id="run-1", job_id=outside.id)


def test_exact_manifest_resumes_after_interruption_with_shared_attempt_history():
    s = _session()
    _, version = _selected(s, "resume")
    freeze_resume_tailoring_manifest(s, "run-1")
    interrupt_stage(s, "run-1", StageName.RESUME_TAILORING, reason="operator stopped skill")
    freeze_resume_tailoring_manifest(s, "run-1")
    report_resume_tailoring(s, "run-1", [TailoringResult(version.id, "tailored")])
    stage = next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value)
    assert [(item.attempt_number, item.status) for item in stage.attempts] == [(1, "interrupted"), (2, "completed")]


def test_stale_resume_is_not_reused_for_a_different_frozen_posting_version():
    s = _session()
    _, version = _selected(s, "stale")
    s.add(TailoredResume(job_id=version.job_id, score=80, artifact_dir="output/stale",
                         jd_hash="older-material-hash", jd_snapshot="older JD"))
    s.flush()

    freeze_resume_tailoring_manifest(s, "run-1")

    assert [item.id for item in versions_needing_tailoring(s, "run-1")] == [version.id]
    stage = next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value)
    assert (stage.counts.advanced, stage.counts.pending) == (0, 1)


def test_normal_preexisting_save_tailored_row_is_reused_for_its_exact_frozen_jd(monkeypatch, tmp_path):
    s = _session()
    job, version = _selected(s, "normal-reuse")
    monkeypatch.setattr("app.resume.tailor.render_resume", lambda *_args, **_kwargs: RenderResult(
        tex_path=Path(tmp_path / "resume.tex"), pdf_path=Path(tmp_path / "resume.pdf"), ok=True,
    ))
    gap = {"score": 80, "must_haves": [], "nice_to_haves": [], "surfaceable_gaps": [], "real_gaps": []}
    save_tailored(
        s, job_id=job.id, tailored=load_master(EXAMPLE_MASTER_PATH), score=80, gap_report=gap,
        out_root=tmp_path, jd_snapshot=version.snapshot["description_raw"],
    )

    freeze_resume_tailoring_manifest(s, "run-1")

    assert versions_needing_tailoring(s, "run-1") == []
    stage = next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value)
    assert (stage.status, stage.counts.advanced) == ("completed", 1)


def test_approved_requirements_and_drilldown_read_the_immutable_version_not_mutable_job():
    s = _session()
    job, version = _selected(s, "immutable")
    job.description_raw = "later mutable JD"
    job.job_url = "https://mutable.jobs/immutable"
    job.apply_url = "https://mutable.apply/immutable"
    freeze_resume_tailoring_manifest(s, "run-1")

    requirements = job_requirements_for_approved_version(s, "run-1", version.id)
    assert requirements["description_raw"] == "frozen JD immutable"
    assert requirements["job_url"] == "https://frozen.jobs/immutable"
    detail = decision_run_resume_tailoring_drilldown(s, "run-1")
    assert detail.iloc[0]["job_url"] == "https://frozen.jobs/immutable"
    assert detail.iloc[0]["apply_url"] == "https://frozen.apply/immutable"


def test_approved_requirements_do_not_leak_mutable_enrichment_fields_or_skills():
    s = _session()
    job, version = _selected(s, "frozen-enrichment")
    job.experience_min_years = 3
    job.experience_max_years = 5
    job.education_requirement = "original mutable degree"
    job.qualification_other = "original mutable qualification"
    s.add(JobSkill(job_id=job.id, skill="OriginalMutableSkill", skill_type="hard"))
    s.flush()
    freeze_resume_tailoring_manifest(s, "run-1")
    before = job_requirements_for_approved_version(s, "run-1", version.id)

    job.experience_min_years = 99
    job.experience_max_years = 100
    job.education_requirement = "later mutable degree"
    job.qualification_other = "later mutable qualification"
    s.add(JobSkill(job_id=job.id, skill="LaterMutableSkill", skill_type="soft"))
    s.flush()
    after = job_requirements_for_approved_version(s, "run-1", version.id)

    assert after == before
    assert after["experience_min_years"] is None
    assert after["education_requirement"] is None
    assert after["qualification_other"] is None
    assert after["hard_skills"] == []
    assert after["soft_skills"] == []


def test_all_terminal_failure_and_exceptional_drop_complete_with_disposition():
    s = _session()
    _, failed = _selected(s, "failed-terminal")
    _, dropped = _selected(s, "dropped-terminal")
    freeze_resume_tailoring_manifest(s, "run-1")

    report_resume_tailoring(s, "run-1", [
        TailoringResult(failed.id, TailoringOutcome.FAILED, "render_failed", FailureDisposition.EXCLUDED),
        TailoringResult(dropped.id, TailoringOutcome.DROPPED, TailoringDropReason.APPROVAL_REVOKED),
    ])

    stage = next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value)
    assert (stage.status, stage.counts.dropped, stage.counts.failed, stage.counts.pending) == (
        "completed_with_errors", 1, 1, 0,
    )
    assert [(item.record_id, item.disposition.value) for item in stage.failed_record_dispositions] == [
        (str(failed.id), "excluded"),
    ]


def test_drop_is_only_allowed_for_explicit_post_approval_revocation():
    s = _session()
    _, version = _selected(s, "not-silent")
    freeze_resume_tailoring_manifest(s, "run-1")
    with pytest.raises(StageTransitionError, match="approval_revoked_for_posting"):
        report_resume_tailoring(s, "run-1", [TailoringResult(version.id, "dropped", "render_was_hard")])


def test_failure_requires_disposition_before_it_can_change_progress():
    s = _session()
    _, version = _selected(s, "needs-disposition")
    freeze_resume_tailoring_manifest(s, "run-1")
    with pytest.raises(StageTransitionError, match="explicit disposition"):
        report_resume_tailoring(s, "run-1", [TailoringResult(version.id, "failed", "render_failed")])
    stage = next(x for x in read_run_progress(s, "run-1").stages if x.name == StageName.RESUME_TAILORING.value)
    assert (stage.counts.failed, stage.counts.pending) == (0, 1)


def test_approved_save_persists_frozen_jd_and_material_identity(monkeypatch, tmp_path):
    s = _session()
    job, version = _selected(s, "save-frozen")
    job.description_raw = "later mutable JD"
    freeze_resume_tailoring_manifest(s, "run-1")
    monkeypatch.setattr("app.resume.tailor.render_resume", lambda *_args, **_kwargs: RenderResult(
        tex_path=Path(tmp_path / "resume.tex"), pdf_path=Path(tmp_path / "resume.pdf"), ok=True,
    ))
    gap = {"score": 80, "must_haves": [], "nice_to_haves": [], "surfaceable_gaps": [], "real_gaps": []}

    row, _ = save_tailored(
        s, job_id=job.id, tailored=load_master(EXAMPLE_MASTER_PATH), score=80, gap_report=gap,
        out_root=tmp_path, jd_snapshot=job.description_raw, decision_run_id="run-1",
    )

    assert row.jd_snapshot == "frozen JD save-frozen"
    assert row.jd_hash == version.material_content_hash
