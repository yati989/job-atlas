"""Private local database location for the independent public workflow."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.models.orm import Base


PUBLIC_SCHEMA_VERSION = 7


def _schema_gaps(engine) -> list[str]:
    """Find tables/columns that ``create_all`` cannot repair in place."""
    inspector = inspect(engine)
    available_tables = set(inspector.get_table_names())
    gaps: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in available_tables:
            gaps.append(f"missing table {table.name}")
            continue
        available_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [column.name for column in table.columns if column.name not in available_columns]
        if missing:
            gaps.append(f"{table.name} missing columns: {', '.join(missing)}")
    return gaps


def _migrate_v1_to_v2(engine) -> None:
    """Add the immutable profile-query snapshot introduced in schema v2.

    SQLite's ``create_all`` intentionally leaves existing tables untouched.
    This is therefore an explicit, additive migration: existing authorization
    rows retain every previous value and receive an empty query plan rather
    than being rebuilt or discarded.
    """
    inspector = inspect(engine)
    table_name = "profile_discovery_authorizations"
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if "query_plan" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE profile_discovery_authorizations "
                "ADD COLUMN query_plan JSON NOT NULL DEFAULT '[]'"
            )


def _migrate_v2_to_v3(engine) -> None:
    """Add the optional saved-filter snapshot introduced in schema v3."""
    inspector = inspect(engine)
    table_name = "public_selection_scopes"
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if "filter_snapshot" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE public_selection_scopes "
                "ADD COLUMN filter_snapshot JSON"
            )


def _migrate_v3_to_v4(engine) -> None:
    """Create version-bound public evidence and lifecycle tables."""
    for name in (
        "public_job_phase_a_evidence",
        "public_phase_b_authorizations",
        "public_phase_b_calls",
        "public_phase_b_evidence",
        "public_tailored_resumes",
        "public_workflow_stages",
    ):
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)


def _migrate_v4_to_v5(engine) -> None:
    """Add atomic reservation counters and initialize them from durable calls."""
    migrations = (
        ("profile_discovery_authorizations", "profile_discovery_calls"),
        ("public_phase_b_authorizations", "public_phase_b_calls"),
    )
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        for authorization_table, call_table in migrations:
            if authorization_table not in tables or call_table not in tables:
                continue
            columns = {
                column["name"]
                for column in inspect(engine).get_columns(authorization_table)
            }
            if "reserved_call_count" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {authorization_table} "
                    "ADD COLUMN reserved_call_count INTEGER NOT NULL DEFAULT 0"
                )
            connection.exec_driver_sql(
                f"UPDATE {authorization_table} AS authorization "
                "SET reserved_call_count = ("
                f"SELECT COUNT(*) FROM {call_table} AS call "
                "WHERE call.authorization_id = authorization.id"
                ")"
            )


def _migrate_v5_to_v6(engine) -> None:
    """Mark legacy scopes as final selections before adding research scopes."""
    table_name = "public_selection_scopes"
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if "purpose" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE public_selection_scopes "
                "ADD COLUMN purpose VARCHAR(30) NOT NULL DEFAULT 'selection'"
            )


def _migrate_v6_to_v7(engine) -> None:
    """Create the durable agent-owned role-review queue."""
    Base.metadata.tables["public_role_review_candidates"].create(
        bind=engine, checkfirst=True,
    )


def _apply_schema_migrations(engine, version: int) -> None:
    """Apply only known, additive public-SQLite migrations in order."""
    if version < 1:
        return
    if version <= 1:
        _migrate_v1_to_v2(engine)
    if version <= 2:
        _migrate_v2_to_v3(engine)
    if version <= 3:
        _migrate_v3_to_v4(engine)
    if version <= 4:
        _migrate_v4_to_v5(engine)
    if version <= 5:
        _migrate_v5_to_v6(engine)
    if version <= 6:
        _migrate_v6_to_v7(engine)


def database_path(private_root: Path) -> Path:
    root = Path(private_root).expanduser() / "data"
    current = root / "job-atlas.sqlite3"
    legacy = root / "job-search-agent.sqlite3"
    return legacy if legacy.exists() and not current.exists() else current


def configured_database_url(
    private_root: Path, explicit_database_url: str | None = None,
) -> str:
    """Resolve the public database without coupling callers to one adapter."""
    return (
        explicit_database_url
        or os.getenv("JOB_ATLAS_DATABASE_URL")
        or os.getenv("JOB_SEARCH_AGENT_DATABASE_URL")
        or f"sqlite:///{database_path(private_root)}"
    )


def _create_postgres_engine(database_url: str):
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    Base.metadata.create_all(engine)
    gaps = _schema_gaps(engine)
    if gaps:
        engine.dispose()
        raise RuntimeError(
            "PostgreSQL schema requires an explicit migration; " + "; ".join(gaps)
        )
    return engine


def create_public_engine(
    private_root: Path, database_url: str | None = None,
):
    resolved_url = configured_database_url(private_root, database_url)
    if resolved_url.startswith(("postgresql://", "postgresql+")):
        return _create_postgres_engine(resolved_url)
    if not resolved_url.startswith("sqlite:///"):
        raise ValueError(
            "JOB_ATLAS_DATABASE_URL must use PostgreSQL or SQLite"
        )
    path = Path(make_url(resolved_url).database).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    has_tables = False
    if existed:
        connection = sqlite3.connect(path, timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            has_tables = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone() is not None
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
        finally:
            connection.close()
        if current_version > PUBLIC_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current_version} is newer than supported {PUBLIC_SCHEMA_VERSION}"
            )
    else:
        current_version = 0
    engine = create_engine(
        f"sqlite:///{path}", future=True, connect_args={"timeout": 30},
    )

    preexisting_gaps = _schema_gaps(engine) if has_tables else []
    if has_tables and (current_version < PUBLIC_SCHEMA_VERSION or preexisting_gaps):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.parent / f"{path.stem}.backup-{timestamp}.sqlite3"
        shutil.copy2(path, backup)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    _apply_schema_migrations(engine, current_version)
    gaps = _schema_gaps(engine)
    if gaps:
        engine.dispose()
        raise RuntimeError(
            "database schema upgrade requires a manual migration; "
            + "; ".join(gaps)
        )
    if current_version < PUBLIC_SCHEMA_VERSION:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"PRAGMA user_version={PUBLIC_SCHEMA_VERSION}")
    return engine


def initialize_public_database(
    private_root: Path, database_url: str | None = None,
) -> dict[str, object]:
    """Create every table in the configured database and report the adapter."""
    engine = create_public_engine(private_root, database_url=database_url)
    try:
        return {
            "dialect": engine.dialect.name,
            "table_count": len(inspect(engine).get_table_names()),
        }
    finally:
        engine.dispose()


@contextmanager
def public_session(private_root: Path, database_url: str | None = None):
    session: Session = sessionmaker(
        bind=create_public_engine(private_root, database_url=database_url), future=True,
    )()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
