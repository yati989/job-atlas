"""
Export database tables to an Excel workbook, one sheet per table. Read-only —
no DB writes. Usage:

    python -m scripts.export_db_excel [output_path.xlsx]

Exports companies, contacts, and job_skills by default (jobs has its own
richer, company-joined export in scripts/export_jobs_excel.py). Datetimes are
rendered as naive strings, JSON/dict columns as compact JSON, and any single
cell is truncated to Excel's 32,767-character limit.
"""
import json
import sys
from datetime import datetime

import pandas as pd
from sqlalchemy import inspect, select

from app.db.session import SessionLocal
from app.models.orm import Company, Contact, JobSkill

EXCEL_CELL_LIMIT = 32767

# Sheet name -> ORM model. (jobs intentionally omitted — export_jobs_excel.py
# produces a better company-joined version.)
TABLES = {
    "companies": Company,
    "contacts": Contact,
    "job_skills": JobSkill,
}


def _cell(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and len(value) > EXCEL_CELL_LIMIT:
        return value[: EXCEL_CELL_LIMIT - 1] + "…"
    return value


def _rows_for(session, model) -> pd.DataFrame:
    columns = [c.key for c in inspect(model).mapper.column_attrs]
    records = session.execute(select(model)).scalars().all()
    return pd.DataFrame(
        [{col: _cell(getattr(r, col)) for col in columns} for r in records],
        columns=columns,
    )


def main() -> None:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "db_tables.xlsx"

    session = SessionLocal()
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for sheet, model in TABLES.items():
            df = _rows_for(session, model)
            df.to_excel(writer, sheet_name=sheet, index=False)
            print(f"  {sheet}: {len(df)} rows")

    print(f"Wrote {len(TABLES)} sheets to {out_path}")


if __name__ == "__main__":
    main()
