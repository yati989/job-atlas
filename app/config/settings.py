"""
Environment/secrets loading.

Loads ``.env`` once and exposes the application's environment-backed settings
from one module.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from app.config.authentication import load_provider_environment

load_provider_environment()


def _positive_int_env(name: str, default: int) -> int:
    """Read a strictly positive integer setting with a useful startup error."""
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero, got {value}")
    return value


# One project-wide freshness intent. Connectors should translate this to an
# exact native filter when possible; if a site's largest preset is shorter,
# they must fetch broadly and let the central relevance gate enforce it.
RECENCY_WINDOW_DAYS = _positive_int_env("RECENCY_WINDOW_DAYS", 60)

# The decision/outreach pipeline snapshots these values on every run.  Keep
# validation here, at the configuration seam, so a malformed environment can
# never create an unreproducible decision.
GLASSDOOR_WLB_MIN_REVIEWS = _positive_int_env("GLASSDOOR_WLB_MIN_REVIEWS", 50)
FRESH_OUTREACH_SUCCESS_THRESHOLD = _positive_int_env("FRESH_OUTREACH_SUCCESS_THRESHOLD", 4)
DAILY_FRESH_COMPANY_TARGET = _positive_int_env("DAILY_FRESH_COMPANY_TARGET", 30)
DELIVERY_PRESUMPTION_MINUTES = _positive_int_env("DELIVERY_PRESUMPTION_MINUTES", 30)
CALIBRATION_WINDOW_MINUTES = _positive_int_env("CALIBRATION_WINDOW_MINUTES", 60)
CALIBRATION_POLL_MINUTES = _positive_int_env("CALIBRATION_POLL_MINUTES", 10)
PIPELINE_INPUT_TIMEZONE = os.getenv("PIPELINE_INPUT_TIMEZONE", "Asia/Kolkata")
try:
    ZoneInfo(PIPELINE_INPUT_TIMEZONE)
except Exception as exc:
    raise ValueError(f"PIPELINE_INPUT_TIMEZONE must be an IANA timezone, got {PIPELINE_INPUT_TIMEZONE!r}") from exc
DECISION_POLICY_VERSION = _positive_int_env("DECISION_POLICY_VERSION", 1)

# The candidate's own inbox — the only address app.outreach.gmail.
# send_self_report() is permitted to send to (see ADR-0009's amendment).
# Never used for outreach; outreach recipients are always contacts/prospects.
SELF_EMAIL = os.getenv("SELF_EMAIL")

# Rollout stage for the rule-based tier/function triage layer (ADR-0007,
# app/contacts/triage.py). "shadow" (default) — compute and log a verdict
# for every kept candidate, change nothing the agent sees. "annotate" — also
# print the verdict alongside each kept row. "gate" — also move AUTO_REJECT
# rows into the structurally-dropped section. There is deliberately no mode
# that auto-STORES a contact without the agent confirming it — see the
# triage.py module docstring for why.
TRIAGE_MODE = os.getenv("TRIAGE_MODE", "shadow")
# Which transport search_engine mode uses to fetch SERP rows.
#   "browser" (default) — our own headed patchright session against
#     google.com/search (see app/contacts/search_source.py). Confirmed live
#     (2026-07-21 feasibility spike) not to trip Google's bot detection
#     across repeated + paginated queries.
#     No query-syntax restriction, so it can use site:/quoted-phrase queries
#     for real precision (unlike Serper's free tier — see below). Zero
#     per-query cost; supersedes the paid-Apify pivot that was scoped for the
#     same reason (see docs/adr/0002).
#   "serper" — Serper.dev JSON API, the prior default. Kept as the automatic
#     fallback when the browser transport hits a block mid-run.
#   "ddg" — DuckDuckGo HTML, no-key spot-check fallback. Confirmed live it
#     bot-blocks after ~2 automated requests; not viable at volume.
SEARCH_TRANSPORT = os.getenv("SEARCH_TRANSPORT", "browser")
# Pagination ceiling for the browser transport — one SERP page is only ~10
# organic results, most discarded as non-/in/ noise, so hitting the 2-4
# contacts/category target needs multiple pages. Each extra page is also
# more bot-detection exposure, hence a ceiling rather than paging until
# quota is met.
SERP_MAX_PAGES = int(os.getenv("SERP_MAX_PAGES", "3"))
# Serper.dev API key — the block-free HTTP transport, used as the
# search_engine-mode fallback when SEARCH_TRANSPORT="browser" hits a block,
# or directly when SEARCH_TRANSPORT="serper". 2,500 free queries on signup,
# no credit card (as of 2026 Brave killed its free tier and Google Custom
# Search API closed to new signups — Serper was the remaining no-card
# option; real Google SERP results, which index LinkedIn more thoroughly
# than Bing/DDG anyway). Its free tier hard-rejects site:/quoted queries,
# which is why it isn't the default transport anymore.
SERPER_API_KEY = os.getenv("SERPER_API_KEY")

# Bright Data SERP passthrough — a raw Google HTML fetch (POST a literal
# google.com/search URL to api.brightdata.com/request, get back the actual
# Google SERP HTML). Funded account, in scope specifically for contact-finding
# per docs/adr/0005 addendum (2026-08-02): replaces WebSearch as the search
# transport in the find-contacts skill's step 3 because WebSearch has no
# result-count control and was confirmed non-deterministic (same query, zero
# results then real profiles on an unchanged same-day re-run). Because this is
# real Google rather than a tool-interpretation layer, site:/quoted-phrase
# queries work as expected.
BRIGHT_DATA_API_KEY = os.getenv("BRIGHT_DATA_API_KEY")
BRIGHT_DATA_API_KEY2 = os.getenv("BRIGHT_DATA_API_KEY2")
BRIGHT_DATA_ZONE = os.getenv("BRIGHT_DATA_ZONE", "serp_api1")

# Direct alerts from a full-pipeline process that may outlive its Codex turn.
FULL_PIPELINE_NTFY_TOPIC = os.getenv("FULL_PIPELINE_NTFY_TOPIC")

# Dedicated Chrome profile for Indeed. The dummy account is authenticated
# manually once; Chrome stores the session locally outside the repository.
INDEED_PROFILE_DIR = Path(
    os.getenv(
        "INDEED_PROFILE_DIR",
        str(Path(os.getenv("LOCALAPPDATA", Path.home())) / "job_agent" / "indeed-profile"),
    )
)
