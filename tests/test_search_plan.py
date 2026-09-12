"""
Tests for the code-enforced search worklist (2026-08-03) — see
search_plan.py's module docstring for the measured under-spend it replaces.

Covers: plan expansion (single-group employer, dual-group employer, staffing
firm), the mandatory/optional split, and coverage detection against a real
(temp-file) call ledger — the exact mechanism that must catch a Vinsari-style
"only ran attempt 1, never topped up" gap.
"""
import json

from app.contacts import ladder as L
from app.contacts.agentic_batch import CompanyContext
from app.contacts.search_plan import (
    coverage_for_company, mandatory_queries, plan_for_company,
)


def _context(company_id=1, company_type="employer", groups=(L.DATA_AI,), name="Acme"):
    groups = list(groups)
    return CompanyContext(
        company_id=company_id, name=name, company_type=company_type,
        canonical_domain="acme.com", industry=None, hq_location=None,
        search_groups=groups,
        ladder={g: L.ladder_for(company_type, g) for g in groups},
        quotas=L.quotas_for(company_type),
    )


# --------------------------------------------------------------- plan shape

def test_single_group_employer_plans_four_tiers_two_attempts_each():
    ctx = _context(groups=(L.DATA_AI,))
    planned = plan_for_company(ctx)

    tiers = {p.tier for p in planned}
    assert tiers == {L.HEAD, L.HIRING_MANAGER, L.IC, L.TALENT_ACQUISITION}
    # exec_fallback is never planned unconditionally (search-tactics.md: firing
    # it at every empty-head company would hit giant enterprises).
    assert L.EXEC_FALLBACK not in tiers

    # Every tier gets exactly 2 attempts (both seeds exist in EMPLOYER_LADDER).
    for tier in (L.HEAD, L.HIRING_MANAGER, L.IC, L.TALENT_ACQUISITION):
        attempts = sorted(p.attempt for p in planned if p.tier == tier)
        assert attempts == [1, 2]

    # Attempt 1 is mandatory, attempt 2 is the optional top-up.
    for p in planned:
        assert p.conditional == (p.attempt > 1)


def test_dual_group_employer_doubles_leadership_only():
    ctx = _context(groups=(L.DATA_AI, L.CREDIT_RISK))
    planned = plan_for_company(ctx)

    # head/hiring_manager: one plan entry per group (real, different people).
    for tier in (L.HEAD, L.HIRING_MANAGER):
        groups_seen = {p.search_group for p in planned if p.tier == tier}
        assert groups_seen == {L.DATA_AI, L.CREDIT_RISK}

    # ic/talent_acquisition: PER_COMPANY_TIERS — searched once for the whole
    # company, not once per matched group.
    for tier in (L.IC, L.TALENT_ACQUISITION):
        entries = [p for p in planned if p.tier == tier]
        assert len(entries) == 2, "expected 2 attempts total, not 2 per group"
        assert len({p.search_group for p in entries}) == 1


def test_staffing_firm_plans_talent_acquisition_only():
    ctx = _context(company_type="staffing", groups=(L.DATA_AI,))
    planned = plan_for_company(ctx)
    tiers = {p.tier for p in planned}
    assert tiers == {L.TALENT_ACQUISITION}
    assert len(planned) == 2  # both seeds of the 2-seed staffing ladder


def test_no_search_groups_plans_nothing():
    ctx = _context(groups=())
    assert plan_for_company(ctx) == []


def test_mandatory_queries_excludes_top_ups():
    ctx = _context(groups=(L.DATA_AI,))
    mandatory = mandatory_queries(ctx)
    assert all(not p.conditional for p in mandatory)
    assert all(p.attempt == 1 for p in mandatory)
    # One mandatory query per tier.
    assert len(mandatory) == 4


def test_query_text_uses_company_name_and_regional_subdomain():
    ctx = _context(groups=(L.DATA_AI,), name="Weave")
    planned = plan_for_company(ctx)
    head1 = next(p for p in planned if p.tier == L.HEAD and p.attempt == 1)
    assert head1.query == f"{head1.seed_title} at Weave site:in.linkedin.com/in"
    assert "in.linkedin.com/in" in head1.query
    assert '"' not in head1.query  # unquoted, per search-tactics.md


def test_query_seed_titles_match_current_contact_strategy():
    assert L.EMPLOYER_LADDER[L.DATA_AI][L.HEAD] == [
        "Head of Data Science", "Head of AI",
    ]
    assert L.EMPLOYER_LADDER[L.DATA_AI][L.HIRING_MANAGER] == [
        "Data Science Manager", "Principal Data Scientist",
    ]
    assert L.EMPLOYER_LADDER[L.CREDIT_RISK][L.HEAD] == [
        "Head of Credit Risk", "Risk Head",
    ]
    assert L.EMPLOYER_LADDER[L.CREDIT_RISK][L.IC] == [
        "Senior Credit Risk Analyst", "Data Scientist Credit Risk",
    ]
    assert L.STAFFING_LADDER[L.TALENT_ACQUISITION] == [
        "Technical Recruiter", "Talent Acquisition",
    ]


def test_product_manager_plan_searches_each_level_before_alternate_titles():
    """The first pass is one distinct query for each target level.

    Product lead and senior product manager deliberately belong to different
    levels. The broad people-function search comes before narrower recruiter
    titles.
    """
    ctx = _context(groups=("product_manager",), name="Acme")

    plan = plan_for_company(ctx)

    assert [
        (entry.tier, entry.attempt, entry.seed_title, entry.conditional)
        for entry in plan
    ] == [
        (L.HEAD, 1, "Head of Product", False),
        (L.HIRING_MANAGER, 1, "Director of Product", False),
        (L.IC, 1, "Senior Product Manager", False),
        (L.TALENT_ACQUISITION, 1, "Talent Acquisition OR Human Resources OR Recruiter", False),
        (L.HEAD, 2, "VP Product", True),
        (L.HIRING_MANAGER, 2, "Product Lead", True),
        (L.IC, 2, "Product Manager", True),
        (L.TALENT_ACQUISITION, 2, "Technical Recruiter OR Talent Recruiter", True),
    ]

    recruiter_queries = [entry.query for entry in plan if entry.tier == L.TALENT_ACQUISITION]
    assert recruiter_queries == [
        "site:in.linkedin.com/in (Talent Acquisition at Acme OR Human Resources at Acme OR Recruiter at Acme)",
        "site:in.linkedin.com/in (Technical Recruiter at Acme OR Talent Recruiter at Acme)",
    ]


# ---------------------------------------------------------- coverage checks

def _write_log(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _entry(company_id, group, tier, attempt=1, ts="2026-08-03T00:00:00+00:00"):
    return {
        "ts": ts, "query": "q", "results": 1,
        "company_id": company_id, "search_group": group, "tier": tier, "attempt": attempt,
    }


def test_coverage_empty_ledger_reports_everything_outstanding(tmp_path, monkeypatch):
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)

    ctx = _context(company_id=42, groups=(L.DATA_AI,))
    outstanding = coverage_for_company(ctx)
    assert len(outstanding) == 4  # one mandatory query per tier, none issued


def test_coverage_clears_once_mandatory_query_logged(tmp_path, monkeypatch):
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [
        _entry(42, L.DATA_AI, L.HEAD, attempt=1),
        _entry(42, L.DATA_AI, L.HIRING_MANAGER, attempt=1),
        _entry(42, L.DATA_AI, L.IC, attempt=1),
        _entry(42, L.DATA_AI, L.TALENT_ACQUISITION, attempt=1),
    ])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)

    ctx = _context(company_id=42, groups=(L.DATA_AI,))
    assert coverage_for_company(ctx) == []


def test_coverage_reports_single_missing_tier_vinsari_style(tmp_path, monkeypatch):
    """The exact real-world shape: attempt 1 run for every tier except one
    that was skipped entirely (Binance's actual failure), not just a missing
    top-up."""
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [
        _entry(42, L.DATA_AI, L.HEAD, attempt=1),
        _entry(42, L.DATA_AI, L.HIRING_MANAGER, attempt=1),
        _entry(42, L.DATA_AI, L.IC, attempt=1),
        # talent_acquisition never issued at all.
    ])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)

    ctx = _context(company_id=42, groups=(L.DATA_AI,))
    outstanding = coverage_for_company(ctx)
    assert len(outstanding) == 1
    assert outstanding[0].tier == L.TALENT_ACQUISITION
    assert outstanding[0].attempt == 1


def test_coverage_ignores_calls_for_a_different_company(tmp_path, monkeypatch):
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [
        _entry(999, L.DATA_AI, L.HEAD, attempt=1),  # wrong company_id
    ])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)

    ctx = _context(company_id=42, groups=(L.DATA_AI,))
    outstanding = coverage_for_company(ctx)
    assert len(outstanding) == 4  # the 999 entry must not count toward 42


def test_coverage_ignores_unkeyed_legacy_log_lines(tmp_path, monkeypatch):
    """Pre-2026-08-03 log lines have no company_id/search_group/tier — they
    must not be silently attributed to any company's coverage."""
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [
        {"ts": "2026-08-02T00:00:00+00:00", "query": "Technical Recruiter at Vinsari site:in.linkedin.com", "results": 1},
    ])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)

    ctx = _context(company_id=42, groups=(L.DATA_AI,))
    assert len(coverage_for_company(ctx)) == 4


# ------------------------------------------------- quota-aware coverage (2026-08-04)
# A query aimed at one tier routinely returns people belonging to others
# ("Head of Data Science at X" surfacing three ICs). Those are now kept and
# counted, so a tier can be full before the plan reaches its own query —
# billing it anyway buys nothing. See coverage_for_company's docstring.

def _judged(tier, group=L.DATA_AI, n=1, email="a@example.com"):
    from app.contacts.agentic_batch import JudgedContact
    return [
        JudgedContact(
            full_name=f"P{i}", title="t", profile_url=f"https://in.linkedin.com/in/p{tier}{i}",
            tier=tier, search_group=group, tier_rationale="r", judged_text="j", query="q",
            email=email, email_confidence="unverified",
        )
        for i in range(n)
    ]


def test_coverage_skips_a_tier_whose_quota_is_already_filled(tmp_path, monkeypatch):
    """The harvest case: ic was never searched, but its quota is already met by
    people harvested from other tiers' queries. That query is not worth billing."""
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [
        _entry(42, L.DATA_AI, L.HEAD, attempt=1),
        _entry(42, L.DATA_AI, L.HIRING_MANAGER, attempt=1),
        _entry(42, L.DATA_AI, L.TALENT_ACQUISITION, attempt=1),
    ])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)
    ctx = _context(company_id=42, groups=(L.DATA_AI,))

    # Ledger-only reading still reports the unissued ic query.
    assert [p.tier for p in coverage_for_company(ctx)] == [L.IC]

    # With ic's quota filled by harvest, it is no longer outstanding.
    harvested = _judged(L.IC, n=L.QUOTAS[L.IC])
    assert coverage_for_company(ctx, harvested) == []


def test_coverage_still_reports_a_short_tier_that_was_never_searched(tmp_path, monkeypatch):
    """The guard's real purpose is unchanged: partially-filled is NOT filled,
    so the Binance/Vinsari 'never looked' failure still raises."""
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [
        _entry(42, L.DATA_AI, L.HEAD, attempt=1),
        _entry(42, L.DATA_AI, L.HIRING_MANAGER, attempt=1),
        _entry(42, L.DATA_AI, L.TALENT_ACQUISITION, attempt=1),
    ])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)
    ctx = _context(company_id=42, groups=(L.DATA_AI,))

    one_short = _judged(L.IC, n=L.QUOTAS[L.IC] - 1)
    assert [p.tier for p in coverage_for_company(ctx, one_short)] == [L.IC]


def test_coverage_ignores_emailless_contacts_when_counting_quota(tmp_path, monkeypatch):
    """Quota counts only contacts with an email (agentic_batch._status_for's
    rule) — an emailless lead is stored but cannot retire a tier's query."""
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [
        _entry(42, L.DATA_AI, L.HEAD, attempt=1),
        _entry(42, L.DATA_AI, L.HIRING_MANAGER, attempt=1),
        _entry(42, L.DATA_AI, L.TALENT_ACQUISITION, attempt=1),
    ])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)
    ctx = _context(company_id=42, groups=(L.DATA_AI,))

    emailless = _judged(L.IC, n=L.QUOTAS[L.IC], email=None)
    assert [p.tier for p in coverage_for_company(ctx, emailless)] == [L.IC]


# ------------------------------------------------- fetched vs kept (2026-08-04)
# `results` is the RAW SERP count (near-always 10 — a full Google page), so it
# is useless as a "how much did we look at" denominator. `profile_results` is
# the /in/ rows actually handed to the agent. See search_source.
# _log_bright_data_call.

def _entry_with_profiles(company_id, group, tier, profiles, attempt=1):
    e = _entry(company_id, group, tier, attempt=attempt)
    e["profile_results"] = profiles
    return e


def test_profiles_fetched_sums_only_this_company(tmp_path, monkeypatch):
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [
        _entry_with_profiles(42, L.DATA_AI, L.HEAD, 7),
        _entry_with_profiles(42, L.DATA_AI, L.IC, 3),
        _entry_with_profiles(99, L.DATA_AI, L.HEAD, 8),  # different company
    ])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)
    assert ss.profiles_fetched_for(42) == 10


def test_profiles_fetched_is_unknown_not_zero_for_legacy_entries(tmp_path, monkeypatch):
    """Pre-2026-08-04 lines logged only the raw SERP count. Reporting them as 0
    would invert the signal — a well-searched company would read as a total
    filtering failure."""
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [_entry(42, L.DATA_AI, L.HEAD), _entry(42, L.DATA_AI, L.IC)])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)
    assert ss.profiles_fetched_for(42) is None


def test_profiles_fetched_counts_zero_result_calls(tmp_path, monkeypatch):
    """A query that returned no profiles is real information (genuine scarcity),
    distinct from a query that was never keyed at all."""
    import app.contacts.search_source as ss
    log = tmp_path / "brightdata_calls.jsonl"
    _write_log(log, [_entry_with_profiles(42, L.DATA_AI, L.HEAD, 0)])
    monkeypatch.setattr(ss, "BRIGHT_DATA_CALL_LOG", log)
    assert ss.profiles_fetched_for(42) == 0
