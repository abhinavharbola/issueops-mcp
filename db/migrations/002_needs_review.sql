-- Historical: already applied to any database bootstrapped from db/schema.sql, which
-- records this filename in schema_migrations. Kept for databases that were built up
-- from these incremental files instead, and for the historical record. New changes go
-- in new files here, applied with `python scripts/migrate.py`, not by editing this one.
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS execution_started_at TIMESTAMPTZ;

ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_status_check;
ALTER TABLE pending_actions
    ADD CONSTRAINT pending_actions_status_check
    CHECK (status IN ('pending', 'approving', 'needs_review', 'rejected', 'expired', 'stale', 'blocked', 'executed', 'failed'));


