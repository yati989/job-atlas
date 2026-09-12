from pathlib import Path

import pytest

from app.decision_runs.google_sheet_approval import (
    GOOGLE_SHEET_MIME_TYPE,
    create_approval_spreadsheet,
    export_approval_spreadsheet,
)


class _Request:
    def __init__(self, result=None):
        self.result = result

    def execute(self):
        return self.result


class _Files:
    def __init__(self, create_result=None):
        self.create_result = create_result
        self.created = None
        self.exported = None

    def create(self, **kwargs):
        self.created = kwargs
        return _Request(self.create_result)

    def export_media(self, **kwargs):
        self.exported = kwargs
        return object()


class _Service:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def test_create_approval_spreadsheet_converts_xlsx_and_returns_observed_link(tmp_path):
    source = tmp_path / "approval.xlsx"
    source.write_bytes(b"PK workbook")
    files = _Files({
        "id": "sheet-42",
        "mimeType": GOOGLE_SHEET_MIME_TYPE,
        "webViewLink": "https://docs.google.com/spreadsheets/d/sheet-42/edit",
    })

    result = create_approval_spreadsheet(_Service(files), source, title="Approval")

    assert result == {
        "spreadsheet_id": "sheet-42",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/sheet-42/edit",
    }
    assert files.created["body"] == {
        "name": "Approval", "mimeType": GOOGLE_SHEET_MIME_TYPE,
    }


def test_create_approval_spreadsheet_rejects_non_native_result(tmp_path):
    source = tmp_path / "approval.xlsx"
    source.write_bytes(b"PK workbook")
    files = _Files({"id": "file-42", "mimeType": "application/octet-stream"})

    with pytest.raises(RuntimeError, match="native Sheet"):
        create_approval_spreadsheet(_Service(files), source, title="Approval")


def test_export_approval_spreadsheet_uses_exact_recorded_id(tmp_path, monkeypatch):
    files = _Files()

    class Downloader:
        def __init__(self, buffer, request):
            self.buffer = buffer

        def next_chunk(self):
            self.buffer.write(b"PK reviewed")
            return None, True

    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", Downloader)
    destination = tmp_path / "reviewed.xlsx"

    result = export_approval_spreadsheet(_Service(files), "sheet-42", destination)

    assert result == destination
    assert destination.read_bytes() == b"PK reviewed"
    assert files.exported["fileId"] == "sheet-42"
