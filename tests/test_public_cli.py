from __future__ import annotations

import json
from dataclasses import replace

import pytest

import job_search_agent
from job_search_agent import main


def test_init_command_creates_configured_database(tmp_path, capsys):
    database = tmp_path / "package.sqlite3"
    assert main([
        "init", "--database-url", f"sqlite:///{database}",
        "--private-home", str(tmp_path / "private"),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["dialect"] == "sqlite"
    assert payload["table_count"] > 0
    assert database.exists()


def test_plan_command_prints_reviewable_json_without_executing_sources(tmp_path, capsys):
    config = tmp_path / "profile.yaml"
    config.write_text(
        """\
version: 1
name: product-design
professions: [product designer]
search_terms: [product designer, ux designer]
relevance_terms: [product designer, ux designer]
countries: [IN]
arrangements: [remote]
seniority: [individual_contributor]
collection_window_days: 30
source_selection:
  mode: named
  sources: [weworkremotely]
""",
        encoding="utf-8",
    )

    assert main(["plan", "--config", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "product-design"
    assert payload["selected_sources"] == [
        {
            "name": "weworkremotely",
            "runtime": "http",
            "attendance_required": False,
            "prerequisites": [],
            "coverage": "global remote feed; country is listing evidence",
            "query_instances": 1,
        }
    ]
    assert payload["source_count"] == 1
    assert payload["query_instance_count"] == 1
    assert payload["collection_window_days"] == 30
    assert payload["date_policy"] == {
        "source_search_window": "requested_where_supported",
        "known_older_jobs_returned_by_source": "kept_if_other_gates_pass",
        "unknown_posting_date": "kept_with_unknown_age",
    }
    assert payload["job_type_policy"] == {
        "summary": "Internships and part-time jobs are excluded unless you explicitly ask to include them.",
        "include_internships": False,
        "include_part_time": False,
    }
    assert payload["requires_confirmation"] is True


def test_plan_summary_records_explicit_internship_opt_in(tmp_path, capsys):
    config = tmp_path / "profile.yaml"
    config.write_text(
        """\
version: 1
name: student-search
professions: [research assistant]
search_terms: [research assistant]
relevance_terms: [research assistant]
countries: [IN]
arrangements: [remote]
include_internships: true
source_selection: {mode: named, sources: [weworkremotely]}
""",
        encoding="utf-8",
    )

    assert main(["plan", "--config", str(config)]) == 0
    policy = json.loads(capsys.readouterr().out)["job_type_policy"]
    assert policy == {
        "summary": "Part-time jobs are excluded. Internships are included because you explicitly requested them.",
        "include_internships": True,
        "include_part_time": False,
    }


def test_accept_command_saves_profile_and_plan_can_reuse_it(
    tmp_path, capsys, monkeypatch,
):
    config = tmp_path / "profile.yaml"
    config.write_text(
        """\
version: 1
name: product-design
professions: [product designer]
search_terms: [product designer]
relevance_terms: [product designer]
countries: [IN]
arrangements: [remote]
seniority: [individual_contributor]
collection_window_days: 30
source_selection: {mode: named, sources: [weworkremotely]}
""",
        encoding="utf-8",
    )
    private_home = tmp_path / "private"

    assert main(
        [
            "accept-profile",
            "--config",
            str(config),
            "--private-home",
            str(private_home),
            "--accept",
        ]
    ) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["revision"] == 1

    assert main(
        ["plan", "--profile", "product-design", "--private-home", str(private_home)]
    ) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["profile"] == "product-design"
    assert planned["selected_sources"][0]["name"] == "weworkremotely"

    assert main([
        "start-run", "--profile", "product-design",
        "--private-home", str(private_home), "--confirm",
    ]) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["state"] == "collecting"

    assert main([
        "status", "--run-id", started["run_id"],
        "--private-home", str(private_home),
    ]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["sources"] == [{
        "source": "weworkremotely",
        "status": "pending",
        "attempt_count": 0,
        "outcome_counts": {},
        "failure_detail": None,
    }]

    monkeypatch.setattr(
        job_search_agent,
        "build_fetchers",
        lambda preview, allow_attended: {"weworkremotely": lambda _profile: []},
    )
    assert main([
        "collect", "--run-id", started["run_id"],
        "--private-home", str(private_home),
    ]) == 0
    collected = json.loads(capsys.readouterr().out)
    assert collected["state"] == "collected"


def test_resume_stops_when_source_details_drift_from_approved_snapshot(
    tmp_path, capsys, monkeypatch,
):
    config = tmp_path / "profile.yaml"
    config.write_text(
        """\
version: 1
name: product-design
professions: [product designer]
search_terms: [product designer]
relevance_terms: [product designer]
countries: [IN]
arrangements: [remote]
seniority: [individual_contributor]
collection_window_days: 30
source_selection: {mode: named, sources: [weworkremotely]}
""",
        encoding="utf-8",
    )
    private_home = tmp_path / "private"
    main(["accept-profile", "--config", str(config), "--private-home", str(private_home), "--accept"])
    capsys.readouterr()
    main(["start-run", "--profile", "product-design", "--private-home", str(private_home), "--confirm"])
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    changed = tuple(
        replace(source, coverage="changed after approval")
        if source.name == "weworkremotely" else source
        for source in job_search_agent.SOURCE_CATALOG
    )
    monkeypatch.setattr(job_search_agent, "SOURCE_CATALOG", changed)

    with pytest.raises(RuntimeError, match="no longer reproduces"):
        main(["collect", "--run-id", run_id, "--private-home", str(private_home)])
