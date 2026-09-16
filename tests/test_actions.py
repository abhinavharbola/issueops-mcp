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


def test_approve_action_marks_failed_when_github_call_raises():
    row = _pending_row()
    conn = FakeConn(pending_action_row=row)
    write_client = MagicMock()
    write_client.add_comment.side_effect = RuntimeError("GitHub 500")

    result = actions.approve_action(conn, _read_client(), write_client, "action-1", "alice")

    assert result["status"] == "failed"
    assert "GitHub 500" in result["error"]
    assert any("status = 'failed'" in sql for sql, _ in conn.queries)


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



