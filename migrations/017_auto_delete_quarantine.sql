-- Quarantine window for HARMFUL auto-removal. Today a HARMFUL verdict is
-- deleted from Facebook the instant it is classified (015_delete_action.sql).
-- This adds a configurable delay: the comment is hidden immediately
-- (reversible), and only actually deleted once the delay expires with no
-- human intervention. Default 0 keeps every existing workspace on today's
-- instant-delete behaviour until an owner opts in.

ALTER TABLE workspace
    ADD COLUMN IF NOT EXISTS auto_delete_delay_minutes INT NOT NULL DEFAULT 0
        CHECK (auto_delete_delay_minutes IN (0, 5, 30, 60, 720, 1440));

-- One row per comment currently quarantined: HIDE has been applied, DELETE
-- is pending. Deleting the row *is* cancellation, so cancelling never needs
-- its own state machine.
CREATE TABLE IF NOT EXISTS scheduled_deletion (
    comment_id     TEXT PRIMARY KEY REFERENCES comment_content (comment_id) ON DELETE CASCADE,
    workspace_id   TEXT        NOT NULL,
    page_id        TEXT        NOT NULL,
    scheduled_for  TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS scheduled_deletion_due_idx ON scheduled_deletion (scheduled_for);
