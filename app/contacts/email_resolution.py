"""
Email derivation for a found contact: build every plausible candidate address
from the person's name against the company's canonical domain, MX-check the
domain, and return all candidates for the human reviewer to pick from — or
None when nothing is derivable at all.

Confirmed live (2026-08): outbound port 25 is blocked on this project's
network entirely — a raw TCP connect to `gmail.com`'s MX timed out exactly
like every corporate domain tested, so an SMTP RCPT probe can never complete
from here, regardless of target. This module previously tried to work around
that with a "read the domain's real pattern off one leaked address" mechanism
(`pattern_name_from_example`, `PATTERN_LEAKED`) — dropped per docs/adr/0005's
2026-08-02 addendum after it found nothing for 10 of 10 companies outside the
narrow "recruiter posts an email in a LinkedIn job ad" convention it was
built against. The replacement: no per-mailbox guessing at all. MX presence
is the only gate (does the domain accept mail at all), and every candidate
address is surfaced together rather than the system silently picking one.
"""
import re
import time
from dataclasses import dataclass
from typing import Callable

import dns.exception
import dns.resolver

UNVERIFIED = "unverified"


@dataclass(frozen=True)
class DerivedEmail:
    """The likeliest address plus its confidence. `status` is always
    UNVERIFIED — MX presence confirms the domain accepts mail, not that this
    specific local part is right, and no probe can run from this network to
    confirm further (see module docstring)."""
    address: str
    status: str


def _split_name(full_name: str) -> tuple[str, str] | None:
    parts = [p for p in re.sub(r"[^A-Za-z\s'-]", "", full_name).split() if p]
    if len(parts) < 2:
        return None
    return parts[0].lower(), parts[-1].lower()


# Public alias — `upsert.py` needs this to populate `Contact.first_name`/
# `last_name` at persist time, independent of email derivation.
split_name = _split_name


# The 4 candidate shapes shown to the reviewer, most-likely first. Trimmed
# from an earlier 8-pattern list per docs/adr/0005's 2026-08-02 addendum —
# with no way to probe or leak-confirm which shape is correct, showing 8
# near-duplicate guesses added noise without adding signal.
_PATTERN_SPECS: tuple[tuple[str, Callable[[str, str], str]], ...] = (
    ("first.last", lambda f, l: f"{f}.{l}"),
    ("first.l", lambda f, l: f"{f}.{l[0]}"),
    ("f.last", lambda f, l: f"{f[0]}.{l}"),
    ("first_last", lambda f, l: f"{f}_{l}"),
)
_PATTERNS = tuple(fn for _, fn in _PATTERN_SPECS)


def candidate_addresses(full_name: str, domain: str) -> list[str]:
    """All 4 candidate addresses for `full_name` at `domain`, most-likely
    first — cheap to recompute on demand, nothing stored beyond the name.
    Empty when the name can't be split into first/last (e.g. a mononym) or
    the domain is missing — the un-derivable case the caller gates on."""
    if not domain:
        return []
    names = _split_name(full_name)
    if names is None:
        return []
    first, last = names
    seen: set[str] = set()
    out = []
    for pattern in _PATTERNS:
        local = pattern(first, last)
        address = f"{local}@{domain}"
        if address not in seen:
            seen.add(address)
            out.append(address)
    return out


# A DNS lookup that fails is not the same as a domain with no mail records,
# and the original code conflated them by catching bare `Exception`. Measured
# 2026-08-03 on the first pilot batch: two of Caspex's four contacts were
# stored with NO email while the other two resolved fine, against the same
# domain in the same run — a transient resolver failure, since a retry derived
# both addresses immediately. That failure mode is silent and doubly harmful:
# the contact is stored emailless AND does not count toward its tier quota, so
# the company is written down as `partial` on what is really a network blip.
# At 890 companies this would suppress emails across the backlog invisibly.
#
# So: retry the transient failures, and never retry the definitive ones.
_TRANSIENT_DNS_ERRORS = (
    dns.resolver.NoNameservers,   # every nameserver SERVFAILed - usually temporary
    dns.resolver.LifetimeTimeout, # query deadline exceeded
    dns.exception.Timeout,
)
# NXDOMAIN (domain does not exist) and NoAnswer (domain exists, no MX record)
# are real answers, not failures. Retrying them just burns time to get the
# same result, and they are safe to remember.
_DEFINITIVE_DNS_ERRORS = (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer)

_MX_ATTEMPTS = 3
_MX_RETRY_BACKOFF_SECONDS = 0.5

# Memoised per process so every contact at one company sees the same answer.
# Without this, two contacts of the same employer can disagree about whether
# their shared domain accepts mail — exactly the Caspex symptom above. Only
# positives and DEFINITIVE negatives are cached; an exhausted retry stays
# uncached so a later call in the same run still gets a fresh chance.
_MX_CACHE: dict[str, str | None] = {}


def _get_mx_host(domain: str) -> str | None:
    """The lowest-preference MX host for `domain`, or None when the domain
    genuinely has no mail records (or DNS stayed unreachable across retries).

    Retries transient resolver failures rather than reporting them as "this
    domain accepts no mail" — see the comment above for why that distinction
    is load-bearing."""
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]

    for attempt in range(_MX_ATTEMPTS):
        try:
            answers = dns.resolver.resolve(domain, "MX")
            best = min(answers, key=lambda r: r.preference)
            host = str(best.exchange).rstrip(".")
            _MX_CACHE[domain] = host
            return host
        except _DEFINITIVE_DNS_ERRORS:
            _MX_CACHE[domain] = None
            return None
        except _TRANSIENT_DNS_ERRORS:
            if attempt == _MX_ATTEMPTS - 1:
                # Deliberately NOT cached: the domain may well have MX records
                # and we simply could not reach DNS to see them.
                return None
            time.sleep(_MX_RETRY_BACKOFF_SECONDS * (attempt + 1))
        except Exception:
            # Anything unclassified is treated as transient for the same
            # reason, but without a retry budget spent on it.
            return None
    return None


def derive_email(full_name: str, domain: str) -> DerivedEmail | None:
    """The default-guess address for `full_name` at `domain` (candidates[0]),
    or None when nothing is derivable: no name to build an address from, no
    domain, or the domain has no mail records at all. Always UNVERIFIED — see
    module docstring. Callers wanting the other 3 shapes for reviewer display
    should call `candidate_addresses` directly."""
    candidates = candidate_addresses(full_name, domain)
    if not candidates:
        return None
    if not _get_mx_host(domain):
        return None
    return DerivedEmail(candidates[0], UNVERIFIED)
