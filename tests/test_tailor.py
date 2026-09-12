"""
Tailoring core (ATS #4 / issue #43): the deterministic scaffolding around the
agent's reasoning — frontier selection, requirement assembly, and render+persist.

SQLite in-memory for the DB logic; the render steps skip if Tectonic is absent.
"""
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.orm import Base, Job, JobSkill, TailoredResume
from app.resume.render import RenderError, _resolve_tectonic
from app.resume.schema import EXAMPLE_MASTER_PATH, load_master
from app.resume.tailor import (
    GapReportError,
    find_job_by_url,
    get_or_create_adhoc_job,
    job_requirements,
    render_adhoc,
    save_tailored,
    select_jobs_needing_resume,
    validate_gap_report,
)


def _tectonic_available() -> bool:
    try:
        _resolve_tectonic()
        return True
    except RenderError:
        return False


needs_tectonic = pytest.mark.skipif(not _tectonic_available(), reason="Tectonic not installed")


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _enriched_job(session, ext_id="e1", title="Credit Risk Data Scientist"):
    job = Job(
        source="test", external_job_id=ext_id, title=title,
        company_name_raw="Acme", description_raw="We need PySpark and credit scoring.",
        enrichment_status="done", experience_min_years=3,
    )
    session.add(job)
    session.flush()
    session.add_all([
        JobSkill(job_id=job.id, skill="PySpark", skill_type="hard"),
        JobSkill(job_id=job.id, skill="Communication", skill_type="soft"),
    ])
    session.flush()
    return job


def test_frontier_excludes_unenriched_and_already_tailored():
    session = _session()
    done = _enriched_job(session, "done")
    pending = Job(source="test", external_job_id="pend", title="X", enrichment_status="pending")
    session.add(pending)
    session.flush()

    assert [j.id for j in select_jobs_needing_resume(session)] == [done.id]

    # once it has a tailored resume, it drops off the frontier
    session.add(TailoredResume(job_id=done.id, score=50.0))
    session.flush()
    assert select_jobs_needing_resume(session) == []


def test_job_requirements_reads_decomposed_fields():
    session = _session()
    job = _enriched_job(session)
    reqs = job_requirements(session, job)
    assert reqs["hard_skills"] == ["PySpark"]
    assert reqs["soft_skills"] == ["Communication"]
    assert reqs["experience_min_years"] == 3
    assert reqs["title"] == "Credit Risk Data Scientist"


@needs_tectonic
def test_save_tailored_persists_and_is_idempotent(tmp_path):
    session = _session()
    job = _enriched_job(session)
    master = load_master(EXAMPLE_MASTER_PATH)  # a real tailored master would be a reworded copy; shape is identical
    gap = {"score": 80, "must_haves": [], "nice_to_haves": [], "surfaceable_gaps": [], "real_gaps": []}

    row1, res1 = save_tailored(
        session, job_id=job.id, tailored=master, score=80.0, gap_report=gap, out_root=tmp_path,
    )
    session.commit()
    assert res1.ok and res1.pdf_path.exists()
    assert row1.tailored_yaml["contact"]["name"] == "Alex Morgan"

    row2, _ = save_tailored(
        session, job_id=job.id, tailored=master, score=88.0, gap_report=gap, out_root=tmp_path,
    )
    session.commit()
    assert session.query(TailoredResume).count() == 1  # updated, not duplicated
    assert row2.score == 88.0


def test_find_job_by_url_matches_posting_or_apply_url():
    session = _session()
    job = _enriched_job(session)
    job.job_url = "https://acme.example/jobs/123"
    session.flush()
    assert find_job_by_url(session, "https://acme.example/jobs/123").id == job.id
    assert find_job_by_url(session, "https://nope.example/x") is None
    assert find_job_by_url(session, "") is None


def test_find_job_by_url_matches_linkedin_share_link_by_embedded_job_id():
    # LinkedIn hands out multiple URL shapes for the same posting: the
    # canonical /jobs/view/<slug>-<id> page (what gets scraped/stored) and a
    # search-results?currentJobId=<id>&... share link (what a user pastes).
    # An exact-string match misses the second shape; the id fallback shouldn't.
    session = _session()
    job = _enriched_job(session)
    job.job_url = (
        "https://in.linkedin.com/jobs/view/credit-risk-analyst-at-outsourced-4437226316"
        "?position=7&pageNum=0&refId=abc"
    )
    session.flush()

    share_link = (
        "https://www.linkedin.com/jobs/search-results/?currentJobId=4437226316"
        "&eBP=BUDGET_EXHAUSTED_JOB&keywords=Data+Scientist&origin=PREFERENCES_LANDING"
    )
    assert find_job_by_url(session, share_link).id == job.id
    # a different job id must not false-positive match
    assert find_job_by_url(session, "https://www.linkedin.com/jobs/search-results/?currentJobId=999999999") is None


# ---- get_or_create_adhoc_job: existence check always runs first, insert only
# on a genuine miss, using the same upsert_job/NormalizedJob path every
# connector uses (not a separate ad-hoc insert path) -------------------------

def test_get_or_create_adhoc_job_returns_existing_match_without_reinserting():
    session = _session()
    job = _enriched_job(session)
    job.job_url = "https://acme.example/jobs/123"
    session.flush()

    found = get_or_create_adhoc_job(session, url="https://acme.example/jobs/123", jd_text="ignored")
    assert found.id == job.id
    assert session.query(Job).count() == 1  # no duplicate inserted


def test_get_or_create_adhoc_job_inserts_new_row_when_no_match():
    session = _session()
    job = get_or_create_adhoc_job(
        session, url="https://newboard.example/jobs/9",
        jd_text="We need a Data Scientist with Python and SQL.",
        title="Data Scientist", company="NewCo",
    )
    session.commit()
    assert job.id is not None
    assert job.source == "adhoc"
    assert job.title == "Data Scientist"
    assert job.enrichment_status == "pending"  # freshly inserted, not yet enriched
    assert session.query(Job).count() == 1


def test_get_or_create_adhoc_job_linkedin_url_uses_linkedin_source_and_embedded_id():
    session = _session()
    url = "https://www.linkedin.com/jobs/view/data-scientist-at-acme-1234567890"
    job = get_or_create_adhoc_job(session, url=url, jd_text="JD text", title="DS", company="Acme")
    session.commit()
    assert job.source == "linkedin"
    assert job.external_job_id == "1234567890"


def test_get_or_create_adhoc_job_reinsert_same_link_is_idempotent():
    session = _session()
    j1 = get_or_create_adhoc_job(
        session, url="https://newboard.example/jobs/9", jd_text="JD v1", title="DS", company="NewCo",
    )
    session.commit()
    j2 = get_or_create_adhoc_job(
        session, url="https://newboard.example/jobs/9", jd_text="JD v1 updated", title="DS", company="NewCo",
    )
    session.commit()
    assert j1.id == j2.id
    assert session.query(Job).count() == 1


def test_get_or_create_adhoc_job_no_url_hashes_jd_text_and_is_idempotent():
    session = _session()
    text = "A very specific pasted JD with no source link at all."
    j1 = get_or_create_adhoc_job(session, jd_text=text, title="DS", company="NewCo")
    session.commit()
    assert j1.source == "adhoc"
    assert j1.job_url is None

    # Re-pasting the exact same text: with no url, find_job_by_url can't run,
    # but the external_job_id is a deterministic hash of jd_text, so
    # upsert_job's own (source, external_job_id) dedup still finds the same
    # row rather than inserting a duplicate.
    j2 = get_or_create_adhoc_job(session, jd_text=text, title="DS", company="NewCo")
    session.commit()
    assert j2.id == j1.id
    assert session.query(Job).count() == 1


def test_get_or_create_adhoc_job_raises_without_url_match_or_jd_text():
    session = _session()
    with pytest.raises(ValueError):
        get_or_create_adhoc_job(session, url="https://nomatch.example/x")


# ---- gap report completeness (every covered:false item must have its own,
# exactly-named entry — no merging/renaming multiple requirements together) --

def _gap_report(**overrides):
    base = {
        "must_haves": [
            {"requirement": "Python", "covered": True},
            # covered=true only because tailoring surfaced/reworded true content
            {"requirement": "Risk governance frameworks", "covered": True},
        ],
        "nice_to_haves": [
            {"requirement": "Communication", "covered": False},
            {"requirement": "Stakeholder management", "covered": False},
        ],
        "surfaceable_gaps": [
            {"requirement": "Risk governance frameworks", "note": "was present but unsurfaced; now surfaced"},
        ],
        "real_gaps": [
            {"requirement": "Communication", "note": "not evidenced"},
            {"requirement": "Stakeholder management", "note": "not evidenced"},
        ],
    }
    base.update(overrides)
    return base


def test_validate_gap_report_passes_when_every_uncovered_item_named_exactly():
    validate_gap_report(_gap_report())  # no raise — including the legitimate
    # surfaceable_gaps entry for a now-covered=true requirement (not a bug)


def test_validate_gap_report_catches_merged_gap_entry():
    # the real bug this guards against: two uncovered requirements merged into
    # one paraphrased gap entry, leaving "Communication" untraceable
    bad = _gap_report(real_gaps=[
        {"requirement": "Stakeholder management / explicit communication", "note": "merged"},
    ])
    with pytest.raises(GapReportError, match="Communication"):
        validate_gap_report(bad)


def test_validate_gap_report_catches_missing_entry():
    bad = _gap_report(real_gaps=[{"requirement": "Communication", "note": "x"}])
    # Stakeholder management has no gap entry at all
    with pytest.raises(GapReportError, match="Stakeholder management"):
        validate_gap_report(bad)


def test_validate_gap_report_catches_covered_item_in_real_gaps():
    bad = _gap_report(real_gaps=[
        {"requirement": "Communication", "note": "x"},
        {"requirement": "Stakeholder management", "note": "x"},
        {"requirement": "Python", "note": "contradiction — Python is covered=true above"},
    ])
    with pytest.raises(GapReportError, match="Python"):
        validate_gap_report(bad)


def test_validate_gap_report_catches_uncovered_item_in_surfaceable_gaps():
    # claiming a still-uncovered requirement was "surfaced" would be dishonest
    bad = _gap_report(surfaceable_gaps=[
        {"requirement": "Communication", "note": "falsely claimed as surfaced while still covered=false"},
    ])
    with pytest.raises(GapReportError, match="Communication"):
        validate_gap_report(bad)


def test_validate_gap_report_catches_same_requirement_in_both_buckets():
    bad = _gap_report(
        real_gaps=[
            {"requirement": "Communication", "note": "x"},
            {"requirement": "Stakeholder management", "note": "x"},
        ],
        surfaceable_gaps=[
            {"requirement": "Risk governance frameworks", "note": "ok"},
            {"requirement": "Communication", "note": "listed in both buckets — contradiction"},
        ],
    )
    with pytest.raises(GapReportError, match="both real_gaps and surfaceable_gaps"):
        validate_gap_report(bad)


def test_save_tailored_rejects_incomplete_gap_report(tmp_path):
    session = _session()
    job = _enriched_job(session)
    master = load_master(EXAMPLE_MASTER_PATH)
    incomplete = _gap_report(real_gaps=[{"requirement": "Communication", "note": "x"}])
    with pytest.raises(GapReportError):
        save_tailored(session, job_id=job.id, tailored=master, score=50.0, gap_report=incomplete, out_root=tmp_path)


@needs_tectonic
def test_render_adhoc_returns_pdf_without_persisting(tmp_path):
    session = _session()
    master = load_master(EXAMPLE_MASTER_PATH)
    result = render_adhoc(master, out_dir=tmp_path / "adhoc")
    assert result.ok and result.pdf_path.exists()
    # ad-hoc never writes a tailored_resumes row
    assert session.query(TailoredResume).count() == 0
