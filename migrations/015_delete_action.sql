-- Deleting replaces hiding as the removal action.
--
-- The policy is now: harmful is deleted from the Page without asking, and
-- offensive goes to a person who chooses DELETE or LEAVE. HIDE and UNHIDE stay
-- valid values because rows already recorded with them are history and must
-- not be rewritten, but the API no longer accepts them for new actions.
--
-- Deletion is irreversible on Facebook's side. The action row remains, so the
-- record of what KCMS did survives even though the comment does not.

ALTER TABLE action DROP CONSTRAINT IF EXISTS action_kind_check;
ALTER TABLE action
    ADD CONSTRAINT action_kind_check
    CHECK (kind IN ('LEAVE', 'DELETE', 'HIDE', 'UNHIDE'));
