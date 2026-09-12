import json
import logging
from types import SimpleNamespace

import pytest

from app.contacts import brightdata_query
from app.resume.plan import ResumeTailoringPlan, apply_tailoring_plan
from app.resume.schema import EXAMPLE_MASTER_PATH, load_master
from app.resume.tailor import compact_job_requirements
from app.jobs.screening_brief import screening_brief
from scripts import run_full_pipeline
from scripts.apply_company_identity_review import apply_reviews
from scripts.prepare_company_identity_review import prepare


def test_compact_job_requirements_keeps_structure_and_bounds_raw_description():
    raw = "Boilerplate.\n" + ("Build Python data products with SQL. " * 300)
    compact = compact_job_requirements({
        "job_id": 7,
        "hard_skills": ["Python", "SQL"],
        "experience_min_years": 3,
        "description_raw": raw,
    }, max_description_chars=240)

    assert compact["hard_skills"] == ["Python", "SQL"]
    assert compact["experience_min_years"] == 3
    assert compact["description_chars"] == len(raw)
    assert len(compact["description_excerpt"]) <= 240
    assert compact["description_truncated"] is True
    assert "description_raw" not in compact


def test_screening_brief_excludes_unrelated_jd_text():
    brief = screening_brief({
        "title": "Data Scientist",
        "salary_raw": "₹20L–₹30L",
        "description_raw": (
            "About the company and its long history. " * 100
            + "You must have 5 years of experience. Build models in Python."
        ),
    })

    assert brief["salary_raw"] == "₹20L–₹30L"
    assert "5 years of experience" in brief["screening_evidence"]
    assert "long history" not in brief["screening_evidence"]


def test_resume_plan_applies_small_edits_without_allowing_identity_rewrites():
    master = load_master(EXAMPLE_MASTER_PATH)
    plan = ResumeTailoringPlan.model_validate({
        "replacements": [{
            "path": "/experience/0/bullets/0",
            "value": "Reworded true bullet",
        }],
        "selections": [{"path": "/experience/0/bullets", "indices": [0, 2]}],
    })

    tailored = apply_tailoring_plan(master, plan)

    assert tailored.experience[0].company == master.experience[0].company
    assert tailored.experience[0].bullets == [
        "Reworded true bullet", master.experience[0].bullets[2],
    ]
    with pytest.raises(ValueError, match="only summary"):
        ResumeTailoringPlan.model_validate({
            "replacements": [{"path": "/contact/name", "value": "Someone Else"}],
        })


def test_agent_result_writes_full_artifact_and_prints_counts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_full_pipeline, "AGENT_RESULT_ROOT", tmp_path)
    payload = {"run_id": "run-1", "company_ids": [1, 2], "outcomes": {"eligible": 3}}

    path = run_full_pipeline._emit_agent_result("scope", payload, run_id="run-1")

    assert json.loads(path.read_text()) == payload
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["company_ids_count"] == 2
    assert receipt["outcomes_counts"] == {"eligible": 3}
    assert "company_ids" not in receipt


def test_contact_rows_are_deduplicated_truncated_and_rejections_are_count_only(monkeypatch, capsys):
    kept = SimpleNamespace(title="Person - Data Scientist at Acme", url="https://linkedin.com/in/kept", snippet="x" * 500)
    dropped = SimpleNamespace(title="Wrong Person", url="https://linkedin.com/in/dropped", snippet="irrelevant")
    monkeypatch.setattr(
        "app.contacts.profile_relevance.filter_relevant",
        lambda _candidates: ([kept, kept], {"shape": 1, "company": 0}, [{"candidate": dropped, "axis": "shape"}], []),
    )
    monkeypatch.setattr("app.contacts.profile_relevance.log_gate_verdict", lambda *_a, **_k: None)
    monkeypatch.setattr("app.contacts.profile_relevance._company_match_reason", lambda _candidate: "exact")
    monkeypatch.setattr("app.contacts.profile_relevance.suggest_tier", lambda _title: SimpleNamespace(tier=None, score=0))
    monkeypatch.setattr("app.contacts.triage.log_triage_shadow", lambda *_a, **_k: None)
    monkeypatch.setattr("app.contacts.triage.triage", lambda *_a, **_k: SimpleNamespace(decision="escalate", tier=None, reasons=()))

    brightdata_query._print_rows_gated(
        [kept, dropped], "Acme", snippet_chars=40, show_dropped=False,
    )

    output = capsys.readouterr().out
    assert output.count("https://linkedin.com/in/kept") == 1
    assert "x" * 40 not in output
    assert "1 structurally dropped" in output
    assert "https://linkedin.com/in/dropped" not in output


def test_authorized_contact_shadow_log_redacts_email_but_standalone_preserves_it(tmp_path, monkeypatch):
    email = "person@example.com"
    verdict = SimpleNamespace(
        headline=SimpleNamespace(text=f"Data Scientist ({email})", trusted=True),
        seniority=SimpleNamespace(level=1, matched=(), ambiguous=False),
        function=SimpleNamespace(in_scope=("data",), recruiting=(), out_of_scope=()),
        decision="AUTO_ACCEPT",
        tier="ic",
        reasons=(),
    )
    log_path = tmp_path / "triage_shadow.jsonl"
    monkeypatch.setattr("app.contacts.triage.TRIAGE_SHADOW_LOG", log_path)

    from app.contacts.triage import log_triage_shadow

    log_triage_shadow(
        {"https://linkedin.com/in/kept": verdict},
        company_id=1, search_group="data_ai", tier_searched="ic", attempt=1,
        company_reasons={}, redact_emails=True,
    )
    public_row = json.loads(log_path.read_text().splitlines()[-1])
    assert email not in public_row["headline_text"]
    assert "[email removed]" in public_row["headline_text"]

    log_triage_shadow(
        {"https://linkedin.com/in/kept": verdict},
        company_id=1, search_group="data_ai", tier_searched="ic", attempt=1,
        company_reasons={},
    )
    standalone_row = json.loads(log_path.read_text().splitlines()[-1])
    assert email in standalone_row["headline_text"]


def test_public_gated_rows_passes_redaction_to_shadow_log(monkeypatch):
    candidate = SimpleNamespace(
        title="Person - Data Scientist at Acme",
        url="https://linkedin.com/in/kept",
        snippet="irrelevant",
    )
    logged = {}
    monkeypatch.setattr(
        "app.contacts.profile_relevance.filter_relevant",
        lambda _candidates: ([candidate], {"shape": 0, "company": 0}, [], []),
    )
    monkeypatch.setattr("app.contacts.profile_relevance.log_gate_verdict", lambda *_a, **_k: None)
    monkeypatch.setattr("app.contacts.profile_relevance._company_match_reason", lambda _candidate: "exact")
    monkeypatch.setattr("app.contacts.profile_relevance.suggest_tier", lambda _title: SimpleNamespace(tier=None, score=0))
    monkeypatch.setattr(
        "app.contacts.triage.log_triage_shadow",
        lambda *_args, **kwargs: logged.update(kwargs),
    )

    brightdata_query._print_rows_gated([candidate], "Acme", redact_emails=True)

    assert logged["redact_emails"] is True


def test_full_pipeline_console_is_warning_only_but_file_keeps_info(tmp_path):
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    try:
        root.handlers.clear()
        run_full_pipeline._setup_logging(tmp_path)
        file_handler = next(handler for handler in root.handlers if isinstance(handler, logging.FileHandler))
        stream_handler = next(handler for handler in root.handlers if type(handler) is logging.StreamHandler)
        assert file_handler.level == logging.INFO
        assert stream_handler.level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = old_handlers
        root.setLevel(old_level)


def test_company_identity_agent_context_contains_only_ambiguous_rows():
    base = {"judgments": [
        {"company_id": 1, "company": "Exact", "status": "accepted", "evidence": {"large": "x" * 1000}},
        {"company_id": 2, "company": "Missing", "status": "missing", "evidence": {"large": "x" * 1000}},
        {"company_id": 3, "company": "Ambiguous", "status": "review_required", "evidence": {"candidates": [{"name": "Candidate"}]}},
    ]}
    compact = prepare(base)
    assert [row["company_id"] for row in compact["review_required"]] == [3]
    compact["review_required"][0].update({"status": "accepted", "accepted_slug": "candidate"})
    merged = apply_reviews(base, compact)
    assert [row["status"] for row in merged["judgments"]] == ["accepted", "missing", "accepted"]
    assert merged["judgments"][0]["evidence"]["large"] == "x" * 1000
