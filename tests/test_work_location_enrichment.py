import pytest

from app.models.schemas import NormalizedJob
from app.pipeline.work_location_enrichment import (
    WorkLocationEnrichmentResult,
    apply_results,
    build_input,
    classify_work_location,
    evidence_from_result,
    load_results,
)
from app.pipeline.relevance import location_ok


def _job(location="Pune Division, Maharashtra, India", *, remote_query=True):
    return NormalizedJob(
        source="linkedin",
        external_job_id="4436347282",
        title="Data Scientist",
        company_name_raw="Avahi",
        location_raw=location,
        description_raw=(
            "At Avahi, we are a remote-first global team. Remote-First "
            "Flexibility allows you to work from anywhere."
        ),
        raw_payload={"query_location_mode": "remote_india"} if remote_query else {},
    )


def _result(**changes):
    values = {
        "external_job_id": "4436347282",
        "description_sha256": build_input(_job()).description_sha256,
        "decision": "remote_india",
        "evidence": "Remote-First Flexibility allows you to work from anywhere.",
        "reason": "The role can be performed remotely while based in India.",
        "confidence": "high",
    }
    values.update(changes)
    return WorkLocationEnrichmentResult(**values)


@pytest.mark.parametrize(
    ("description", "decision", "confidence"),
    [
        (
            "Our structured hybrid approach is centered around our offices and remote work environments.",
            "unclear",
            "medium",
        ),
        (
            "Location: Gurgaon Job Type: [Full-time/Contract/Remote/Hybrid]",
            "unclear",
            "medium",
        ),
        (
            "We have been and will remain a remote first employer.",
            "remote_unspecified",
            "medium",
        ),
        (
            "This hybrid role requires three days each week in our Bengaluru office.",
            "bengaluru_workplace",
            "high",
        ),
    ],
)
def test_deterministic_classifier_handles_benchmark_edge_cases(
    description, decision, confidence,
):
    job = _job(location="India")
    job.description_raw = description

    result = classify_work_location(build_input(job))

    assert result.decision == decision
    assert result.confidence == confidence
    assert result.reason.startswith("deterministic_rule:")


def test_deterministic_classifier_marks_explicit_india_remote():
    job = _job(location="India")
    job.description_raw = "Location: Remote — India. This is a fully remote role."

    result = classify_work_location(build_input(job))

    assert result.decision == "remote_india"
    assert result.required_location == "India"
    assert result.confidence == "high"


@pytest.mark.parametrize(
    ("listing_location", "description", "decision", "required_location"),
    [
        (
            "Mumbai Metropolitan Region",
            "Custom Work Environment: Work remotely with EST Shift hours",
            "remote_unspecified",
            None,
        ),
        (
            "Telangana, India",
            "REMOTE - Bogota, Colombia, REMOTE - Hyderabad, India, "
            "REMOTE - Mexico City, Mexico, REMOTE - Pennsylvania, USA. "
            "Hiring Location: Canada or Colombia. You are working hybrid in "
            "a collaborative workspace.",
            "remote_india",
            "India",
        ),
        (
            "Greater Kolkata Area",
            "What you can expect from us: Flexible WFH Policy",
            "remote_unspecified",
            None,
        ),
        (
            "Gurugram, Haryana, India",
            "India Employment Benefits Include Flexible work arrangements, "
            "supporting work-life balance",
            "remote_unspecified",
            None,
        ),
    ],
)
def test_deterministic_classifier_accepts_real_remote_benefit_wording(
    listing_location, description, decision, required_location,
):
    job = _job(location=listing_location)
    job.description_raw = description

    result = classify_work_location(build_input(job))

    assert result.decision == decision
    assert result.required_location == required_location


@pytest.mark.parametrize(
    ("description", "decision"),
    [
        ("Location: Permanent WFH", "remote_unspecified"),
        (
            "Flexible work arrangements (remote and/or office-based)",
            "remote_unspecified",
        ),
    ],
)
def test_deterministic_classifier_accepts_additional_frozen_remote_wording(
    description, decision,
):
    job = _job(location="Pune Division, Maharashtra, India")
    job.description_raw = description

    result = classify_work_location(build_input(job))

    assert result.decision == decision
    assert result.confidence == "medium"


def test_capgemini_flexible_remote_or_office_benefit_is_remote():
    job = _job(location="Pune Division, Maharashtra, India")
    job.external_job_id = "4456906502"
    job.title = "FBS - Elasticsearch Data Engineer (Medallion Architecture)"
    job.company_name_raw = "Capgemini"
    job.description_raw = (
        "Benefits This position comes with competitive compensation and benefits "
        "package: Competitive salary and performance-based bonuses Comprehensive "
        "benefits package Career development and training opportunities Flexible "
        "work arrangements (remote and/or office-based) Dynamic and inclusive work "
        "culture within a globally renowned group Private Health Insurance."
    )

    result = classify_work_location(build_input(job))

    assert result.decision == "remote_unspecified"
    assert result.confidence == "medium"
    assert result.evidence == job.description_raw
    assert result.reason == "deterministic_rule:explicit_remote_benefit"


@pytest.mark.parametrize("separator", ["-", "–", "—"])
def test_deterministic_classifier_accepts_all_remote_india_city_separators(separator):
    job = _job(location="Telangana, India")
    job.description_raw = f"REMOTE {separator} Hyderabad, India"

    result = classify_work_location(build_input(job))

    assert result.decision == "remote_india"
    assert result.required_location == "India"


@pytest.mark.parametrize(
    "description",
    [
        "We work as distributed teams across many countries.",
        "Flexible office and/or home-based working is available.",
        "We offer flexible work arrangements.",
        "This hybrid role includes remote flexibility.",
    ],
)
def test_deterministic_classifier_keeps_ambiguous_flexibility_non_remote(description):
    job = _job(location="Pune Division, Maharashtra, India")
    job.description_raw = description

    result = classify_work_location(build_input(job))

    assert result.decision == "unclear"


def test_agent_remote_india_result_maps_to_gate_fields_without_model_call():
    job = _job()
    applied = apply_results([job], {job.external_job_id: _result()})

    assert applied == 1
    assert job.location_raw == "India"
    assert job.is_remote is True
    assert job.remote_scope == "Remote — India"
    assert (
        job.raw_payload["description_location_agent_evidence"]["decision"]
        == "remote_india"
    )


def test_input_carries_company_grouping_and_listing_match_fields():
    job = _job()
    job.posted_at = "2026-08-24T00:00:00Z"
    job.job_url = "https://in.linkedin.com/jobs/view/4436347282"

    packet = build_input(job)

    assert packet.company_name == "Avahi"
    assert packet.posted_at.isoformat() == "2026-08-24T00:00:00+00:00"
    assert packet.job_url == job.job_url


def test_combined_input_carries_job_fields_and_run_policy():
    job = _job(location="India")
    job.employment_type = "Full-time"
    job.seniority = "Mid-Senior level"
    job.salary_raw = "₹25L–₹35L"

    packet = build_input(
        job,
        full_job_enrichment_requested=True,
    )

    assert packet.full_job_enrichment_requested is True
    assert packet.employment_type == "Full-time"
    assert packet.seniority == "Mid-Senior level"
    assert packet.salary_raw == "₹25L–₹35L"


def test_combined_result_applies_reusable_full_job_enrichment():
    job = _job(location="India")
    result = _result(
        decision="unclear",
        confidence="medium",
        evidence="",
        reason="No job-specific work arrangement is stated.",
        job_enrichment={
            "status": "done",
            "experience_min_years": 3,
            "experience_max_years": 5,
            "education_requirement": "Bachelor's degree",
            "qualification_other": "AWS certification preferred",
            "hard_skills": ["Python", "SQL", "Python"],
            "soft_skills": ["Communication"],
            "minimum_experience": {
                "years": 3,
                "mandatory": True,
                "evidence": "3+ years of experience",
            },
        },
    )

    apply_results([job], {job.external_job_id: result})

    assert job.is_remote is True
    assert job.remote_scope == "Remote — India"
    enrichment = job.raw_payload["pre_gate_job_enrichment"]
    assert enrichment["status"] == "done"
    assert enrichment["experience_min_years"] == 3
    assert enrichment["experience_max_years"] == 5
    assert enrichment["hard_skills"] == ["Python", "SQL"]
    assert enrichment["soft_skills"] == ["Communication"]
    assert enrichment["minimum_experience"]["years"] == 3
    assert enrichment["enriched_at"]


def test_combined_loader_requires_full_enrichment(tmp_path):
    packet = build_input(
        _job(location="India"),
        full_job_enrichment_requested=True,
    )
    path = tmp_path / "results.jsonl"
    path.write_text(_result().model_dump_json() + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="full job enrichment is required"):
        load_results(path, (packet,))

    enriched = _result(job_enrichment={"status": "done"})
    path.write_text(enriched.model_dump_json() + "\n", encoding="utf-8")
    assert load_results(path, (packet,))[packet.external_job_id] == enriched


def test_bengaluru_hybrid_is_bengaluru_not_remote():
    evidence = evidence_from_result(
        _result(
            decision="bengaluru_workplace",
            evidence="Three days each week in our Bengaluru office.",
            reason="Office attendance is required in Bengaluru.",
            required_location="Bengaluru",
        ),
        listing_location="Bengaluru, Karnataka, India",
    )

    assert evidence["location_raw"] == "Bengaluru, Karnataka, India"
    assert evidence["is_remote"] is False
    assert evidence["remote_scope"] is None


@pytest.mark.parametrize("confidence", ["low", "medium"])
def test_unclear_low_or_medium_confidence_exact_india_is_kept_and_tagged_remote_india(
    confidence,
):
    job = _job(location="India")
    applied = apply_results([job], {job.external_job_id: _result(
        decision="unclear",
        evidence="",
        reason="The description does not establish a work arrangement.",
        confidence=confidence,
    )})

    assert applied == 1
    assert job.location_raw == "India"
    assert job.is_remote is True
    assert job.remote_scope == "Remote — India"
    assert location_ok(job)
    assert job.raw_payload["description_location_agent_evidence"][
        "fallback_remote_tagged"
    ] is True


@pytest.mark.parametrize(
    ("location", "confidence"),
    [
        ("Greater Kolkata Area", "low"),
        ("India", "high"),
    ],
)
def test_unclear_without_low_confidence_exact_india_is_rejected(location, confidence):
    job = _job(location=location)
    apply_results([job], {job.external_job_id: _result(
        decision="unclear",
        evidence="",
        reason="The description does not establish a work arrangement.",
        confidence=confidence,
    )})

    assert job.is_remote is False
    assert job.location_raw == location
    assert not location_ok(job)
    assert job.raw_payload["description_location_agent_evidence"][
        "fallback_remote_tagged"
    ] is False


@pytest.mark.parametrize("confidence", ["medium", "high"])
def test_medium_or_high_negative_linkedin_remote_query_fails_location(confidence):
    job = _job()
    apply_results([job], {job.external_job_id: _result(
        decision="onsite_outside_bengaluru",
        evidence="Employees must work from our Pune office.",
        reason="Pune office attendance is required.",
        confidence=confidence,
        required_location="Pune, India",
    )})

    assert job.is_remote is False
    assert job.location_raw == "Pune, India"
    assert not location_ok(job)
    assert job.raw_payload["description_location_agent_evidence"][
        "fallback_remote_tagged"
    ] is False


def test_low_confidence_negative_linkedin_remote_query_is_rejected():
    job = _job()
    apply_results([job], {job.external_job_id: _result(
        decision="onsite_outside_bengaluru",
        evidence="The role may work from our Pune office.",
        reason="The wording weakly suggests Pune office attendance.",
        confidence="low",
        required_location="Pune, India",
    )})

    assert job.is_remote is False
    assert job.location_raw == "Pune, India"
    assert not location_ok(job)


def test_unclear_fallback_does_not_affect_other_linkedin_location_modes():
    job = _job(remote_query=False)
    apply_results([job], {job.external_job_id: _result(
        decision="unclear",
        evidence="",
        reason="The description does not establish a work arrangement.",
        confidence="low",
    )})

    assert job.is_remote is False
    assert job.location_raw == "Pune Division, Maharashtra, India"
    assert not location_ok(job)


def test_unresolved_remote_instance_with_bengaluru_listing_passes_as_bengaluru():
    job = _job(location="Bengaluru, Karnataka, India")
    apply_results([job], {job.external_job_id: _result(
        decision="unclear",
        evidence="",
        reason="The hydrated description does not establish a work arrangement.",
        confidence="high",
    )})

    evidence = job.raw_payload["description_location_agent_evidence"]
    assert evidence["listing_location"] == "Bengaluru, Karnataka, India"
    assert job.location_raw == "Bengaluru, Karnataka, India"
    assert location_ok(job)


def test_result_loader_requires_complete_current_description_coverage(tmp_path):
    packet = build_input(_job())
    path = tmp_path / "results.jsonl"
    path.write_text(_result().model_dump_json() + "\n", encoding="utf-8")

    loaded = load_results(path, (packet,))
    assert set(loaded) == {"4436347282"}

    stale = _result(description_sha256="stale")
    path.write_text(stale.model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale LinkedIn location result"):
        load_results(path, (packet,))


def test_result_loader_rejects_missing_agent_judgments(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="coverage mismatch"):
        load_results(path, (build_input(_job()),))
