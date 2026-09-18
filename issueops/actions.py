from datetime import datetime, timedelta, timezone

from issueops import tools
from issueops.github_client import GitHubReadClient, GitHubWriteClient

DEFAULT_PENDING_ACTION_TTL_HOURS = 48
DEFAULT_STUCK_APPROVING_RECOVERY_MINUTES = 10


def _now_minus(hours: int = 0, minutes: int = 0):
    return datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes)


def expire_stale_pending(conn, ttl_hours: int = DEFAULT_PENDING_ACTION_TTL_HOURS):
    conn.execute(
        """
        UPDATE pending_actions
        SET status = 'expired'
        WHERE status = 'pending' AND created_at < now() - (%s * interval '1 hour')
        """,
        (ttl_hours,),
    )


def recover_stuck_approving(conn, minutes: int = DEFAULT_STUCK_APPROVING_RECOVERY_MINUTES):
    rows = conn.execute(
        """
        UPDATE pending_actions
        SET status = 'pending', claimed_at = NULL
        WHERE status = 'approving' AND claimed_at < now() - (%s * interval '1 minute')
        RETURNING id, tool_name, repo, issue_number, arguments
        """,
        (minutes,),
    ).fetchall()
    for row in rows:
        tools.write_audit_log(
            conn, row["tool_name"], row["repo"], row["issue_number"], row["arguments"],
            row["id"], "system:recovery", "recovered",
            "reset from a stuck approving state back to pending", 0,
        )
    return rows


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
        removed = []
        for label in arguments["labels"]:
            try:
                write_client.remove_label(repo, issue_number, label)
                removed.append(label)
            except Exception as exc:
                attempted = removed + [label]
                not_attempted = [l for l in arguments["labels"] if l not in attempted]
                detail = f"removed {removed} before failing on {label!r} ({exc})"
                if not_attempted:
                    detail += f"; never attempted {not_attempted}"
                raise RuntimeError(detail) from exc
        return removed
    if tool_name == "propose_assign":
        return write_client.assign(repo, issue_number, arguments["assignee"])
    if tool_name == "propose_close":
        return write_client.close(repo, issue_number, arguments.get("reason"))
    raise ValueError(f"unknown mutating tool: {tool_name}")


def _claim_pending_action(conn, action_id: str, approver: str, ttl_hours: int):
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id = %s AND status = 'pending' FOR UPDATE", (action_id,)
        ).fetchone()
        if row is None:
            return None, None, {"status": "not_found_or_not_pending"}

        tool_name, repo, issue_number, arguments = row["tool_name"], row["repo"], row["issue_number"], row["arguments"]

        if row["created_at"] < _now_minus(hours=ttl_hours):
            conn.execute("UPDATE pending_actions SET status = 'expired' WHERE id = %s", (action_id,))
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "expired", "expired between page load and approve click", 0)
            return None, None, {"status": "expired"}

        if not tools.check_repo_active(conn, repo):
            conn.execute("UPDATE pending_actions SET status = 'blocked' WHERE id = %s", (action_id,))
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "blocked", "repo not active in allowlist", 0)
            return None, None, {"status": "blocked"}

        claim_row = conn.execute(
            "UPDATE pending_actions SET status = 'approving', claimed_at = now() WHERE id = %s RETURNING claimed_at",
            (action_id,),
        ).fetchone()
        return row, claim_row["claimed_at"], None


def _lease_is_still_held(conn, action_id: str, lease: object) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pending_actions WHERE id = %s AND status = 'approving' AND claimed_at = %s",
        (action_id, lease),
    ).fetchone()
    return row is not None


def _finish_with_lease(conn, action_id: str, lease: object, sql: str, params: tuple) -> bool:
    with conn.transaction():
        row = conn.execute(sql + " AND status = 'approving' AND claimed_at = %s RETURNING id", params + (lease,)).fetchone()
        return row is not None


def approve_action(
    conn, read_client: GitHubReadClient, write_client: GitHubWriteClient, action_id: str, approver: str,
    ttl_hours: int = DEFAULT_PENDING_ACTION_TTL_HOURS,
):
    row, lease, early_result = _claim_pending_action(conn, action_id, approver, ttl_hours)
    if early_result is not None:
        return early_result

    tool_name, repo, issue_number, arguments = row["tool_name"], row["repo"], row["issue_number"], row["arguments"]

    try:
        current_snapshot = tools.snapshot_issue_state(read_client, repo, issue_number)
    except Exception as exc:
        _finish_with_lease(
            conn, action_id, lease,
            "UPDATE pending_actions SET status = 'failed', failure_reason = %s WHERE id = %s",
            (str(exc), action_id),
        )
        tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "failed", str(exc), 0)
        return {"status": "failed", "error": str(exc)}

    if current_snapshot != row["issue_state_snapshot"]:
        _finish_with_lease(
            conn, action_id, lease,
            "UPDATE pending_actions SET status = 'stale' WHERE id = %s",
            (action_id,),
        )
        tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "stale", "issue state diverged from snapshot", 0)
        return {"status": "stale"}

    if not _lease_is_still_held(conn, action_id, lease):
        tools.write_audit_log(
            conn, tool_name, repo, issue_number, arguments, action_id, approver, "lost_lease",
            "claim was reclaimed before the GitHub call; nothing was sent to GitHub by this call", 0,
        )
        return {"status": "lost_lease"}

    try:
        _execute_on_github(write_client, tool_name, repo, issue_number, arguments)
    except Exception as exc:
        _finish_with_lease(
            conn, action_id, lease,
            "UPDATE pending_actions SET status = 'failed', failure_reason = %s WHERE id = %s",
            (str(exc), action_id),
        )
        tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "failed", str(exc), 0)
        return {"status": "failed", "error": str(exc)}

    updated = _finish_with_lease(
        conn, action_id, lease,
        "UPDATE pending_actions SET status = 'executed', approved_by = %s, approved_at = now(), executed_at = now() WHERE id = %s",
        (approver, action_id),
    )
    if not updated:
        tools.write_audit_log(
            conn, tool_name, repo, issue_number, arguments, action_id, approver, "lost_lease_after_execution",
            "the GitHub call succeeded but the lease was lost before this could be recorded; check GitHub and the audit log for a possible duplicate action", 0,
        )
        return {"status": "lost_lease_after_execution"}

    tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "executed", None, 0)
    return {"status": "executed"}


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
