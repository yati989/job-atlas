-- Issue #94: reviewed delivery artifact only; apply separately to live Postgres.
-- The manifest freezes exact posting-version scope before agent-driven job enrichment.
ALTER TABLE decision_runs
    ADD COLUMN IF NOT EXISTS telemetry_version VARCHAR(40);
CREATE INDEX IF NOT EXISTS ix_decision_runs_telemetry_version
    ON decision_runs (telemetry_version);

CREATE TABLE decision_run_canonical_jobs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    ingestion_observation_id INTEGER NOT NULL REFERENCES decision_run_ingestion_observations(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    posting_version_id INTEGER NOT NULL REFERENCES job_posting_versions(id),
    CONSTRAINT uq_run_canonical_job UNIQUE (run_id, job_id),
    CONSTRAINT uq_run_canonical_version UNIQUE (run_id, posting_version_id)
);
CREATE INDEX ix_decision_run_canonical_jobs_run_id
    ON decision_run_canonical_jobs (run_id);
CREATE INDEX ix_decision_run_canonical_jobs_job_id
    ON decision_run_canonical_jobs (job_id);

CREATE TABLE decision_run_job_enrichment_manifest (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    posting_version_id INTEGER NOT NULL REFERENCES job_posting_versions(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    outcome VARCHAR(20),
    reason VARCHAR(100),
    failure_disposition VARCHAR(20),
    CONSTRAINT uq_run_job_enrichment_version UNIQUE (run_id, posting_version_id)
);
CREATE INDEX ix_run_job_enrichment_manifest_run_id
    ON decision_run_job_enrichment_manifest (run_id);
CREATE INDEX ix_run_job_enrichment_manifest_job_id
    ON decision_run_job_enrichment_manifest (job_id);
