-- Issue #95: reviewed delivery artifact only; apply separately to live Postgres.
-- Exact Decision Run company scope, enrichment outcomes, Phase B evidence, and
-- final company placement.  This never backfills or infers legacy telemetry.
CREATE TABLE decision_run_company_funnel_manifest (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    eligible_job_count INTEGER NOT NULL CHECK (eligible_job_count > 0),
    phase_a_outcome VARCHAR(20),
    phase_a_reason VARCHAR(100),
    phase_a_failure_disposition VARCHAR(20),
    phase_b_outcome VARCHAR(20),
    phase_b_reason VARCHAR(100),
    phase_b_failure_disposition VARCHAR(20),
    phase_b_evidence_outcome VARCHAR(30),
    terminal_outcome VARCHAR(20),
    terminal_reason VARCHAR(100),
    CONSTRAINT uq_run_company_funnel_company UNIQUE (run_id, company_id),
    CONSTRAINT ck_run_company_phase_a_outcome
        CHECK (phase_a_outcome IS NULL OR phase_a_outcome IN ('enriched', 'failed')),
    CONSTRAINT ck_run_company_phase_b_outcome
        CHECK (phase_b_outcome IS NULL OR phase_b_outcome IN ('enriched', 'failed')),
    CONSTRAINT ck_run_company_phase_b_evidence
        CHECK (phase_b_evidence_outcome IS NULL OR phase_b_evidence_outcome IN ('found', 'confirmed_missing', 'source_error')),
    CONSTRAINT ck_run_company_terminal_outcome
        CHECK (terminal_outcome IS NULL OR terminal_outcome IN ('group_1', 'group_2', 'group_3', 'group_4', 'ungroupable'))
);
CREATE INDEX ix_run_company_funnel_manifest_run_id
    ON decision_run_company_funnel_manifest (run_id);
CREATE INDEX ix_run_company_funnel_manifest_company_id
    ON decision_run_company_funnel_manifest (company_id);

CREATE TABLE decision_run_company_phase_b_evidence (
    id SERIAL PRIMARY KEY,
    manifest_id INTEGER NOT NULL REFERENCES decision_run_company_funnel_manifest(id),
    source VARCHAR(30) NOT NULL,
    outcome VARCHAR(30) NOT NULL CHECK (outcome IN ('found', 'confirmed_missing', 'source_error')),
    reason VARCHAR(100),
    failure_disposition VARCHAR(20),
    evidence_url VARCHAR(1000),
    source_status VARCHAR(100),
    CONSTRAINT uq_run_company_phase_b_source UNIQUE (manifest_id, source)
);
CREATE INDEX ix_run_company_phase_b_evidence_manifest_id
    ON decision_run_company_phase_b_evidence (manifest_id);
