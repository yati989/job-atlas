-- Issue #99 reviewed artifact; apply separately to live Postgres. Never auto-run.
CREATE TABLE decision_run_draft_preparation_manifest (
    id SERIAL PRIMARY KEY, run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    company_id INTEGER NOT NULL REFERENCES companies(id), contact_id INTEGER NOT NULL REFERENCES contacts(id),
    posting_version_id INTEGER REFERENCES job_posting_versions(id), job_id INTEGER REFERENCES jobs(id),
    candidate_job_ids JSONB NOT NULL DEFAULT '[]'::jsonb, resume_kind VARCHAR(20) NOT NULL,
    pairing_reason VARCHAR(80) NOT NULL, outcome VARCHAR(20), reason VARCHAR(100),
    failure_disposition VARCHAR(20), outreach_draft_id INTEGER REFERENCES outreach_drafts(id),
    CONSTRAINT uq_run_draft_manifest_contact UNIQUE (run_id, contact_id),
    CONSTRAINT ck_run_draft_manifest_outcome CHECK (outcome IS NULL OR outcome IN ('prepared','dropped','failed')),
    CONSTRAINT ck_run_draft_manifest_disposition CHECK (failure_disposition IS NULL OR failure_disposition IN ('non_blocking','excluded'))
);
CREATE INDEX ix_run_draft_manifest_run_id ON decision_run_draft_preparation_manifest (run_id);
CREATE TABLE decision_run_gmail_draft_manifest (
    id SERIAL PRIMARY KEY, run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    draft_preparation_manifest_id INTEGER NOT NULL REFERENCES decision_run_draft_preparation_manifest(id),
    outreach_draft_id INTEGER NOT NULL REFERENCES outreach_drafts(id),
    delivery_attempt_id INTEGER NOT NULL REFERENCES outreach_delivery_attempts(id), outcome VARCHAR(20), reason VARCHAR(100),
    failure_disposition VARCHAR(20), attempt_evidence JSONB, CONSTRAINT uq_run_gmail_manifest_draft UNIQUE (run_id, draft_preparation_manifest_id),
    CONSTRAINT ck_run_gmail_manifest_outcome CHECK (outcome IS NULL OR outcome IN ('posted','dropped','failed','indeterminate')),
    CONSTRAINT ck_run_gmail_manifest_disposition CHECK (failure_disposition IS NULL OR failure_disposition IN ('non_blocking','excluded'))
);
CREATE INDEX ix_run_gmail_manifest_run_id ON decision_run_gmail_draft_manifest (run_id);
ALTER TABLE decision_runs ADD COLUMN IF NOT EXISTS final_report_path VARCHAR(1000);
ALTER TABLE decision_runs ADD COLUMN IF NOT EXISTS final_report_delivery_requested BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE decision_runs ADD COLUMN IF NOT EXISTS final_report_delivery_receipt VARCHAR(255);
ALTER TABLE decision_runs ADD COLUMN IF NOT EXISTS final_report_delivery_state VARCHAR(30);
