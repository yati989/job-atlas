"""
Shared pagination/depth-policy mechanism (issue #14).

Every connector paginates differently (headed Playwright with a card loop,
httpx + BeautifulSoup with a detail-page fetch per card, ...), so this module
does not take over a connector's fetch loop. It only owns the *stop/continue
decision* — "have I gone deep enough?" — via `DepthCursor`, which a
connector's existing per-page loop consults each iteration:

    cursor = tier1_cursor()               # or tier2_cursor() / tier3_cursor()
    page_num = 0
    while cursor.should_continue():
        page_num += 1
        page_items = self._scrape_page(page_num)
        if not page_items:
            break
        ...accumulate page_items...
        cursor.record_page(len(page_items), posted_dates=[it.posted_at for it in page_items])

Tier-assignment procedure (apply this when onboarding/classifying a source
for #15/#16 rollout — check live, don't assume from documentation):

- **Tier 1** — the site has a date filter or date sort you have *verified
  live* actually narrows/orders results (e.g. Naukri's `?jobAge=N`, confirmed
  by diffing returned "posted X days ago" text across values). Use
  `tier1_cursor()`: pages forward until every dated posting on a page is
  older than `RECENCY_WINDOW_DAYS`, or until the `TIER1_PAGE_CEILING`
  safety cap is hit, whichever comes first. The ceiling exists because a
  broken/misread date filter should never turn into an unbounded crawl.
- **Tier 2** — the site shows a posted-date per listing (so freshness can
  still be post-filtered) but has no working sort/filter to page toward the
  cutoff purposefully (e.g. Shine: dates are scraped and used to drop stale
  rows, but there's no date-sort param to page against). Use
  `tier2_cursor()`: a raised fixed cap (~10 pages / ~200 results) — more
  generous than tier 3 since the connector can still discard what's stale
  after the fact.
- **Tier 3** — the site exposes no usable date signal at all, or the site
  itself hard-caps anonymous depth regardless of any filter you set (e.g.
  AI Jobs Net: anonymous browsing is walled at page 2 by the site, "Sign in
  to view more" — confirmed live, not a bug in this codebase). Use
  `tier3_cursor()`: a modest fixed cap (~2 pages / ~50 results). Document
  the reason at the connector, same as AIJobsNetConnector's docstring does,
  so it reads as an intentional exception rather than an unfinished
  migration.

If it's ambiguous which tier a source falls into, verify live (fetch a page,
check whether the date param/sort actually changes the returned set) rather
than guessing from the site's marketing copy about "advanced filters".

**Depth cap (issue #53, revised by the search-coverage redesign, issue #47
tickets #60-#67)**: `MAX_RESULTS_PER_QUERY` (700) and the tier1/tier2 page
ceilings below belong to **broad/board connectors only** (decision 10b of
the redesign plan — issue #47's plan file, "tell-me-what-s-the-noble-eclipse").
These were originally raised for the abandoned combined-OR-query approach
("one query now covers what 13 used to"); since broad connectors were never
part of that and keep their current one-pass-per-source behavior unchanged,
they stay at these values rather than being dragged down to the new
native-search budget. **Native-search connectors get a separate, smaller
budget (`NATIVE_SEARCH_MAX_RESULTS` = 300 results/query) via the
`relevance_drift_cursor()` added in ticket #60 — a dedicated constant, not a
repurposing of `MAX_RESULTS_PER_QUERY`.** Do not let native-search work shrink these
broad-connector values, and do not let broad-connector work reuse the
native-search 300 cap — the two are deliberately independent (locked
2026-07-25, see decision 10 vs 10b).
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app.config.categories import RECENCY_WINDOW_DAYS

# Broad/board-connector result cap — every tier's max_results targets this
# ceiling. NOT used by native-search connectors once they migrate onto the
# relevance-drift cursor (ticket #60), which has its own separate, smaller
# per-query budget (300) — see the module docstring above.
MAX_RESULTS_PER_QUERY = 700

# Tier 1: hard safety ceiling regardless of what the date signal says —
# protects against a misread/broken date filter turning into an unbounded
# crawl. Raised alongside MAX_RESULTS_PER_QUERY so a tier-1 source can
# actually page deep enough to reach 700 results (was 30, sized for one
# narrow per-term search).
TIER1_PAGE_CEILING = 70

# Tier 2: raised fixed caps for sites with dates but no usable date sort.
TIER2_MAX_PAGES = 35
TIER2_MAX_RESULTS = MAX_RESULTS_PER_QUERY

# Tier 3: sites with no date signal at all (or a site-imposed anonymous-depth
# wall) — page cap raised only enough to allow reaching the uniform result
# cap; sites that hard-wall anonymous depth well below this will simply keep
# stopping early on their own wall, same as before.
TIER3_MAX_PAGES = 20
TIER3_MAX_RESULTS = MAX_RESULTS_PER_QUERY


@dataclass
class DepthCursor:
    """Stop/continue decision for a connector's own pagination loop.

    Call `should_continue()` before fetching each page, then `record_page()`
    after parsing it. For tier 1 (cutoff set), pass every item's parsed
    `posted_at` to `record_page(posted_dates=...)` — once a whole page's
    dated postings are all older than the cutoff, the cursor reports
    exhausted and the next `should_continue()` returns False. Postings with
    no parseable date don't count toward exhaustion either way (same
    "don't silently drop, don't silently trust" stance connectors already
    take on unparseable dates).
    """

    max_pages: int
    max_results: int | None = None
    cutoff: datetime | None = None
    pages_fetched: int = field(default=0, init=False)
    results_seen: int = field(default=0, init=False)
    _exhausted: bool = field(default=False, init=False)

    def should_continue(self) -> bool:
        if self._exhausted:
            return False
        if self.pages_fetched >= self.max_pages:
            return False
        if self.max_results is not None and self.results_seen >= self.max_results:
            return False
        return True

    def record_page(
        self, item_count: int, posted_dates: Iterable[datetime | None] = ()
    ) -> None:
        self.pages_fetched += 1
        self.results_seen += item_count
        if self.cutoff is None:
            return
        dated = [d for d in posted_dates if d is not None]
        if dated and all(d < self.cutoff for d in dated):
            self._exhausted = True


def tier1_cursor(
    recency_window_days: int = RECENCY_WINDOW_DAYS,
    page_ceiling: int = TIER1_PAGE_CEILING,
) -> DepthCursor:
    """Verified-live date filter/sort: page until the recency window is
    exhausted, capped at `page_ceiling` regardless."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=recency_window_days)
    return DepthCursor(max_pages=page_ceiling, cutoff=cutoff)


def tier2_cursor(
    max_pages: int = TIER2_MAX_PAGES, max_results: int = TIER2_MAX_RESULTS
) -> DepthCursor:
    """Dates present but no usable date sort: raised fixed cap."""
    return DepthCursor(max_pages=max_pages, max_results=max_results)


def tier3_cursor(
    max_pages: int = TIER3_MAX_PAGES, max_results: int = TIER3_MAX_RESULTS
) -> DepthCursor:
    """No date signal at all (or a site-imposed anonymous-depth wall):
    modest fixed cap, documented as an intentional exception."""
    return DepthCursor(max_pages=max_pages, max_results=max_results)


# --- Relevance-drift cursor (native-search connectors only, issue #60) -----
# Separate budget from the broad/board-connector constants above (decision
# 10b) — do not repurpose MAX_RESULTS_PER_QUERY for this.
NATIVE_SEARCH_MAX_RESULTS = 300
DRIFT_WINDOW_SIZE = 10
DRIFT_ON_SLICE_THRESHOLD = 0.5


@dataclass
class RelevanceDriftCursor:
    """Stream-oriented stop/continue decision for native-search connectors,
    exploiting relevance-ranked result ordering as a proxy for "relevant
    supply exhausted" instead of a fixed page/result cap.

    Call `should_continue()` before fetching each job, then `record_job()`
    with whether that job was on-slice for the searched term (see
    `app.config.categories.is_on_slice`). The cursor arms after the first
    full window of `window_size` jobs (decision 9: no extra floor) and
    signals stop once the on-slice share of the trailing window drops below
    `on_slice_threshold` (decision 8). A hard `max_results` ceiling (decision
    10) always applies too, as a backstop for deep uniform pools or boards
    that can't relevance-sort.
    """

    max_results: int = NATIVE_SEARCH_MAX_RESULTS
    window_size: int = DRIFT_WINDOW_SIZE
    on_slice_threshold: float = DRIFT_ON_SLICE_THRESHOLD
    results_seen: int = field(default=0, init=False)
    _window: deque = field(default_factory=deque, init=False)
    _drifted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._window = deque(maxlen=self.window_size)

    def should_continue(self) -> bool:
        if self._drifted:
            return False
        if self.results_seen >= self.max_results:
            return False
        return True

    def record_job(self, on_slice: bool) -> None:
        self.results_seen += 1
        self._window.append(on_slice)
        if len(self._window) < self.window_size:
            return
        on_slice_share = sum(self._window) / len(self._window)
        if on_slice_share < self.on_slice_threshold:
            self._drifted = True


def relevance_drift_cursor(
    max_results: int = NATIVE_SEARCH_MAX_RESULTS,
    window_size: int = DRIFT_WINDOW_SIZE,
    on_slice_threshold: float = DRIFT_ON_SLICE_THRESHOLD,
) -> RelevanceDriftCursor:
    """Native-search connectors only (decision 11) — see `RelevanceDriftCursor`."""
    return RelevanceDriftCursor(
        max_results=max_results,
        window_size=window_size,
        on_slice_threshold=on_slice_threshold,
    )
