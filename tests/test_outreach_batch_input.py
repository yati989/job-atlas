import csv

import pytest
from openpyxl import Workbook

from app.outreach.batch_input import load_outreach_targets, main, parse_pasted_targets


def test_reads_csv_and_excel_with_the_same_validated_contract(tmp_path):
    headers = ["recipient_email", "company_name", "to_name", "recipient_title"]
    rows = [
        ["person@example.com", "Example Co", "Person", "Engineering Manager"],
        ["", "Discovery Co", "", ""],
    ]
    csv_path = tmp_path / "targets.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    xlsx_path = tmp_path / "targets.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(xlsx_path)

    csv_targets = load_outreach_targets(csv_path)
    excel_targets = load_outreach_targets(xlsx_path)

    assert excel_targets == csv_targets
    assert csv_targets[0].recipient_email == "person@example.com"
    assert csv_targets[1].recipient_email is None
    assert csv_targets[1].requires_email_discovery is True


def test_manual_paste_accepts_header_rows_or_plain_email_lines():
    table = parse_pasted_targets(
        "recipient_email,company_name\nfirst@example.com,One Co\nsecond@example.com,Two Co"
    )
    plain = parse_pasted_targets("first@example.com\nsecond@example.com")

    assert [row.company_name for row in table] == ["One Co", "Two Co"]
    assert [row.recipient_email for row in plain] == [
        "first@example.com", "second@example.com",
    ]


def test_row_without_email_or_company_is_rejected_with_row_number(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("recipient_email,company_name\n,\n", encoding="utf-8")

    with pytest.raises(ValueError, match="row 2"):
        load_outreach_targets(path)


def test_cli_previews_exact_batch_and_discovery_count(tmp_path, capsys):
    path = tmp_path / "targets.csv"
    path.write_text(
        "recipient_email,company_name\nperson@example.com,One Co\n,Discovery Co\n",
        encoding="utf-8",
    )

    assert main(["--input", str(path)]) == 0
    output = capsys.readouterr().out
    assert '"target_count": 2' in output
    assert '"requires_email_discovery": 1' in output
