"""Detect a self-authored approval signal in its authoritative Gmail thread."""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any

from app.config.settings import SELF_EMAIL


class ApprovalReplyNotFound(ValueError):
    """No qualifying reply exists yet; callers may safely poll."""


@dataclass(frozen=True)
class ApprovalReply:
    gmail_message_id: str
    gmail_thread_id: str | None
    internal_date: int
    approver: str


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in message.get("payload", {}).get("headers", [])
    }


def _message_refs(service, query: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    page_token = None
    while True:
        request = service.users().messages().list(
            userId="me", q=query, pageToken=page_token,
        )
        page = request.execute()
        refs.extend(page.get("messages", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            return refs


def find_latest_approval_reply(
    service, run_id: str, *,
    expected_thread_id: str,
    expected_self_email: str | None = SELF_EMAIL,
    excluded_message_ids: set[str] | None = None,
    after_internal_date: int | None = None,
) -> ApprovalReply:
    """Return the newest self-authored reply in the recorded approval thread."""
    if not expected_self_email:
        raise ValueError("SELF_EMAIL is not configured")
    subject = f"job_agent decision run — {run_id}"
    query = f'in:anywhere subject:"{subject}"'
    candidates: list[tuple[int, dict[str, Any], str]] = []
    excluded = excluded_message_ids or set()
    for ref in _message_refs(service, query):
        if ref["id"] in excluded:
            continue
        message = service.users().messages().get(
            userId="me", id=ref["id"], format="full",
        ).execute()
        internal_date = int(message.get("internalDate", 0))
        if after_internal_date is not None and internal_date <= after_internal_date:
            continue
        headers = _headers(message)
        if message.get("threadId") != expected_thread_id:
            continue
        if not (headers.get("in-reply-to") or headers.get("references")):
            continue
        if subject not in headers.get("subject", ""):
            continue
        sender = parseaddr(headers.get("from", ""))[1].lower()
        if sender != expected_self_email.lower():
            continue
        candidates.append((internal_date, message, sender))
    if not candidates:
        raise ApprovalReplyNotFound(
            f"no approval reply found for run {run_id}; update the linked Sheet and reply in the decision email thread"
        )
    internal_date, message, sender = max(candidates, key=lambda item: item[0])
    return ApprovalReply(
        gmail_message_id=message["id"],
        gmail_thread_id=message.get("threadId"),
        internal_date=internal_date,
        approver=sender,
    )
