"""Shared XLSX read-model shaping; keeps renderers free of live-query rules."""
from datetime import datetime
import re


_EXCEL_ILLEGAL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _excel_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return _EXCEL_ILLEGAL_CHARACTERS.sub("", value)
    if isinstance(value, dict):
        return {key: _excel_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_excel_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_excel_value(item) for item in value)
    return value


def excel_rows(rows: list[dict]) -> list[dict]:
    """Shape values for XLSX without changing the underlying read model."""
    return [{key: _excel_value(value) for key, value in row.items()} for row in rows]
