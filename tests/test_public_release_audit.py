from __future__ import annotations

from pathlib import Path

from scripts.audit_public_release import audit_public_tree


def test_public_tree_audit_reports_private_and_machine_specific_content(tmp_path: Path):
    (tmp_path / "app" / "resume").mkdir(parents=True)
    personal_email = "candidate" + "@gmail.com"
    (tmp_path / "app" / "resume" / "master.yaml").write_text(
        f'contact:\n  email: "{personal_email}"\n', encoding="utf-8"
    )
    absolute_user_path = "/" + "Users/developer/Projects/job-search-agent"
    (tmp_path / "script.sh").write_text(
        f'project_root="{absolute_user_path}"\n', encoding="utf-8"
    )

    findings = audit_public_tree(tmp_path)

    assert [(finding.path, finding.rule) for finding in findings] == [
        ("app/resume/master.yaml", "private-resume-path"),
        ("app/resume/master.yaml", "personal-email"),
        ("script.sh", "absolute-user-path"),
    ]


def test_public_tree_audit_accepts_neutral_examples(tmp_path: Path):
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "candidate-profile.example.yaml").write_text(
        'contact:\n  email: "candidate@example.com"\n', encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text(
        "SELF_EMAIL=you@example.com\n", encoding="utf-8"
    )

    assert audit_public_tree(tmp_path) == []


def test_public_tree_audit_rejects_packaged_authentication_files(tmp_path: Path):
    (tmp_path / "credentials.env").write_text("TOKEN=private\n", encoding="utf-8")
    (tmp_path / "google-oauth-client.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "gmail-token.json").write_text("{}\n", encoding="utf-8")

    assert [(finding.path, finding.rule) for finding in audit_public_tree(tmp_path)] == [
        ("credentials.env", "private-credential-path"),
        ("gmail-token.json", "private-credential-path"),
        ("google-oauth-client.json", "private-credential-path"),
    ]
