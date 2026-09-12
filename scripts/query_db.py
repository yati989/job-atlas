"""
Ad-hoc DB query helper for job_agent's Postgres tables (jobs, companies,
job_skills). Usage:

    python -m scripts.query_db jobs
    python -m scripts.query_db companies --where "enrichment_status='done'" --limit 20
    python -m scripts.query_db job_skills --where "job_id=346"
    python -m scripts.query_db --sql "SELECT industry, count(*) FROM companies GROUP BY industry ORDER BY 2 DESC"

Prints results as a readable table. Falls back to a plain print per row if
a column value is too wide for a table cell.
"""
import argparse
import sys

from sqlalchemy import text

from app.db.session import engine


def run_query(sql: str):
    with engine.begin() as conn:
        result = conn.execute(text(sql))
        columns = list(result.keys()) if result.returns_rows else []
        rows = result.fetchall() if result.returns_rows else []
    return columns, rows


def print_table(columns, rows, max_col_width: int = 60):
    if not rows:
        print("(no rows)")
        return

    def fmt(v):
        s = "" if v is None else str(v)
        return (s[: max_col_width - 3] + "...") if len(s) > max_col_width else s

    widths = [len(c) for c in columns]
    str_rows = []
    for row in rows:
        str_row = [fmt(v) for v in row]
        str_rows.append(str_row)
        widths = [max(w, len(s)) for w, s in zip(widths, str_row)]

    header = " | ".join(c.ljust(w) for c, w in zip(columns, widths))
    print(header)
    print("-+-".join("-" * w for w in widths))
    for str_row in str_rows:
        print(" | ".join(s.ljust(w) for s, w in zip(str_row, widths)))
    print(f"\n({len(rows)} row(s))")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "table",
        nargs="?",
        choices=["jobs", "companies", "job_skills"],
        help="Table to query (ignored if --sql is given)",
    )
    parser.add_argument("--columns", default="*", help="Comma-separated columns (default: all)")
    parser.add_argument("--where", default=None, help="Raw SQL WHERE clause (no 'WHERE' keyword)")
    parser.add_argument("--order-by", default="id", help="ORDER BY column (default: id)")
    parser.add_argument("--limit", type=int, default=50, help="Row limit (default: 50)")
    parser.add_argument("--sql", default=None, help="Run a fully custom SQL query instead")
    parser.add_argument("--max-col-width", type=int, default=60, help="Truncate wide cell values")
    args = parser.parse_args()

    if args.sql:
        sql = args.sql
    else:
        if not args.table:
            parser.error("either a table name or --sql is required")
        sql = f"SELECT {args.columns} FROM {args.table}"
        if args.where:
            sql += f" WHERE {args.where}"
        sql += f" ORDER BY {args.order_by} LIMIT {args.limit}"

    columns, rows = run_query(sql)
    print_table(columns, rows, max_col_width=args.max_col_width)


if __name__ == "__main__":
    sys.exit(main())
