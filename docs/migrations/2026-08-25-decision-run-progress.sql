-- Issue #92: reviewed manually before applying to live Postgres.
-- `scripts.init_db` only calls create_all and will not apply this alteration.
CREATE TABLE decision_run_stages (
  id SERIAL PRIMARY KEY, run_id VARCHAR(64) NOT NULL REFERENCES decision_runs(id),
  name VARCHAR(80) NOT NULL, status VARCHAR(30) NOT NULL, expected_count INTEGER NOT NULL,
  processed_count INTEGER NOT NULL DEFAULT 0, advanced_count INTEGER NOT NULL DEFAULT 0,
  dropped_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0,
  pending_count INTEGER NOT NULL DEFAULT 0, reason_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
  reason TEXT, started_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ, heartbeat_at TIMESTAMPTZ,
  attempt_number INTEGER NOT NULL DEFAULT 0, CONSTRAINT uq_decision_run_stage UNIQUE (run_id, name),
  CONSTRAINT ck_decision_run_stage_status CHECK (status IN ('pending','running','waiting_for_approval','completed','completed_with_errors','failed','interrupted','skipped')),
  CONSTRAINT ck_decision_run_stage_counts CHECK (expected_count = advanced_count + dropped_count + failed_count + pending_count AND processed_count = advanced_count + dropped_count + failed_count)
);
CREATE INDEX ix_decision_run_stages_run_id ON decision_run_stages(run_id);
CREATE INDEX ix_decision_run_stages_status ON decision_run_stages(status);
CREATE TABLE decision_run_stage_attempts (
  id SERIAL PRIMARY KEY, stage_id INTEGER NOT NULL REFERENCES decision_run_stages(id),
  attempt_number INTEGER NOT NULL, status VARCHAR(30) NOT NULL, process_id INTEGER,
  expected_count INTEGER NOT NULL, processed_count INTEGER NOT NULL DEFAULT 0, reason TEXT,
  advanced_count INTEGER NOT NULL DEFAULT 0, dropped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0, pending_count INTEGER NOT NULL DEFAULT 0,
  reason_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at TIMESTAMPTZ NOT NULL, heartbeat_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
  CONSTRAINT uq_stage_attempt_number UNIQUE (stage_id, attempt_number),
  CONSTRAINT ck_decision_run_stage_attempt_status CHECK (status IN ('running','waiting_for_approval','completed','completed_with_errors','failed','interrupted','skipped')),
  CONSTRAINT ck_decision_run_stage_attempt_counts CHECK (expected_count = advanced_count + dropped_count + failed_count + pending_count AND processed_count = advanced_count + dropped_count + failed_count)
);
CREATE INDEX ix_decision_run_stage_attempts_stage_id ON decision_run_stage_attempts(stage_id);
CREATE TABLE decision_run_stage_transitions (
  id SERIAL PRIMARY KEY, stage_id INTEGER NOT NULL REFERENCES decision_run_stages(id),
  attempt_id INTEGER REFERENCES decision_run_stage_attempts(id), from_status VARCHAR(30),
  to_status VARCHAR(30) NOT NULL, reason TEXT, occurred_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_decision_run_stage_transitions_stage_id ON decision_run_stage_transitions(stage_id);
CREATE TABLE decision_run_stage_records (
  id SERIAL PRIMARY KEY, stage_id INTEGER NOT NULL REFERENCES decision_run_stages(id),
  attempt_id INTEGER NOT NULL REFERENCES decision_run_stage_attempts(id),
  record_type VARCHAR(40) NOT NULL, record_id VARCHAR(255) NOT NULL,
  outcome VARCHAR(20) NOT NULL, reason VARCHAR(100),
  CONSTRAINT uq_stage_attempt_record UNIQUE (attempt_id, record_type, record_id),
  CONSTRAINT ck_decision_run_stage_record_outcome CHECK (outcome IN ('advanced','dropped','failed'))
);
CREATE INDEX ix_decision_run_stage_records_stage_id ON decision_run_stage_records(stage_id);
CREATE INDEX ix_decision_run_stage_records_attempt_id ON decision_run_stage_records(attempt_id);

-- Issue #96: reviewed manually before applying to live Postgres.  These
-- fields extend the #92 foundation without changing its existing counts.
ALTER TABLE decision_run_stages
  ADD COLUMN IF NOT EXISTS waiting_started_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS waiting_seconds INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS selection_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS failed_record_dispositions JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE decision_run_stage_attempts
  ADD COLUMN IF NOT EXISTS failed_record_dispositions JSONB NOT NULL DEFAULT '[]'::jsonb;
