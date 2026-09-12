from __future__ import annotations

import json
import os

from app.config.authentication import (
    credential_status,
    google_oauth_paths,
    save_bright_data_credentials,
    save_google_oauth_client,
)
from app.outreach import gmail
from job_search_agent import main


def test_bright_data_credentials_are_private_and_status_never_returns_secrets(tmp_path):
    private_home = tmp_path / "private"
    path = save_bright_data_credentials(
        private_home,
        api_key="bright-secret",
        secondary_api_key="second-secret",
        zone="custom-zone",
    )

    assert path.parent == private_home
    assert path.read_text(encoding="utf-8") == (
        "BRIGHT_DATA_API_KEY=bright-secret\n"
        "BRIGHT_DATA_API_KEY2=second-secret\n"
        "BRIGHT_DATA_ZONE=custom-zone\n"
    )
    if os.name != "nt":
        assert os.stat(private_home).st_mode & 0o777 == 0o700
        assert os.stat(path).st_mode & 0o777 == 0o600
    status = credential_status(private_home, environ={})
    assert status["bright_data"] == {
        "configured": True,
        "secondary_key_configured": True,
        "zone": "custom-zone",
        "source": "private_store",
    }
    assert "bright-secret" not in json.dumps(status)
    assert "second-secret" not in json.dumps(status)


def test_google_client_is_validated_copied_privately_and_reused_by_gmail(tmp_path):
    private_home = tmp_path / "private"
    downloaded = tmp_path / "client.json"
    downloaded.write_text(json.dumps({"installed": {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "auth_uri": "https://accounts.example/auth",
        "token_uri": "https://accounts.example/token",
    }}), encoding="utf-8")

    saved = save_google_oauth_client(private_home, downloaded)
    paths = google_oauth_paths(private_home)

    assert saved == private_home / "google-oauth-client.json"
    assert paths.client == saved
    assert paths.token == private_home / "gmail-token.json"
    if os.name != "nt":
        assert os.stat(saved).st_mode & 0o777 == 0o600
    assert "client-secret" not in json.dumps(credential_status(private_home, environ={}))
    assert gmail.resolve_credential_paths(private_home) == (paths.client, paths.token)


def test_google_paths_fall_back_to_legacy_repo_files(tmp_path, monkeypatch):
    private_home = tmp_path / "private"
    legacy_client = tmp_path / "credentials.json"
    legacy_token = tmp_path / ".gmail_token.json"
    legacy_client.write_text("{}", encoding="utf-8")
    legacy_token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail, "CREDENTIALS_PATH", legacy_client)
    monkeypatch.setattr(gmail, "TOKEN_PATH", legacy_token)

    assert gmail.resolve_credential_paths(private_home) == (
        legacy_client,
        legacy_token,
    )


def test_auth_setup_and_status_cli_do_not_print_secret(tmp_path, monkeypatch, capsys):
    private_home = tmp_path / "private"
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "cli-secret")

    assert main([
        "auth", "setup", "--bright-data", "--zone", "serp-test",
        "--private-home", str(private_home),
    ]) == 0
    setup_output = capsys.readouterr().out
    assert "cli-secret" not in setup_output
    assert json.loads(setup_output)["bright_data"]["configured"] is True

    assert main(["auth", "status", "--private-home", str(private_home)]) == 0
    status_output = capsys.readouterr().out
    assert "cli-secret" not in status_output
    assert json.loads(status_output)["setup_command"] == "job-atlas auth setup"


def test_init_reports_clear_first_use_instruction_when_credentials_are_missing(
    tmp_path, capsys, monkeypatch,
):
    from app.config import authentication

    monkeypatch.setattr(authentication, "LEGACY_ENV_PATH", tmp_path / "absent.env")
    monkeypatch.setattr(
        authentication, "LEGACY_GOOGLE_CLIENT_PATH", tmp_path / "absent-client.json",
    )
    monkeypatch.setattr(
        authentication, "LEGACY_GMAIL_TOKEN_PATH", tmp_path / "absent-token.json",
    )
    monkeypatch.delenv("BRIGHT_DATA_API_KEY", raising=False)
    private_home = tmp_path / "private"
    assert main(["init", "--private-home", str(private_home)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["authentication"]["ready_for_contact_research"] is False
    assert payload["authentication"]["ready_for_gmail_drafts"] is False
    assert payload["authentication"]["first_use_instruction"] == (
        "Run `job-atlas auth setup` before paid contact research or "
        "creating Gmail drafts."
    )
