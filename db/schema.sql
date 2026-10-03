CREATE TABLE IF NOT EXISTS repo_allowlist (
    repo TEXT PRIMARY KEY,
    active BOOLEAN NOT NULL DEFAULT true,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool_name TEXT NOT NULL,
    repo TEXT NOT NULL REFERENCES repo_allowlist(repo) ON UPDATE CASCADE,
    issue_number INT NOT NULL,
    arguments JSONB NOT NULL,
    issue_state_snapshot JSONB NOT NULL,
    heuristic_flagged BOOLEAN NOT NULL DEFAULT false,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    claimed_by TEXT,
    execution_started_at TIMESTAMPTZ,
    failure_reason TEXT,
    rejected_by TEXT,
    rejected_at TIMESTAMPTZ,
    source_excerpt JSONB,
    rationale TEXT,
    requeued_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS triage_attempts (
    repo TEXT NOT NULL REFERENCES repo_allowlist(repo) ON UPDATE CASCADE,
    issue_number INT NOT NULL,
    content_hash TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('proposed', 'no_action', 'error')),
    attempts INT NOT NULL DEFAULT 1,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, issue_number)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool_name TEXT NOT NULL,
    repo TEXT,
    issue_number INT,
    arguments JSONB,
    pending_action_id UUID REFERENCES pending_actions(id),
    initiator TEXT NOT NULL,
    result_status TEXT NOT NULL,
    result_summary TEXT,
    latency_ms INT,
    trace_id TEXT
);

ALTER TABLE repo_allowlist ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ DEFAULT now();
UPDATE repo_allowlist SET added_at = now() WHERE added_at IS NULL;
ALTER TABLE repo_allowlist ALTER COLUMN added_at SET DEFAULT now();
ALTER TABLE repo_allowlist ALTER COLUMN added_at SET NOT NULL;

ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS heuristic_flagged BOOLEAN DEFAULT false;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS approved_by TEXT;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS failure_reason TEXT;
UPDATE pending_actions SET created_at = now() WHERE created_at IS NULL;
UPDATE pending_actions SET heuristic_flagged = false WHERE heuristic_flagged IS NULL;
ALTER TABLE pending_actions ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE pending_actions ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE pending_actions ALTER COLUMN heuristic_flagged SET DEFAULT false;
ALTER TABLE pending_actions ALTER COLUMN heuristic_flagged SET NOT NULL;

ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS execution_started_at TIMESTAMPTZ;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS rejected_by TEXT;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS source_excerpt JSONB;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS rationale TEXT;
ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS requeued_at TIMESTAMPTZ;
UPDATE pending_actions
SET rejected_by = approved_by, rejected_at = approved_at, approved_by = NULL, approved_at = NULL
WHERE status = 'rejected' AND rejected_by IS NULL AND approved_by IS NOT NULL;

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ DEFAULT now();
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS result_summary TEXT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS latency_ms INT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS trace_id TEXT;
UPDATE audit_log SET timestamp = now() WHERE timestamp IS NULL;
ALTER TABLE audit_log ALTER COLUMN timestamp SET DEFAULT now();
ALTER TABLE audit_log ALTER COLUMN timestamp SET NOT NULL;

DO $$
DECLARE
    existing record;
    status_ok boolean;
    lower_ok boolean;
    shape_ok boolean;
    fk_ok boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'pending_actions'::regclass
          AND conname = 'pending_actions_status_check'
          AND pg_get_constraintdef(oid) LIKE '%needs_review%'
    ) INTO status_ok;

    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'repo_allowlist'::regclass AND conname = 'repo_allowlist_repo_lowercase'
    ) INTO lower_ok;

    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'repo_allowlist'::regclass AND conname = 'repo_allowlist_repo_shape'
    ) INTO shape_ok;

    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'pending_actions'::regclass
          AND contype = 'f'
          AND conname = 'pending_actions_repo_fkey'
          AND confrelid = 'repo_allowlist'::regclass
          AND pg_get_constraintdef(oid) LIKE '%ON UPDATE CASCADE%'
    ) INTO fk_ok;

    fk_ok := fk_ok AND lower_ok;

    IF NOT status_ok THEN
        FOR existing IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'pending_actions'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%status%'
        LOOP
            EXECUTE format('ALTER TABLE pending_actions DROP CONSTRAINT %I', existing.conname);
        END LOOP;
    END IF;

    IF NOT fk_ok THEN
        FOR existing IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'pending_actions'::regclass
              AND contype = 'f'
              AND confrelid = 'repo_allowlist'::regclass
        LOOP
            EXECUTE format('ALTER TABLE pending_actions DROP CONSTRAINT %I', existing.conname);
        END LOOP;
    END IF;

    IF NOT lower_ok OR NOT shape_ok THEN
        FOR existing IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'repo_allowlist'::regclass
              AND contype = 'c'
              AND (pg_get_constraintdef(oid) ILIKE '%lower%' OR pg_get_constraintdef(oid) ILIKE '%[.]{1,2}%')
        LOOP
            EXECUTE format('ALTER TABLE repo_allowlist DROP CONSTRAINT %I', existing.conname);
        END LOOP;
        lower_ok := false;
        shape_ok := false;
    END IF;

    IF NOT lower_ok THEN
        DELETE FROM repo_allowlist mixed
        USING repo_allowlist lowered
        WHERE mixed.repo <> lower(mixed.repo) AND lowered.repo = lower(mixed.repo);

        UPDATE repo_allowlist SET repo = lower(repo) WHERE repo <> lower(repo);
        UPDATE pending_actions SET repo = lower(repo) WHERE repo <> lower(repo);

        ALTER TABLE repo_allowlist
            ADD CONSTRAINT repo_allowlist_repo_lowercase CHECK (repo = lower(repo));
    END IF;

    IF NOT shape_ok THEN
        ALTER TABLE repo_allowlist
            ADD CONSTRAINT repo_allowlist_repo_shape
            CHECK (repo ~ '^[a-z0-9_.-]+/[a-z0-9_.-]+$' AND repo !~ '(^|/)[.]{1,2}(/|$)');
    END IF;

    IF NOT fk_ok THEN
        ALTER TABLE pending_actions
            ADD CONSTRAINT pending_actions_repo_fkey
            FOREIGN KEY (repo) REFERENCES repo_allowlist(repo) ON UPDATE CASCADE;
    END IF;

    IF NOT status_ok THEN
        ALTER TABLE pending_actions
            ADD CONSTRAINT pending_actions_status_check
            CHECK (status IN ('pending', 'approving', 'needs_review', 'rejected', 'expired', 'stale', 'blocked', 'executed', 'failed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_pending_actions_dedup ON pending_actions (repo, issue_number, tool_name, status);
CREATE INDEX IF NOT EXISTS idx_pending_actions_status_created ON pending_actions (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pending_actions_requested_by ON pending_actions (requested_by, status);
CREATE INDEX IF NOT EXISTS idx_pending_actions_approving ON pending_actions (claimed_at) WHERE status = 'approving';
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_pending_action ON audit_log (pending_action_id);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version)
VALUES ('001_hardening.sql'), ('002_needs_review.sql'), ('003_requeued_at.sql')
ON CONFLICT (version) DO NOTHING;
