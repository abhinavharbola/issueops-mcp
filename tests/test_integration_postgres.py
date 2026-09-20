import os
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import issueops.actions as actions
import issueops.tools as tools

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not set")


def _scoped_dsn(url: str, schema: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}options=-csearch_path%3D{schema}"


@pytest.fixture
def dsn():
    import psycopg

    schema = "it_" + uuid.uuid4().hex[:12]
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    scoped = _scoped_dsn(DATABASE_URL, schema)
    with psycopg.connect(scoped, autocommit=True) as conn:
        conn.execute(SCHEMA_PATH.read_text())
        conn.execute("INSERT INTO repo_allowlist (repo, active) VALUES ('owner/repo', true)")
    yield scoped
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _read_client():
    client = MagicMock()
    client.get_issue.return_value = {
        "state": "open", "labels": [], "assignees": [], "title": "t", "body": "b", "comments_detail": [],
    }
    client.get_issue_comments.return_value = []
    return client


def _insert_pending(dsn, tool_name="propose_add_comment", arguments=None):
    from psycopg.types.json import Jsonb

    with tools.sync_connection(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO pending_actions
                (tool_name, repo, issue_number, arguments, issue_state_snapshot, requested_by)
            VALUES (%s, 'owner/repo', 7, %s, %s, 'agent:test')
            RETURNING id
            """,
            (
                tool_name,
                Jsonb(arguments or {"body": "hello"}),
                Jsonb({"state": "open", "labels": [], "assignees": []}),
            ),
        ).fetchone()
    return str(row["id"])


def _status(dsn, action_id):
    with tools.sync_connection(dsn) as conn:
        return conn.execute("SELECT status FROM pending_actions WHERE id = %s", (action_id,)).fetchone()["status"]


def test_two_concurrent_approvals_execute_the_action_exactly_once(dsn):
    action_id = _insert_pending(dsn)
    write_client = MagicMock()
    write_client.add_comment.side_effect = lambda *a, **k: time.sleep(0.3)
    read_client = _read_client()
    barrier = threading.Barrier(2)
    results = []

    def approve(name):
        barrier.wait()
        with tools.sync_connection(dsn) as conn:
            results.append(actions.approve_action(conn, read_client, write_client, action_id, name))

    threads = [threading.Thread(target=approve, args=(n,)) for n in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(r["status"] for r in results) == ["executed", "not_found_or_not_pending"]
    assert write_client.add_comment.call_count == 1
    assert _status(dsn, action_id) == "executed"


def test_a_lease_reclaimed_during_the_github_call_is_reported_not_recorded_as_success(dsn):
    action_id = _insert_pending()
    read_client = _read_client()
    write_client = MagicMock()

    def reclaim_while_the_call_is_in_flight(*args, **kwargs):
        with tools.sync_connection(dsn) as other:
            actions.recover_stuck_approving(other, minutes=0)
            row, lease, early = actions._claim_pending_action(other, action_id, "bob", 48)
            assert early is None

    write_client.add_comment.side_effect = reclaim_while_the_call_is_in_flight

    with tools.sync_connection(dsn) as conn:
        result = actions.approve_action(conn, read_client, write_client, action_id, "alice")

    assert result["status"] == "lost_lease_after_execution"
    assert _status(dsn, action_id) == "approving"


def test_concurrent_identical_proposals_produce_a_single_pending_row(dsn):
    read_client = _read_client()
    issue = read_client.get_issue.return_value
    barrier = threading.Barrier(4)
    ids = []

    def propose():
        barrier.wait()
        result = tools.propose_close(dsn, read_client, "owner/repo", 7, "completed", "agent:test", issue=issue)
        ids.append(result["id"])

    threads = [threading.Thread(target=propose) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with tools.sync_connection(dsn) as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM pending_actions WHERE tool_name = 'propose_close' AND status = 'pending'"
        ).fetchone()["n"]

    assert count == 1
    assert len({str(i) for i in ids}) == 1


def test_the_per_issue_cap_is_enforced_against_real_rows(dsn, monkeypatch):
    monkeypatch.setenv("MAX_PENDING_PER_ISSUE", "2")
    read_client = _read_client()
    issue = read_client.get_issue.return_value

    tools.propose_add_comment(dsn, read_client, "owner/repo", 7, "one", "agent:test", issue=issue)
    tools.propose_add_comment(dsn, read_client, "owner/repo", 7, "two", "agent:test", issue=issue)

    with pytest.raises(tools.ValidationError, match="limit 2"):
        tools.propose_add_comment(dsn, read_client, "owner/repo", 7, "three", "agent:test", issue=issue)


def test_only_agent_rows_in_handled_statuses_count_as_handled_issues(dsn):
    pending_id = _insert_pending(dsn)
    with tools.sync_connection(dsn) as conn:
        conn.execute("UPDATE pending_actions SET status = 'expired' WHERE id = %s", (pending_id,))
    assert tools.list_handled_issue_numbers(dsn, "owner/repo") == set()

    with tools.sync_connection(dsn) as conn:
        conn.execute("UPDATE pending_actions SET status = 'rejected' WHERE id = %s", (pending_id,))
    assert tools.list_handled_issue_numbers(dsn, "owner/repo") == {7}


def test_the_schema_rejects_an_unknown_status(dsn):
    import psycopg

    action_id = _insert_pending(dsn)
    with tools.sync_connection(dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE pending_actions SET status = 'bogus' WHERE id = %s", (action_id,))
