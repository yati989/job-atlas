"""Google Drive-backed approval sheet creation and readback.

The detached approval watcher cannot use Codex's interactive Drive connector,
so it has a dedicated, narrowly-scoped OAuth token.  The app can access only
files it creates or that the user explicitly opens with it (``drive.file``).
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any


SCOPES = ["https://www.googleapis.com/auth/drive.file"]
REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS_PATH = REPO_ROOT / "credentials.json"
TOKEN_PATH = REPO_ROOT / ".google_sheets_token.json"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class GoogleSheetsNotConfigured(RuntimeError):
    """The detached pipeline has no usable Google Drive credential."""


def _require_packages() -> None:
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload  # noqa: F401
    except ImportError as exc:
        raise GoogleSheetsNotConfigured(
            "google-api-python-client / google-auth-oauthlib not installed — "
            "`pip install -r requirements.txt`"
        ) from exc


def get_credentials(*, force_reauth: bool = False, interactive: bool = False):
    """Load/refresh cached Drive credentials; never open a browser implicitly."""
    _require_packages()
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_PATH.exists() and not force_reauth:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.valid and creds.has_scopes(SCOPES):
        return creds
    if creds and creds.expired and creds.refresh_token and creds.has_scopes(SCOPES):
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise GoogleSheetsNotConfigured(
                "Cached Google Sheets OAuth token is expired or revoked. Reauthorize with "
                "`python -m scripts.google_sheets_auth`."
            ) from exc
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds
    if not interactive:
        raise GoogleSheetsNotConfigured(
            "A cached Google Sheets OAuth token is required. Run "
            "`python -m scripts.google_sheets_auth` once before an unattended pipeline."
        )
    if not CREDENTIALS_PATH.exists():
        raise GoogleSheetsNotConfigured(
            f"{CREDENTIALS_PATH} not found. Add the Google OAuth desktop client "
            "credentials already used by Gmail, then retry."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_service(*, interactive: bool = False):
    _require_packages()
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=get_credentials(interactive=interactive))


def create_approval_spreadsheet(service, workbook_path: str | Path, *, title: str) -> dict[str, str]:
    """Import an XLSX as a native Google Sheet and return provider-observed IDs."""
    from googleapiclient.http import MediaFileUpload

    path = Path(workbook_path)
    if not path.is_file():
        raise ValueError(f"approval workbook not found: {path}")
    result: dict[str, Any] = service.files().create(
        body={"name": title, "mimeType": GOOGLE_SHEET_MIME_TYPE},
        media_body=MediaFileUpload(str(path), mimetype=XLSX_MIME_TYPE, resumable=False),
        fields="id,name,mimeType,webViewLink",
    ).execute()
    if result.get("mimeType") != GOOGLE_SHEET_MIME_TYPE:
        raise RuntimeError("Google Drive did not convert the approval workbook to a native Sheet")
    spreadsheet_id = str(result.get("id") or "")
    spreadsheet_url = str(result.get("webViewLink") or "")
    if not spreadsheet_id or not spreadsheet_url:
        raise RuntimeError("Google Drive returned no durable approval Sheet ID/link")
    return {"spreadsheet_id": spreadsheet_id, "spreadsheet_url": spreadsheet_url}


def export_approval_spreadsheet(
    service, spreadsheet_id: str, destination: str | Path,
) -> Path:
    """Export the exact recorded native Sheet to XLSX for existing validation."""
    from googleapiclient.http import MediaIoBaseDownload

    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    request = service.files().export_media(fileId=spreadsheet_id, mimeType=XLSX_MIME_TYPE)
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    content = buffer.getvalue()
    if not content.startswith(b"PK"):
        raise ValueError("Google Sheet export is not a valid .xlsx container")
    output.write_bytes(content)
    return output
