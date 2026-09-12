-- Apply explicitly to live Postgres before a --record-dropped-observations run.
-- SQLAlchemy create_all does not alter an existing database schema.
CREATE TABLE IF NOT EXISTS relevance_audit_runs (
    id VARCHAR(64) PRIMARY KEY,
    since_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    command VARCHAR(255) NOT NULL,
    state VARCHAR(30) NOT NULL,
    instance_count INTEGER NOT NULL,
    drop_count INTEGER NOT NULL DEFAULT 0,
    failure_detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_relevance_audit_runs_state ON relevance_audit_runs(state);

CREATE TABLE IF NOT EXISTS relevance_audit_observations (
    id SERIAL PRIMARY KEY,
    audit_run_id VARCHAR(64) NOT NULL REFERENCES relevance_audit_runs(id),
    source VARCHAR(50) NOT NULL,
    external_job_id VARCHAR(255) NOT NULL,
    first_failed_axis VARCHAR(30) NOT NULL,
    location_reason VARCHAR(30),
    dimensions JSON NOT NULL,
    job_snapshot JSON NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_relevance_audit_observations_run ON relevance_audit_observations(audit_run_id);
CREATE INDEX IF NOT EXISTS ix_relevance_audit_observations_source ON relevance_audit_observations(source);
CREATE INDEX IF NOT EXISTS ix_relevance_audit_observations_external_id ON relevance_audit_observations(external_job_id);
CREATE INDEX IF NOT EXISTS ix_relevance_audit_observations_axis ON relevance_audit_observations(first_failed_axis);
