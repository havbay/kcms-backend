-- One provider connection per workspace. Acquisition method is metadata only:
-- OAuth and manual token setup converge on the same encrypted credential.

CREATE TABLE IF NOT EXISTS page_connection (
    workspace_id           TEXT PRIMARY KEY REFERENCES workspace (id) ON DELETE CASCADE,
    provider               TEXT NOT NULL DEFAULT 'FACEBOOK'
        CHECK (provider = 'FACEBOOK'),
    external_page_id       TEXT NOT NULL,
    page_name              TEXT NOT NULL,
    connection_method      TEXT NOT NULL
        CHECK (connection_method IN ('FACEBOOK_LOGIN', 'MANUAL_TOKEN')),
    credential_ciphertext  TEXT NOT NULL,
    tasks                  TEXT[] NOT NULL DEFAULT '{}',
    connected_by           TEXT NOT NULL REFERENCES app_user (id),
    connected_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_synced_at          TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS page_connection_provider_page_idx
    ON page_connection (provider, external_page_id);
