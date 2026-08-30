-- Part 1: moderation spine.
--
-- Storage is split along the erasure boundary (see ARCHITECTURE §7):
--   comment_content  is purged when a commenter deletes at source
--   verdict / action are append-only and survive that purge
--
-- Verdict, Action and Correction are never derived from one another
-- (ARCHITECTURE §6). Hiding a comment writes NO correction.

CREATE TABLE IF NOT EXISTS comment_content (
    comment_id       TEXT PRIMARY KEY,
    page_id          TEXT        NOT NULL,
    author_ref       TEXT        NOT NULL,
    text             TEXT        NOT NULL,
    post_text        TEXT,
    parent_text      TEXT,
    is_reply         BOOLEAN     NOT NULL DEFAULT FALSE,
    posted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- What the model asserted. Append-only.
CREATE TABLE IF NOT EXISTS verdict (
    id                   BIGSERIAL PRIMARY KEY,
    comment_id           TEXT        NOT NULL,
    severity             TEXT        NOT NULL
        CHECK (severity IN ('SAFE', 'OFFENSIVE', 'HARMFUL')),
    severity_confidence  REAL        NOT NULL,
    target               TEXT        NOT NULL
        CHECK (target IN ('PERSON', 'INSTITUTION', 'NEITHER')),
    target_confidence    REAL        NOT NULL,
    abstain              BOOLEAN     NOT NULL DEFAULT FALSE,
    -- Why this surfaced. Required so anyone training on the resulting
    -- corrections can correct for selection bias.
    surfaced_reason      TEXT        NOT NULL
        CHECK (surfaced_reason IN
            ('triage', 'uncertainty', 'institution_sample', 'novel_language', 'cleared')),
    rationale            TEXT,
    model_version        TEXT        NOT NULL,
    occurred_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS verdict_comment_idx ON verdict (comment_id, occurred_at DESC);

-- What happened to the comment. Append-only, reversible, attributable.
CREATE TABLE IF NOT EXISTS action (
    id            BIGSERIAL PRIMARY KEY,
    comment_id    TEXT        NOT NULL,
    kind          TEXT        NOT NULL CHECK (kind IN ('LEAVE', 'HIDE', 'UNHIDE')),
    actor         TEXT        NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS action_comment_idx ON action (comment_id, occurred_at DESC);
