from unittest.mock import MagicMock

import pytest

import issueops.tools as tools
from conftest import FakeConn, sync_connection_returning


def _patch_sync_connection(monkeypatch, fake_conn):
    monkeypatch.setattr(tools, "sync_connection", sync_connection_returning(fake_conn))


def _read_client(get_issue_return=None, labels=None):
    client = MagicMock()
    client.get_issue.return_value = get_issue_return or {
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    client.get_repo_labels.return_value = [{"name": name} for name in (labels or [])]
    return client


def test_propose_close_rejects_invalid_reason(monkeypatch):
    _patch_sync_connection(monkeypatch, FakeConn())
    with pytest.raises(tools.ValidationError):
        tools.propose_close("dsn", _read_client(), "owner/repo", 1, "not-a-real-reason", "test")


def test_propose_close_accepts_a_valid_reason(monkeypatch):
    fake_conn = FakeConn(new_id="abc-123")
    _patch_sync_connection(monkeypatch, fake_conn)

    result = tools.propose_close("dsn", _read_client(), "owner/repo", 1, "completed", "test")

    assert result["id"] == "abc-123"
    assert "queued for approval" in result["preview"]


def test_propose_add_labels_rejects_a_label_that_does_not_exist_on_the_repo(monkeypatch):
    _patch_sync_connection(monkeypatch, FakeConn())
    read_client = _read_client(labels=["bug", "enhancement"])

    with pytest.raises(tools.ValidationError):
        tools.propose_add_labels("dsn", read_client, "owner/repo", 1, ["not-a-real-label"], "test")


def test_propose_add_labels_accepts_known_labels(monkeypatch):
    fake_conn = FakeConn(new_id="lbl-1")
    _patch_sync_connection(monkeypatch, fake_conn)
    read_client = _read_client(labels=["bug", "enhancement"])

    result = tools.propose_add_labels("dsn", read_client, "owner/repo", 1, ["bug"], "test")

    assert result["id"] == "lbl-1"


def test_propose_assign_rejects_a_malformed_login(monkeypatch):
    _patch_sync_connection(monkeypatch, FakeConn())
    with pytest.raises(tools.ValidationError):
        tools.propose_assign("dsn", _read_client(), "owner/repo", 1, "not a login!", "test")


def test_propose_assign_accepts_a_well_formed_login(monkeypatch):
    fake_conn = FakeConn(new_id="asg-1")
    _patch_sync_connection(monkeypatch, fake_conn)

    result = tools.propose_assign("dsn", _read_client(), "owner/repo", 1, "octocat", "test")

    assert result["id"] == "asg-1"


def test_inactive_repo_is_rejected_before_anything_else(monkeypatch):
    _patch_sync_connection(monkeypatch, FakeConn(repo_active=False))
    with pytest.raises(tools.RepoNotAllowedError):
        tools.propose_close("dsn", _read_client(), "owner/repo", 1, "completed", "test")


def test_duplicate_proposal_is_deduped_instead_of_reinserted(monkeypatch):
    fake_conn = FakeConn(existing_pending=("existing-id", {"reason": "completed"}))
    _patch_sync_connection(monkeypatch, fake_conn)

    result = tools.propose_close("dsn", _read_client(), "owner/repo", 1, "completed", "test")

    assert result["id"] == "existing-id"
    assert not any("INSERT INTO pending_actions" in sql for sql, _ in fake_conn.queries)
