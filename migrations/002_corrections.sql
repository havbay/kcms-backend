-- A Correction is what a human explicitly asserts the labels SHOULD be.
--
-- It is a separate record from Action, and is never derived from one
-- (ARCHITECTURE section 6). Hiding a comment writes no Correction: if moderator
-- actions became training labels, the model would drift toward suppression
-- while the dashboard showed improving agreement with humans.
--
-- Append-only, like verdict and action, and survives content purge.

CREATE TABLE IF NOT EXISTS correction (
    id                    BIGSERIAL PRIMARY KEY,
    comment_id            TEXT        NOT NULL,
    severity              TEXT        NOT NULL
        CHECK (severity IN ('SAFE', 'OFFENSIVE', 'HARMFUL')),
    target                TEXT        NOT NULL
        CHECK (target IN ('PERSON', 'INSTITUTION', 'NEITHER')),
    -- The model version this human disagrees with. Without it a correction
    -- cannot be attributed to the thing it was correcting.
    disagrees_with_model  TEXT        NOT NULL,
    note                  TEXT,
    actor                 TEXT        NOT NULL,
    occurred_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS correction_comment_idx
    ON correction (comment_id, occurred_at DESC);
