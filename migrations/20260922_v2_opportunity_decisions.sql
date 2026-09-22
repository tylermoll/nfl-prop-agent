-- Phase 1 V2 prospective decisions. Apply before enabling decision writes.
-- This migration intentionally performs no INSERT/UPDATE and no V1 backfill.
CREATE TABLE IF NOT EXISTS shadow_opportunity_decisions (
    decision_id VARCHAR(36) PRIMARY KEY,
    opportunity_id VARCHAR(96) NOT NULL,
    exposure_id VARCHAR(96) NOT NULL,
    over_observation_id VARCHAR(36) NOT NULL REFERENCES shadow_observations(observation_id),
    under_observation_id VARCHAR(36) NOT NULL REFERENCES shadow_observations(observation_id),
    selected_observation_id VARCHAR(36) REFERENCES shadow_observations(observation_id),
    action VARCHAR(16) NOT NULL,
    intended_stake DOUBLE PRECISION,
    decided_at_utc TIMESTAMPTZ NOT NULL,
    reason_codes JSON NOT NULL,
    policy_name VARCHAR NOT NULL,
    policy_version VARCHAR NOT NULL,
    input_schema_version VARCHAR NOT NULL,
    input_digest VARCHAR(64) NOT NULL,
    CONSTRAINT uq_shadow_opportunity_policy UNIQUE (opportunity_id, policy_name, policy_version),
    CONSTRAINT ck_shadow_decision_action CHECK (action IN ('SELECT_OVER','SELECT_UNDER','PASS')),
    CONSTRAINT ck_shadow_decision_distinct_sides CHECK (over_observation_id <> under_observation_id),
    CONSTRAINT ck_shadow_decision_selected_member CHECK (
        selected_observation_id IS NULL OR
        selected_observation_id IN (over_observation_id, under_observation_id)),
    CONSTRAINT ck_shadow_decision_action_payload CHECK (
        (action = 'PASS' AND selected_observation_id IS NULL AND intended_stake IS NULL) OR
        (action = 'SELECT_OVER' AND selected_observation_id = over_observation_id AND intended_stake = 10.0) OR
        (action = 'SELECT_UNDER' AND selected_observation_id = under_observation_id AND intended_stake = 10.0))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_selected_exposure_policy
    ON shadow_opportunity_decisions (exposure_id, policy_name, policy_version)
    WHERE action <> 'PASS';

CREATE INDEX IF NOT EXISTS ix_shadow_decisions_selected_observation
    ON shadow_opportunity_decisions (selected_observation_id);
