from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import issueops.actions as actions
from conftest import FakeConn


def _pending_row(**overrides):
    row = {
        "id": "action-1",
        "created_at": datetime.now(timezone.utc),
        "tool_name": "propose_add_comment",
        "repo": "owner/repo",
        "issue_number": 5,
        "arguments": {"body": "looks good"},
        "issue_state_snapshot": {"state": "open", "labels": [], "assignees": []},
    }
    row.update(overrides)
    return row


def _read_client(snapshot=None):
    client = MagicMock()
    client.get_issue.return_value = snapshot or {
        "state": "open",
        "labels": [],
        "assignees": [],
    }
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


def test_approve_action_marks_stale_when_snapshot_diverges():
    row = _pending_row(issue_state_snapshot={"state": "open", "labels": [], "assignees": []})
    conn = FakeConn(pending_action_row=row)
    read_client = _read_client(snapshot={"state": "closed", "labels": [], "assignees": []})
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "stale"
    write_client.add_comment.assert_not_called()


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


def test_approve_action_marks_failed_when_the_stale_check_fetch_raises():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    read_client = MagicMock()
    read_client.get_issue.side_effect = RuntimeError("GitHub unreachable")
    write_client = MagicMock()

    result = actions.approve_action(conn, read_client, write_client, "action-1", "alice")

    assert result["status"] == "failed"
    assert "GitHub unreachable" in result["error"]
    write_client.add_comment.assert_not_called()
    assert any("status = 'failed'" in sql for sql, _ in conn.queries)


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
