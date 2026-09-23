-- Historical: already applied to any database bootstrapped from db/schema.sql, which
-- records this filename in schema_migrations. Kept for databases that were built up
-- from these incremental files instead, and for the historical record. New changes go
-- in new files here, applied with `python scripts/migrate.py`, not by editing this one.
UPDATE repo_allowlist SET added_at = now() WHERE added_at IS NULL;
ALTER TABLE repo_allowlist ALTER COLUMN added_at SET NOT NULL;

UPDATE pending_actions SET created_at = now() WHERE created_at IS NULL;
ALTER TABLE pending_actions ALTER COLUMN created_at SET NOT NULL;

UPDATE pending_actions SET heuristic_flagged = false WHERE heuristic_flagged IS NULL;
ALTER TABLE pending_actions ALTER COLUMN heuristic_flagged SET NOT NULL;

UPDATE audit_log SET timestamp = now() WHERE timestamp IS NULL;
ALTER TABLE audit_log ALTER COLUMN timestamp SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pending_actions_requested_by ON pending_actions (requested_by, status);


