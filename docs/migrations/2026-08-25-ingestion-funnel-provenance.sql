-- Issue #93: immutable, ingestion-owned observation provenance.
-- Reviewed delivery artifact only; apply separately to live Postgres.
CREATE TABLE decision_run_ingestion_observations (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    source VARCHAR(50) NOT NULL,
    external_job_id VARCHAR(255) NOT NULL,
    connector_instance INTEGER NOT NULL,
    connector_dimensions JSON NOT NULL,
    fetch_attempt INTEGER NOT NULL,
    occurrence_ordinal INTEGER NOT NULL,
    job_url VARCHAR(1000),
    apply_url VARCHAR(1000),
    CONSTRAINT uq_run_ingestion_observation UNIQUE
        (run_id, connector_instance, fetch_attempt, occurrence_ordinal)
);
CREATE INDEX ix_decision_run_ingestion_observations_run_id
    ON decision_run_ingestion_observations (run_id);
CREATE INDEX ix_decision_run_ingestion_observations_source
    ON decision_run_ingestion_observations (source);
