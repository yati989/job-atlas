from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.orm import (
    Base,
    Job,
    ProfiledSearchOutcome,
    PublicRoleReviewCandidate,
    PublicWorkflowStage,
)
from app.models.schemas import NormalizedJob
from app.workflows.guided_run import collect_guided_run, start_guided_run
from app.workflows.planning import SourceCapability, prepare_run
from app.workflows.public_role_review import apply_role_review, role_review_context
from app.workflows.public_location_review import (
    apply_location_review,
    location_review_context,
)


CATALOG = (
    SourceCapability(
        name="fixture", runtime="http", countries=frozenset({"IN"}),
        arrangements=frozenset({"remote"}), rationale="fixture",
    ),
)


def _session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _start(session, **overrides):
    profile = {
        "version": 1,
        "name": "veterinary-remote",
        "professions": ["veterinary professional"],
        "search_terms": ["veterinary"],
        "relevance_terms": ["veterinary"],
        "countries": ["IN"],
        "arrangements": ["remote"],
        "seniority": ["individual_contributor"],
        "collection_window_days": 30,
        "source_selection": {"mode": "named", "sources": ["fixture"]},
    }
    profile.update(overrides)
    preview = prepare_run(profile, CATALOG)
    start_guided_run(
        session, preview=preview, profile_fingerprint="profile-fp",
        confirmed=True, now=datetime(2026, 9, 11, tzinfo=timezone.utc),
        run_id="run-1",
    )


def _job(external_id="1"):
    return NormalizedJob(
        source="fixture",
        external_job_id=external_id,
        title="Clinical Knowledge Associate",
        company_name_raw="Animal Care Co",
        location_raw="Remote - India",
        is_remote=True,
        description_raw="Review animal-health clinical material with veterinarians.",
        job_url=f"https://jobs.example/{external_id}",
    )


def _apply_payload(context, decisions):
    return {
        "mode": context["mode"],
        "context_fingerprint": context["context_fingerprint"],
        "judgments": [
            {"candidate_id": item["candidate_id"], **decision}
            for item, decision in zip(context["candidates"], decisions, strict=True)
        ],
    }


def test_semantic_title_acceptance_promotes_nonliteral_role_into_jobs():
    session = _session()
    _start(session)
    collect_guided_run(
        session, run_id="run-1", semantic_role_review=True,
        fetchers={"fixture": lambda _profile: [_job()]},
    )

    assert session.scalar(select(Job)) is None
    title_context = role_review_context(session, "run-1", "titles")
    assert "description" not in title_context["candidates"][0]
    result = apply_role_review(session, "run-1", _apply_payload(
        title_context,
        [{"decision": "relevant", "reason": "Animal-health clinical review role"}],
    ))

    assert result["unresolved"] == 0
    assert session.scalar(select(Job)).title == "Clinical Knowledge Associate"
    outcome = session.scalar(select(ProfiledSearchOutcome))
    assert outcome.outcome == "kept"
    stage = session.scalar(select(PublicWorkflowStage).where(
        PublicWorkflowStage.name == "phase_a",
    ))
    assert stage.status == "awaiting_confirmation"


def test_uncertain_title_exposes_description_before_terminal_judgment():
    session = _session()
    _start(session)
    collect_guided_run(
        session, run_id="run-1", semantic_role_review=True,
        fetchers={"fixture": lambda _profile: [_job()]},
    )
    title_context = role_review_context(session, "run-1", "titles")
    first = apply_role_review(session, "run-1", _apply_payload(
        title_context,
        [{"decision": "uncertain", "reason": "Title does not identify the domain"}],
    ))

    assert first["uncertain_details"] == 1
    detail_context = role_review_context(session, "run-1", "details")
    assert "animal-health" in detail_context["candidates"][0]["description"]
    final = apply_role_review(session, "run-1", _apply_payload(
        detail_context,
        [{"decision": "relevant", "reason": "Description confirms veterinary work"}],
    ))
    assert final["unresolved"] == 0
    assert session.scalar(select(PublicRoleReviewCandidate)).status == "relevant"


def test_role_review_rejects_incomplete_or_stale_batches():
    session = _session()
    _start(session)
    collect_guided_run(
        session, run_id="run-1", semantic_role_review=True,
        fetchers={"fixture": lambda _profile: [_job("1"), _job("2")]},
    )
    context = role_review_context(session, "run-1", "titles")
    incomplete = _apply_payload(
        {**context, "candidates": context["candidates"][:1]},
        [{"decision": "relevant", "reason": "Relevant role"}],
    )
    with pytest.raises(ValueError, match="exactly one judgment"):
        apply_role_review(session, "run-1", incomplete)

    stale = _apply_payload(
        context,
        [
            {"decision": "relevant", "reason": "Relevant role"},
            {"decision": "irrelevant", "reason": "Unrelated role"},
        ],
    )
    stale["context_fingerprint"] = "wrong"
    with pytest.raises(ValueError, match="stale"):
        apply_role_review(session, "run-1", stale)


def test_clear_location_mismatch_is_rejected_before_title_review():
    session = _session()
    _start(session)
    onsite = _job()
    onsite.location_raw = "Pune, India"
    onsite.is_remote = False
    collect_guided_run(
        session, run_id="run-1", semantic_role_review=True,
        fetchers={"fixture": lambda _profile: [onsite]},
    )

    assert role_review_context(session, "run-1", "titles")["candidates"] == []
    outcome = session.scalar(select(ProfiledSearchOutcome))
    assert outcome.outcome == "rejected"
    assert outcome.first_failed_axis == "location"


def test_default_job_type_exclusions_run_before_title_review_and_allow_opt_in():
    session = _session()
    _start(session)
    internship = _job()
    internship.employment_type = "Internship"
    collect_guided_run(
        session, run_id="run-1", semantic_role_review=True,
        fetchers={"fixture": lambda _profile: [internship]},
    )

    assert role_review_context(session, "run-1", "titles")["candidates"] == []
    outcome = session.scalar(select(ProfiledSearchOutcome))
    assert (outcome.outcome, outcome.first_failed_axis) == ("rejected", "job_type")

    opted_in_session = _session()
    _start(opted_in_session, include_internships=True)
    collect_guided_run(
        opted_in_session, run_id="run-1", semantic_role_review=True,
        fetchers={"fixture": lambda _profile: [internship]},
    )
    context = role_review_context(opted_in_session, "run-1", "titles")
    assert len(context["candidates"]) == 1
    assert context["intent"]["include_internships"] is True
    assert context["intent"]["include_part_time"] is False


def test_known_posting_older_than_source_window_still_reaches_title_review():
    session = _session()
    _start(session)
    old = _job()
    old.posted_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    collect_guided_run(
        session, run_id="run-1", semantic_role_review=True,
        fetchers={"fixture": lambda _profile: [old]},
    )

    candidates = role_review_context(session, "run-1", "titles")["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["title"] == old.title
    assert session.scalar(select(ProfiledSearchOutcome)) is None


def test_unclear_remote_country_is_resolved_with_evidence_before_phase_a():
    session = _session()
    _start(session)
    remote = _job()
    remote.location_raw = "Remote"
    remote.remote_scope = "Remote"
    collect_guided_run(
        session, run_id="run-1", semantic_role_review=True,
        fetchers={"fixture": lambda _profile: [remote]},
    )
    titles = role_review_context(session, "run-1", "titles")
    apply_role_review(session, "run-1", _apply_payload(
        titles,
        [{"decision": "relevant", "reason": "Veterinary clinical knowledge work"}],
    ))

    context = location_review_context(session, "run-1")
    result = apply_location_review(session, "run-1", {
        "context_fingerprint": context["context_fingerprint"],
        "judgments": [{
            "outcome_id": context["items"][0]["outcome_id"],
            "decision": "eligible",
            "reason": "The source's native route is restricted to remote India jobs",
        }],
    })

    assert result == {
        "run_id": "run-1", "eligible": 1, "ineligible": 0, "phase_a_count": 1,
    }
    assert session.scalar(select(Job)).title == "Clinical Knowledge Associate"
    outcome = session.scalar(select(ProfiledSearchOutcome))
    assert outcome.outcome == "kept"
    assert outcome.first_failed_axis is None
    assert outcome.posting_version_id is not None
