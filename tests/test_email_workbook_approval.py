from app.decision_runs.email_approval import (
    find_latest_approval_reply,
)


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Messages:
    def __init__(self, messages):
        self.messages = messages
        self.query = None

    def list(self, *, userId, q, pageToken=None):
        self.query = q
        return _Request({"messages": [{"id": key} for key in self.messages]})

    def get(self, *, userId, id, format):
        return _Request(self.messages[id])

class _Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _Service:
    def __init__(self, messages):
        self.messages_api = _Messages(messages)

    def users(self):
        return _Users(self.messages_api)


def _message(message_id, *, sender, replied, internal_date, thread_id="thread-42"):
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": "Re: job_agent decision run — run-42"},
    ]
    if replied:
        headers.append({"name": "In-Reply-To", "value": "<original@example>"})
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": str(internal_date),
        "payload": {
            "headers": headers,
        },
    }


def test_find_latest_approval_reply_accepts_only_self_authored_reply():
    messages = {
        "original": _message("original", sender="candidate@example.com", replied=False, internal_date=1),
        "untrusted": _message("untrusted", sender="someone@example.com", replied=True, internal_date=3),
        "reply": _message("reply", sender="Candidate <candidate@example.com>", replied=True, internal_date=2),
    }
    service = _Service(messages)

    result = find_latest_approval_reply(
        service, "run-42", expected_self_email="candidate@example.com",
        expected_thread_id="thread-42",
    )

    assert result.gmail_message_id == "reply"
    assert result.gmail_thread_id == "thread-42"
    assert result.approver == "candidate@example.com"
    assert 'subject:"job_agent decision run — run-42"' in service.messages_api.query
    assert "has:attachment" not in service.messages_api.query


def test_find_latest_approval_reply_ignores_matching_subject_in_other_thread():
    messages = {
        "right-thread": _message(
            "right-thread",
            sender="candidate@example.com",
            replied=True,
            internal_date=2,
        ),
        "wrong-thread": _message(
            "wrong-thread",
            sender="candidate@example.com",
            replied=True,
            internal_date=3,
            thread_id="thread-other",
        ),
    }
    result = find_latest_approval_reply(
        _Service(messages),
        "run-42",
        expected_self_email="candidate@example.com",
        expected_thread_id="thread-42",
    )

    assert result.gmail_message_id == "right-thread"
    assert result.gmail_thread_id == "thread-42"
