from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.contacts.agentic_batch import contexts_for_approved_companies
from app.decision_runs import (approve_decision_run,
                               approve_decision_run_from_workbook,
                               approved_contact_search_groups,
                               create_decision_run, prepare_decision_run)
from app.decision_runs.workbook_approval import (
    read_contact_search_selections,
    read_workbook_approval,
)
from app.models.orm import Base, DecisionRunApproval
from app.models.schemas import NormalizedJob
from app.pipeline.upsert import upsert_job
from app.reporting.decision_report import build_decision_workbook


def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _populated_group(workbook):
    return next(sheet for sheet in (workbook[f"Group {number}"] for number in range(1, 5)) if sheet.max_row > 1)


def test_decision_workbook_contact_search_dropdowns_control_approved_scope(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    first = upsert_job(s, NormalizedJob(source="x", external_job_id="role-1", title="Data Scientist", company_name_raw="Acme", description_raw="work", posted_at=now, is_remote=True))
    upsert_job(s, NormalizedJob(source="x", external_job_id="role-2", title="Credit Risk Analyst", company_name_raw="Acme", description_raw="work", posted_at=now, is_remote=True))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None  # This fixture intentionally covers a legacy snapshot.
    prepare_decision_run(s, run.id)
    decision_path = tmp_path / "decision.xlsx"
    build_decision_workbook(s, run.id, decision_path)

    workbook = openpyxl.load_workbook(decision_path)
    assert "Approval instructions" in workbook.sheetnames
    group = _populated_group(workbook)
    assert workbook.active.title == group.title
    headers = [cell.value for cell in group[1]]
    approval_col = headers.index("company_approval") + 1
    selection_col = headers.index("contact_search_selection") + 1
    assert approval_col == 1
    assert selection_col == 2
    assert headers[2] == "company_name"
    assert group.freeze_panes == "D2"
    primary_col = headers.index("primary_job") + 1
    primary_row = next(row for row in range(2, group.max_row + 1) if group.cell(row, primary_col).value is True)
    assert group.cell(primary_row, approval_col).value == "Approved"
    assert group.cell(primary_row, selection_col).value == "Automatic (posting-derived)"
    assert group.cell(1, approval_col).fill.fill_type == "solid"
    assert group.cell(primary_row, approval_col).fill.fill_type == "solid"
    validations = [validation for validation in group.data_validations.dataValidation
                   if str(group.cell(primary_row, approval_col).coordinate) in str(validation.sqref)]
    assert len(validations) == 1
    assert "Approved" in validations[0].formula1
    assert "Rejected" in validations[0].formula1
    assert validations[0].showInputMessage is True
    assert validations[0].showErrorMessage is True

    group.cell(primary_row, selection_col).value = "Credit Risk only"
    workbook.save(decision_path)
    selections = read_contact_search_selections(decision_path, run.id)
    scope = approve_decision_run(s, run.id, "target 1; G1 all; G2 all; G3 all; G4 all",
                                 contact_search_selections=selections)
    assert scope.company_ids == (first.company_id,)
    approval = s.query(DecisionRunApproval).filter_by(run_id=run.id).one()
    assert approval.normalized_rules["contact_search_selections"][str(first.company_id)] == "credit_risk"
    assert approved_contact_search_groups(s, run.id, first.company_id) == ["credit_risk"]


def test_decision_workbook_defaults_all_group_1_and_only_group_1_to_approved():
    from types import SimpleNamespace
    from app.reporting.decision_report import _default_company_approvals

    companies = [
        SimpleNamespace(company_id=1, group_number=1, rank=1),
        SimpleNamespace(company_id=2, group_number=1, rank=2),
        SimpleNamespace(company_id=3, group_number=2, rank=1),
        SimpleNamespace(company_id=4, group_number=3, rank=1),
        SimpleNamespace(company_id=5, group_number=4, rank=1),
    ]
    assert _default_company_approvals(companies) == {1, 2}


def test_decision_workbook_opens_on_first_populated_group(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    upsert_job(s, NormalizedJob(source="x", external_job_id="onsite", title="Data Scientist", company_name_raw="Acme", description_raw="work", posted_at=now, is_remote=False))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None
    prepare_decision_run(s, run.id)
    decision_path = tmp_path / "decision.xlsx"
    build_decision_workbook(s, run.id, decision_path)

    workbook = openpyxl.load_workbook(decision_path)
    assert workbook["Group 1"].max_row == 1
    assert workbook["Group 2"].max_row > 1
    assert workbook.active.title == "Group 2"


def test_workbook_contact_search_reader_rejects_unknown_choice(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    upsert_job(s, NormalizedJob(source="x", external_job_id="role-invalid", title="Data Scientist", company_name_raw="Acme", description_raw="work", posted_at=now, is_remote=True))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None  # This fixture intentionally covers a legacy snapshot.
    prepare_decision_run(s, run.id)
    decision_path = tmp_path / "decision.xlsx"; build_decision_workbook(s, run.id, decision_path)
    workbook = openpyxl.load_workbook(decision_path); group = _populated_group(workbook)
    headers = [cell.value for cell in group[1]]
    group.cell(2, headers.index("contact_search_selection") + 1).value = "Anything else"
    workbook.save(decision_path)
    with pytest.raises(ValueError, match="invalid contact search selection"):
        read_contact_search_selections(decision_path, run.id)


def test_workbook_reader_accepts_libreoffice_boolean_formulas(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    first = upsert_job(s, NormalizedJob(source="x", external_job_id="libreoffice", title="Data Scientist", company_name_raw="Acme", description_raw="work", posted_at=now, is_remote=True))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None
    prepare_decision_run(s, run.id)
    decision_path = tmp_path / "decision.xlsx"
    build_decision_workbook(s, run.id, decision_path)

    workbook = openpyxl.load_workbook(decision_path)
    group = _populated_group(workbook)
    headers = [cell.value for cell in group[1]]
    primary_col = headers.index("primary_job") + 1
    approval_col = headers.index("company_approval") + 1
    primary_row = next(row for row in range(2, group.max_row + 1) if group.cell(row, primary_col).value is True)
    group.cell(primary_row, primary_col).value = "=TRUE()"
    group.cell(primary_row, approval_col).value = "Approved"
    workbook.save(decision_path)

    result = read_workbook_approval(decision_path, run.id)
    assert result.company_approvals == {first.company_id: True}
    assert result.contact_search_selections == {first.company_id: "auto"}


def test_no_contact_search_selection_removes_company_from_contact_work_scope():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    job = upsert_job(s, NormalizedJob(source="x", external_job_id="no-contact", title="Data Scientist", company_name_raw="Acme", description_raw="work", posted_at=now, is_remote=True))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None  # This fixture intentionally covers a legacy snapshot.
    prepare_decision_run(s, run.id)
    scope = approve_decision_run(
        s, run.id, "target 1; G1 all; G2 all; G3 all; G4 all",
        contact_search_selections={job.company_id: "none"},
    )
    assert approved_contact_search_groups(s, run.id, job.company_id) == []
    assert contexts_for_approved_companies(s, scope) == []


def test_workbook_exact_approval_selects_arbitrary_approved_company():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    first = upsert_job(s, NormalizedJob(source="x", external_job_id="ranked-first", title="Data Scientist", company_name_raw="First", description_raw="work", posted_at=now, is_remote=True))
    second = upsert_job(s, NormalizedJob(source="x", external_job_id="ranked-second", title="Credit Risk Analyst", company_name_raw="Second", description_raw="work", posted_at=now, is_remote=True))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None
    prepare_decision_run(s, run.id)

    scope = approve_decision_run_from_workbook(
        s, run.id,
        {first.company_id: False, second.company_id: True},
        {first.company_id: "auto", second.company_id: "credit_risk"},
        approver="candidate@example.com",
    )

    assert scope.company_ids == (second.company_id,)
    approval = s.query(DecisionRunApproval).filter_by(run_id=run.id).one()
    assert approval.normalized_rules["selection_mode"] == "workbook_exact"
    assert approval.normalized_rules["selected_company_ids"] == [second.company_id]


def test_approval_rejects_incomplete_workbook_company_selections():
    s = session(); now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    first = upsert_job(s, NormalizedJob(source="x", external_job_id="first", title="Data Scientist", company_name_raw="First", description_raw="work", posted_at=now, is_remote=True))
    upsert_job(s, NormalizedJob(source="x", external_job_id="second", title="Data Scientist", company_name_raw="Second", description_raw="work", posted_at=now, is_remote=True))
    run = create_decision_run(s, since=(now - timedelta(days=1)).isoformat(), cutoff=now)
    run.telemetry_version = None  # This fixture intentionally covers a legacy snapshot.
    prepare_decision_run(s, run.id)

    with pytest.raises(ValueError, match="missing companies"):
        approve_decision_run(
            s, run.id, "target 2; G1 all; G2 all; G3 all; G4 all",
            contact_search_selections={first.company_id: "auto"},
        )
