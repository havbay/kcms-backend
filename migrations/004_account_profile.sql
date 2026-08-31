-- Collected at sign-up so a pilot request can be sized (Page volume, team,
-- review workload) without a second conversation. Optional: a judge or a
-- curious visitor should not be forced to invent one.
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS organization TEXT;
