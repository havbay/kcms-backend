-- Public pilot requests are deliberately separate from the authenticated
-- request to connect a Page. A visitor can ask for access without first
-- creating an account, while existing sandbox users keep their current flow.

CREATE TABLE IF NOT EXISTS pilot_request (
    id                TEXT PRIMARY KEY,
    name              TEXT        NOT NULL,
    organization      TEXT        NOT NULL,
    email             TEXT        NOT NULL,
    facebook_page     TEXT        NOT NULL,
    note              TEXT,
    status            TEXT        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'APPROVED', 'DECLINED')),
    decision_reason   TEXT,
    decided_by        TEXT REFERENCES app_user (id) ON DELETE SET NULL,
    decided_at        TIMESTAMPTZ,
    workspace_id      TEXT REFERENCES workspace (id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS pilot_request_status_idx
    ON pilot_request (status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS pilot_request_one_open_per_email
    ON pilot_request (LOWER(email)) WHERE status = 'PENDING';

ALTER TABLE invitation ADD COLUMN IF NOT EXISTS recipient_email TEXT;
ALTER TABLE invitation ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'TEAM'
    CHECK (purpose IN ('TEAM', 'PILOT_SETUP'));

CREATE TABLE IF NOT EXISTS notification_delivery (
    id            BIGSERIAL PRIMARY KEY,
    entity_type   TEXT        NOT NULL,
    entity_id     TEXT        NOT NULL,
    recipient     TEXT        NOT NULL,
    kind          TEXT        NOT NULL,
    status        TEXT        NOT NULL
        CHECK (status IN ('SENT', 'FAILED', 'MANUAL_REQUIRED')),
    detail        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS notification_delivery_entity_idx
    ON notification_delivery (entity_type, entity_id, created_at DESC);
