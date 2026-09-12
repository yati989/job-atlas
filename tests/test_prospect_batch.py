"""
The bookkeeping around prospect discovery's judgement half.

Whether a company is genuinely a good prospect is the agent's call, verified
by the human trial gate. What is testable here is everything built to stop
that judgement eroding under pace: the search cap, idempotency on re-run, the
refusal to qualify a row without evidence, and an evidence file that renders
even when a run found nothing.

SQLite in-memory, same pattern as test_agentic_batch.py.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config.prospect_theses import TARGET_QUALIFIED_PER_THESIS, THESES
from app.models.orm import Base, Prospect
from app.prospects import batch

THESIS = "india-lending-bnpl"


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _qualified(session, name, *, band=1, thesis=THESIS):
    p = batch.record_candidate(session, name, thesis=thesis)
    return batch.qualify(
        session, p, rank_band=band,
        function_evidence="Head of Data Science, Acme — LinkedIn snippet",
        india_signal="Bengaluru, Karnataka, India",
    )


# ------------------------------------------------------------ idempotency ---

def test_recording_the_same_name_twice_updates_rather_than_duplicates():
    session = _session()
    a = batch.record_candidate(session, "Volt Money", thesis=THESIS, source_query="q1")
    b = batch.record_candidate(session, "Volt Money Pvt Ltd", thesis=THESIS, source_query="q2")
    session.flush()

    assert a.id == b.id
    assert session.query(Prospect).count() == 1
    # First provenance wins — it is the one that actually found the company.
    assert b.source_query == "q1"


def test_re_running_a_thesis_never_resets_a_reached_status():
    """A second pass over the same directory must not undo a judgement."""
    session = _session()
    p = _qualified(session, "Volt Money")
    batch.record_candidate(session, "Volt Money", thesis=THESIS)
    session.flush()
    assert p.status == batch.STATUS_QUALIFIED


def test_a_typod_thesis_slug_is_refused_before_anything_is_written():
    session = _session()
    with pytest.raises(KeyError):
        batch.record_candidate(session, "Volt Money", thesis="india-lendng-bnpl")
    assert session.query(Prospect).count() == 0


# ------------------------------------------------------------------ cap ---

def test_the_fourth_search_is_refused():
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    for expected in range(1, batch.MAX_SEARCHES_PER_CANDIDATE + 1):
        assert batch.spend_search(session, p) == expected

    assert not batch.spend_ok(p)
    with pytest.raises(batch.BudgetExceeded):
        batch.spend_search(session, p)


def test_a_refused_search_does_not_increment_the_counter():
    """The check happens before the increment, so a refusal costs nothing —
    same ordering as bright_data_profiles' budget check preceding its bill."""
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    for _ in range(batch.MAX_SEARCHES_PER_CANDIDATE):
        batch.spend_search(session, p)
    with pytest.raises(batch.BudgetExceeded):
        batch.spend_search(session, p)
    assert p.searches_spent == batch.MAX_SEARCHES_PER_CANDIDATE


def test_cap_reached_is_a_storable_disqualification():
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    batch.disqualify(session, p, batch.REASON_CAP_REACHED)
    assert p.status == batch.STATUS_UNQUALIFIED
    assert p.unqualified_reason == batch.REASON_CAP_REACHED


# ------------------------------------------------------------- evidence ---

def test_qualifying_without_function_evidence_is_refused():
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    with pytest.raises(batch.MissingEvidence):
        batch.qualify(session, p, rank_band=1, function_evidence="  ", india_signal="Bengaluru")
    assert p.status == batch.STATUS_CANDIDATE


def test_qualifying_without_an_india_signal_is_refused():
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    with pytest.raises(batch.MissingEvidence):
        batch.qualify(session, p, rank_band=1, function_evidence="Head of DS", india_signal="")
    assert p.status == batch.STATUS_CANDIDATE


def test_an_out_of_range_band_is_refused():
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    with pytest.raises(ValueError):
        batch.qualify(session, p, rank_band=4, function_evidence="x", india_signal="y")


def test_an_unknown_disqualification_reason_is_refused():
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    with pytest.raises(ValueError):
        batch.disqualify(session, p, "vibes")


def test_disqualifying_keeps_the_row():
    """Free search produces real false negatives — a negative is stored so it
    is re-checkable later, never deleted."""
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    batch.disqualify(session, p, batch.REASON_NO_FUNCTION)
    session.flush()
    assert session.query(Prospect).count() == 1


# ----------------------------------------------------- evidence + report ---

def test_evidence_file_renders_a_run_that_found_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "EVIDENCE_DIR", tmp_path)
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    batch.disqualify(session, p, batch.REASON_NO_FUNCTION)

    path = batch.write_evidence(session, THESIS, batch_name="test")
    text = path.read_text(encoding="utf-8")

    assert "## Qualified" in text
    assert "_None._" in text
    assert "Volt Money" in text
    assert batch.REASON_NO_FUNCTION in text


def test_evidence_file_carries_the_verbatim_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "EVIDENCE_DIR", tmp_path)
    session = _session()
    _qualified(session, "Volt Money", band=2)

    text = batch.write_evidence(session, THESIS, batch_name="test").read_text(encoding="utf-8")

    assert "Volt Money" in text
    assert "band 2" in text
    assert "Head of Data Science, Acme — LinkedIn snippet" in text
    assert "Bengaluru, Karnataka, India" in text


def test_report_flags_a_pool_that_was_harvested_but_never_researched():
    session = _session()
    batch.record_candidate(session, "Volt Money", thesis=THESIS)
    batch.record_candidate(session, "PayCrunch", thesis=THESIS)
    report = batch.batch_report(session, THESIS)
    assert "unresearched: 2" in report
    assert "never" in report


def test_report_flags_qualification_that_spent_almost_no_search():
    session = _session()
    _qualified(session, "Volt Money")
    report = batch.batch_report(session, THESIS)
    assert "under 1 search per candidate" in report


def test_dedup_excluded_rows_do_not_drag_down_the_effort_average():
    """Rows excluded by dedup spend zero searches by design, so counting them
    fired the under-search warning on the first live run even though every
    real candidate had been searched."""
    session = _session()
    p = batch.record_candidate(session, "Volt Money", thesis=THESIS)
    batch.spend_search(session, p)
    batch.spend_search(session, p)
    batch.disqualify(session, p, batch.REASON_NO_FUNCTION)
    batch.record_excluded(
        session, [f"Excluded {i}" for i in range(10)],
        thesis=THESIS, reason=batch.REASON_ALREADY_IN_COMPANIES,
    )

    report = batch.batch_report(session, THESIS)

    assert "avg searches: 2.0" in report
    assert "under 1 search per candidate" not in report


def test_report_counts_bands_and_reasons():
    session = _session()
    _qualified(session, "Volt Money", band=1)
    _qualified(session, "PayCrunch", band=3)
    dead = batch.record_candidate(session, "Acme", thesis=THESIS)
    batch.disqualify(session, dead, batch.REASON_NO_FUNCTION)

    report = batch.batch_report(session, THESIS)
    assert "qualified   : 2" in report
    assert "band 1: 1" in report
    assert "band 3: 1" in report
    assert f"{batch.REASON_NO_FUNCTION}: 1" in report


# ---------------------------------------------------------------- thesis ---

def test_next_thesis_walks_the_checked_in_order():
    session = _session()
    assert batch.next_thesis(session).slug == THESES[0].slug


def test_a_thesis_at_target_is_skipped():
    session = _session()
    first = THESES[0]
    for i in range(TARGET_QUALIFIED_PER_THESIS):
        _qualified(session, f"Company {i}", thesis=first.slug)
    session.flush()
    assert batch.next_thesis(session).slug == THESES[1].slug


def test_next_thesis_is_none_when_every_thesis_is_exhausted():
    session = _session()
    n = 0
    for thesis in THESES:
        for _ in range(TARGET_QUALIFIED_PER_THESIS):
            _qualified(session, f"Company {n}", thesis=thesis.slug)
            n += 1
    session.flush()
    assert batch.next_thesis(session) is None
