from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import psycopg
import pytest
import requests

import issueops.actions as actions
import issueops.tools as tools
from conftest import FakeConn
from issueops.github_client import GitHubAPIError


def _pending_row(**overrides):
    row = {
        "id": "action-1",
        "created_at": datetime.now(timezone.utc),
        "db_now": datetime.now(timezone.utc),
        "tool_name": "propose_add_comment",
        "repo": "owner/repo",
        "issue_number": 5,
        "arguments": {"body": "looks good"},
        "issue_state_snapshot": {"state": "open", "labels": [], "assignees": []},
    }
    row.update(overrides)
    return row


def _read_client(snapshot=None, comments=None):
    client = MagicMock()
    client.get_issue.return_value = snapshot or {
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    client.get_issue_comments.return_value = comments or []
    return client


def test_approve_action_returns_not_found_when_row_is_missing():
    conn = FakeConn(pending_action_row=None)
    result = actions.approve_action(conn, _read_client(), MagicMock(), "missing-id", "alice")
    assert result["status"] == "not_found_or_not_pending"


def test_approve_action_expires_when_past_the_configured_ttl():
    old_row = _pending_row(created_at=datetime.now(timezone.utc) - timedelta(hours=49))
    conn = FakeConn(pending_action_row=old_row)
    write_client = MagicMock()

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice", ttl_hours=48)

    assert result["status"] == "expired"
    write_client.add_comment.assert_not_called()
    assert any("status = 'expired'" in sql for sql, _ in conn.queries)


def test_approve_action_respects_a_custom_ttl():
    row = _pending_row(created_at=datetime.now(timezone.utc) - timedelta(hours=2))
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice", ttl_hours=1)

    assert result["status"] == "expired"


def test_approve_action_blocks_when_repo_is_not_active():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row, repo_active=False)
    write_client = MagicMock()

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "blocked"
    write_client.add_comment.assert_not_called()


def test_approve_close_is_stale_when_the_issue_is_no_longer_open():
    row = _pending_row(tool_name="propose_close", arguments={"reason": "completed"})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(snapshot={"state": "closed", "labels": [], "assignees": []})
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "stale"
    write_client.close.assert_not_called()


def test_approve_marks_any_action_stale_when_the_title_or_body_changed():
    row = _pending_row(
        issue_state_snapshot={
            "state": "open", "labels": [], "assignees": [],
            "content_hash": tools.content_hash("t", "original body"),
        }
    )
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(snapshot={
        "state": "open", "labels": [], "assignees": [], "title": "t", "body": "edited body",
    })
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "stale"
    assert "title or body" in result["error"]
    write_client.add_comment.assert_not_called()


def test_approve_still_executes_when_the_title_and_body_are_unchanged():
    row = _pending_row(
        issue_state_snapshot={
            "state": "open", "labels": [], "assignees": [],
            "content_hash": tools.content_hash("t", "b"),
        }
    )
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(snapshot={
        "state": "open", "labels": [], "assignees": [], "title": "t", "body": "b",
    })
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "executed"


def test_a_sibling_action_stays_approvable_after_another_action_changed_labels_and_assignees():
    row = _pending_row(
        tool_name="propose_assign",
        arguments={"assignee": "octocat"},
        issue_state_snapshot={"state": "open", "labels": [], "assignees": []},
    )
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(snapshot={"state": "open", "labels": ["bug"], "assignees": []})
    write_client = MagicMock()
    write_client.assign.return_value = {"assignees": [{"login": "octocat"}]}

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "executed"
    write_client.assign.assert_called_once_with("owner/repo", 5, "octocat")


def test_remove_labels_is_stale_when_a_label_is_already_gone():
    row = _pending_row(tool_name="propose_remove_labels", arguments={"labels": ["bug", "wontfix"]})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(snapshot={"state": "open", "labels": ["bug"], "assignees": []})
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "stale"
    write_client.remove_label.assert_not_called()


def test_add_comment_is_stale_when_an_identical_comment_already_exists():
    row = _pending_row(arguments={"body": "looks good"})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(comments=[{"body": "  looks good\n"}])
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "stale"
    assert "identical comment" in result["error"]
    write_client.add_comment.assert_not_called()


def test_add_comment_still_posts_when_existing_comments_differ():
    row = _pending_row(arguments={"body": "looks good"})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(comments=[{"body": "something else"}])
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "executed"


def test_a_network_failure_during_the_write_is_reported_as_outcome_unknown():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()
    write_client.add_comment.side_effect = requests.exceptions.ReadTimeout("timed out")

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "failed"
    assert result["outcome_unknown"] is True
    assert "outcome unknown" in result["error"]


def test_an_http_error_from_github_is_not_reported_as_outcome_unknown():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()
    write_client.add_comment.side_effect = RuntimeError("GitHub 422")

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "failed"
    assert "outcome_unknown" not in result


def test_assign_fails_when_github_did_not_actually_apply_the_assignee():
    row = _pending_row(tool_name="propose_assign", arguments={"assignee": "octocat"})
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()
    write_client.assign.return_value = {"assignees": []}

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "failed"
    assert "not an assignee" in result["error"]


def test_approve_action_executes_on_success():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "executed"
    write_client.add_comment.assert_called_once_with("owner/repo", 5, "looks good")
    assert any("status = 'executed'" in sql for sql, _ in conn.queries)


def test_approve_action_claims_the_row_before_calling_github():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()
    order = []
    write_client.add_comment.side_effect = lambda *a, **k: order.append("github_call")

    original_execute = conn.execute

    def tracking_execute(sql, params=None):
        if "SET status = 'approving'" in " ".join(sql.split()):
            order.append("claim")
        return original_execute(sql, params)

    conn.execute = tracking_execute

    actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert order == ["claim", "github_call"]


def test_approve_action_does_not_call_github_when_the_lease_was_already_reclaimed():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row, lease_held=False)
    write_client = MagicMock()

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "lost_lease"
    write_client.add_comment.assert_not_called()
    assert any("lost_lease" in str(params) for _, params in conn.queries if params)


def test_approve_action_flags_a_lost_lease_discovered_only_after_the_github_call():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()

    def drop_lease_during_the_github_call(*args, **kwargs):
        conn.lease_held = False

    write_client.add_comment.side_effect = drop_lease_during_the_github_call

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "lost_lease_after_execution"
    write_client.add_comment.assert_called_once()


def test_approve_action_reports_lost_lease_not_stale_when_lease_reclaimed_before_staleness_check():
    row = _pending_row(issue_state_snapshot={"state": "open", "labels": [], "assignees": []})
    conn = FakeConn(pending_action_row=row, lease_held=False)
    read_client = _read_client(snapshot={"state": "closed", "labels": [], "assignees": []})
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "lost_lease"
    write_client.add_comment.assert_not_called()
    assert not any("status = 'stale'" in sql for sql, _ in conn.queries)


def test_approve_action_reports_lost_lease_not_failed_when_lease_reclaimed_before_fetch_raises():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row, lease_held=False)
    read_client = MagicMock()
    read_client.get_issue.side_effect = RuntimeError("GitHub unreachable")
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "lost_lease"
    write_client.add_comment.assert_not_called()
    audit_inserts = [params for sql, params in conn.queries if "INSERT INTO audit_log" in sql]
    assert not any(params[6] == "failed" for params in audit_inserts)
    assert any(params[6] == "lost_lease" for params in audit_inserts)


def test_approve_action_flags_a_lost_lease_when_github_call_raises_after_lease_lost():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()

    def drop_lease_and_raise(*args, **kwargs):
        conn.lease_held = False
        raise RuntimeError("GitHub 500")

    write_client.add_comment.side_effect = drop_lease_and_raise

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "lost_lease_after_execution"
    assert "GitHub 500" in result["error"]


def test_a_transient_read_failure_releases_the_claim_instead_of_failing_the_action():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    read_client = MagicMock()
    read_client.get_issue.side_effect = RuntimeError("GitHub unreachable")
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "released"
    assert "GitHub unreachable" in result["error"]
    write_client.add_comment.assert_not_called()
    assert any("status = 'pending', claimed_at = NULL" in sql for sql, _ in conn.queries)
    assert not any("status = 'failed'" in sql for sql, _ in conn.queries)
    audit_inserts = [params for sql, params in conn.queries if "INSERT INTO audit_log" in sql]
    assert any(params[6] == "released" for params in audit_inserts)


def test_a_github_auth_failure_on_the_read_releases_the_claim():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    read_client = MagicMock()
    read_client.get_issue.side_effect = GitHubAPIError(401, "Bad credentials")

    result = actions.approve_action(conn, read_client, MagicMock(), "action-1", "alice")

    assert result["status"] == "released"


def test_a_missing_issue_on_the_read_fails_the_action_permanently():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    read_client = MagicMock()
    read_client.get_issue.side_effect = GitHubAPIError(404, "Not Found")
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "failed"
    write_client.add_comment.assert_not_called()
    assert any("status = 'failed'" in sql for sql, _ in conn.queries)


def test_the_duplicate_comment_check_reads_comments_with_partial_results_allowed():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client()

    actions.approve_action(conn, read_client, MagicMock(), "action-1", "alice")

    read_client.get_issue_comments.assert_called_once_with("owner/repo", 5, allow_partial=True)


def test_remove_labels_stale_check_ignores_label_case():
    row = _pending_row(tool_name="propose_remove_labels", arguments={"labels": ["bug"]})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(snapshot={"state": "open", "labels": ["Bug"], "assignees": []})
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "executed"
    write_client.remove_label.assert_called_once_with("owner/repo", 5, "bug")


class _BrokenThenHealthyConn:
    def __init__(self, inner, failures):
        self.inner = inner
        self.failures = failures

    def execute(self, sql, params=None):
        if "SET status = 'executed'" in " ".join(sql.split()) and self.failures > 0:
            self.failures -= 1
            raise psycopg.OperationalError("connection dropped")
        return self.inner.execute(sql, params)

    def transaction(self):
        return self.inner.transaction()


def test_recording_is_retried_after_the_github_call_when_the_database_blips(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda seconds: None)
    row = _pending_row()
    inner = FakeConn(pending_action_row=row)
    conn = _BrokenThenHealthyConn(inner, failures=1)
    write_client = MagicMock()

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "executed"
    write_client.add_comment.assert_called_once()


def test_recording_failure_after_a_successful_github_call_is_reported_without_raising(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda seconds: None)
    row = _pending_row()
    inner = FakeConn(pending_action_row=row)
    conn = _BrokenThenHealthyConn(inner, failures=99)
    write_client = MagicMock()

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "recording_failed"
    assert result["github_call_succeeded"] is True
    write_client.add_comment.assert_called_once()


def test_approve_action_marks_failed_when_github_call_raises():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()
    write_client.add_comment.side_effect = RuntimeError("GitHub 500")

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "failed"
    assert "GitHub 500" in result["error"]
    assert any("status = 'failed'" in sql for sql, _ in conn.queries)


def test_a_second_approve_after_the_row_is_claimed_finds_it_already_gone():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()

    actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    conn.pending_action_row = None
    second_result = actions.approve_action(conn, _read_client(), write_client, "action-1", "bob")

    assert second_result["status"] == "not_found_or_not_pending"
    write_client.add_comment.assert_called_once()


def test_reject_action_marks_rejected():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)

    result = actions.reject_action(conn, "action-1", "alice", reason="not needed")

    assert result["status"] == "rejected"
    assert any("status = 'rejected'" in sql for sql, _ in conn.queries)


def test_reject_action_not_found_when_row_missing():
    conn = FakeConn(pending_action_row=None)
    result = actions.reject_action(conn, "missing-id", "alice")
    assert result["status"] == "not_found_or_not_pending"


def test_execute_on_github_remove_labels_records_partial_failure():
    write_client = MagicMock()
    write_client.remove_label.side_effect = [None, RuntimeError("label locked"), None]

    with pytest.raises(RuntimeError) as exc_info:
        actions._execute_on_github(
            write_client, "propose_remove_labels", "owner/repo", 5,
            {"labels": ["bug", "wontfix", "duplicate"]},
        )

    message = str(exc_info.value)
    assert "removed ['bug']" in message
    assert "wontfix" in message
    assert "never attempted ['duplicate']" in message


def test_execute_on_github_rejects_an_unknown_tool_name():
    with pytest.raises(ValueError):
        actions._execute_on_github(MagicMock(), "propose_teleport", "owner/repo", 5, {})


def test_expire_stale_pending_passes_the_configured_ttl_as_a_parameter():
    conn = FakeConn()
    actions.expire_stale_pending(conn, ttl_hours=24)
    sql, params = conn.queries[0]
    assert params == (24,)


def test_expire_stale_pending_defaults_to_48_hours():
    conn = FakeConn()
    actions.expire_stale_pending(conn)
    sql, params = conn.queries[0]
    assert params == (actions.DEFAULT_PENDING_ACTION_TTL_HOURS,)


def test_recover_stuck_approving_writes_an_audit_log_row_for_each_recovered_action():
    stuck = [{"id": "stuck-1", "tool_name": "propose_close", "repo": "owner/repo", "issue_number": 9, "arguments": {"reason": "completed"}}]
    conn = FakeConn(stuck_approving_rows=stuck)

    rows = actions.recover_stuck_approving(conn)

    assert rows == stuck
    assert any("INSERT INTO audit_log" in sql for sql, _ in conn.queries)
    audit_params = next(params for sql, params in conn.queries if "INSERT INTO audit_log" in sql)
    assert "system:recovery" in audit_params


def test_recover_stuck_approving_passes_the_configured_minutes_as_a_parameter():
    conn = FakeConn()
    actions.recover_stuck_approving(conn, minutes=5)
    sql, params = conn.queries[0]
    assert params == (5,)


def test_recover_stuck_approving_defaults_to_ten_minutes():
    conn = FakeConn()
    actions.recover_stuck_approving(conn)
    sql, params = conn.queries[0]
    assert params == (actions.DEFAULT_STUCK_APPROVING_RECOVERY_MINUTES,)


def test_a_network_failure_while_removing_labels_is_still_reported_as_outcome_unknown():
    row = _pending_row(tool_name="propose_remove_labels", arguments={"labels": ["bug"]})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(snapshot={"state": "open", "labels": ["bug"], "assignees": []})
    write_client = MagicMock()
    write_client.remove_label.side_effect = requests.exceptions.ConnectionError("reset")

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "failed"
    assert result["outcome_unknown"] is True


def test_list_pending_actions_is_paginated():
    conn = FakeConn()

    actions.list_pending_actions(conn, limit=10, offset=20)

    query, params = conn.queries[-1]
    assert "LIMIT %s OFFSET %s" in query
    assert params == (10, 20)


def test_claim_compares_the_row_age_against_the_database_clock():
    row = _pending_row(created_at=datetime.now(timezone.utc) - timedelta(hours=49))
    row["db_now"] = row["created_at"] + timedelta(hours=1)
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "executed"


def _audit_statuses(conn):
    return [params[6] for sql, params in conn.queries if "INSERT INTO audit_log" in sql]


def test_the_claim_writes_its_own_audit_row_and_records_the_approver():
    conn = FakeConn(pending_action_row=_pending_row())

    actions.approve_action(conn, _read_client(), MagicMock(), "action-1", "alice")

    claim_updates = [(sql, params) for sql, params in conn.queries if "SET status = 'approving'" in sql]
    assert claim_updates and claim_updates[0][1] == ("alice", "action-1")
    assert _audit_statuses(conn)[0] == "claimed"


def test_the_executed_status_and_its_audit_row_are_written_in_the_same_transaction():
    conn = FakeConn(pending_action_row=_pending_row())
    in_transaction_at_audit = {}
    original_execute = conn.execute

    def tracking_execute(sql, params=None):
        if "INSERT INTO audit_log" in sql:
            in_transaction_at_audit[params[6]] = conn.in_transaction
        return original_execute(sql, params)

    conn.execute = tracking_execute

    actions.approve_action(conn, _read_client(), MagicMock(), "action-1", "alice")

    assert in_transaction_at_audit["executed"] is True
    assert in_transaction_at_audit["claimed"] is True


def test_a_failed_github_call_writes_its_audit_row_in_the_same_transaction_as_the_failed_status():
    conn = FakeConn(pending_action_row=_pending_row())
    write_client = MagicMock()
    write_client.add_comment.side_effect = RuntimeError("GitHub 500")
    in_transaction_at_audit = {}
    original_execute = conn.execute

    def tracking_execute(sql, params=None):
        if "INSERT INTO audit_log" in sql:
            in_transaction_at_audit[params[6]] = conn.in_transaction
        return original_execute(sql, params)

    conn.execute = tracking_execute

    actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert in_transaction_at_audit["failed"] is True


def test_no_executed_audit_row_is_written_when_the_lease_was_lost_during_the_github_call():
    conn = FakeConn(pending_action_row=_pending_row())
    write_client = MagicMock()
    write_client.add_comment.side_effect = lambda *a, **k: setattr(conn, "lease_held", False)

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "lost_lease_after_execution"
    assert "executed" not in _audit_statuses(conn)
    assert "lost_lease_after_execution" in _audit_statuses(conn)


def test_expiring_pending_actions_audits_each_expired_row():
    expired = [{"id": "e-1", "tool_name": "propose_close", "repo": "owner/repo", "issue_number": 3, "arguments": {}}]
    conn = FakeConn(stuck_approving_rows=expired)

    rows = actions.expire_stale_pending(conn, ttl_hours=24)

    assert rows == expired
    audit_params = next(params for sql, params in conn.queries if "INSERT INTO audit_log" in sql)
    assert audit_params[5] == "system:expiry" and audit_params[6] == "expired"


def test_the_add_labels_approval_goes_stale_when_a_label_no_longer_exists_on_the_repo():
    row = _pending_row(tool_name="propose_add_labels", arguments={"labels": ["Bug"]})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client()
    read_client.get_repo_labels.return_value = [{"name": "enhancement"}]
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "stale"
    write_client.add_labels.assert_not_called()


def test_the_add_labels_approval_executes_when_the_label_exists_in_any_case():
    row = _pending_row(tool_name="propose_add_labels", arguments={"labels": ["Bug"]})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client()
    read_client.get_repo_labels.return_value = [{"name": "bug"}]
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "executed"


def test_a_failure_to_read_the_repo_labels_releases_the_claim():
    row = _pending_row(tool_name="propose_add_labels", arguments={"labels": ["bug"]})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client()
    read_client.get_repo_labels.side_effect = GitHubAPIError(502, "bad gateway")

    result = actions.approve_action(conn, read_client, MagicMock(), "action-1", "alice")

    assert result["status"] == "released"


def test_rejecting_records_the_rejector_in_the_rejected_columns():
    conn = FakeConn(pending_action_row=_pending_row())

    actions.reject_action(conn, "action-1", "alice")

    update = next(sql for sql, _ in conn.queries if "status = 'rejected'" in sql)
    assert "rejected_by" in update and "approved_by" not in update


def test_the_execution_marker_is_written_before_the_github_call():
    conn = FakeConn(pending_action_row=_pending_row())
    seen = {}
    write_client = MagicMock()

    def record_call(*args, **kwargs):
        seen["queries"] = [sql for sql, _ in conn.queries]
        return {}

    write_client.add_comment.side_effect = record_call

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "executed"
    assert any("SET execution_started_at = now()" in sql for sql in seen["queries"])


def test_no_github_call_is_made_when_the_marker_cannot_be_written(monkeypatch):
    conn = FakeConn(pending_action_row=_pending_row())
    write_client = MagicMock()
    monkeypatch.setattr(actions, "_mark_execution_started", lambda *args: False)

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "lost_lease"
    write_client.add_comment.assert_not_called()
    audit_params = [params for sql, params in conn.queries if "INSERT INTO audit_log" in sql]
    assert any("lost_lease" in params for params in audit_params)


def test_a_marker_failure_that_raises_leaves_github_untouched(monkeypatch):
    conn = FakeConn(pending_action_row=_pending_row())
    write_client = MagicMock()

    def boom(*args):
        raise psycopg.OperationalError("connection dropped")

    monkeypatch.setattr(actions, "_mark_execution_started", boom)

    with pytest.raises(psycopg.OperationalError):
        actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    write_client.add_comment.assert_not_called()


def test_recovery_moves_a_row_whose_github_call_started_to_needs_review():
    stuck = [{
        "id": "stuck-2", "tool_name": "propose_add_comment", "repo": "owner/repo", "issue_number": 9,
        "arguments": {"body": "x"}, "status": "needs_review", "previous_claimant": "alice",
    }]
    conn = FakeConn(stuck_approving_rows=stuck)

    actions.recover_stuck_approving(conn)

    sql = conn.queries[0][0]
    assert "execution_started_at IS NULL THEN 'pending' ELSE 'needs_review'" in sql
    audit_params = next(params for sql, params in conn.queries if "INSERT INTO audit_log" in sql)
    assert "needs_review" in audit_params
    assert "recovered" not in audit_params
    assert any("may or may not have been applied" in str(p) for p in audit_params)


def test_recovery_returns_a_row_that_never_reached_github_to_pending():
    stuck = [{
        "id": "stuck-3", "tool_name": "propose_close", "repo": "owner/repo", "issue_number": 9,
        "arguments": {"reason": None}, "status": "pending", "previous_claimant": "alice",
    }]
    conn = FakeConn(stuck_approving_rows=stuck)

    actions.recover_stuck_approving(conn)

    audit_params = next(params for sql, params in conn.queries if "INSERT INTO audit_log" in sql)
    assert "recovered" in audit_params
    assert "needs_review" not in audit_params


def _review_row(**overrides):
    row = _pending_row(status="needs_review", claimed_by="alice", claimed_at=datetime.now(timezone.utc))
    row.update(overrides)
    return row


def test_resolving_as_applied_marks_it_executed_with_an_audit_row():
    conn = FakeConn(pending_action_row=_review_row())

    result = actions.resolve_needs_review(conn, "action-1", "bob", applied=True, note="saw the comment")

    assert result == {"status": "executed"}
    update_sql = next(sql for sql, _ in conn.queries if sql.startswith("UPDATE pending_actions"))
    assert "status = 'executed'" in update_sql
    audit_params = next(params for sql, params in conn.queries if "INSERT INTO audit_log" in sql)
    assert "executed" in audit_params
    assert "bob" in audit_params
    assert any("saw the comment" in str(p) for p in audit_params)


def test_resolving_as_not_applied_requeues_it_and_clears_the_marker():
    conn = FakeConn(pending_action_row=_review_row())

    result = actions.resolve_needs_review(conn, "action-1", "bob", applied=False)

    assert result == {"status": "requeued"}
    update_sql = next(sql for sql, _ in conn.queries if sql.startswith("UPDATE pending_actions"))
    assert "status = 'pending'" in update_sql
    assert "execution_started_at = NULL" in update_sql
    audit_params = next(params for sql, params in conn.queries if "INSERT INTO audit_log" in sql)
    assert "requeued" in audit_params


def test_resolving_a_row_that_is_not_in_needs_review_does_nothing():
    conn = FakeConn(pending_action_row=None)

    result = actions.resolve_needs_review(conn, "action-1", "bob", applied=True)

    assert result == {"status": "not_found_or_not_needs_review"}
    assert not any(sql.startswith("UPDATE pending_actions") for sql, _ in conn.queries)


