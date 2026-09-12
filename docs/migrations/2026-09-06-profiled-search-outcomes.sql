-- Durable profile-based relevance decisions for issue #101.
-- Back up the database before applying. Existing tables and rows are unchanged.
CREATE TABLE profiled_search_outcomes (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    source VARCHAR(50) NOT NULL,
    external_job_id VARCHAR(255) NOT NULL,
    outcome VARCHAR(20) NOT NULL,
    first_failed_axis VARCHAR(30),
    reason TEXT,
    profile_fingerprint VARCHAR(64) NOT NULL,
    job_url TEXT,
    apply_url TEXT,
    evidence JSON NOT NULL,
    job_id INTEGER REFERENCES jobs(id),
    posting_version_id INTEGER REFERENCES job_posting_versions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_profiled_search_outcome_run_job
        UNIQUE (run_id, source, external_job_id),
    CONSTRAINT ck_profiled_search_outcome
        CHECK (outcome IN ('kept', 'rejected', 'needs_review'))
);

CREATE INDEX ix_profiled_search_outcomes_run_id
    ON profiled_search_outcomes (run_id);
CREATE INDEX ix_profiled_search_outcomes_source
    ON profiled_search_outcomes (source);
CREATE INDEX ix_profiled_search_outcomes_outcome
    ON profiled_search_outcomes (outcome);
CREATE INDEX ix_profiled_search_outcomes_profile_fingerprint
    ON profiled_search_outcomes (profile_fingerprint);
CREATE INDEX ix_profiled_search_outcomes_job_id
    ON profiled_search_outcomes (job_id);
CREATE INDEX ix_profiled_search_outcomes_posting_version_id
    ON profiled_search_outcomes (posting_version_id);
