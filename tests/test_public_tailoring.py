from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.orm import (
    Base, Company, Job, JobPostingVersion, PublicSelectionScope,
    PublicJobPhaseAEvidence, PublicSelectionScopeItem, PublicTailoredResume,
    TailoredResume,
)
from app.resume.render import RenderResult
from app.resume.schema import ResumeMaster
from app.workflows.public_tailoring import save_public_tailored, tailoring_context
from app.workflows.public_tailoring import materialize_public_resume_for_outreach


def test_tailoring_context_is_exact_scope_and_recognizes_same_version_artifact(tmp_path: Path):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    company = Company(name="DesignCo")
    session.add(company); session.flush()
    jobs = []
    versions = []
    for index in (1, 2):
        job = Job(
            source="fixture", external_job_id=str(index), company_id=company.id,
            company_name_raw=company.name, title=f"Designer {index}",
            description_raw=f"JD {index}", enrichment_status="done",
        )
        session.add(job); session.flush(); jobs.append(job)
        version = JobPostingVersion(
            job_id=job.id, canonical_duplicate_root_id=job.id,
            material_content_hash=f"hash-{index}", posting_instance_key=f"fixture:{index}",
            snapshot={"description_raw": f"Frozen JD {index}"},
        )
        session.add(version); session.flush(); versions.append(version)
    scope = PublicSelectionScope(
        run_id="run-1", revision=1, mode="manual", fingerprint="fp",
        job_count=1, company_count=1,
    )
    session.add(scope); session.flush()
    session.add(PublicSelectionScopeItem(
        scope_id=scope.id, posting_version_id=versions[0].id, company_id=company.id,
    ))
    session.add(PublicJobPhaseAEvidence(
        posting_version_id=versions[0].id, status="done",
        evidence={"status": "done", "hard_skills": ["Figma"]},
    ))
    artifact = tmp_path / "job_1"
    artifact.mkdir(); (artifact / "resume.pdf").write_bytes(b"pdf")
    session.add(PublicTailoredResume(
        posting_version_id=versions[0].id, material_content_hash="hash-1",
        posting_snapshot=versions[0].snapshot, artifact_dir=str(artifact),
    ))
    session.flush()

    context = tailoring_context(session, scope.id)

    assert len(context["items"]) == 1
    assert context["items"][0]["posting_version_id"] == versions[0].id
    assert context["items"][0]["requirements"]["description_raw"] == "Frozen JD 1"
    assert context["items"][0]["requirements"]["hard_skills"] == ["Figma"]
    assert context["items"][0]["status"] == "reused"


def test_save_public_tailored_uses_posting_version_without_touching_legacy_resume(
    tmp_path: Path, monkeypatch,
):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    company = Company(name="DesignCo")
    session.add(company); session.flush()
    job = Job(
        source="fixture", external_job_id="one", company_id=company.id,
        company_name_raw=company.name, title="Designer", description_raw="Mutable JD",
        enrichment_status="done",
    )
    session.add(job); session.flush()
    version = JobPostingVersion(
        job_id=job.id, canonical_duplicate_root_id=job.id,
        material_content_hash="frozen-hash", posting_instance_key="fixture:one",
        snapshot={"description_raw": "Frozen JD"},
    )
    session.add(version); session.flush()
    scope = PublicSelectionScope(
        run_id="run-1", revision=1, mode="manual", fingerprint="fp",
        job_count=1, company_count=1,
    )
    session.add(scope); session.flush()
    session.add(PublicSelectionScopeItem(
        scope_id=scope.id, posting_version_id=version.id, company_id=company.id,
    ))
    session.add(PublicJobPhaseAEvidence(
        posting_version_id=version.id, status="done", evidence={"status": "done"},
    ))
    legacy = TailoredResume(job_id=job.id, score=42.0, artifact_dir="legacy")
    session.add(legacy); session.flush()

    def fake_render(_master, artifact_dir, basename):
        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = artifact_dir / f"{basename}.pdf"
        pdf_path.write_bytes(b"pdf")
        return RenderResult(artifact_dir / f"{basename}.tex", pdf_path, ok=True)

    monkeypatch.setattr("app.workflows.public_tailoring.render_resume", fake_render)
    master = ResumeMaster.model_validate({"contact": {"name": "Ada Lovelace"}})
    gap_report = {"must_haves": [], "nice_to_haves": [], "real_gaps": [], "surfaceable_gaps": []}

    saved, rendered = save_public_tailored(
        session, scope_id=scope.id, posting_version_id=version.id, tailored=master,
        score=91.0, gap_report=gap_report, out_root=tmp_path,
    )

    assert rendered.ok
    assert saved.posting_version_id == version.id
    assert saved.material_content_hash == "frozen-hash"
    assert saved.posting_snapshot == {"description_raw": "Frozen JD"}
    assert saved.artifact_dir == str(tmp_path / f"posting_version_{version.id}")
    assert session.query(PublicTailoredResume).count() == 1
    assert session.get(TailoredResume, legacy.id).score == 42.0


def test_tailoring_rejects_phase_b_research_scope(tmp_path: Path):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    scope = PublicSelectionScope(
        run_id="run-1", revision=1, mode="all_eligible", purpose="phase_b_research",
        fingerprint="research", job_count=0, company_count=0,
    )
    session.add(scope); session.flush()

    with pytest.raises(ValueError, match="final selection scope"):
        tailoring_context(session, scope.id)


def test_public_resume_can_feed_existing_outreach_seam_after_exact_scope_check(
    tmp_path: Path, monkeypatch,
):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    company = Company(name="DesignCo")
    session.add(company); session.flush()
    job = Job(source="fixture", external_job_id="one", company_id=company.id,
              company_name_raw=company.name, title="Designer", enrichment_status="done")
    session.add(job); session.flush()
    version = JobPostingVersion(job_id=job.id, canonical_duplicate_root_id=job.id,
        material_content_hash="frozen-hash", posting_instance_key="fixture:one",
        snapshot={"title": "Designer", "description_raw": "Frozen JD"})
    session.add(version); session.flush()
    scope = PublicSelectionScope(run_id="run-1", revision=1, mode="manual",
        fingerprint="fp", job_count=1, company_count=1)
    session.add(scope); session.flush()
    session.add(PublicSelectionScopeItem(scope_id=scope.id,
        posting_version_id=version.id, company_id=company.id))
    artifact = tmp_path / "posting"
    artifact.mkdir(); (artifact / "resume.pdf").write_bytes(b"pdf")
    session.add(PublicTailoredResume(posting_version_id=version.id,
        material_content_hash="frozen-hash", posting_snapshot=version.snapshot,
        score=71, gap_report={}, tailored_yaml={"contact": {"name": "Ada"}},
        artifact_dir=str(artifact)))
    session.flush()

    bridged = materialize_public_resume_for_outreach(
        session, scope_id=scope.id, posting_version_id=version.id,
    )

    assert bridged.job_id == job.id
    assert bridged.jd_hash == "frozen-hash"
    assert bridged.artifact_dir == str(artifact)
