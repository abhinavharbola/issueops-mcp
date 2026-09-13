from datetime import datetime, timedelta, timezone

from issueops import tools
from issueops.github_client import GitHubReadClient, GitHubWriteClient


def _now_minus_48h():
    return datetime.now(timezone.utc) - timedelta(hours=48)


def expire_stale_pending(conn):
    conn.execute(
        """
        UPDATE pending_actions
        SET status = 'expired'
        WHERE status = 'pending' AND created_at < now() - interval '48 hours'
        """
    )


def list_pending_actions(conn):
    return conn.execute(
        "SELECT * FROM pending_actions WHERE status = 'pending' ORDER BY created_at DESC"
    ).fetchall()


def list_recent_audit_log(conn, limit: int = 50):
    return conn.execute(
        "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT %s", (limit,)
    ).fetchall()


def _execute_on_github(write_client: GitHubWriteClient, tool_name: str, repo: str, issue_number: int, arguments: dict):
    if tool_name == "propose_add_comment":
        return write_client.add_comment(repo, issue_number, arguments["body"])
    if tool_name == "propose_add_labels":
        return write_client.add_labels(repo, issue_number, arguments["labels"])
    if tool_name == "propose_remove_labels":
        return [write_client.remove_label(repo, issue_number, label) for label in arguments["labels"]]
    if tool_name == "propose_assign":
        return write_client.assign(repo, issue_number, arguments["assignee"])
    if tool_name == "propose_close":
        return write_client.close(repo, issue_number, arguments.get("reason"))
    raise ValueError(f"unknown mutating tool: {tool_name}")


def approve_action(conn, read_client: GitHubReadClient, write_client: GitHubWriteClient, action_id: str, approver: str):
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id = %s AND status = 'pending' FOR UPDATE", (action_id,)
        ).fetchone()
        if row is None:
            return {"status": "not_found_or_not_pending"}

        tool_name, repo, issue_number, arguments = row["tool_name"], row["repo"], row["issue_number"], row["arguments"]

        if row["created_at"] < _now_minus_48h():
            conn.execute("UPDATE pending_actions SET status = 'expired' WHERE id = %s", (action_id,))
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "expired", "expired between page load and approve click", 0)
            return {"status": "expired"}

        if not tools.check_repo_active(conn, repo):
            conn.execute("UPDATE pending_actions SET status = 'blocked' WHERE id = %s", (action_id,))
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "blocked", "repo not active in allowlist", 0)
            return {"status": "blocked"}

        current_snapshot = tools.snapshot_issue_state(read_client, repo, issue_number)
        if current_snapshot != row["issue_state_snapshot"]:
            conn.execute("UPDATE pending_actions SET status = 'stale' WHERE id = %s", (action_id,))
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "stale", "issue state diverged from snapshot", 0)
            return {"status": "stale"}

        try:
            _execute_on_github(write_client, tool_name, repo, issue_number, arguments)
            conn.execute(
                """
                UPDATE pending_actions
                SET status = 'executed', approved_by = %s, approved_at = now(), executed_at = now()
                WHERE id = %s
                """,
                (approver, action_id),
            )
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "executed", None, 0)
            return {"status": "executed"}
        except Exception as exc:
            conn.execute(
                "UPDATE pending_actions SET status = 'failed', failure_reason = %s WHERE id = %s",
                (str(exc), action_id),
            )
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "failed", str(exc), 0)
            return {"status": "failed", "error": str(exc)}


def reject_action(conn, action_id: str, approver: str, reason: str | None = None):
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id = %s AND status = 'pending' FOR UPDATE", (action_id,)
        ).fetchone()
        if row is None:
            return {"status": "not_found_or_not_pending"}

        conn.execute(
            "UPDATE pending_actions SET status = 'rejected', approved_by = %s, approved_at = now() WHERE id = %s",
            (approver, action_id),
        )
        tools.write_audit_log(
            conn, row["tool_name"], row["repo"], row["issue_number"], row["arguments"],
            action_id, approver, "rejected", reason, 0,
        )
        return {"status": "rejected"}
