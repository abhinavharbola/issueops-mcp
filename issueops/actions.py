import time
from datetime import timedelta

import psycopg
import requests

from issueops import tools
from issueops.db import sync_connection
from issueops.github_client import GitHubAPIError, GitHubReadClient, GitHubWriteClient

DEFAULT_PENDING_ACTION_TTL_HOURS = 48
DEFAULT_STUCK_APPROVING_RECOVERY_MINUTES = 10
PERMANENT_READ_FAILURE_STATUSES = {404, 410}
RECORD_ATTEMPTS = 3
RECORD_RETRY_DELAY_SECONDS = 0.5


class RecordingFailedError(Exception):
    pass


def _is_permanent_read_failure(exc: Exception) -> bool:
    return isinstance(exc, GitHubAPIError) and exc.status_code in PERMANENT_READ_FAILURE_STATUSES


def _record_with_retry(conn, dsn, operation):
    last_exc = None
    for attempt in range(RECORD_ATTEMPTS):
        try:
            if attempt == 0 or dsn is None:
                return operation(conn, attempt)
            with sync_connection(dsn) as fresh:
                return operation(fresh, attempt)
        except psycopg.Error as exc:
            last_exc = exc
            if attempt < RECORD_ATTEMPTS - 1:
                time.sleep(RECORD_RETRY_DELAY_SECONDS * (attempt + 1))
    raise RecordingFailedError(str(last_exc)) from last_exc


def _audit_with_retry(conn, dsn, *audit_args) -> bool:
    try:
        _record_with_retry(conn, dsn, lambda c, attempt: tools.write_audit_log(c, *audit_args))
    except RecordingFailedError:
        return False
    return True


def _already_recorded(conn, action_id: str, lease: object, status: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pending_actions WHERE id = %s AND status = %s AND claimed_at = %s",
        (action_id, status, lease),
    ).fetchone()
    return row is not None


def _finish_recorded(conn, dsn, action_id, lease, set_sql, set_params, audit_args, target_status) -> bool:
    def operation(c, attempt):
        if _finish_with_lease(c, action_id, lease, set_sql, set_params, audit_args):
            return True
        return attempt > 0 and _already_recorded(c, action_id, lease, target_status)

    return _record_with_retry(conn, dsn, operation)


def expire_stale_pending(conn, ttl_hours: int = DEFAULT_PENDING_ACTION_TTL_HOURS):
    with conn.transaction():
        rows = conn.execute(
            """
            UPDATE pending_actions
            SET status = 'expired'
            WHERE status = 'pending' AND created_at < now() - (%s * interval '1 hour')
            RETURNING id, tool_name, repo, issue_number, arguments
            """,
            (ttl_hours,),
        ).fetchall()
        for row in rows:
            tools.write_audit_log(
                conn, row["tool_name"], row["repo"], row["issue_number"], row["arguments"],
                row["id"], "system:expiry", "expired", f"pending longer than {ttl_hours} hours", 0,
            )
    return rows


def recover_stuck_approving(conn, minutes: int = DEFAULT_STUCK_APPROVING_RECOVERY_MINUTES):
    with conn.transaction():
        rows = conn.execute(
            """
            WITH stuck AS (
                SELECT id, claimed_by, execution_started_at FROM pending_actions
                WHERE status = 'approving' AND claimed_at < now() - (%s * interval '1 minute')
                FOR UPDATE
            )
            UPDATE pending_actions p
            SET status = CASE WHEN stuck.execution_started_at IS NULL THEN 'pending' ELSE 'needs_review' END,
                claimed_at = CASE WHEN stuck.execution_started_at IS NULL THEN NULL ELSE p.claimed_at END,
                claimed_by = CASE WHEN stuck.execution_started_at IS NULL THEN NULL ELSE p.claimed_by END
            FROM stuck
            WHERE p.id = stuck.id
            RETURNING p.id, p.tool_name, p.repo, p.issue_number, p.arguments, p.status,
                      stuck.claimed_by AS previous_claimant
            """,
            (minutes,),
        ).fetchall()
        for row in rows:
            claimant = row.get("previous_claimant") or "an unknown approver"
            if row.get("status") == "needs_review":
                result_status = "needs_review"
                summary = (
                    f"the approval started by {claimant} had already begun its GitHub call and never recorded an "
                    "outcome; it may or may not have been applied, so a person must check GitHub and resolve it"
                )
            else:
                result_status = "recovered"
                summary = (
                    f"reset from a stuck approving state back to pending; the approval had been started by "
                    f"{claimant} and had not reached GitHub"
                )
            tools.write_audit_log(
                conn, row["tool_name"], row["repo"], row["issue_number"], row["arguments"],
                row["id"], "system:recovery", result_status, summary, 0,
            )
    return rows


def count_pending_actions(conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM pending_actions WHERE status = 'pending'").fetchone()
    return row["n"]


def list_pending_actions(conn, limit: int = 25, offset: int = 0):
    return conn.execute(
        "SELECT * FROM pending_actions WHERE status = 'pending' ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
        (limit, offset),
    ).fetchall()


def count_needs_review(conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM pending_actions WHERE status = 'needs_review'").fetchone()
    return row["n"]


def list_needs_review(conn, limit: int = 100):
    return conn.execute(
        "SELECT * FROM pending_actions WHERE status = 'needs_review' ORDER BY claimed_at, id LIMIT %s", (limit,)
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
        assignee = arguments["assignee"]
        result = write_client.assign(repo, issue_number, assignee)
        applied = {a.get("login", "").lower() for a in (result or {}).get("assignees", [])}
        if assignee.lower() not in applied:
            raise RuntimeError(f"GitHub accepted the request but {assignee!r} is not an assignee of the issue")
        return result
    if tool_name == "propose_close":
        return write_client.close(repo, issue_number, arguments.get("reason"))
    raise ValueError(f"unknown mutating tool: {tool_name}")


def _stale_reason(tool_name: str, arguments: dict, then: dict, now: dict) -> str | None:
    then_hash = then.get("content_hash")
    if then_hash is not None and then_hash != now.get("content_hash"):
        return "issue title or body changed since the proposal"
    if tool_name == "propose_close" and now.get("state") != "open":
        return "issue is no longer open"
    if tool_name == "propose_remove_labels":
        present = {label.lower() for label in now.get("labels") or []}
        missing = [label for label in arguments["labels"] if label.lower() not in present]
        if missing:
            return f"labels are no longer on the issue: {missing}"
    return None


def _claim_pending_action(conn, action_id: str, approver: str, ttl_hours: int):
    with conn.transaction():
        row = conn.execute(
            "SELECT *, now() AS db_now FROM pending_actions WHERE id = %s AND status = 'pending' FOR UPDATE",
            (action_id,),
        ).fetchone()
        if row is None:
            return None, None, {"status": "not_found_or_not_pending"}

        tool_name, repo, issue_number, arguments = row["tool_name"], row["repo"], row["issue_number"], row["arguments"]

        if row["created_at"] < row["db_now"] - timedelta(hours=ttl_hours):
            conn.execute("UPDATE pending_actions SET status = 'expired' WHERE id = %s", (action_id,))
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "expired", "expired between page load and approve click", 0)
            return None, None, {"status": "expired"}

        if not tools.check_repo_active(conn, repo):
            conn.execute("UPDATE pending_actions SET status = 'blocked' WHERE id = %s", (action_id,))
            tools.write_audit_log(conn, tool_name, repo, issue_number, arguments, action_id, approver, "blocked", "repo not active in allowlist", 0)
            return None, None, {"status": "blocked"}

        claim_row = conn.execute(
            "UPDATE pending_actions SET status = 'approving', claimed_at = now(), claimed_by = %s "
            "WHERE id = %s RETURNING claimed_at",
            (approver, action_id),
        ).fetchone()
        tools.write_audit_log(
            conn, tool_name, repo, issue_number, arguments, action_id, approver, "claimed",
            "approval started", 0,
        )
        return row, claim_row["claimed_at"], None


def _mark_execution_started(conn, action_id: str, lease: object) -> bool:
    return _finish_with_lease(conn, action_id, lease, "SET execution_started_at = now()")


def _lease_is_still_held(conn, action_id: str, lease: object) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pending_actions WHERE id = %s AND status = 'approving' AND claimed_at = %s",
        (action_id, lease),
    ).fetchone()
    return row is not None


def _finish_with_lease(
    conn, action_id: str, lease: object, set_sql: str, set_params: tuple = (), audit_args: tuple | None = None,
) -> bool:
    with conn.transaction():
        row = conn.execute(
            f"UPDATE pending_actions {set_sql} WHERE id = %s AND status = 'approving' AND claimed_at = %s RETURNING id",
            set_params + (action_id, lease),
        ).fetchone()
        if row is None:
            return False
        if audit_args is not None:
            tools.write_audit_log(conn, *audit_args)
        return True


def _finish_before_github_call(conn, tool_name, repo, issue_number, arguments, action_id, approver, lease, set_sql, set_params, status, summary):
    audit_args = (tool_name, repo, issue_number, arguments, action_id, approver, status, summary, 0)
    if _finish_with_lease(conn, action_id, lease, set_sql, set_params, audit_args):
        return {"status": status, **({"error": summary} if status in ("failed", "stale", "released") else {})}

    tools.write_audit_log(
        conn, tool_name, repo, issue_number, arguments, action_id, approver, "lost_lease",
        "claim was reclaimed before this could be recorded; nothing was sent to GitHub by this call", 0,
    )
    return {"status": "lost_lease"}


def approve_action(
    conn, read_client: GitHubReadClient, write_client: GitHubWriteClient, action_id: str, approver: str,
    ttl_hours: int = DEFAULT_PENDING_ACTION_TTL_HOURS, dsn: str | None = None,
):
    row, lease, early_result = _claim_pending_action(conn, action_id, approver, ttl_hours)
    if early_result is not None:
        return early_result

    tool_name, repo, issue_number, arguments = row["tool_name"], row["repo"], row["issue_number"], row["arguments"]

    try:
        current_snapshot = tools.snapshot_issue_state(read_client, repo, issue_number)
        existing_comments = (
            read_client.get_issue_comments(repo, issue_number, allow_partial=True)
            if tool_name == "propose_add_comment" else []
        )
        current_repo_labels = (
            {label["name"].lower() for label in read_client.get_repo_labels(repo)}
            if tool_name == "propose_add_labels" else None
        )
    except Exception as exc:
        message = str(exc)
        if _is_permanent_read_failure(exc):
            return _finish_before_github_call(
                conn, tool_name, repo, issue_number, arguments, action_id, approver, lease,
                "SET status = 'failed', failure_reason = %s", (message,), "failed", message,
            )
        return _finish_before_github_call(
            conn, tool_name, repo, issue_number, arguments, action_id, approver, lease,
            "SET status = 'pending', claimed_at = NULL, claimed_by = NULL", (), "released", message,
        )

    if not _lease_is_still_held(conn, action_id, lease):
        tools.write_audit_log(
            conn, tool_name, repo, issue_number, arguments, action_id, approver, "lost_lease",
            "claim was reclaimed before the GitHub call; nothing was sent to GitHub by this call", 0,
        )
        return {"status": "lost_lease"}

    stale_reason = _stale_reason(tool_name, arguments, row["issue_state_snapshot"], current_snapshot)
    if stale_reason is None and tool_name == "propose_add_comment":
        wanted = arguments["body"].strip()
        if any((comment.get("body") or "").strip() == wanted for comment in existing_comments):
            stale_reason = "an identical comment already exists on the issue"
    if stale_reason is None and current_repo_labels is not None:
        missing = [label for label in arguments["labels"] if label.lower() not in current_repo_labels]
        if missing:
            stale_reason = f"labels no longer exist on the repo and GitHub would recreate them: {missing}"
    if stale_reason is not None:
        return _finish_before_github_call(
            conn, tool_name, repo, issue_number, arguments, action_id, approver, lease,
            "SET status = 'stale', failure_reason = %s", (stale_reason,), "stale", stale_reason,
        )

    if not _mark_execution_started(conn, action_id, lease):
        tools.write_audit_log(
            conn, tool_name, repo, issue_number, arguments, action_id, approver, "lost_lease",
            "claim was reclaimed before the GitHub call; nothing was sent to GitHub by this call", 0,
        )
        return {"status": "lost_lease"}

    try:
        _execute_on_github(write_client, tool_name, repo, issue_number, arguments)
    except Exception as exc:
        outcome_unknown = isinstance(exc.__cause__ or exc, requests.exceptions.RequestException)
        message = (
            f"outcome unknown, the request may have been applied before the connection failed: {exc}"
            if outcome_unknown
            else str(exc)
        )
        try:
            finished = _finish_recorded(
                conn, dsn, action_id, lease, "SET status = 'failed', failure_reason = %s", (message,),
                (tool_name, repo, issue_number, arguments, action_id, approver, "failed", message, 0),
                "failed",
            )
        except RecordingFailedError as recording_error:
            return {
                "status": "recording_failed",
                "error": f"{message}; the database write also failed: {recording_error}",
                "github_call_succeeded": False,
                "outcome_unknown": outcome_unknown,
            }
        if finished:
            result = {"status": "failed", "error": message}
            if outcome_unknown:
                result["outcome_unknown"] = True
            return result

        _audit_with_retry(
            conn, dsn, tool_name, repo, issue_number, arguments, action_id, approver,
            "lost_lease_after_execution",
            f"the GitHub call raised ({message}) but the lease was lost before this could be recorded; "
            "check GitHub and the audit log for a possible duplicate or partial action", 0,
        )
        return {"status": "lost_lease_after_execution", "error": message}

    try:
        updated = _finish_recorded(
            conn, dsn, action_id, lease,
            "SET status = 'executed', approved_by = %s, approved_at = now(), executed_at = now()", (approver,),
            (tool_name, repo, issue_number, arguments, action_id, approver, "executed", None, 0),
            "executed",
        )
    except RecordingFailedError as recording_error:
        return {
            "status": "recording_failed",
            "error": str(recording_error),
            "github_call_succeeded": True,
        }
    if not updated:
        _audit_with_retry(
            conn, dsn, tool_name, repo, issue_number, arguments, action_id, approver,
            "lost_lease_after_execution",
            "the GitHub call succeeded but the lease was lost before this could be recorded; check GitHub and the audit log for a possible duplicate action", 0,
        )
        return {"status": "lost_lease_after_execution"}

    return {"status": "executed"}


def reject_action(conn, action_id: str, approver: str, reason: str | None = None):
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id = %s AND status = 'pending' FOR UPDATE", (action_id,)
        ).fetchone()
        if row is None:
            return {"status": "not_found_or_not_pending"}

        conn.execute(
            "UPDATE pending_actions SET status = 'rejected', rejected_by = %s, rejected_at = now() WHERE id = %s",
            (approver, action_id),
        )
        tools.write_audit_log(
            conn, row["tool_name"], row["repo"], row["issue_number"], row["arguments"],
            action_id, approver, "rejected", reason, 0,
        )
        return {"status": "rejected"}


def resolve_needs_review(conn, action_id: str, resolver: str, applied: bool, note: str | None = None):
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id = %s AND status = 'needs_review' FOR UPDATE", (action_id,)
        ).fetchone()
        if row is None:
            return {"status": "not_found_or_not_needs_review"}

        detail = f" ({note})" if note else ""
        if applied:
            conn.execute(
                """
                UPDATE pending_actions
                SET status = 'executed', approved_by = COALESCE(claimed_by, %s),
                    approved_at = COALESCE(claimed_at, now()), executed_at = now(), failure_reason = NULL
                WHERE id = %s
                """,
                (resolver, action_id),
            )
            tools.write_audit_log(
                conn, row["tool_name"], row["repo"], row["issue_number"], row["arguments"],
                action_id, resolver, "executed",
                f"{resolver} confirmed on GitHub that the action was applied after an unknown outcome{detail}", 0,
            )
            return {"status": "executed"}

        conn.execute(
            """
            UPDATE pending_actions
            SET status = 'pending', claimed_at = NULL, claimed_by = NULL, execution_started_at = NULL
            WHERE id = %s
            """,
            (action_id,),
        )
        tools.write_audit_log(
            conn, row["tool_name"], row["repo"], row["issue_number"], row["arguments"],
            action_id, resolver, "requeued",
            f"{resolver} confirmed on GitHub that the action was not applied and returned it to pending{detail}", 0,
        )
        return {"status": "requeued"}


