CREATE TABLE repo_allowlist (
    repo TEXT PRIMARY KEY,
    active BOOLEAN NOT NULL DEFAULT true,
    added_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE pending_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT now(),
    tool_name TEXT NOT NULL,
    repo TEXT NOT NULL REFERENCES repo_allowlist(repo),
    issue_number INT NOT NULL,
    arguments JSONB NOT NULL,
    issue_state_snapshot JSONB NOT NULL,
    heuristic_flagged BOOLEAN DEFAULT false,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'stale', 'blocked', 'executed', 'failed')),
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    failure_reason TEXT
);

CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT now(),
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
