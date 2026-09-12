-- Issue #98: reviewed artifact; apply separately to live Postgres.
-- Frozen approved company/function contact-search obligations.  No legacy
-- backfill and no automatic migration execution are intended.
CREATE TABLE decision_run_contact_enrichment_manifest (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    search_group VARCHAR(40) NOT NULL,
    outcome VARCHAR(30),
    reason VARCHAR(100),
    failure_disposition VARCHAR(20),
    reused_contact_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    new_contact_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_run_contact_manifest_function UNIQUE (run_id, company_id, search_group),
    CONSTRAINT ck_run_contact_manifest_outcome CHECK (outcome IS NULL OR outcome IN ('enriched', 'exhausted', 'no_match', 'excluded', 'failed')),
    CONSTRAINT ck_run_contact_manifest_disposition CHECK (failure_disposition IS NULL OR failure_disposition IN ('non_blocking', 'excluded'))
);
CREATE INDEX ix_run_contact_manifest_run_id ON decision_run_contact_enrichment_manifest (run_id);
CREATE INDEX ix_run_contact_manifest_company_id ON decision_run_contact_enrichment_manifest (company_id);
