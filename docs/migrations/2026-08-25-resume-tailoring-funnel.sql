-- Issue #97: reviewed delivery artifact only; apply separately to live Postgres.
-- Immutable approved posting-version denominator and terminal tailoring state.
CREATE TABLE decision_run_resume_tailoring_manifest (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    posting_version_id INTEGER NOT NULL REFERENCES job_posting_versions(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    outcome VARCHAR(20),
    reason VARCHAR(100),
    failure_disposition VARCHAR(20),
    CONSTRAINT uq_run_resume_tailoring_version UNIQUE (run_id, posting_version_id)
);
CREATE INDEX ix_run_resume_tailoring_manifest_run_id
    ON decision_run_resume_tailoring_manifest (run_id);
CREATE INDEX ix_run_resume_tailoring_manifest_job_id
    ON decision_run_resume_tailoring_manifest (job_id);
