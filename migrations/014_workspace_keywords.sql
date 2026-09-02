-- Keywords belong to a workspace, not to the platform.
--
-- Until now every workspace was matched against one shared CSV on the server,
-- so a word one client added changed how every other client's comments were
-- classified. A bank and a news page do not agree on what is harmful, and one
-- of them adding a term must never silently re-classify the other's queue.
--
-- The JSON file that ships with the backend stays, but only as the starting
-- set. Anything a workspace adds lives here, scoped to that workspace.

CREATE TABLE IF NOT EXISTS workspace_keyword (
    id           BIGSERIAL PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace (id) ON DELETE CASCADE,
    keyword      TEXT NOT NULL CHECK (length(btrim(keyword)) BETWEEN 1 AND 100),
    -- Only the two severities that carry an outcome. There is no "safe"
    -- keyword: a word can surface a comment, it can never clear one.
    severity     TEXT NOT NULL CHECK (severity IN ('HARMFUL', 'OFFENSIVE')),
    note         TEXT,
    created_by   TEXT REFERENCES app_user (id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Case- and whitespace-insensitive, matching how the matcher compares them,
-- so the same word cannot be added twice in two spellings of the same case.
CREATE UNIQUE INDEX IF NOT EXISTS workspace_keyword_unique_idx
    ON workspace_keyword (workspace_id, lower(btrim(keyword)));

CREATE INDEX IF NOT EXISTS workspace_keyword_workspace_idx
    ON workspace_keyword (workspace_id);
