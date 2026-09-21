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
