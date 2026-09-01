-- Whether an Action actually reached the provider.
--
-- An Action records what happened to a comment. Hiding a sample comment, or a
-- comment on no connected Page, changes nothing on Facebook — and until now
-- that was indistinguishable from a hide that did. The moderator saw the same
-- green status either way.
--
-- Existing rows predate any provider call, so FALSE is the truthful default.

ALTER TABLE action
    ADD COLUMN IF NOT EXISTS provider_applied BOOLEAN NOT NULL DEFAULT FALSE;
