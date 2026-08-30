-- Identity is modelled as (user) + (identity per provider), not as columns on
-- a user row. Email and Telegram are two providers over one account, so a
-- client can eventually link both without a migration.

CREATE TABLE IF NOT EXISTS app_user (
    id            TEXT PRIMARY KEY,
    display_name  TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS identity (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT        NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    provider     TEXT        NOT NULL CHECK (provider IN ('email', 'telegram')),
    -- Email address, or Telegram user id. Unique per provider.
    provider_id  TEXT        NOT NULL,
    -- scrypt digest for email. NULL for telegram: Telegram proves identity
    -- with a signed payload, so there is no secret for us to store.
    secret       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, provider_id)
);

-- Only the SHA-256 of the session token is stored, so a database leak does
-- not hand over usable sessions.
CREATE TABLE IF NOT EXISTS session (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT        NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS session_user_idx ON session (user_id);
