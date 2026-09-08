-- Workspace-scoped, admin-authored automated reply rules.
-- Live provider sending remains an adapter concern and is disabled by default.

ALTER TABLE workspace
    ADD COLUMN IF NOT EXISTS auto_reply_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS auto_reply_dry_run BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS auto_reply_rule (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
    name          TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    keywords      TEXT[] NOT NULL,
    reply_body    TEXT NOT NULL CHECK (length(btrim(reply_body)) BETWEEN 1 AND 1000),
    on_comments   BOOLEAN NOT NULL DEFAULT TRUE,
    on_messages   BOOLEAN NOT NULL DEFAULT FALSE,
    position      INTEGER NOT NULL CHECK (position >= 0),
    enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    created_by    TEXT REFERENCES app_user (id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (cardinality(keywords) BETWEEN 1 AND 50),
    CHECK (on_comments OR on_messages)
);

CREATE INDEX IF NOT EXISTS auto_reply_rule_workspace_order_idx
    ON auto_reply_rule (workspace_id, position, id);

CREATE TABLE IF NOT EXISTS auto_reply_event (
    id                   BIGSERIAL PRIMARY KEY,
    workspace_id         TEXT NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
    rule_id              TEXT REFERENCES auto_reply_rule (id) ON DELETE SET NULL,
    provider_event_id    TEXT NOT NULL,
    channel              TEXT NOT NULL CHECK (channel IN ('comments', 'messages')),
    decision             TEXT NOT NULL CHECK (decision IN ('would_reply', 'replied', 'skipped')),
    reason               TEXT NOT NULL,
    reply_body           TEXT,
    provider_applied     BOOLEAN NOT NULL DEFAULT FALSE,
    occurred_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, channel, provider_event_id)
);

CREATE INDEX IF NOT EXISTS auto_reply_event_workspace_time_idx
    ON auto_reply_event (workspace_id, occurred_at DESC, id DESC);
