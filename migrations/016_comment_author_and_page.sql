-- Who commented, as far as Meta will say.
--
-- fetch_comments already reads `from` but keeps only the name. Meta withholds
-- `from` entirely for commenters who have not authorized the app, which is the
-- normal case on a real Page, so this column is often null and the interface
-- must not depend on it. Where Meta does supply it, the id is app-scoped: it
-- identifies the person to this app only and is not a public profile id.

ALTER TABLE comment_content
    ADD COLUMN IF NOT EXISTS author_id TEXT;
