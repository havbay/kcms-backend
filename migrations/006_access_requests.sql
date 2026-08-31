-- A sandbox workspace asks to connect a real Facebook Page; a Platform
-- Administrator decides. Approval lifts workspace.is_sandbox. It does not
-- itself connect a Page: Meta OAuth remains a later slice.

-- Platform Administration is a platform-level role. membership.role scopes a
-- user to one workspace and is the wrong axis for it. This column is set from
-- an environment allowlist at sign-in and is never settable through the API,
-- so the role cannot be self-assigned.
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS access_request (
    id                TEXT PRIMARY KEY,
    workspace_id      TEXT        NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
    requested_by      TEXT        NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    page_name         TEXT        NOT NULL,
    monthly_comments  TEXT        NOT NULL
        CHECK (monthly_comments IN ('UNDER_1K', '1K_TO_10K', '10K_TO_50K', 'OVER_50K')),
    team_size         TEXT        NOT NULL
        CHECK (team_size IN ('JUST_ME', '2_TO_5', '6_TO_20', 'OVER_20')),
    note              TEXT,
    status            TEXT        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'APPROVED', 'DECLINED')),
    decision_reason   TEXT,
    decided_by        TEXT REFERENCES app_user (id) ON DELETE SET NULL,
    decided_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS access_request_workspace_idx
    ON access_request (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS access_request_status_idx ON access_request (status, created_at DESC);

-- At most one open request per workspace, enforced by the database rather than
-- by an application check a concurrent submit could race past.
CREATE UNIQUE INDEX IF NOT EXISTS access_request_one_open_per_workspace
    ON access_request (workspace_id) WHERE status = 'PENDING';
