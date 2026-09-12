"""Regression tests for MX lookup retry.

The bug: `_get_mx_host` caught bare `Exception`, so a transient DNS failure
was indistinguishable from "this domain has no mail records". Two of four
contacts at the same company were stored with no email, and the company was
marked `partial` on what was really a network blip.
"""
import dns.exception
import dns.resolver
import pytest

from app.contacts import email_resolution as er


class _Answer:
    def __init__(self, preference, exchange):
        self.preference = preference
        self.exchange = exchange


@pytest.fixture(autouse=True)
def _clear_cache():
    er._MX_CACHE.clear()
    yield
    er._MX_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(er.time, "sleep", lambda _s: None)


def _resolver(monkeypatch, side_effects):
    """Patch dns.resolver.resolve to walk `side_effects`, one per call."""
    calls = {"n": 0}

    def fake_resolve(domain, rdtype):
        effect = side_effects[calls["n"]]
        calls["n"] += 1
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr(er.dns.resolver, "resolve", fake_resolve)
    return calls


def test_transient_failure_is_retried_and_succeeds(monkeypatch):
    """A first lookup can time out before a retry succeeds."""
    calls = _resolver(monkeypatch, [
        dns.exception.Timeout(),
        [_Answer(10, "mx.example.com.")],
    ])
    assert er._get_mx_host("example.com") == "mx.example.com"
    assert calls["n"] == 2


def test_no_answer_is_definitive_and_not_retried(monkeypatch):
    """A domain that exists with no MX record is a real answer, not a failure."""
    calls = _resolver(monkeypatch, [dns.resolver.NoAnswer()])
    assert er._get_mx_host("no-mail.example") is None
    assert calls["n"] == 1


def test_nxdomain_is_definitive_and_not_retried(monkeypatch):
    calls = _resolver(monkeypatch, [dns.resolver.NXDOMAIN()])
    assert er._get_mx_host("nope.example") is None
    assert calls["n"] == 1


def test_transient_failure_exhausts_retries_then_gives_up(monkeypatch):
    calls = _resolver(monkeypatch, [dns.exception.Timeout()] * er._MX_ATTEMPTS)
    assert er._get_mx_host("flaky.example") is None
    assert calls["n"] == er._MX_ATTEMPTS


def test_exhausted_transient_failure_is_not_cached(monkeypatch):
    """A domain we simply could not reach must stay retryable later in the run
    — caching that as 'no mail' is what silently suppressed emails."""
    _resolver(monkeypatch, [dns.exception.Timeout()] * er._MX_ATTEMPTS)
    assert er._get_mx_host("flaky.example") is None
    assert "flaky.example" not in er._MX_CACHE


def test_positive_and_definitive_results_are_cached(monkeypatch):
    calls = _resolver(monkeypatch, [[_Answer(10, "mx.example.com.")]])
    assert er._get_mx_host("example.com") == "mx.example.com"
    assert er._get_mx_host("example.com") == "mx.example.com"
    assert calls["n"] == 1, "second call should be served from cache"


def test_same_domain_never_disagrees_across_contacts(monkeypatch):
    """Contacts sharing a domain get the same cached MX answer."""
    _resolver(monkeypatch, [
        dns.exception.Timeout(),
        [_Answer(10, "mx.example.com.")],
    ])
    names = ["Alex Rivera", "Casey Morgan", "Jordan Lee", "Taylor Singh"]
    derived = [er.derive_email(n, "example.com") for n in names]
    assert all(d is not None for d in derived)
    assert [d.address for d in derived] == [
        "alex.rivera@example.com",
        "casey.morgan@example.com",
        "jordan.lee@example.com",
        "taylor.singh@example.com",
    ]
