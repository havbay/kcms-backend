-- Multi-tenancy. Until now every account shared one demo Page, so "your
-- workspace" was a claim the product could not keep.
--
-- Each sign-up gets its own sandbox workspace with its own copy of the sample
-- comments, so two people exploring at once never see each other's decisions.

CREATE TABLE IF NOT EXISTS workspace (
    id          TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    -- Sandbox workspaces hold sample data. Connecting a real Facebook Page is
    -- gated on approval, so this flag is what the gate reads.
    is_sandbox  BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS membership (
    workspace_id  TEXT        NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
    user_id       TEXT        NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    role          TEXT        NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'member')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, user_id)
);

CREATE INDEX IF NOT EXISTS membership_user_idx ON membership (user_id);

-- Comments belong to a workspace. Nullable for the pre-existing demo rows,
-- which no longer belong to anyone and are excluded from every query.
ALTER TABLE comment_content ADD COLUMN IF NOT EXISTS workspace_id TEXT;

CREATE INDEX IF NOT EXISTS comment_workspace_idx ON comment_content (workspace_id);
