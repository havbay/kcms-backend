-- OAuth state is short-lived and stores Page-token candidates encrypted until
-- the Client confirms one Page. The raw state is never stored.

CREATE TABLE IF NOT EXISTS facebook_oauth_attempt (
    state_hash             TEXT PRIMARY KEY,
    workspace_id           TEXT NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
    user_id                TEXT NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    candidate_ciphertext   TEXT,
    expires_at             TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS facebook_oauth_attempt_expiry_idx
    ON facebook_oauth_attempt (expires_at);
