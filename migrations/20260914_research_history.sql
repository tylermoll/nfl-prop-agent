-- Apply once before deploying the historical research endpoints.
-- Existing settlements remain immutable; their price is still available on
-- the immutable observation joined by observation_id.
ALTER TABLE shadow_settlements ADD COLUMN IF NOT EXISTS american_odds INTEGER;
ALTER TABLE shadow_settlements ADD COLUMN IF NOT EXISTS line DOUBLE PRECISION;
ALTER TABLE shadow_settlements ADD COLUMN IF NOT EXISTS side VARCHAR;

CREATE TABLE IF NOT EXISTS research_selection_journal (
    journal_id VARCHAR(36) PRIMARY KEY,
    observation_id VARCHAR(36) NOT NULL,
    selected BOOLEAN NOT NULL,
    intended_stake DOUBLE PRECISION,
    selected_at_utc TIMESTAMPTZ NOT NULL,
    reason_codes JSON NOT NULL,
    note VARCHAR
);
CREATE INDEX IF NOT EXISTS ix_research_selection_journal_observation_id
    ON research_selection_journal (observation_id);
