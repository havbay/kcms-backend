-- Team membership by shareable invitation link.
--
-- There is no email infrastructure, and adding one would put deliverability on
-- the critical path of onboarding. An owner creates a link and shares it
-- through whatever channel they already use, which in Cambodia is usually
-- Telegram.

CREATE TABLE IF NOT EXISTS invitation (
    -- The token IS the credential, so only its SHA-256 is stored. A database
    -- leak must not hand over the ability to join a workspace.
    token_hash    TEXT PRIMARY KEY,
    workspace_id  TEXT        NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
    created_by    TEXT        NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    role          TEXT        NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
    -- Single use: cleared once someone joins with it.
    accepted_by   TEXT REFERENCES app_user (id) ON DELETE SET NULL,
    accepted_at   TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS invitation_workspace_idx
    ON invitation (workspace_id, created_at DESC);
