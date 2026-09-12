"""
Deterministic, zero-token replacement for the agent-driven side of the
`enrich-company-domains` skill (issue #22): fills `companies.canonical_domain`
by code instead of by an agent spending WebSearch + judgement tokens per
company. Same MX-verification contract as the skill (`_get_mx_host` is the
only thing that turns a candidate into a stored domain) plus one more the
skill got from agent judgement for free: a name-plausibility check
(`_plausible_match`), since without it an MX-valid domain that merely
happens to exist is not the same as being the right company's domain.

Search-first, heuristic-fallback:
  1. One Serper.dev search ("<name> official website") — Serper is already
     a funded transport used elsewhere in this project
     (app/contacts/search_source.py), so this adds no new billing account.
     (2026-08-04: briefly swapped to Bright Data's SERP passthrough after
     Serper's account ran dry mid-backlog, then reverted the same day —
     Bright Data's raw-HTML fetch runs ~20-25s/call vs. Serper's
     near-instant JSON, and this script's whole value is speed through a
     large backlog, so a fresh Serper key beat the slower transport.)
     Walk the organic results in rank order; the first one that is both
     off the aggregator/ATS blocklist AND plausibly matches the company
     name (see `_plausible_match`) AND MX-verifies is accepted with
     confidence (`done_search`) — the only status this script auto-writes.
  2. If nothing passes all three checks: try name-slug heuristics against a
     short TLD list. A heuristic hit is written NOWHERE — a blind guess that
     happens to MX-verify is indistinguishable from a wrong company that
     merely registered the same domain (confirmed live: "Caterpillar Inc."
     heuristically guessed `caterpillar.ai`, an unrelated but real mail
     domain, when the true corporate domain is `cat.com`). It's surfaced in
     the printout as an unconfirmed `guess` and the row is marked
     `ambiguous`, for a human/agent to resolve with actual judgement.
  3. Nothing at all found (no plausible search hit, no MX-valid guess) is
     `unresolvable`.

What this deliberately does NOT do: disambiguate between two MX-valid
candidates using business-context judgement (e.g. Curefit's cure.fit vs.
cult.fit, or a same-named-but-wrong company). That judgement call is exactly
what agent review is for — this script optimizes for cheaply and *safely*
resolving the unambiguous majority, routing everything else to `ambiguous`
rather than guessing, not for replacing judgement on the hard cases.

Usage:
    python -m scripts.resolve_company_domains --limit 200
    python -m scripts.resolve_company_domains --limit 50 --dry-run
"""
import argparse
import re
import sys
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.contacts.email_resolution import _get_mx_host
from app.db.session import get_session
from app.models.orm import Company

SERPER_ENDPOINT = "https://google.serper.dev/search"

# Ordered most-to-least common across this project's company set (India-heavy,
# tech/services-heavy) — checked cheapest (no network beyond DNS) first.
_CANDIDATE_TLDS = (".com", ".ai", ".io", ".co", ".in", ".co.in")

# Legal suffixes / boilerplate stripped before slugging, so "Foo Bar Pvt Ltd"
# and "Foo Bar" try the same candidates. Longest-first so "private limited"
# matches before a lone "limited" would truncate it wrong.
_LEGAL_SUFFIXES = (
    "private limited", "pvt ltd", "pvt. ltd.", "pvt ltd.", "limited",
    "incorporated", "corporation", "llc", "l.l.c.", "inc.", "inc",
    "ltd.", "ltd", "corp.", "corp", "gmbh", "b.v.", "bv", "s.a.", "sa",
    "co.", "llp",
)

# Hosts a Serper top result must never resolve to — job boards, socials,
# directories, data brokers, and third-party ATS/careers platforms (a
# company's Greenhouse/Lever page ranks high but is never their mail domain).
_AGGREGATOR_HOSTS = (
    "linkedin.com", "naukri.com", "indeed.com", "glassdoor.com",
    "glassdoor.co.in", "crunchbase.com", "wikipedia.org", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "youtube.com", "zaubacorp.com",
    "tofler.in", "apollo.io", "rocketreach.co", "wellfound.com", "angel.co",
    "bloomberg.com", "similarweb.com", "owler.com", "pitchbook.com",
    "signalhire.com", "github.com", "medium.com", "reddit.com",
    "quora.com", "ambitionbox.com", "yellowpages.com", "justdial.com",
    "monster.com", "shine.com", "foundit.in", "timesjobs.com",
    "clutch.co", "goodfirms.co", "producthunt.com",
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "myworkday.com",
    "icims.com", "smartrecruiters.com", "jobvite.com", "taleo.net",
    "successfactors.com", "dover.com", "ashbyhq.com", "bamboohr.com",
    "workable.com", "breezy.hr", "recruitee.com", "personio.com",
    # Confirmed live: generic directory/placeholder domains that ranked #1
    # for two entirely different banks' "official website" queries.
    "bank.in", "google.com", "site.com",
)

# Multi-label public suffixes seen in this dataset; anything else is assumed
# a plain single-label TLD (a real public-suffix list is overkill here —
# this only needs to be right for the TLDs this project's companies use).
_MULTI_LABEL_SUFFIXES = ("co.in", "com.au", "co.uk", "co.nz", "com.sg")


def _printable(text: str) -> str:
    """For console display only — never for search queries or DB writes,
    which always use the real name. Confirmed live: a company name
    containing U+200E (invisible left-to-right mark) crashed the whole batch
    with UnicodeEncodeError on Windows' cp1252 console, which get_session()
    then turned into a full rollback of every prior resolution in that run
    (100 companies' worth of already-spent Serper calls, thrown away for
    nothing). A print crash must never be able to cost DB writes."""
    encoding = sys.stdout.encoding or "ascii"
    return text.encode(encoding, errors="replace").decode(encoding)


def _slug(name: str) -> str:
    lowered = name.lower()
    lowered = re.sub(r"\([^)]*\)", " ", lowered)  # parenthetical asides
    lowered = re.split(r"[-–—]", lowered)[0]  # "Foo - ServiceNow Partner" -> "Foo"
    for suffix in _LEGAL_SUFFIXES:
        lowered = re.sub(rf"\b{re.escape(suffix)}\b", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", "", lowered).strip()


def _heuristic_candidates(name: str) -> list[str]:
    slug = _slug(name)
    if not slug:
        return []
    return [f"{slug}{tld}" for tld in _CANDIDATE_TLDS]


def _registrable_domain(netloc: str) -> str:
    host = netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# Generic business words that carry no identity signal on their own — "Why
# Hiring" and "Grow the Roses" both have every non-stopword token be exactly
# this kind of noise, which is why the plausibility check below rejected
# them (correctly) as `ambiguous` rather than accepting whatever the top
# search result happened to be (amazon.com, brightview.com — both wrong).
_GENERIC_TOKENS = frozenset({
    "the", "and", "for", "of", "in", "on", "at", "is", "an", "a", "by", "as",
    "to", "inc", "llc", "ltd", "co", "corp", "corporation",
    "consulting", "solutions", "technologies", "technology", "group",
    "services", "india", "global", "systems", "labs", "ai", "tech",
    "company", "companies", "partners", "holdings", "private", "limited",
    "pvt", "llp", "hiring", "recruitment", "recruiting", "staffing",
    # Category words for common industries — confirmed live: "bank" alone
    # let a generic banking directory (bank.in) pass identity-check for BOTH
    # HDFC Bank and IDFC FIRST Bank, two completely different companies,
    # the same failure mode "consulting"/"services" were already excluded
    # for.
    "bank", "banking", "finance", "financial", "insurance", "healthcare",
})


def _core_tokens(name: str) -> list[str]:
    """Length >=2 (not >=3) so short but genuinely distinguishing tokens
    survive — "YO IT Consulting" vs. "YO HR Consultancy" differ only in a
    2-letter token ("IT"/"HR"); filtering those out left both companies
    with an empty token list, which made every candidate look equally
    plausible and let one silently claim the other's domain."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    return [w for w in words if len(w) >= 2 and w not in _GENERIC_TOKENS]


def _plausible_match(name: str, domain: str) -> bool:
    """A candidate domain earns trust only if the company's name is actually
    legible in it — every non-generic token substring-matches, or the full
    heuristic slug does. Filters out the "Serper's top hit for a generic
    query happened to be some big unrelated brand" failure mode, which a
    bare aggregator blocklist can't catch since amazon.com/brightview.com
    aren't job boards or ATS platforms, just wrong answers for this query.

    An empty token list (name is all generic/stopword words) returns False,
    not True — with nothing left to check identity against, treating that
    as "anything passes" is exactly the gap that let "YO IT Consulting"
    accept "YO HR Consultancy"'s domain. No signal means no trust, not free
    trust; the caller routes the rejection to `ambiguous` for a human/agent
    look rather than silently writing a guess.

    Also checks the REVERSE containment (domain label is a prefix of the
    slug, not just the slug in the label) — confirmed live: "QuiverAI"
    slugs to "quiverai", but the real domain is quiver.ai (label "quiver"),
    and "GrackerAI"/gracker.ai is the same shape. Dropping the trailing
    "AI" for a shorter .ai domain is an extremely common startup-naming
    convention this project's dataset is full of; only checking name-in-domain
    silently missed all of them. Length-gated at 4 chars so this can't
    reopen the "cat" ⊂ "caterpillar" false-positive class (Caterpillar's
    real domain, cat.com, correctly stays unconfirmed — 3 chars is still
    too short and generic to trust)."""
    label = domain.split(".")[0]
    slug = _slug(name)
    if slug and slug in label:
        return True
    if slug and len(label) >= 4 and slug.startswith(label):
        return True
    tokens = _core_tokens(name)
    if not tokens:
        return False
    return any(token in label for token in tokens)


def _match_strength(name: str, domain: str) -> int:
    """How convincingly `domain` names `name`, for picking the BEST plausible
    candidate rather than the first-ranked one. Confirmed live: "Advice
    Media" and "Arbor Tek Systems" both passed `_plausible_match` against a
    bigger, unrelated, more-prominent company's domain (myadvice.com,
    teksystems.com) purely because a single short/generic-ish token
    ("advice", "tek") substring-matched — while a far more precise candidate
    (advicemedia.com, arborteksys.com) sat right below it in the same
    result set, unused, because `resolve_one` stopped at the first hit.
    A full-slug match is unambiguously the strongest signal; short of that,
    more/longer matched tokens beat one weak one."""
    label = domain.split(".")[0]
    slug = _slug(name)
    if slug and slug in label:
        return 1000 + len(slug)
    if slug and len(label) >= 4 and slug.startswith(label):
        return 500 + len(label)  # strong, but a full match still wins if both are candidates
    matched = [t for t in _core_tokens(name) if t in label]
    return sum(len(t) for t in matched)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _search_candidate_domains(query: str, name: str, *, company_id: int | None = None) -> list[str]:
    """Non-aggregator organic-result hosts, in ranked order, deduped. Empty
    if Serper isn't configured. One call regardless of how many candidates
    are checked afterward — no per-result agent judgement, the trade this
    script makes for running at zero tokens.

    Briefly switched to Bright Data's SERP passthrough (2026-08-04) after
    Serper's account ran out of credits — reverted the same day per explicit
    request: Bright Data's raw-HTML fetch is materially slower per call
    (~20-25s vs. Serper's near-instant JSON response), and speed matters more
    than transport diversity here since this script's whole value is
    processing the backlog fast. `company_id` is accepted but unused now —
    kept so callers (and Bright-Data-era log entries) don't need touching if
    this needs to flip back again."""
    if not settings.SERPER_API_KEY:
        return []
    headers = {"X-API-KEY": settings.SERPER_API_KEY, "Content-Type": "application/json"}
    body = {"q": query, "gl": "in", "num": 10}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        resp = client.post(SERPER_ENDPOINT, json=body)
        resp.raise_for_status()
        data = resp.json()
    name_slug = _slug(name)
    out: list[str] = []
    for result in data.get("organic") or []:
        url = result.get("link", "")
        netloc = httpx.URL(url).host or ""
        if not netloc:
            continue
        domain = _registrable_domain(netloc)
        if not domain or domain in out:
            continue
        # The blocklist exists to stop OTHER companies' pages (a LinkedIn
        # profile, a Reddit thread) from being mistaken for their own site —
        # but confirmed live, it also silently blocked "Reddit" itself from
        # ever resolving to reddit.com. Narrow, safe override: only when the
        # company's own full name-slug exactly equals the domain label (so
        # this can't be tricked by a company merely mentioning "reddit" in
        # its name), let it through despite the blocklist.
        if domain in _AGGREGATOR_HOSTS and domain.split(".")[0] != name_slug:
            continue
        # .gov/.edu/.mil are institutions, never a recruiting-pipeline
        # company — confirmed live: "Junction" (a tech company) matched
        # apachejunctionaz.gov purely because "junction" substring-matches
        # an Arizona city's domain, which the name-plausibility check can't
        # catch since the substring genuinely is there.
        if domain.endswith((".gov", ".edu", ".mil")):
            continue
        out.append(domain)
    return out


@dataclass(frozen=True)
class Resolution:
    domain: str | None       # written to canonical_domain — only set when status == "done_search"
    status: str              # "done_search" | "ambiguous" | "unresolvable"
    guess: str | None        # unconfirmed heuristic guess, for the printout only, never persisted


def resolve_one(name: str, *, company_id: int | None = None) -> Resolution:
    """Search-first, heuristic-fallback — deliberately the opposite order
    from an earlier version of this script. A blind name-slug guess has no
    way to know a company's real domain diverges from its name (Caterpillar
    Inc.'s actual domain is cat.com, not caterpillar.com/.ai) and will happily
    accept an unrelated but MX-valid domain that merely matches the slug. A
    real search result, filtered against the aggregator/ATS blocklist, is
    far less likely to be that kind of false positive, so it goes first;
    heuristics only cover the case where search finds nothing usable.

    Returns (domain_or_None, status): 'done_search' (search hit that also
    passed the name-plausibility check — see `_plausible_match`) — the only
    status this function auto-writes with confidence. Everything else
    returns domain=None even if a candidate technically MX-verified:
    'ambiguous' means a heuristic guess resolved but was never confirmed by
    an actual search result (the Caterpillar Inc. failure mode — a slug
    guess landing on a real-but-wrong company's MX record is indistinguishable
    from a correct guess without content the script doesn't have), and
    'unresolvable' means nothing MX-verified at all. Both leave
    `canonical_domain` NULL for a human/agent to resolve with judgement;
    the guessed domain is only surfaced in the batch printout (via `guess`),
    never persisted un-confirmed."""
    best: tuple[int, str] | None = None  # (strength, domain) — highest strength wins, first-seen breaks ties
    for candidate in _search_candidate_domains(f"{name} official website", name, company_id=company_id):
        if not _plausible_match(name, candidate) or not _get_mx_host(candidate):
            continue
        strength = _match_strength(name, candidate)
        if best is None or strength > best[0]:
            best = (strength, candidate)
    if best is not None:
        return Resolution(best[1], "done_search", None)

    for candidate in _heuristic_candidates(name):
        if _get_mx_host(candidate):
            return Resolution(None, "ambiguous", candidate)

    return Resolution(None, "unresolvable", None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true", help="resolve but don't write to DB")
    parser.add_argument("--min-id", type=int, default=None,
                         help="restrict to id >= this, for partitioning disjoint work across parallel processes")
    parser.add_argument("--max-id", type=int, default=None,
                         help="restrict to id <= this, for partitioning disjoint work across parallel processes")
    parser.add_argument("--retry-ambiguous", action="store_true",
                         help="re-attempt the ambiguous bucket instead of fresh/unattempted companies — "
                              "worth doing after an algorithm improvement, since a company marked ambiguous "
                              "under an older/weaker matching rule may resolve cleanly now")
    args = parser.parse_args()

    with get_session() as session:
        if args.retry_ambiguous:
            query = (
                select(Company)
                .where(Company.canonical_domain.is_(None))
                .where(Company.domain_resolution_status == "ambiguous")
                # A row already merged away by dedup_companies.py doesn't
                # need its own domain — its jobs live on the canonical row
                # now. Confirmed live: retrying these wasted ~43% of a
                # retry batch and inflated the collision count, since a
                # merged-away row will always legitimately collide against
                # its own canonical sibling's already-claimed domain
                # ("Openrouter" vs "OpenRouter" — id 170 was 'duplicate'
                # but still 'ambiguous' from before it lost the merge).
                .where(Company.contact_enrichment_status.is_distinct_from("duplicate"))
            )
        else:
            query = (
                select(Company)
                .where(Company.canonical_domain.is_(None))
                .where(Company.domain_resolution_status.is_distinct_from("unresolvable"))
                .where(Company.domain_resolution_status.is_distinct_from("ambiguous"))
            )
        if args.min_id is not None:
            query = query.where(Company.id >= args.min_id)
        if args.max_id is not None:
            query = query.where(Company.id <= args.max_id)
        companies = session.execute(
            query.order_by(Company.id).limit(args.limit)
        ).scalars().all()

        # Domains already claimed by a DIFFERENT company from a prior run —
        # a fresh resolution landing on one of these is just as much a
        # collision as two rows colliding within this batch, and needs the
        # same demotion rather than silently overwriting/duplicating it.
        existing_domains = {
            domain
            for (domain,) in session.execute(
                select(Company.canonical_domain).where(Company.canonical_domain.is_not(None))
            )
        }

        # Two passes: resolve everyone first, THEN write, so a same-batch
        # domain collision (two different companies both landing on
        # yohrconsultancy.com — confirmed live for "YO IT Consulting" vs.
        # "YO HR Consultancy", two real sibling-brand companies the
        # substring check can't tell apart) can be caught and demoted for
        # BOTH rows before anything is persisted, instead of silently
        # letting whichever ran second win.
        pending: list[tuple] = []  # (company, result)
        skipped = 0
        for company in companies:
            if company.industry and "placeholder" in company.industry.lower():
                skipped += 1
                print(f"{company.id:>6}  {_printable(company.name)[:45]:<45}  {'-':<30}  skipped (placeholder row)")
                if not args.dry_run:
                    # Mark it, not just skip it — otherwise this garbage row
                    # keeps re-matching the "unresolved" query and gets
                    # reprocessed every future batch forever.
                    company.domain_resolution_status = "unresolvable"
                continue
            try:
                pending.append((company, resolve_one(company.name, company_id=company.id)))
            except Exception as exc:
                # get_session() rolls back the WHOLE batch on any
                # uncaught exception — confirmed live, a single company
                # crashing threw away 99 other companies' worth of
                # already-spent Serper calls. Leaving this one company
                # untouched (unresolved, retried next run) costs far less
                # than that.
                print(f"{company.id:>6}  {_printable(company.name)[:45]:<45}  {'-':<30}  ERROR: {exc!r}")

        claimed: dict[str, list[int]] = {}
        for company, result in pending:
            if result.status == "done_search":
                claimed.setdefault(result.domain, []).append(company.id)
        collided_domains = {d for d, ids in claimed.items() if len(ids) > 1} | (
            {d for d in claimed if d in existing_domains}
        )

        counts = {"done_search": 0, "unresolvable": 0, "ambiguous": 0, "collision": 0}
        for company, result in pending:
            if result.status == "done_search" and result.domain in collided_domains:
                result = Resolution(None, "ambiguous", result.domain)
                counts["collision"] += 1
            counts[result.status] += 1
            shown = result.domain or (f"guess: {result.guess}" if result.guess else "-")
            marker = {"done_search": "serper", "unresolvable": "-", "ambiguous": "? needs review"}[result.status]
            print(f"{company.id:>6}  {_printable(company.name)[:45]:<45}  {_printable(shown):<30}  {marker}")
            if not args.dry_run:
                company.canonical_domain = result.domain
                company.domain_resolution_status = "done" if result.domain else result.status

        resolved = counts["done_search"]
        total = len(pending)
        print()
        print(f"Batch: {len(companies)} companies ({skipped} skipped as placeholder rows)")
        print(f"  resolved (search-confirmed): {resolved}")
        print(f"  ambiguous (unconfirmed guess, needs a human/agent look): {counts['ambiguous']}"
              + (f", {counts['collision']} of which were same-batch domain collisions demoted for safety" if counts['collision'] else ""))
        print(f"  unresolvable (nothing found at all): {counts['unresolvable']}")
        if total:
            print(f"  hit rate: {resolved / total:.0%}")
        if args.dry_run:
            print("\n--dry-run: nothing written to the DB")


if __name__ == "__main__":
    main()
