-- Automated replies are live when the owner enables them. The old dry-run
-- switch is removed so the API and workspace state cannot advertise a mode the
-- product no longer supports.

ALTER TABLE workspace
    DROP COLUMN IF EXISTS auto_reply_dry_run;
