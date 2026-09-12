import sqlite3

import pytest
from sqlalchemy import create_engine, inspect

from app.models.orm import Base
from app.workflows.public_database import (
    PUBLIC_SCHEMA_VERSION, _migrate_v4_to_v5, _migrate_v5_to_v6,
    _migrate_v6_to_v7,
    configured_database_url, create_public_engine, database_path,
    initialize_public_database,
)


def test_database_url_prefers_public_postgres_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JOB_SEARCH_AGENT_DATABASE_URL",
        "postgresql+psycopg2://user:password@db.example/jobs",
    )
    assert configured_database_url(tmp_path).startswith("postgresql+psycopg2://")


def test_initializer_creates_a_fresh_configured_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'configured.sqlite3'}"
    result = initialize_public_database(tmp_path, database_url=url)
    assert result["dialect"] == "sqlite"
    assert result["table_count"] == len(Base.metadata.tables)


def test_existing_database_is_backed_up_and_preserved_before_upgrade(tmp_path):
    path = database_path(tmp_path)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE user_note (value TEXT NOT NULL)")
    connection.execute("INSERT INTO user_note VALUES ('keep me')")
    connection.commit()
    connection.close()

    engine = create_public_engine(tmp_path)

    backups = list(path.parent.glob(f"{path.stem}.backup-*.sqlite3"))
    assert len(backups) == 1
    with engine.connect() as current:
        assert current.exec_driver_sql("PRAGMA user_version").scalar_one() == PUBLIC_SCHEMA_VERSION
        assert current.exec_driver_sql("SELECT value FROM user_note").scalar_one() == "keep me"
    assert "guided_run_plans" in inspect(engine).get_table_names()


def test_current_database_does_not_create_repeated_backups(tmp_path):
    create_public_engine(tmp_path).dispose()
    create_public_engine(tmp_path).dispose()
    path = database_path(tmp_path)
    assert not list(path.parent.glob(f"{path.stem}.backup-*.sqlite3"))


def test_stale_existing_table_is_backed_up_but_never_marked_current(tmp_path):
    path = database_path(tmp_path)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE guided_run_plans (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="guided_run_plans missing columns"):
        create_public_engine(tmp_path)

    assert len(list(path.parent.glob(f"{path.stem}.backup-*.sqlite3"))) == 1
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        connection.close()


def test_incomplete_database_claiming_current_version_is_also_backed_up(tmp_path):
    path = database_path(tmp_path)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE guided_run_plans (id INTEGER PRIMARY KEY)")
    connection.execute(f"PRAGMA user_version={PUBLIC_SCHEMA_VERSION}")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="guided_run_plans missing columns"):
        create_public_engine(tmp_path)

    assert len(list(path.parent.glob(f"{path.stem}.backup-*.sqlite3"))) == 1


def test_v1_authorizations_gain_query_plan_without_losing_existing_data(tmp_path):
    path = database_path(tmp_path)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE profile_discovery_authorizations (
            id INTEGER PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL,
            scope_id INTEGER NOT NULL,
            plan_fingerprint VARCHAR(64) NOT NULL,
            maximum_calls INTEGER NOT NULL,
            retry_allowance INTEGER NOT NULL,
            planned_query_count INTEGER NOT NULL,
            created_at DATETIME NOT NULL
        )
    """)
    connection.execute("""
        INSERT INTO profile_discovery_authorizations
        (id, run_id, scope_id, plan_fingerprint, maximum_calls,
         retry_allowance, planned_query_count, created_at)
        VALUES (7, 'run-1', 9, 'original-plan', 12, 3, 9, '2026-09-06')
    """)
    connection.execute("""
        CREATE TABLE public_selection_scopes (
            id INTEGER PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL,
            revision INTEGER NOT NULL,
            mode VARCHAR(30) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            job_count INTEGER NOT NULL,
            company_count INTEGER NOT NULL,
            created_at DATETIME NOT NULL
        )
    """)
    connection.execute("""
        INSERT INTO public_selection_scopes
        (id, run_id, revision, mode, fingerprint, job_count, company_count, created_at)
        VALUES (9, 'run-1', 1, 'filtered', 'saved-filter', 2, 1, '2026-09-06')
    """)
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    engine = create_public_engine(tmp_path)

    backups = list(path.parent.glob(f"{path.stem}.backup-*.sqlite3"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert "query_plan" not in {
            row[1] for row in backup.execute("PRAGMA table_info(profile_discovery_authorizations)")
        }
    finally:
        backup.close()
    with engine.connect() as current:
        row = current.exec_driver_sql("""
            SELECT id, run_id, scope_id, plan_fingerprint, maximum_calls,
                   retry_allowance, planned_query_count, query_plan
            FROM profile_discovery_authorizations
        """).one()
        assert row == (7, "run-1", 9, "original-plan", 12, 3, 9, "[]")
        scope = current.exec_driver_sql("""
            SELECT id, run_id, revision, mode, fingerprint, job_count,
                   company_count, filter_snapshot
            FROM public_selection_scopes
        """).one()
        assert scope == (9, "run-1", 1, "filtered", "saved-filter", 2, 1, None)
        assert current.exec_driver_sql("PRAGMA user_version").scalar_one() == PUBLIC_SCHEMA_VERSION


def test_v2_selection_scopes_gain_filter_snapshot_without_losing_existing_data(tmp_path):
    path = database_path(tmp_path)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE public_selection_scopes (
            id INTEGER PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL,
            revision INTEGER NOT NULL,
            mode VARCHAR(30) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            job_count INTEGER NOT NULL,
            company_count INTEGER NOT NULL,
            created_at DATETIME NOT NULL
        )
    """)
    connection.execute("""
        INSERT INTO public_selection_scopes
        (id, run_id, revision, mode, fingerprint, job_count, company_count, created_at)
        VALUES (8, 'run-2', 2, 'manual', 'existing-scope', 3, 2, '2026-09-06')
    """)
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()

    engine = create_public_engine(tmp_path)

    backups = list(path.parent.glob(f"{path.stem}.backup-*.sqlite3"))
    assert len(backups) == 1
    with engine.connect() as current:
        row = current.exec_driver_sql("""
            SELECT id, run_id, revision, mode, fingerprint, job_count,
                   company_count, filter_snapshot
            FROM public_selection_scopes
        """).one()
        assert row == (8, "run-2", 2, "manual", "existing-scope", 3, 2, None)
        assert current.exec_driver_sql("PRAGMA user_version").scalar_one() == PUBLIC_SCHEMA_VERSION


def test_v3_database_gains_version_bound_public_evidence_tables(tmp_path):
    path = database_path(tmp_path)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE user_note (value TEXT NOT NULL)")
    connection.execute("INSERT INTO user_note VALUES ('preserve')")
    connection.execute("PRAGMA user_version=3")
    connection.commit()
    connection.close()

    engine = create_public_engine(tmp_path)

    assert len(list(path.parent.glob(f"{path.stem}.backup-*.sqlite3"))) == 1
    tables = set(inspect(engine).get_table_names())
    assert {
        "public_job_phase_a_evidence",
        "public_phase_b_authorizations",
        "public_phase_b_calls",
        "public_phase_b_evidence",
        "public_tailored_resumes",
        "public_workflow_stages",
    } <= tables
    with engine.connect() as current:
        assert current.exec_driver_sql("SELECT value FROM user_note").scalar_one() == "preserve"
        assert current.exec_driver_sql("PRAGMA user_version").scalar_one() == PUBLIC_SCHEMA_VERSION


def test_v4_atomic_counter_migration_backfills_existing_call_counts(tmp_path):
    """The additive v5 migration preserves already-reserved provider work."""
    path = tmp_path / "v4.sqlite3"
    connection = sqlite3.connect(path)
    for authorization, calls in (
        ("profile_discovery_authorizations", "profile_discovery_calls"),
        ("public_phase_b_authorizations", "public_phase_b_calls"),
    ):
        connection.execute(f"CREATE TABLE {authorization} (id INTEGER PRIMARY KEY)")
        connection.executemany(
            f"INSERT INTO {authorization} (id) VALUES (?)", [(1,), (2,)]
        )
        connection.execute(
            f"CREATE TABLE {calls} (id INTEGER PRIMARY KEY, authorization_id INTEGER NOT NULL)"
        )
        connection.executemany(
            f"INSERT INTO {calls} (authorization_id) VALUES (?)", [(1,), (1,), (2,)]
        )
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite:///{path}", future=True)
    _migrate_v4_to_v5(engine)

    with engine.connect() as current:
        for authorization in ("profile_discovery_authorizations", "public_phase_b_authorizations"):
            rows = current.exec_driver_sql(
                f"SELECT id, reserved_call_count FROM {authorization} ORDER BY id"
            ).all()
            assert rows == [(1, 2), (2, 1)]


def test_v5_scope_migration_marks_existing_scopes_as_final_selection(tmp_path):
    path = tmp_path / "v5.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE public_selection_scopes (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO public_selection_scopes (id) VALUES (1)")
    connection.commit(); connection.close()

    engine = create_engine(f"sqlite:///{path}", future=True)
    _migrate_v5_to_v6(engine)

    with engine.connect() as current:
        assert current.exec_driver_sql(
            "SELECT purpose FROM public_selection_scopes WHERE id=1"
        ).scalar_one() == "selection"


def test_v6_database_gains_durable_role_review_queue(tmp_path):
    path = tmp_path / "v6.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE decision_runs (id VARCHAR(64) PRIMARY KEY)")
    connection.commit(); connection.close()

    engine = create_engine(f"sqlite:///{path}", future=True)
    _migrate_v6_to_v7(engine)

    assert "public_role_review_candidates" in inspect(engine).get_table_names()
