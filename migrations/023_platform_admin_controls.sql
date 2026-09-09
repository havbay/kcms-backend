-- Platform-operator controls. Customer workspaces can be suspended without
-- deleting their data, and sensitive operator actions leave an audit trail.
ALTER TABLE workspace
    ADD COLUMN IF NOT EXISTS is_suspended BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS suspended_by TEXT REFERENCES app_user (id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS platform_audit_event (
    id             BIGSERIAL PRIMARY KEY,
    actor_user_id  TEXT REFERENCES app_user (id) ON DELETE SET NULL,
    action         TEXT NOT NULL CHECK (length(btrim(action)) BETWEEN 1 AND 80),
    target_type    TEXT NOT NULL CHECK (length(btrim(target_type)) BETWEEN 1 AND 80),
    target_id      TEXT NOT NULL CHECK (length(btrim(target_id)) BETWEEN 1 AND 200),
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS platform_audit_event_time_idx
    ON platform_audit_event (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS platform_audit_event_target_idx
    ON platform_audit_event (target_type, target_id, created_at DESC);
