-- Two more advanced moderation settings, alongside auto_delete_delay_minutes
-- (017_auto_delete_quarantine.sql): auto-hiding OFFENSIVE comments, and a
-- workspace-level keyword override that can force SAFE or HARMFUL ahead of
-- the pattern matcher's own vocabulary.

ALTER TABLE workspace
    ADD COLUMN IF NOT EXISTS auto_hide_offensive BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS keyword_allowlist TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS keyword_blocklist TEXT[] NOT NULL DEFAULT '{}';
