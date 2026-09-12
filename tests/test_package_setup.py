from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.packaging.skills import (
    END_USER_SKILLS,
    bundled_skill_status,
    install_bundled_skills,
    uninstall_bundled_skills,
)
from job_search_agent import main


def test_setup_initializes_database_and_installs_user_skills(tmp_path, capsys):
    private_home = tmp_path / "private"
    skills_dir = tmp_path / "skills"

    assert main([
        "setup",
        "--private-home", str(private_home),
        "--skills-dir", str(skills_dir),
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["product"] == "Job Atlas"
    assert payload["status"] == "ready"
    assert payload["database"]["dialect"] == "sqlite"
    assert (private_home / "data" / "job-atlas.sqlite3").is_file()
    assert [item["name"] for item in payload["skills"]] == list(END_USER_SKILLS)
    for name in END_USER_SKILLS:
        assert (skills_dir / name / "SKILL.md").is_file()


def test_skill_update_refuses_unmanaged_or_modified_directories(tmp_path):
    skills_dir = tmp_path / "skills"
    unmanaged = skills_dir / END_USER_SKILLS[0]
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text("mine", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unmanaged skill"):
        install_bundled_skills(skills_dir)

    install_bundled_skills(skills_dir, force=True)
    (unmanaged / "SKILL.md").write_text("locally changed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="locally modified"):
        install_bundled_skills(skills_dir)


def test_skill_status_and_uninstall_only_manage_job_atlas_skills(tmp_path):
    skills_dir = tmp_path / "skills"
    install_bundled_skills(skills_dir)
    assert {row["status"] for row in bundled_skill_status(skills_dir)} == {"installed"}
    assert {row["status"] for row in uninstall_bundled_skills(skills_dir)} == {"removed"}
    assert {row["status"] for row in bundled_skill_status(skills_dir)} == {"missing"}


def test_bundled_skill_inventory_excludes_maintainer_workflows():
    assert "full-pipeline" in END_USER_SKILLS
    assert "add-connector" not in END_USER_SKILLS
    assert "verify-connector" not in END_USER_SKILLS
