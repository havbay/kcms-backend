-- Subscription plans. Until now every workspace could connect exactly one
-- Facebook Page (page_connection.workspace_id was itself the primary key),
-- so "how many Pages does your plan allow" was not a question the schema
-- could answer.
--
-- Enterprise is a custom annual quote handled outside the product, so it is
-- deliberately not a value this CHECK accepts yet.

ALTER TABLE workspace
    ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'STARTER'
        CHECK (plan IN ('STARTER', 'GROWTH'));

-- Widen page_connection from one row per workspace to many, keeping the
-- guarantee that a given Page can only ever belong to one workspace.
ALTER TABLE page_connection DROP CONSTRAINT page_connection_pkey;
ALTER TABLE page_connection ADD COLUMN IF NOT EXISTS id TEXT;
UPDATE page_connection SET id = workspace_id WHERE id IS NULL;
ALTER TABLE page_connection ALTER COLUMN id SET NOT NULL;
ALTER TABLE page_connection ADD PRIMARY KEY (id);

CREATE UNIQUE INDEX IF NOT EXISTS page_connection_workspace_page_idx
    ON page_connection (workspace_id, external_page_id);
