"""
Public LinkedIn profile discovery via search engines.

The insight: search engines already index public LinkedIn profiles, and a
result row like

    Satish Bogala - Senior Data Scientist at Quantzig | Ex-Tiger Analytics ...

already carries name + headline + company + profile URL, with no LinkedIn
session touched. So we never call LinkedIn's gated search at all; we ask a
search engine "who at <company> has <role>-ish titles, restricted to
linkedin.com/in profiles" and parse the results.

Transport ladder (cheap-first, same philosophy as the connectors' escalation
ladder in CLAUDE.md):
  - Serper.dev (default when SERPER_API_KEY is set) — block-free JSON backed
    by real Google results, the reliable path for real volume. Chosen over
    Brave/Google-CSE because (checked live, 2026): Brave killed its free tier
    (card required, metered), and Google's own Custom Search JSON API is
    closed to new signups — Serper's 2,500-query no-card free trial was the
    option that actually works without payment, and Google indexes LinkedIn
    profiles more thoroughly than Bing/DDG anyway. Free credit alone covers
    the backlog because we issue ONE broad query per company/category, not
    one per title.
  - DuckDuckGo HTML (no key) — a zero-setup fallback for spot-checks. NOTE:
    confirmed live it bot-blocks ("anomaly-modal") after ~2 automated
    requests even through a proxy, so it is NOT viable for volume — kept only
    so the module is usable without any API key for a quick one-off check.

This module only *discovers and parses* profiles into `RawProfile`s. Tier
classification (which rung a profile belongs to) stays in
`people_search.classify_profiles`.
"""
import ipaddress
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.browser.session_utils import human_delay, launch_anonymous, wait_past_interstitial
from app.config import settings

logger = logging.getLogger("contacts.search_source")

SERPER_ENDPOINT = "https://google.serper.dev/search"
DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
GOOGLE_SEARCH_URL = "https://www.google.com/search"
BRIGHT_DATA_ENDPOINT = "https://api.brightdata.com/request"
BRIGHT_DATA_WHITELIST_ENDPOINT = "https://api.brightdata.com/zone/whitelist"
PUBLIC_IP_ENDPOINT = "https://api.ipify.org?format=json"

# Confirmed live (2026-07-21) against real Google SERPs: each organic result
# sits in a div.tF2Cxc, title in h3.LC20lb, snippet in one of these (Google
# uses more than one class depending on result type — data-sncf is the most
# common, the others are fallbacks seen live). Do not assume these are
# stable forever — Google changes result markup periodically; re-confirm
# live if this transport starts returning zero rows despite a 200 response.
_RESULT_CONTAINER_SELECTOR = "div.tF2Cxc"
_RESULT_TITLE_SELECTOR = "h3.LC20lb"
_RESULT_SNIPPET_SELECTOR = "div[data-sncf], div.VwiC3b, span.aCOpRe, div.MUxGbd"
_RESULTS_PRESENCE_SELECTOR = "#search"
# Text/markup signals of a block or challenge page — checked against the
# raw page HTML before trusting any parsed rows.
_BLOCK_MARKERS = ("google.com/sorry", "unusual traffic", "recaptcha", "captcha-form")


class BudgetExceeded(Exception):
    """Raised by `bright_data_profiles` BEFORE any request is sent when the
    caller's (company, search_group, tier) triple has already spent its
    `ladder.MAX_SEARCHES_PER_TIER` (or MAX_SEARCHES_EXEC_FALLBACK) calls. This
    is the hard cap search_plan.py's plan describes — see that module's
    docstring for why a code-enforced refusal replaced a prose one."""


class SearchBlocked(Exception):
    """Raised when the browser transport detects a Google block/challenge
    page, so the caller can fall back to Serper/DDG instead of returning an
    empty (and misleading) result list."""

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# How many title terms to OR into one query. Confirmed live: this is a hard
# ceiling, not just a nicety — an 8-term OR'd query for a real company
# (Skan) returned ZERO usable /in/ profiles (buried under "999 jobs in
# India" aggregator pages and job-board listings), while a 3-term version of
# the *same* company/intent surfaced its actual Director of Data Science and
# Co-founder/CEO cleanly. Google's ranking dilutes hard once an OR clause
# gets long; keep it short even at the cost of narrower per-query recall.
MAX_QUERY_TITLE_TERMS = 3
RESULTS_PER_QUERY = 20


@dataclass
class SerpResult:
    title: str
    url: str
    snippet: str


@dataclass
class RawProfile:
    """Source-agnostic profile the classifier consumes — deliberately the
    same three fields (name, headline, url) whatever engine produced it, so
    downstream tier-validation never learns where a profile came from."""
    full_name: str
    headline: str
    linkedin_url: str


def _build_precise_query(company_name: str, title_terms: list[str], location: str | None) -> str:
    """`site:linkedin.com/in "<company>" (Title A OR Title B OR ...)` — only
    usable by a transport with no site:/quote restriction (the browser
    transport). Recovers the precision `_build_query`'s loose shape gave up
    to work around Serper's free-tier rejection of both operators."""
    terms = [t for t in dict.fromkeys(title_terms) if t][:MAX_QUERY_TITLE_TERMS]
    parts = ["site:linkedin.com/in", f'"{company_name}"']
    if terms:
        parts.append(f"({' OR '.join(terms)})")
    if location:
        parts.append(location)
    return " ".join(parts)


def _build_query(company_name: str, title_terms: list[str], location: str | None) -> str:
    """`<company> (Title A OR Title B OR ...) linkedin`, plus an optional
    location term.

    Confirmed live against Serper's free tier: quoted phrases AND `site:`
    both get hard-rejected ("Query pattern not allowed for free accounts",
    HTTP 400) — an undocumented free-tier restriction, not in their
    published docs. Unquoted `OR` inside parens is accepted, though. This
    matters beyond just working around the 400: space-separating every term
    with no OR (an early version of this function) turns a multi-term query
    into keyword soup that Google ranks *worse* than a clean OR'd query —
    confirmed live it buried real employee profiles under generic
    "999 jobs in India" aggregator pages for a term list as short as 8 words.
    OR'ing keeps each alternative legible to Google's ranking. The `/in/`
    check in `_parse_profile` does the filtering `site:` would have, and its
    company-mention check does what quoting the company name would have."""
    terms = [t for t in dict.fromkeys(title_terms) if t][:MAX_QUERY_TITLE_TERMS]
    parts = [company_name]
    if terms:
        parts.append(f"({' OR '.join(terms)})")
    if location:
        parts.append(location)
    parts.append("linkedin")
    return " ".join(parts)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _serper_search(query: str) -> list[SerpResult]:
    headers = {"X-API-KEY": settings.SERPER_API_KEY, "Content-Type": "application/json"}
    body = {"q": query, "gl": "in", "num": RESULTS_PER_QUERY}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        resp = client.post(SERPER_ENDPOINT, json=body)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("organic") or []
    return [
        SerpResult(
            title=r.get("title", ""),
            url=r.get("link", ""),
            snippet=r.get("snippet", ""),
        )
        for r in results
    ]


# Every Bright Data request is billed, so each one is appended here as a JSON
# line. `agentic_batch.calls_spent_since` reads this back for the batch-level
# spend report; `spent_for` (below) reads it back keyed by (company, group,
# tier) so `bright_data_profiles` can refuse a call before billing it — see
# search_plan.py's docstring for why a prose-only cap was not enough.
#
# `company_id`/`search_group`/`tier`/`attempt` are optional on read (older log
# lines predate this key and simply won't match any `spent_for` lookup) but
# REQUIRED on write from 2026-08-03 onward — `brightdata_query.py`'s CLI now
# refuses to run unlabeled specifically so no call can bypass the ledger.
BRIGHT_DATA_CALL_LOG = Path(__file__).resolve().parents[2] / "logs" / "brightdata_calls.jsonl"


def _log_bright_data_call(
    query: str,
    result_count: int,
    *,
    profile_results: int | None = None,
    profile_filter_diagnostic: dict[str, dict[str, int]] | None = None,
    company_id: int | None = None,
    search_group: str | None = None,
    tier: str | None = None,
    attempt: int | None = None,
) -> None:
    """`results` is the RAW SERP row count; `profile_results` is how many of
    those survived the `/in/` URL-shape filter and were actually handed to the
    agent to judge. When every raw row is filtered away, the optional
    `profile_filter_diagnostic` records aggregate URL hosts and shape classes
    only; it deliberately excludes titles, snippets, URLs, and email text.

    Both are logged because they answer different questions and conflating
    them misleads (2026-08-04). `results` is near-always 10 — it just means
    Google returned a full page — so it is useless as a denominator. The
    "did we filter everything away?" check needs `profile_results`: a company
    with many profiles fetched and none kept is a filtering failure, whereas
    one with few profiles fetched is genuine scarcity. Entries written before
    2026-08-04 carry no `profile_results` and must be treated as unknown
    rather than zero."""
    try:
        BRIGHT_DATA_CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with BRIGHT_DATA_CALL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "query": query,
                "results": result_count,
                "profile_results": profile_results,
                "profile_filter_diagnostic": profile_filter_diagnostic,
                "company_id": company_id,
                "search_group": search_group,
                "tier": tier,
                "attempt": attempt,
            }) + "\n")
    except OSError as exc:
        # Never let bookkeeping break a run mid-batch.
        logger.warning("Could not write Bright Data call log: %s", exc)


def spent_for(company_id: int, search_group: str, tier: str) -> int:
    """Billed calls already made for this exact (company, search_group, tier)
    triple, read back from the ledger. This is the budget-enforcement primitive
    `bright_data_profiles` checks before spending, and what
    `search_plan.coverage_for_company` checks to find mandatory queries that
    were never issued. Lines written before 2026-08-03 have no `company_id` and
    are silently skipped — they predate per-tier keying and cannot be
    attributed to a triple."""
    if not BRIGHT_DATA_CALL_LOG.exists():
        return 0
    count = 0
    with BRIGHT_DATA_CALL_LOG.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (
                entry.get("company_id") == company_id
                and entry.get("search_group") == search_group
                and entry.get("tier") == tier
            ):
                count += 1
    return count


def profiles_fetched_for(company_id: int) -> int | None:
    """How many `/in/` profile rows this company's billed calls handed the
    agent to judge — the denominator of the batch report's "fetched vs kept"
    column.

    `None` means unknown, not zero: entries written before 2026-08-04 logged
    only the raw SERP count and cannot be re-derived. Reporting those as 0
    would invert the signal, making a well-searched company look like a
    filtering failure.

    Counts ROWS, not distinct people — the same person surfacing in a company's
    `head` and `ic` searches is counted twice. That is the right denominator
    for "how much did we look at and throw away", which is the question this
    answers; it is not a headcount of who exists at the company."""
    if not BRIGHT_DATA_CALL_LOG.exists():
        return None
    total = 0
    seen_any = False
    with BRIGHT_DATA_CALL_LOG.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("company_id") != company_id:
                continue
            profiles = entry.get("profile_results")
            if profiles is None:
                continue
            seen_any = True
            total += profiles
    return total if seen_any else None


def _max_calls_for_tier(tier: str) -> int:
    from app.contacts import ladder as ladder_cfg
    if tier == ladder_cfg.EXEC_FALLBACK:
        return ladder_cfg.MAX_SEARCHES_EXEC_FALLBACK
    return ladder_cfg.MAX_SEARCHES_PER_TIER


def _current_public_ip() -> str:
    """Return this machine's current public IP without exposing Bright Data's
    bearer token to the IP-discovery service."""
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(PUBLIC_IP_ENDPOINT)
            resp.raise_for_status()
            value = resp.json().get("ip", "")
        return str(ipaddress.ip_address(value))
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise SearchBlocked("Could not determine the current public IP for Bright Data allowlisting") from exc


def _allowlist_ip(client: httpx.Client, ip: str) -> None:
    """Add ``ip`` to the configured Bright Data zone using the API key that
    already authenticates SERP requests. The endpoint is account-mutating but
    not a billed SERP call."""
    try:
        resp = client.post(
            BRIGHT_DATA_WHITELIST_ENDPOINT,
            json={"ip": ip, "zone": settings.BRIGHT_DATA_ZONE},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SearchBlocked(
            f"Bright Data could not allowlist the current IP for zone={settings.BRIGHT_DATA_ZONE!r} "
            f"(HTTP {exc.response.status_code}); the API key must belong to an Admin or Ops user"
        ) from exc
    except httpx.HTTPError as exc:
        raise SearchBlocked(
            f"Bright Data could not allowlist the current IP for zone={settings.BRIGHT_DATA_ZONE!r}"
        ) from exc
    logger.info("Refreshed the current public IP in Bright Data zone %s", settings.BRIGHT_DATA_ZONE)


def _json_response_payload(resp: httpx.Response, url: str) -> dict:
    if not resp.content:
        provider_error = resp.headers.get("x-brd-error") or "no provider error header"
        raise SearchBlocked(
            f"Bright Data returned an empty SERP response for url={url!r}: "
            f"{provider_error}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SearchBlocked(
            f"Bright Data returned a non-JSON SERP response for url={url!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise SearchBlocked(
            f"Bright Data returned a non-object JSON SERP response for url={url!r}"
        )
    return payload


def _envelope_error(payload: dict) -> str:
    headers = payload.get("headers")
    if not isinstance(headers, dict):
        return ""
    return str(
        headers.get("x-brd-error")
        or headers.get("x-brd-err-msg")
        or headers.get("proxy-status")
        or ""
    )


def _unwrap_bright_data_payload(payload: dict, url: str) -> dict:
    """Return parsed SERP JSON from Bright Data's optional JSON envelope."""
    if "status_code" not in payload:
        return payload
    try:
        status_code = int(payload["status_code"])
    except (TypeError, ValueError) as exc:
        raise SearchBlocked(
            f"Bright Data returned an invalid embedded status for url={url!r}"
        ) from exc
    provider_error = _envelope_error(payload)
    if not 200 <= status_code < 300:
        detail = provider_error or "no embedded provider error"
        raise SearchBlocked(
            f"Bright Data upstream returned status {status_code} for url={url!r}: {detail}"
        )
    body = payload.get("body")
    if isinstance(body, dict):
        return body
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise SearchBlocked(
                f"Bright Data returned a non-JSON envelope body for url={url!r}"
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    raise SearchBlocked(
        f"Bright Data returned an empty or invalid envelope body for url={url!r}"
    )


def _bright_data_fetch_once(
    url: str,
    *,
    recover_ip_forbidden: bool,
    api_key: str | None = None,
) -> dict:
    """Issue one SERP request, optionally allowing one billed IP recovery.

    ``format: "json"`` selects the outer response while
    ``data_format: "parsed"`` requests Bright Data's parsed JSON. The
    initial POST is one charge; IP recovery can issue one additional charge.
    """
    headers = {
        "Authorization": f"Bearer {api_key or settings.BRIGHT_DATA_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "zone": settings.BRIGHT_DATA_ZONE,
        "url": url,
        "format": "json",
        "data_format": "parsed",
    }
    with httpx.Client(timeout=180.0, headers=headers) as client:
        resp = client.post(BRIGHT_DATA_ENDPOINT, json=body)
        resp.raise_for_status()
        provider_error = resp.headers.get("x-brd-error") or ""
        if (
            recover_ip_forbidden
            and not resp.content
            and "code: ip_forbidden" in provider_error.lower()
        ):
            _allowlist_ip(client, _current_public_ip())
            resp = client.post(BRIGHT_DATA_ENDPOINT, json=body)
            resp.raise_for_status()
        payload = _json_response_payload(resp, url)
        if (
            recover_ip_forbidden
            and "code: ip_forbidden" in _envelope_error(payload).lower()
        ):
            _allowlist_ip(client, _current_public_ip())
            resp = client.post(BRIGHT_DATA_ENDPOINT, json=body)
            resp.raise_for_status()
            payload = _json_response_payload(resp, url)
        return _unwrap_bright_data_payload(payload, url)


# Two attempts, not three: a retry re-bills the same page, so a flaky fetch
# costs at most double rather than triple. This remains the contact-discovery
# transport; the company market-profile batch uses the strict one-request seam
# below.
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=10))
def _bright_data_fetch(url: str, *, api_key: str | None = None) -> dict:
    """Fetch with the existing retry and IP-allowlist recovery behaviour."""
    return _bright_data_fetch_once(
        url,
        recover_ip_forbidden=True,
        api_key=api_key,
    )


def _parse_google_json_rows(parsed: dict) -> list[SerpResult]:
    """Read Bright Data's own parsed `organic` array — no HTML/selector
    parsing involved (see `_bright_data_fetch`)."""
    rows: list[SerpResult] = []
    for r in parsed.get("organic") or []:
        link = r.get("link", "")
        title = r.get("title", "")
        if not link or not title:
            continue
        rows.append(SerpResult(title=title, url=link, snippet=r.get("description", "")))
    return rows


def bright_data_search(
    query: str,
    *,
    api_key: str | None = None,
) -> list[SerpResult]:
    """Bright Data SERP passthrough against real google.com/search — exactly
    ONE billed request, one page.

    Deliberately not paginated (2026-08-02). The earlier version looped up to
    SERP_MAX_PAGES chasing RESULTS_PER_QUERY rows, but Google returns only
    ~10 organic rows per fetch regardless (confirmed live 2026-08-05: Bright
    Data silently drops a `num` query param in JSON mode too — same ceiling
    as before), so "one query" silently cost 2-3 billed calls. Tier quotas
    are 2-3 contacts, and one page filters to ~6-8 usable /in/ profiles —
    enough to fill a tier on its own, which is what happened for most tiers
    in the 2026-08-02 batch.

    When a tier IS short, the find-contacts skill spends its second (and
    final) call on a *different seed title*, not page 2 of this query: page 2
    is the low-relevance tail of a query that already underperformed, while a
    different title surfaces a structurally different set of people.

    Raises SearchBlocked when Bright Data's upstream status isn't 200 (see
    `_bright_data_fetch`), though Bright Data is not expected to see Google's
    own bot detection since it isn't our IP making the request."""
    url = f"{GOOGLE_SEARCH_URL}?q={quote(query)}&gl=in&hl=en"
    parsed = _bright_data_fetch(url, api_key=api_key)
    rows = _parse_google_json_rows(parsed)
    return rows


def bright_data_search_once(query: str) -> list[SerpResult]:
    """Run exactly one billed SERP request with no retry or recovery re-fetch."""
    url = f"{GOOGLE_SEARCH_URL}?q={quote(query)}&gl=in&hl=en"
    parsed = _bright_data_fetch_once(
        url,
        recover_ip_forbidden=False,
    )
    return _parse_google_json_rows(parsed)


def _contact_search_api_key(
    company_id: int | None,
    search_group: str | None,
    tier: str | None,
    attempt: int,
) -> str | None:
    """Distribute labelled contact calls deterministically across both keys."""
    keys = [
        key
        for key in (
            settings.BRIGHT_DATA_API_KEY,
            settings.BRIGHT_DATA_API_KEY2,
        )
        if key
    ]
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    label = f"{company_id}:{search_group}:{tier}:{attempt}".encode()
    index = int.from_bytes(hashlib.sha256(label).digest()[:4], "big") % len(keys)
    return keys[index]


def bright_data_profiles(
    query: str,
    *,
    company_id: int | None = None,
    search_group: str | None = None,
    tier: str | None = None,
    attempt: int = 1,
) -> list[SerpResult]:
    """Run `query` through Bright Data and return rows filtered to `/in/`
    profile URLs only — structural filtering, not a company/title match.
    Deliberately does NOT reuse `_mentions_company`/`_parse_profile`'s
    heuristic company-match filter (see the ADR-0005 addendum): that
    heuristic silently drops candidates before anyone judges them, which is
    exactly what agentic search replaced. Hand every /in/ row to the agent
    to read and decide; this only cuts obvious non-profile noise (job ads,
    aggregator pages).

    `company_id`/`search_group`/`tier` are the ledger key this call bills
    against (see `spent_for`). When all three are given, this function
    REFUSES to spend a call once the triple has already hit
    `ladder.MAX_SEARCHES_PER_TIER` (or MAX_SEARCHES_EXEC_FALLBACK for that
    tier) — raising BudgetExceeded before `bright_data_search` runs, so an
    over-budget call is never billed, not merely flagged afterward (2026-08-03,
    see search_plan.py). Callers that omit the key skip this check entirely —
    reserved for one-off manual/debug queries; the skill-facing CLI always
    passes the full key."""
    if company_id is not None and search_group is not None and tier is not None:
        already_spent = spent_for(company_id, search_group, tier)
        cap = _max_calls_for_tier(tier)
        if already_spent >= cap:
            raise BudgetExceeded(
                f"company_id={company_id} search_group={search_group!r} tier={tier!r} "
                f"already spent {already_spent}/{cap} calls — refusing to bill another. "
                f"Query was: {query!r}"
            )

    rows = bright_data_search(
        query,
        api_key=_contact_search_api_key(
            company_id,
            search_group,
            tier,
            attempt,
        ),
    )
    out = []
    for r in rows:
        url = _normalize_public_linkedin_profile_url(r.url)
        if url:
            out.append(SerpResult(title=r.title, url=url, snippet=r.snippet))

    diagnostic = _profile_filter_diagnostic(rows) if rows and not out else None

    # Logged AFTER the request succeeds (a SearchBlocked raise upstream in
    # bright_data_search also skips the log line, same as before) but BEFORE
    # returning, so `spent_for` sees this call immediately for the very next
    # invocation in the same process.
    _log_bright_data_call(
        query, len(rows), profile_results=len(out),
        profile_filter_diagnostic=diagnostic,
        company_id=company_id, search_group=search_group, tier=tier, attempt=attempt,
    )
    return out


def _ddg_search(query: str) -> list[SerpResult]:
    """No-key fallback. Bot-blocks fast (see module docstring) — best-effort
    only. Returns [] rather than raising on a block, so a spot-check degrades
    to "no results" instead of crashing the run."""
    from bs4 import BeautifulSoup

    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
            resp = client.post(DDG_ENDPOINT, data={"q": query})
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as exc:
        logger.warning("DDG request failed: %s", exc)
        return []
    if "anomaly-modal" in html:
        logger.warning("DDG returned a bot-check page; no results.")
        return []

    soup = BeautifulSoup(html, "html.parser")
    out: list[SerpResult] = []
    for res in soup.find_all("div", class_="result"):
        a = res.find("a", class_="result__a")
        if not a:
            continue
        url = a.get("href", "")
        # DDG wraps outbound links as /l/?uddg=<encoded real url>.
        if "uddg=" in url:
            url = unquote(parse_qs(urlparse(url).query).get("uddg", [""])[0])
        snip_el = res.find(class_="result__snippet")
        out.append(
            SerpResult(
                title=a.get_text(strip=True),
                url=url,
                snippet=snip_el.get_text(strip=True) if snip_el else "",
            )
        )
    return out


# Lazily-opened, process-wide browser/page for the browser transport — kept
# open across every query in a batch (looks like one person browsing, not a
# fresh session per query, which is both cheaper and less bot-signal-y).
# Torn down explicitly via close_search_browser() at the end of a batch.
_playwright = None
_browser = None
_page = None
# Once Google blocks/challenges this session, confirmed live it keeps
# blocking on the very next query too (not a one-off) — so this is a
# per-process latch, not a per-query retry: once tripped, every remaining
# call in this run skips straight to the Serper/DDG fallback. Two reasons
# this matters beyond wasted requests: retrying the browser transport
# anyway re-renders an interactive CAPTCHA in the VISIBLE headed window each
# time, which is both pointless (it was still blocked) and, since the
# window is headed on the user's real desktop, an unwanted prompt in their
# face on every subsequent company. Resets only via close_search_browser()
# (i.e. the next script invocation starts fresh).
_blocked = False


def _ensure_browser_page():
    global _playwright, _browser, _page
    if _page is not None:
        return _page
    from patchright.sync_api import sync_playwright

    _playwright = sync_playwright().start()
    _browser, context = launch_anonymous(_playwright)
    _page = context.new_page()
    return _page


def close_search_browser() -> None:
    """Tear down the process-wide browser opened by the browser transport,
    if one was opened. Safe to call even if it was never used."""
    global _playwright, _browser, _page, _blocked
    if _browser is not None:
        _browser.close()
    if _playwright is not None:
        _playwright.stop()
    _playwright = _browser = _page = None
    _blocked = False


def _accept_google_consent(page) -> None:
    for text in ("Accept all", "I agree", "Accept"):
        btn = page.query_selector(f"button:has-text('{text}')")
        if btn:
            btn.click()
            human_delay(1.0, 2.0)
            return


def _detect_block(html: str) -> str | None:
    lowered = html.lower()
    for marker in _BLOCK_MARKERS:
        if marker in lowered:
            return marker
    return None


def _parse_google_result_rows(page) -> list[SerpResult]:
    rows: list[SerpResult] = []
    for container in page.query_selector_all(_RESULT_CONTAINER_SELECTOR):
        title_el = container.query_selector(_RESULT_TITLE_SELECTOR)
        link_el = container.query_selector("a[href]")
        if not title_el or not link_el:
            continue
        snippet_el = container.query_selector(_RESULT_SNIPPET_SELECTOR)
        rows.append(
            SerpResult(
                title=title_el.inner_text(),
                url=link_el.get_attribute("href") or "",
                snippet=snippet_el.inner_text() if snippet_el else "",
            )
        )
    return rows


def _browser_google_search(query: str, max_pages: int | None = None) -> list[SerpResult]:
    """Headed patchright session against google.com/search — no site:/quote
    restriction, so callers pass the precise query. Paginates up to
    max_pages (default settings.SERP_MAX_PAGES), stopping early once enough
    rows are collected. Raises SearchBlocked on a detected block/challenge
    page so the caller can fall back to Serper/DDG."""
    max_pages = settings.SERP_MAX_PAGES if max_pages is None else max_pages
    page = _ensure_browser_page()
    results: list[SerpResult] = []
    for page_num in range(max_pages):
        start = page_num * 10
        url = f"{GOOGLE_SEARCH_URL}?q={quote(query)}"
        if start:
            url += f"&start={start}"
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if page_num == 0:
            _accept_google_consent(page)
        found = wait_past_interstitial(page, _RESULTS_PRESENCE_SELECTOR, max_wait_s=20.0)
        html = page.content()
        block = _detect_block(html)
        if block:
            global _blocked
            _blocked = True
            # Navigate the VISIBLE headed window away from the CAPTCHA/block
            # page immediately — otherwise it sits there displaying an
            # interactive challenge to the user for the rest of the run,
            # even though the pipeline itself has already recovered via the
            # fallback transport.
            page.goto("about:blank")
            raise SearchBlocked(f"Google block detected ({block}) for query: {query}")
        if not found:
            break
        page_rows = _parse_google_result_rows(page)
        if not page_rows:
            break
        results.extend(page_rows)
        # A bit above FETCH_CAP_PER_CATEGORY, since the classifier will
        # discard some rows as non-matches — stop paginating once there's
        # enough raw material rather than exhausting max_pages every time.
        if len(results) >= RESULTS_PER_QUERY:
            break
        if page_num < max_pages - 1:
            human_delay(2.0, 4.0)
    return results


_PROFILE_URL_RE = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/]+", re.I)
# The trailing " | LinkedIn" (or " | Professional Profile") suffix engines
# append to LinkedIn result titles.
_TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*(?:linkedin|professional profile).*$", re.I)


def _is_google_redirect_host(host: str) -> bool:
    """Recognize Google's public search hosts without trusting lookalikes.

    Google commonly uses both ``www.google.com`` and country domains such as
    ``www.google.co.in`` for outbound ``/url`` redirects.  A host must be
    exactly ``google.<suffix>`` or ``www.google.<suffix>``; a string merely
    containing ``google`` is never enough to make its redirect target trusted.
    """
    labels = host.lower().split(".")
    if labels[:1] == ["google"]:
        labels = labels[1:]
    elif labels[:2] == ["www", "google"]:
        labels = labels[2:]
    else:
        return False
    return 1 <= len(labels) <= 2 and all(label.isalnum() for label in labels)


def _normalize_public_linkedin_profile_url(raw_url: str) -> str | None:
    """Return one public LinkedIn ``/in/<slug>`` URL, or ``None``.

    Bright Data usually returns the destination link, but some parsed Google
    rows retain Google's public ``/url`` redirect.  Unwrap only that exact
    trusted redirect form, then remove tracking query/fragment data.  This is
    structural filtering only; it does not infer a person or an employer.
    """
    candidate = raw_url.strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if _is_google_redirect_host(host) and parsed.path == "/url":
        query = parse_qs(parsed.query)
        candidate = (query.get("url") or query.get("q") or [""])[0]
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()

    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0].lower() != "in" or not path_parts[1]:
        return None
    return f"{parsed.scheme.lower()}://{host}/in/{path_parts[1]}"


def _profile_filter_diagnostic(rows: list[SerpResult]) -> dict[str, dict[str, int]]:
    """Summarize filtered rows without retaining personally identifying SERP data."""
    host_counts: dict[str, int] = {}
    shape_counts: dict[str, int] = {}
    for row in rows:
        try:
            parsed = urlsplit(row.url.strip())
        except ValueError:
            parsed = None
        if parsed is None:
            host = "[invalid]"
            shape = "invalid_url"
            host_counts[host] = host_counts.get(host, 0) + 1
            shape_counts[shape] = shape_counts.get(shape, 0) + 1
            continue
        host = (parsed.hostname or "[missing]").lower()
        host_counts[host] = host_counts.get(host, 0) + 1
        if not row.url.strip():
            shape = "missing_url"
        elif _is_google_redirect_host(host) and parsed.path == "/url":
            query = parse_qs(parsed.query)
            destination = (query.get("url") or query.get("q") or [""])[0]
            shape = (
                "google_wrapper_non_profile_destination"
                if destination else "google_wrapper_missing_destination"
            )
        elif host == "linkedin.com" or host.endswith(".linkedin.com"):
            shape = "linkedin_non_profile_path"
        else:
            shape = "other_host"
        shape_counts[shape] = shape_counts.get(shape, 0) + 1
    return {"url_host_counts": host_counts, "url_shape_counts": shape_counts}


# Corporate-suffix/industry words that overlap across unrelated companies
# too easily to count as a company match on their own — confirmed live:
# "ICICI Bank" split into {"icici", "bank"} let "Data & AI Scientist @
# Lloyds Banking Group" pass purely on "bank"/"banking", an entirely
# different company. Same class of bug as people_search's
# _GENERIC_ROLE_WORDS (generic word standing in for a specific match).
_GENERIC_COMPANY_WORDS = {
    "bank", "banking", "technologies", "technology", "software", "solutions",
    "services", "systems", "group", "limited", "ltd", "llc", "inc", "pvt",
    "private", "international", "global", "company", "corp", "corporation",
    "india", "labs", "consulting", "consultancy",
}


def _company_word_match(company_name: str, text: str) -> bool:
    company_words = {
        w for w in re.sub(r"[^a-z0-9 ]", " ", company_name.lower()).split()
        if len(w) > 2 and w not in _GENERIC_COMPANY_WORDS
    }
    if not company_words:
        return True
    return any(w in text.lower() for w in company_words)


# Marks a headline as stating a specific *current* employer: "... @ Lloyds
# Banking Group", "... at Skan AI", or LinkedIn's own "<Role> | <Company>"
# title convention (the trailing pipe segment, checked AFTER the generic
# "| LinkedIn" suffix has already been stripped — otherwise every single
# result would spuriously "have an employer marker" via that suffix alone).
_EMPLOYER_MARKER_RE = re.compile(r"(?:@|\bat\b|\|)\s*\S")


def _mentions_company(company_name: str, title: str, snippet: str) -> bool:
    """Company-match check for a SERP row, biased toward the title's stated
    *current* employer over the bio snippet. Needed because the query can
    no longer force an exact company match — Serper's free tier rejects
    quoted phrases, so a bare `Quantzig Data Scientist linkedin` query can
    rank someone unrelated who merely matches on role words. Confirmed live,
    two separate leaks of this shape:
      - naively OR-ing title+snippet let a Lloyds Banking employee (title:
        "...@ Lloyds Banking...") pass an ICICI Bank search purely because
        their bio snippet mentioned "5 years at ICICI Bank" as a PAST role;
      - the "@"/"at"-only marker regex missed LinkedIn's own pipe-delimited
        title convention ("Aditee B. - Credit Risk analyst | Citi"), so a
        Citi employee passed a Kotak Mahindra Bank search via the snippet.
    `title` here must already have the "| LinkedIn"
    suffix stripped — see `_parse_profile`. If the (suffix-stripped) title
    names a specific current employer, that's authoritative: only the title
    is checked. The snippet is trusted only when the title carries no
    employer marker at all (skills-only headlines with nothing to check)."""
    if _EMPLOYER_MARKER_RE.search(title):
        return _company_word_match(company_name, title)
    return _company_word_match(company_name, title) or _company_word_match(company_name, snippet)


def _parse_profile(result: SerpResult, company_name: str) -> RawProfile | None:
    """Turn one SERP row into a RawProfile, or None if it isn't a usable
    public /in/ profile at the target company. Title shape is
    `<Name> - <Headline...> | LinkedIn` — split the name off the front, keep
    the rest as the headline. Falls back to the snippet for a headline
    when the title carries only a name + company."""
    # Strip query params AND fragments — the browser transport's raw Google
    # results include duplicate rows for the same profile via a "jump to
    # text" fragment (e.g. "...#:~:text=Aleksandr%20Volodarsky..."), which
    # would otherwise dedupe as a distinct URL from the plain profile link.
    url = result.url.split("?")[0].split("#")[0].rstrip("/")
    if not _PROFILE_URL_RE.match(url):
        return None

    title = _TITLE_SUFFIX_RE.sub("", result.title).strip()
    if not _mentions_company(company_name, title, result.snippet):
        return None

    if " - " not in title:
        return None
    name, _, remainder = title.partition(" - ")
    name = name.strip()
    headline = remainder.strip()
    if not name:
        return None

    # A headline that is only the company name (title was "<Name> -
    # <Company>") carries no role signal; the snippet usually does.
    if not headline or len(headline.split()) < 2:
        headline = result.snippet.strip() or headline
    if not headline:
        return None

    return RawProfile(full_name=name, headline=headline, linkedin_url=url)


def _fallback_search(company_name: str, title_terms: list[str], location: str | None) -> list[SerpResult]:
    """The loose (non-site:/quote) query shape, for transports that reject
    those operators on their free tier."""
    query = _build_query(company_name, title_terms, location)
    if settings.SERPER_API_KEY:
        return _serper_search(query)
    logger.warning("No SERPER_API_KEY set — falling back to DuckDuckGo (blocks fast).")
    return _ddg_search(query)


def search_profiles(
    company_name: str,
    title_terms: list[str],
    location: str | None = None,
) -> list[RawProfile]:
    """Discover public LinkedIn profiles at `company_name` whose roles relate
    to `title_terms`, via whichever transport is configured
    (settings.SEARCH_TRANSPORT). Returns parsed, de-duplicated `RawProfile`s
    (classification/tier assignment happens in the caller).

    "browser" (default): headed patchright against google.com/search using a
    precise site:/quoted query, paginated. On a detected block, falls back to
    Serper (or DDG with no key) with the loose query shape for this call, AND
    latches — confirmed live a block persists across the very next query too,
    not a one-off, so every subsequent call in this process skips straight to
    the fallback instead of re-triggering another visible CAPTCHA for
    nothing. The latch clears only via close_search_browser()."""
    transport = settings.SEARCH_TRANSPORT
    if transport == "browser" and not _blocked:
        precise_query = _build_precise_query(company_name, title_terms, location)
        try:
            results = _browser_google_search(precise_query)
        except SearchBlocked:
            logger.warning("Browser transport blocked — falling back to Serper/DDG for the rest of this run.")
            results = _fallback_search(company_name, title_terms, location)
    elif transport == "ddg":
        results = _ddg_search(_build_query(company_name, title_terms, location))
    else:
        # "serper", or "browser" once the block latch has tripped.
        results = _fallback_search(company_name, title_terms, location)

    profiles: list[RawProfile] = []
    seen: set[str] = set()
    for result in results:
        profile = _parse_profile(result, company_name)
        if profile and profile.linkedin_url not in seen:
            seen.add(profile.linkedin_url)
            profiles.append(profile)
    return profiles


if __name__ == "__main__":
    import sys

    company = sys.argv[1] if len(sys.argv) > 1 else "Quantzig"
    terms = ["Data Scientist", "Data Science", "Analytics", "Machine Learning"]
    for prof in search_profiles(company, terms):
        print(f"{prof.full_name} | {prof.headline[:80]} | {prof.linkedin_url}")
