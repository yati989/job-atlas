"""
Gmail Drafts as the review-and-send surface (ADR-0009).

A direct Gmail API client, not the claude.ai MCP connector — the "mark it
sent when I send it" half of this pipeline is a polling job with no agent
in the loop, which only a script with its own cached credentials can do.

The `google-api-python-client`/`google-auth-oauthlib` imports are LAZY
(inside functions, not at module top) so importing this module doesn't
require those packages to be installed — same optional-dependency pattern
as app/resume/render.py's `_resolve_tectonic`, which lets the rest of the
outreach pipeline (drafting, pairing, persistence) work and be tested
without Gmail set up at all.

This module never sends *outreach* — `push_draft` creates a Gmail Draft;
a human sends it from Gmail. `sync_all` only ever observes what already
happened. The one exception is `send_self_report`, which sends the
full-pipeline run's own workbook to the candidate's own inbox; it is
hard-pinned to `SELF_EMAIL` and takes no recipient argument, so it cannot
become a second outreach-send path (see its docstring and
docs/adr/0011-self-send-exemption.md).
"""
import base64
import html as _html
import mimetypes
import re
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config.authentication import google_oauth_paths
from app.config.settings import SELF_EMAIL
from app.outreach.state import MailboxPort
from app.models.orm import OutreachDraft, utcnow
from app.outreach import drafts as draft_ops

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS_PATH = REPO_ROOT / "credentials.json"
TOKEN_PATH = REPO_ROOT / ".gmail_token.json"


def resolve_credential_paths(private_home: Path | None = None) -> tuple[Path, Path]:
    paths = google_oauth_paths(
        private_home, legacy_client=CREDENTIALS_PATH, legacy_token=TOKEN_PATH,
    )
    return paths.client, paths.token


class GmailNotConfigured(RuntimeError):
    """Raised when OAuth client credentials are missing, or required packages
    aren't installed — fails loudly with the fix, never silently no-ops."""


def _require_packages():
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        from googleapiclient.errors import HttpError  # noqa: F401
    except ImportError as exc:
        raise GmailNotConfigured(
            "google-api-python-client / google-auth-oauthlib not installed — "
            "`pip install -r requirements.txt`"
        ) from exc


def get_credentials(*, force_reauth: bool = False, private_home: Path | None = None):
    """Load a cached token, refreshing if expired. If no token exists yet,
    runs the interactive OAuth consent flow (opens a browser) — this is
    meant to happen ONCE, via `job-atlas auth setup`, not silently
    mid-pipeline. push_drafts.py / sync_outreach.py both expect a cached
    token to already exist and will raise GmailNotConfigured otherwise so an
    unattended run never blocks waiting on a browser that isn't there."""
    _require_packages()
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials_path, token_path = resolve_credential_paths(private_home)
    creds = None
    if token_path.exists() and not force_reauth:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise GmailNotConfigured(
                "Cached Gmail OAuth token is expired or revoked. Reauthorize with "
                "`job-atlas auth setup --authorize-gmail` before reconciling outreach."
            ) from exc
        token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
        return creds

    if not credentials_path.exists():
        raise GmailNotConfigured(
            "Google OAuth client credentials are not configured. Run "
            "`job-atlas auth setup` once, then authorize Gmail."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    return creds


def get_service(*, private_home: Path | None = None):
    _require_packages()
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=get_credentials(private_home=private_home))


# The one emphasis markup the skill writes: **text** marks a bold span.
# Renderer-agnostic on purpose — app/resume/render.py's `texb` Jinja filter
# understands the identical markup for the resume PDF, so a tailoring
# session only has to decide what to bold once, in one notation.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def strip_bold_markup(text: str) -> str:
    """The text/plain alternative part: **markers removed, content intact."""
    return _BOLD_RE.sub(lambda m: m.group(1), text)


def body_to_html(text: str) -> str:
    """The text/html alternative part: HTML-escape first (so literal
    <, >, & in the drafted copy can't break markup), THEN convert **spans**
    to <b> — safe in that order because escaping never touches '*'. Newlines
    become <br> since HTML collapses them otherwise."""
    escaped = _html.escape(text, quote=False)
    bolded = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
    return bolded.replace("\n", "<br>\n")


def _build_mime_message(*, to_email: str, subject: str, body: str, attachment_path: str | None, html_body: str | None = None) -> str:
    msg = MIMEMultipart("mixed")
    msg["To"] = to_email
    msg["Subject"] = subject

    # multipart/alternative: Gmail's compose UI renders the html part (so
    # **bold** markers actually show as bold when the user opens the draft
    # to edit/send), while the plain part is what sync_outreach.py's
    # _extract_plain_text reads back after the user sends — a real Gmail
    # send from the rich-text compose window always includes both, so this
    # mirrors what "sent from Gmail" already looks like on the wire.
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(strip_bold_markup(body), "plain"))
    alt.attach(MIMEText(html_body if html_body is not None else body_to_html(body), "html"))
    msg.attach(alt)

    if attachment_path:
        path = Path(attachment_path)
        if not path.exists():
            raise draft_ops.InvalidDraft(f"attachment not found: {attachment_path}")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        subtype = content_type.split("/", 1)[1]
        part = MIMEApplication(path.read_bytes(), _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=path.name)
        msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return raw


def create_draft(service, *, to_email: str, subject: str, body: str, attachment_path: str | None) -> dict[str, Any]:
    """One Gmail API call, creating exactly one draft. Returns the raw API
    response — caller extracts the draft id and thread id."""
    raw = _build_mime_message(
        to_email=to_email, subject=subject, body=body, attachment_path=attachment_path,
    )
    return service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()


def update_draft(service, gmail_draft_id: str, *, to_email: str, subject: str, body: str, attachment_path: str | None) -> dict[str, Any]:
    """Replace an existing Gmail draft's content in place — used when a row
    is re-drafted (e.g. re-tailored, or the skill's email format changes)
    before the user has sent it, so re-running never leaves a stale
    duplicate sitting in Drafts alongside the fresh one."""
    raw = _build_mime_message(
        to_email=to_email, subject=subject, body=body, attachment_path=attachment_path,
    )
    return service.users().drafts().update(
        userId="me", id=gmail_draft_id, body={"message": {"raw": raw}}
    ).execute()


class GmailMailboxAdapter:
    """MailboxPort adapter. It creates drafts only; reconciliation reads Gmail.
    Ambiguous events remain absent rather than being treated as bounces."""
    def __init__(self, service):
        self.service = service
        self._threads: dict[str, str] = {}
    def create_or_update_draft(self, *, draft_id, to_email, subject, body, attachment_path):
        response = update_draft(self.service, draft_id, to_email=to_email, subject=subject, body=body, attachment_path=attachment_path) if draft_id else create_draft(self.service, to_email=to_email, subject=subject, body=body, attachment_path=attachment_path)
        thread_id = response["message"]["threadId"]
        self._threads[response["id"]] = thread_id
        return response["id"], thread_id
    def delete_unsent_draft(self, draft_id): self.service.users().drafts().delete(userId="me", id=draft_id).execute()
    def sent_message(self, gmail_draft_id, gmail_thread_id=None):
        # A still-existing Gmail draft has not been manually sent.  Once it
        # disappears, a SENT message on the captured thread is the only
        # positive send signal we accept.
        if _draft_still_exists(self.service, gmail_draft_id):
            return None
        thread_id = gmail_thread_id or self._threads.get(gmail_draft_id)
        if not thread_id:
            return None
        message = _find_sent_message(self.service, thread_id)
        if message is None:
            return None
        internal = message.get("internalDate")
        sent_at = datetime.fromtimestamp(int(internal) / 1000, timezone.utc) if internal else utcnow()
        return {"message_id": message["id"], "sent_at": sent_at}

    def _thread_messages(self, gmail_thread_id):
        return self.service.users().threads().get(userId="me", id=gmail_thread_id, format="full").execute().get("messages", [])

    @staticmethod
    def _headers(message):
        return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}

    def thread_events(self, gmail_thread_id):
        events = []
        for message in self._thread_messages(gmail_thread_id):
            if "SENT" not in message.get("labelIds", []) and "DRAFT" not in message.get("labelIds", []):
                headers = self._headers(message)
                if headers.get("in-reply-to") or headers.get("references"):
                    events.append({"kind": "reply", "gmail_message_id": message["id"], "at": utcnow()})
        return events

    def delivery_failures(self, gmail_thread_id):
        events = []
        for message in self._thread_messages(gmail_thread_id):
            headers = self._headers(message)
            content_type = headers.get("content-type", "").lower()
            # DSNs are identified from protocol headers/MIME, never a sender
            # display string. A non-DSN is deliberately unclassified.
            if "delivery-status" in content_type or "multipart/report" in content_type:
                events.append({"kind": "bounce", "gmail_message_id": message["id"], "at": utcnow(), "headers": headers})
        return events

    def unclassified_events(self, gmail_thread_id):
        """Retain ambiguous inbound messages without calling them replies or bounces."""
        events = []
        for message in self._thread_messages(gmail_thread_id):
            if "SENT" in message.get("labelIds", []) or "DRAFT" in message.get("labelIds", []):
                continue
            headers = self._headers(message)
            content_type = headers.get("content-type", "").lower()
            if headers.get("in-reply-to") or headers.get("references"):
                continue
            if "delivery-status" in content_type or "multipart/report" in content_type:
                continue
            events.append({"kind": "unclassified_mail_event", "gmail_message_id": message["id"], "at": utcnow()})
        return events


class SelfSendRecipientBlocked(RuntimeError):
    """Raised if anything ever tries to make send_self_report deliver to a
    recipient other than the candidate's own configured address. This
    exists so the guard can never be bypassed by a caller-supplied
    argument — see send_self_report's docstring for why that matters."""


def send_self_report(service, *, subject: str, body: str, attachment_path: str | None = None, html_body: str | None = None) -> dict[str, Any]:
    """Send (not draft) one message to the candidate's own inbox — used by
    the full-pipeline run to deliver its workbook. This is the ONE function
    in this module that actually calls messages().send() rather than
    drafts().create()/update(): every other path in this module is
    deliberately draft-only (ADR-0009 — outreach is drafted, never sent, by
    this system) because a cold email under the candidate's name reaching a
    real stranger unreviewed is the risk that policy exists to prevent.

    A self-addressed run report doesn't touch that risk at all, so it's
    exempt (see docs/adr/0011-self-send-exemption.md) — but ONLY because
    the recipient is hard-pinned to `app.config.settings.SELF_EMAIL` and
    takes no caller-supplied recipient argument at all. Widening this
    function to accept a `to_email` parameter would silently turn it into
    a second, unguarded send path and must not be done — if a future
    stage needs to email someone else, that belongs in the drafted-review
    flow (`push_draft`), not here.
    """
    if not SELF_EMAIL:
        raise SelfSendRecipientBlocked("SELF_EMAIL is not configured — refusing to send anywhere.")

    raw = _build_mime_message(
        to_email=SELF_EMAIL, subject=subject, body=body, attachment_path=attachment_path,
        html_body=html_body,
    )
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def push_draft(session: Session, service, draft: OutreachDraft, *, decision_run_id: str | None = None) -> OutreachDraft:
    """Push one drafted (not blocked) row to Gmail. If it was already pushed
    once (gmail_draft_id set) and hasn't been sent, UPDATES that same Gmail
    draft in place rather than creating a second one — mirrors the DB's own
    dedup-on-to_email rule.

    Collision-risk drafts are pushed like any other (ADR-0012) —
    `draft.collision_risk` is informational only, surfaced by the run
    workbook, not a push gate. STATUS_BLOCKED_COLLISION_RISK is checked
    below only as a defensive no-op for any pre-ADR-0012 row that still
    carries it; no row is ever assigned that status going forward."""
    if draft.status == draft_ops.STATUS_BLOCKED_COLLISION_RISK:
        raise draft_ops.InvalidDraft(
            f"draft {draft.id} ({draft.to_email}) has the legacy blocked_collision_risk "
            "status — re-run save_draft to migrate it to drafted before pushing (ADR-0012)"
        )
    if draft.status == draft_ops.STATUS_SENT:
        raise draft_ops.InvalidDraft(f"draft {draft.id} was already sent")

    if draft.gmail_draft_id:
        response = update_draft(
            service, draft.gmail_draft_id, to_email=draft.to_email, subject=draft.subject,
            body=draft.body, attachment_path=draft.resume_path,
        )
    else:
        response = create_draft(
            service, to_email=draft.to_email, subject=draft.subject, body=draft.body,
            attachment_path=draft.resume_path,
        )
    result = draft_ops.mark_pushed(
        session, draft,
        gmail_draft_id=response["id"],
        gmail_thread_id=response["message"]["threadId"],
    )
    from app.outreach.state import record_legacy_gmail_draft
    attempt = record_legacy_gmail_draft(
        session, draft_id=draft.id, gmail_draft_id=result.gmail_draft_id,
        gmail_thread_id=result.gmail_thread_id,
    )
    if attempt is not None and attempt.message_id:
        from app.models.orm import OutreachMessage
        message = session.get(OutreachMessage, attempt.message_id)
        # A company-inbox draft can be safely retargeted to a stored Contact
        # after its generic-inbox ledger was created.  It has no frozen
        # contact manifest, so reporting it through the manifest-only funnel
        # would reject an otherwise valid in-place Gmail Draft update.  Keep
        # the durable retarget provenance on the attempt and exempt only that
        # explicit path; ordinary Contact drafts still require the manifest.
        retargeted_inbox = isinstance(attempt.evidence, dict) and (
            attempt.evidence.get("recipient_kind") == "stored_contact"
            and "retargeted_from" in attempt.evidence
        )
        if message and message.run_id and message.contact_id is not None and not retargeted_inbox:
            from app.decision_runs.outreach_funnel import report_gmail_draft_posted
            report_gmail_draft_posted(session, run_id=message.run_id,
                                      delivery_attempt_id=attempt.id)
    return result


def _extract_plain_text(payload: dict) -> str:
    """Walk a Gmail message payload for the first text/plain part."""
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        found = _extract_plain_text(part)
        if found:
            return found
    return ""


def _draft_still_exists(service, gmail_draft_id: str) -> bool:
    from googleapiclient.errors import HttpError
    try:
        service.users().drafts().get(userId="me", id=gmail_draft_id).execute()
        return True
    except HttpError as exc:
        if exc.resp.status == 404:
            return False
        raise


def _find_sent_message(service, thread_id: str) -> dict | None:
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    for message in thread.get("messages", []):
        if "SENT" in message.get("labelIds", []):
            return message
    return None


def sync_draft(session: Session, service, draft: OutreachDraft) -> str:
    """Reconcile one pushed draft against Gmail. Returns what happened:
    'unchanged' (still sitting in Drafts), 'sent' (flipped, sent_body
    pulled back), or 'gone_unconfirmed' (draft disappeared from Gmail but no
    SENT message was found on its thread — discarded, not sent; reported,
    never guessed at)."""
    if draft.status != draft_ops.STATUS_PUSHED:
        return "unchanged"

    if _draft_still_exists(service, draft.gmail_draft_id):
        return "unchanged"

    sent_message = _find_sent_message(service, draft.gmail_thread_id)
    if sent_message is None:
        return "gone_unconfirmed"

    body = _extract_plain_text(sent_message.get("payload", {})) or draft.body
    draft_ops.mark_sent(session, draft, sent_body=body, sent_at=utcnow())
    return "sent"


def sync_all(session: Session, service) -> dict[str, int]:
    from sqlalchemy import select
    pushed = list(session.execute(
        select(OutreachDraft).where(OutreachDraft.status == draft_ops.STATUS_PUSHED)
    ).scalars())

    counts = {"unchanged": 0, "sent": 0, "gone_unconfirmed": 0}
    for draft in pushed:
        outcome = sync_draft(session, service, draft)
        counts[outcome] += 1
    return counts
