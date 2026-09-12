"""Private first-use storage for optional provider credentials.

The public package keeps user secrets outside the checkout. Existing
repository-local configuration remains a read-only compatibility fallback.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values, load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_ENV_PATH = REPO_ROOT / ".env"
LEGACY_GOOGLE_CLIENT_PATH = REPO_ROOT / "credentials.json"
LEGACY_GMAIL_TOKEN_PATH = REPO_ROOT / ".gmail_token.json"
PRIVATE_ENV_NAME = "credentials.env"
GOOGLE_CLIENT_NAME = "google-oauth-client.json"
GMAIL_TOKEN_NAME = "gmail-token.json"


def default_private_home() -> Path:
    configured = os.getenv("JOB_ATLAS_HOME") or os.getenv("JOB_SEARCH_AGENT_HOME")
    if configured:
        return Path(configured).expanduser()
    current = Path.home() / ".job-atlas"
    legacy = Path.home() / ".job-search-agent"
    # Existing private-beta users keep their saved searches until they choose
    # to move them; fresh installations receive the Job Atlas directory.
    return legacy if legacy.exists() and not current.exists() else current


def private_environment_path(private_home: Path | None = None) -> Path:
    return Path(private_home or default_private_home()).expanduser() / PRIVATE_ENV_NAME


@dataclass(frozen=True)
class GoogleOAuthPaths:
    client: Path
    token: Path


def google_oauth_paths(
    private_home: Path | None = None,
    *,
    legacy_client: Path | None = None,
    legacy_token: Path | None = None,
) -> GoogleOAuthPaths:
    """Resolve private paths first and reuse old checkout files when present."""
    root = Path(private_home or default_private_home()).expanduser()
    legacy_client = legacy_client or LEGACY_GOOGLE_CLIENT_PATH
    legacy_token = legacy_token or LEGACY_GMAIL_TOKEN_PATH
    private_client = root / GOOGLE_CLIENT_NAME
    private_token = root / GMAIL_TOKEN_NAME
    client = private_client if private_client.is_file() else legacy_client
    token = (
        private_token
        if private_token.is_file() or not legacy_token.is_file()
        else legacy_token
    )
    return GoogleOAuthPaths(client=client, token=token)


def load_provider_environment(private_home: Path | None = None) -> None:
    """Load private settings, then the legacy checkout .env as fallback."""
    load_dotenv(private_environment_path(private_home), override=False)
    load_dotenv(LEGACY_ENV_PATH, override=False)


def _make_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _private_write(path: Path, content: str) -> Path:
    _make_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


_UNQUOTED_ENV_VALUE = re.compile(r"^[A-Za-z0-9._~/-]+$")


def _dotenv_value(value: str) -> str:
    if _UNQUOTED_ENV_VALUE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def save_bright_data_credentials(
    private_home: Path,
    *,
    api_key: str,
    secondary_api_key: str | None = None,
    zone: str = "serp_api1",
) -> Path:
    api_key = api_key.strip()
    secondary_api_key = (secondary_api_key or "").strip()
    zone = zone.strip()
    if not api_key:
        raise ValueError("Bright Data API key cannot be empty")
    if not zone:
        raise ValueError("Bright Data zone cannot be empty")
    lines = [f"BRIGHT_DATA_API_KEY={_dotenv_value(api_key)}"]
    if secondary_api_key:
        lines.append(f"BRIGHT_DATA_API_KEY2={_dotenv_value(secondary_api_key)}")
    lines.append(f"BRIGHT_DATA_ZONE={_dotenv_value(zone)}")
    return _private_write(
        private_environment_path(private_home), "\n".join(lines) + "\n"
    )


def save_google_oauth_client(private_home: Path, source: Path) -> Path:
    source = Path(source).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Google OAuth client file is not readable JSON: {source}") from exc
    client = payload.get("installed")
    if (
        not isinstance(client, dict)
        or not client.get("client_id")
        or not client.get("client_secret")
    ):
        raise ValueError(
            "Google OAuth desktop-client JSON must contain installed client_id and client_secret"
        )
    destination = Path(private_home).expanduser() / GOOGLE_CLIENT_NAME
    return _private_write(destination, json.dumps(payload, indent=2) + "\n")


def _provider_values(
    private_home: Path, environ: Mapping[str, str]
) -> tuple[dict, str | None]:
    private_path = private_environment_path(private_home)
    private_values = dict(dotenv_values(private_path)) if private_path.is_file() else {}
    if private_values.get("BRIGHT_DATA_API_KEY"):
        return private_values, "private_store"
    legacy_values = (
        dict(dotenv_values(LEGACY_ENV_PATH)) if LEGACY_ENV_PATH.is_file() else {}
    )
    if legacy_values.get("BRIGHT_DATA_API_KEY"):
        return legacy_values, "legacy_repo_env"
    if environ.get("BRIGHT_DATA_API_KEY"):
        return dict(environ), "environment"
    return {}, None


def credential_status(
    private_home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return presence and source metadata only; never return credential values."""
    root = Path(private_home or default_private_home()).expanduser()
    values, bright_source = _provider_values(
        root, environ if environ is not None else os.environ
    )
    google = google_oauth_paths(root)
    private_client = root / GOOGLE_CLIENT_NAME
    private_token = root / GMAIL_TOKEN_NAME
    client_configured = google.client.is_file()
    token_configured = google.token.is_file()
    return {
        "private_home": str(root),
        "bright_data": {
            "configured": bool(values.get("BRIGHT_DATA_API_KEY")),
            "secondary_key_configured": bool(values.get("BRIGHT_DATA_API_KEY2")),
            "zone": values.get("BRIGHT_DATA_ZONE") or "serp_api1",
            "source": bright_source,
        },
        "google_oauth": {
            "client_configured": client_configured,
            "client_source": (
                "private_store"
                if private_client.is_file()
                else ("legacy_repo_file" if client_configured else None)
            ),
            "gmail_token_configured": token_configured,
            "gmail_token_source": (
                "private_store"
                if private_token.is_file()
                else ("legacy_repo_file" if token_configured else None)
            ),
        },
        "setup_command": "job-atlas auth setup",
    }


def authentication_summary(private_home: Path | None = None) -> dict[str, object]:
    status = credential_status(private_home)
    ready_bright = bool(status["bright_data"]["configured"])
    ready_gmail = bool(status["google_oauth"]["gmail_token_configured"])
    summary = {
        "ready_for_contact_research": ready_bright,
        "ready_for_gmail_drafts": ready_gmail,
    }
    if not ready_bright or not ready_gmail:
        summary["first_use_instruction"] = (
            "Run `job-atlas auth setup` before paid contact research or "
            "creating Gmail drafts."
        )
    return summary
