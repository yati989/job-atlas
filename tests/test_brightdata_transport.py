import json

import httpx
import pytest
from tenacity import RetryError

from app.contacts import search_source


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        # Parsed SERPs normally give us the destination URL directly.  Keep
        # country and www hosts because both are public LinkedIn profile URLs.
        (
            "https://www.linkedin.com/in/alex-morgan-example?trk=public_profile",
            "https://www.linkedin.com/in/alex-morgan-example",
        ),
        (
            "https://in.linkedin.com/in/sam-rivera-example/#experience",
            "https://in.linkedin.com/in/sam-rivera-example",
        ),
        # Google sometimes leaves its outbound /url redirect in an organic
        # row.  Bright Data must unwrap only that known Google wrapper before
        # applying the public-profile URL check.
        (
            "https://www.google.com/url?sa=t&url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Ftaylor-lee-example%3Ftrk%3Dpublic_profile",
            "https://www.linkedin.com/in/taylor-lee-example",
        ),
        (
            "https://www.google.co.in/url?q=https%3A%2F%2Fin.linkedin.com%2Fin%2Fjordan-kim-example%23about",
            "https://in.linkedin.com/in/jordan-kim-example",
        ),
    ],
)
def test_bright_data_profiles_normalizes_public_linkedin_profile_urls(
    monkeypatch, raw_url, expected_url,
):
    """A wrapped public profile must reach agentic judgement, not be lost
    before it.  This is deliberately structural: company/title relevance is
    judged later by the contact-search agent."""
    monkeypatch.setattr(
        search_source,
        "bright_data_search",
        lambda *_args, **_kwargs: [
            search_source.SerpResult("Person", raw_url, "public profile")
        ],
    )
    monkeypatch.setattr(search_source, "_log_bright_data_call", lambda *_args, **_kwargs: None)

    profiles = search_source.bright_data_profiles("site:linkedin.com/in Person")

    assert [profile.url for profile in profiles] == [expected_url]


@pytest.mark.parametrize(
    "raw_url",
    [
        "https://www.linkedin.com/jobs/view/product-manager-123",
        "https://www.linkedin.com/company/onelab-ventures",
        "https://www.linkedin.com/in/",
        "https://linkedin.com.evil.example/in/person",
        "https://search.example/redirect?url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fperson",
        "https://www.google.com/search?url=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fperson",
        "https://[malformed",
    ],
)
def test_bright_data_profiles_rejects_non_profile_or_untrusted_redirect_urls(
    monkeypatch, raw_url,
):
    monkeypatch.setattr(
        search_source,
        "bright_data_search",
        lambda *_args, **_kwargs: [
            search_source.SerpResult("Result", raw_url, "not a public profile")
        ],
    )
    monkeypatch.setattr(search_source, "_log_bright_data_call", lambda *_args, **_kwargs: None)

    assert search_source.bright_data_profiles("site:linkedin.com/in Person") == []


def test_bright_data_profiles_logs_only_aggregate_url_diagnostics_when_all_rows_fail(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(search_source, "BRIGHT_DATA_CALL_LOG", tmp_path / "calls.jsonl")
    monkeypatch.setattr(
        search_source,
        "bright_data_search",
        lambda *_args, **_kwargs: [
            search_source.SerpResult("Job result", "https://www.linkedin.com/jobs/view/123", "private title"),
            search_source.SerpResult("Company result", "https://www.linkedin.com/company/onelab", "private snippet"),
            search_source.SerpResult("Redirect", "https://search.example/redirect?url=https://www.linkedin.com/in/person", "email@example.com"),
            search_source.SerpResult("Missing", "", "another private snippet"),
        ],
    )

    assert search_source.bright_data_profiles("site:linkedin.com/in Person") == []

    entry = json.loads(search_source.BRIGHT_DATA_CALL_LOG.read_text().strip())
    assert entry["profile_results"] == 0
    assert entry["profile_filter_diagnostic"] == {
        "url_host_counts": {
            "www.linkedin.com": 2,
            "search.example": 1,
            "[missing]": 1,
        },
        "url_shape_counts": {
            "linkedin_non_profile_path": 2,
            "other_host": 1,
            "missing_url": 1,
        },
    }
    serialized = json.dumps(entry["profile_filter_diagnostic"])
    assert "private title" not in serialized
    assert "private snippet" not in serialized
    assert "email@example.com" not in serialized
    assert "/in/person" not in serialized


def test_bright_data_fetch_uses_serp_parsed_json_contract(monkeypatch):
    """The JSON response envelope is unwrapped to the parsed SERP body."""

    captured = {}
    expected = {
        "organic": [
            {
                "title": "A Person - Senior Data Scientist",
                "link": "https://in.linkedin.com/in/a-person",
                "description": "Senior Data Scientist at ThoughtFocus",
            }
        ]
    }

    class Response:
        content = b"{}"
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"status_code": 200, "headers": {}, "body": expected}

    class Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, endpoint, json):
            captured["endpoint"] = endpoint
            captured["payload"] = json
            return Response()

    monkeypatch.setattr(search_source.httpx, "Client", Client)
    monkeypatch.setattr(search_source.settings, "BRIGHT_DATA_API_KEY", "test-key")
    monkeypatch.setattr(search_source.settings, "BRIGHT_DATA_ZONE", "serp_api1")

    result = search_source._bright_data_fetch("https://www.google.com/search?q=test")

    assert captured["payload"] == {
        "zone": "serp_api1",
        "url": "https://www.google.com/search?q=test",
        "format": "json",
        "data_format": "parsed",
    }
    assert result == expected


def test_bright_data_fetch_accepts_json_string_envelope_body(monkeypatch):
    expected = {"organic": [{"title": "Person", "link": "https://in.linkedin.com/in/person"}]}

    class Response:
        content = b"{}"
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"status_code": 200, "headers": {}, "body": json.dumps(expected)}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, endpoint, json):
            return Response()

    monkeypatch.setattr(search_source.httpx, "Client", Client)

    assert search_source._bright_data_fetch_once(
        "https://www.google.com/search?q=test",
        recover_ip_forbidden=False,
    ) == expected


def test_bright_data_fetch_rejects_embedded_provider_error(monkeypatch):
    class Response:
        content = b"{}"
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "status_code": 401,
                "headers": {
                    "x-brd-error": "Auth Failed (code: ip_blacklisted)",
                    "x-brd-err-msg": "The current IP is blacklisted",
                },
                "body": "",
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, endpoint, json):
            return Response()

    monkeypatch.setattr(search_source.httpx, "Client", Client)

    try:
        search_source._bright_data_fetch_once(
            "https://www.google.com/search?q=test",
            recover_ip_forbidden=False,
        )
    except search_source.SearchBlocked as exc:
        assert "401" in str(exc)
        assert "ip_blacklisted" in str(exc)
    else:
        raise AssertionError("expected embedded provider failure to be raised")


def test_contact_search_distributes_labelled_calls_across_both_api_keys(
    monkeypatch,
):
    monkeypatch.setattr(search_source.settings, "BRIGHT_DATA_API_KEY", "key-one")
    monkeypatch.setattr(search_source.settings, "BRIGHT_DATA_API_KEY2", "key-two")

    selected = {
        search_source._contact_search_api_key(
            company_id,
            "data_ai",
            tier,
            attempt,
        )
        for company_id in range(1, 20)
        for tier in ("head", "hiring_manager", "ic", "talent_acquisition")
        for attempt in (1, 2)
    }

    assert selected == {"key-one", "key-two"}


def test_bright_data_fetch_surfaces_provider_error_on_empty_response(monkeypatch):
    class Response:
        content = b""
        headers = {"x-brd-error": "Auth Failed (code: ip_blacklisted)"}

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, endpoint, json):
            return Response()

    monkeypatch.setattr(search_source.httpx, "Client", Client)

    try:
        search_source._bright_data_fetch("https://www.google.com/search?q=test")
    except RetryError as exc:
        assert "ip_blacklisted" in str(exc.last_attempt.exception())
    else:
        raise AssertionError("expected the Bright Data provider error")


def test_bright_data_fetch_allowlists_current_ip_then_retries(monkeypatch):
    """An ``ip_forbidden`` response is recoverable: detect this machine's
    current public IP, add it to the configured zone, then retry the same SERP
    request without requiring a manual dashboard update."""

    calls = []
    expected = {"organic": []}

    class Response:
        def __init__(self, *, content=b"{}", headers=None, payload=None):
            self.content = content
            self.headers = headers or {}
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, endpoint):
            calls.append(("get", endpoint))
            return Response(payload={"ip": "49.36.221.2"})

        def post(self, endpoint, json):
            calls.append(("post", endpoint, json))
            serp_calls = [c for c in calls if c[:2] == ("post", search_source.BRIGHT_DATA_ENDPOINT)]
            if endpoint == search_source.BRIGHT_DATA_ENDPOINT and len(serp_calls) == 1:
                return Response(
                    content=b"",
                    headers={"x-brd-error": "Auth Failed (code: ip_forbidden)"},
                )
            if endpoint == search_source.BRIGHT_DATA_WHITELIST_ENDPOINT:
                return Response(payload={"status": "ok"})
            return Response(payload=expected)

    monkeypatch.setattr(search_source.httpx, "Client", Client)
    monkeypatch.setattr(search_source.settings, "BRIGHT_DATA_API_KEY", "test-key")
    monkeypatch.setattr(search_source.settings, "BRIGHT_DATA_ZONE", "serp_api1")

    result = search_source._bright_data_fetch("https://www.google.com/search?q=test")

    assert result == expected
    assert ("get", search_source.PUBLIC_IP_ENDPOINT) in calls
    assert (
        "post",
        search_source.BRIGHT_DATA_WHITELIST_ENDPOINT,
        {"ip": "49.36.221.2", "zone": "serp_api1"},
    ) in calls
    assert len([c for c in calls if c[:2] == ("post", search_source.BRIGHT_DATA_ENDPOINT)]) == 2
    client_kwargs = [c[1] for c in calls if c[0] == "client"]
    assert any(kwargs.get("headers", {}).get("Authorization") == "Bearer test-key" for kwargs in client_kwargs)
    assert any("headers" not in kwargs for kwargs in client_kwargs), (
        "public-IP discovery must use a separate client without the Bright Data bearer token"
    )


def test_bright_data_search_once_never_retries_or_refetches_for_ip_recovery(
    monkeypatch,
):
    calls = []

    class Response:
        content = b""
        headers = {"x-brd-error": "Auth Failed (code: ip_forbidden)"}

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, endpoint, json):
            calls.append((endpoint, json))
            return Response()

    monkeypatch.setattr(search_source.httpx, "Client", Client)
    monkeypatch.setattr(
        search_source,
        "_allowlist_ip",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not allowlist")),
    )

    try:
        search_source.bright_data_search_once("Amazon Glassdoor")
    except search_source.SearchBlocked as exc:
        assert "ip_forbidden" in str(exc)
    else:
        raise AssertionError("expected the single request to fail without retry")

    assert len(calls) == 1


def test_allowlist_ip_explains_required_bright_data_role(monkeypatch):
    class Client:
        def post(self, endpoint, json):
            request = httpx.Request("POST", endpoint)
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(search_source.settings, "BRIGHT_DATA_ZONE", "serp_api1")

    try:
        search_source._allowlist_ip(Client(), "49.36.221.2")
    except search_source.SearchBlocked as exc:
        assert "HTTP 403" in str(exc)
        assert "Admin or Ops" in str(exc)
    else:
        raise AssertionError("expected a clear allowlist permission error")
