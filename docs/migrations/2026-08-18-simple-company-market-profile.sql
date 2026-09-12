-- Add the nullable V1 company market-profile fields to an existing Postgres DB.
ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS glassdoor_employer_id VARCHAR(50),
    ADD COLUMN IF NOT EXISTS glassdoor_overall_rating DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS glassdoor_wlb_rating DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS glassdoor_review_count INTEGER,
    ADD COLUMN IF NOT EXISTS ownership_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS revenue VARCHAR(255),
    ADD COLUMN IF NOT EXISTS ambitionbox_overall_rating DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS ambitionbox_wlb_rating DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS ambitionbox_estimated_salary_lpa DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS levels_fyi_estimated_salary_lpa DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS overall_rating DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS wlb_rating DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS estimated_salary_lpa DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS market_profile_evidence JSON,
    ADD COLUMN IF NOT EXISTS market_profile_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS market_profile_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_companies_market_profile_status
    ON companies (market_profile_status);
