DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'issueops_proposer') THEN
        CREATE ROLE issueops_proposer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'issueops_approver') THEN
        CREATE ROLE issueops_approver NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'issueops_pruner') THEN
        CREATE ROLE issueops_pruner NOLOGIN;
    END IF;
END $$;

REVOKE ALL ON repo_allowlist, pending_actions, triage_attempts, audit_log
    FROM PUBLIC, issueops_proposer, issueops_approver, issueops_pruner;

GRANT SELECT ON repo_allowlist TO issueops_proposer, issueops_approver, issueops_pruner;

GRANT SELECT, INSERT ON pending_actions TO issueops_proposer;
GRANT SELECT, INSERT, UPDATE ON triage_attempts TO issueops_proposer;
GRANT INSERT ON audit_log TO issueops_proposer;

GRANT SELECT, UPDATE ON pending_actions TO issueops_approver;
GRANT SELECT, INSERT ON audit_log TO issueops_approver;

GRANT SELECT, INSERT, DELETE ON audit_log TO issueops_pruner;

CREATE OR REPLACE FUNCTION issueops_guard_audit_log() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND pg_has_role(current_user, 'issueops_pruner', 'member') THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only (% by role % is not allowed)', TG_OP, current_user;
END $$;

DROP TRIGGER IF EXISTS audit_log_guard_rows ON audit_log;
CREATE TRIGGER audit_log_guard_rows
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION issueops_guard_audit_log();

DROP TRIGGER IF EXISTS audit_log_guard_truncate ON audit_log;
CREATE TRIGGER audit_log_guard_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION issueops_guard_audit_log();

CREATE OR REPLACE FUNCTION issueops_guard_proposal_insert() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF pg_has_role(current_user, 'issueops_proposer', 'member')
       AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper) THEN
        IF NEW.status IS DISTINCT FROM 'pending'
           OR NEW.approved_by IS NOT NULL
           OR NEW.approved_at IS NOT NULL
           OR NEW.executed_at IS NOT NULL
           OR NEW.claimed_at IS NOT NULL
           OR NEW.claimed_by IS NOT NULL
           OR NEW.execution_started_at IS NOT NULL
           OR NEW.failure_reason IS NOT NULL
           OR NEW.rejected_by IS NOT NULL
           OR NEW.rejected_at IS NOT NULL THEN
            RAISE EXCEPTION 'proposer role % may only insert a plain pending proposal', current_user;
        END IF;
        NEW.created_at := now();
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS pending_actions_guard_proposer_insert ON pending_actions;
CREATE TRIGGER pending_actions_guard_proposer_insert
    BEFORE INSERT ON pending_actions
    FOR EACH ROW EXECUTE FUNCTION issueops_guard_proposal_insert();

CREATE OR REPLACE FUNCTION issueops_guard_audit_insert() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF pg_has_role(current_user, 'issueops_proposer', 'member')
       AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper) THEN
        IF NEW.result_status IS NULL OR NOT (NEW.result_status = ANY (ARRAY['ok', 'error', 'proposed', 'deduped'])) THEN
            RAISE EXCEPTION 'proposer role % may not write audit rows with result %', current_user, NEW.result_status;
        END IF;
        NEW.timestamp := now();
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS audit_log_guard_proposer_insert ON audit_log;
CREATE TRIGGER audit_log_guard_proposer_insert
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION issueops_guard_audit_insert();
