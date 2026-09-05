-- Self-serve trial workspaces are real workspaces with a bounded lifetime.
ALTER TABLE workspace
    DROP CONSTRAINT IF EXISTS workspace_plan_check;

ALTER TABLE workspace
    ADD CONSTRAINT workspace_plan_check CHECK (plan IN ('TRIAL', 'STARTER', 'GROWTH'));

ALTER TABLE workspace
    ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS trial_expires_at TIMESTAMPTZ;

UPDATE workspace
SET plan = 'TRIAL',
    trial_started_at = COALESCE(trial_started_at, created_at),
    trial_expires_at = COALESCE(trial_expires_at, created_at + INTERVAL '7 days')
WHERE is_sandbox = TRUE AND plan = 'STARTER';

ALTER TABLE identity
    DROP CONSTRAINT IF EXISTS identity_provider_check;

ALTER TABLE identity
    ADD CONSTRAINT identity_provider_check CHECK (provider IN ('email', 'telegram', 'clerk'));
