ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS execution_started_at TIMESTAMPTZ;

ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_status_check;
ALTER TABLE pending_actions
    ADD CONSTRAINT pending_actions_status_check
    CHECK (status IN ('pending', 'approving', 'needs_review', 'rejected', 'expired', 'stale', 'blocked', 'executed', 'failed'));
