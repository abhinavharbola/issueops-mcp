from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import issueops.tools as tools
from conftest import FakeConn, sync_connection_returning
from issueops.github_client import PaginationLimitExceededError


def test_issue_plaintext_joins_title_body_and_comments():
    issue = {
        "title": "crash on save",
        "body": "steps to reproduce",
        "comments_detail": [{"body": "can confirm"}, {"body": "me too"}],
    }

    text = tools.issue_plaintext(issue)

    assert "crash on save" in text
    assert "steps to reproduce" in text
    assert "can confirm" in text
    assert "me too" in text


def test_issue_plaintext_handles_a_missing_body_and_no_comments():
    issue = {"title": "t"}

    text = tools.issue_plaintext(issue)

    assert text.startswith("t")


def _use_conn(monkeypatch, conn=None):
    conn = conn or FakeConn()
    monkeypatch.setattr(tools, "sync_connection", sync_connection_returning(conn))
    return conn


@pytest.mark.parametrize("query", [
    "repo:other/private secret",
    "secret REPO:other/private",
    "org:someorg password",
    "user:someone token",
    "-repo:owner/repo bug",
    "(repo:other/private)",
    "owner:someone bug",
])
def test_search_issues_rejects_scope_qualifiers(monkeypatch, query):
    _use_conn(monkeypatch)
    read_client = MagicMock()

    with pytest.raises(tools.ValidationError, match="qualifiers"):
        tools.search_issues("dsn", read_client, "owner/repo", query, "test")

    read_client.search_issues.assert_not_called()


def test_search_issues_allows_ordinary_qualifiers(monkeypatch):
    _use_conn(monkeypatch)
    read_client = MagicMock()
    read_client.search_issues.return_value = {"items": []}

    tools.search_issues("dsn", read_client, "owner/repo", "is:open label:bug crash on save", "test")

    read_client.search_issues.assert_called_once_with("owner/repo", "is:open label:bug crash on save")


def test_a_rejected_search_is_recorded_in_the_audit_log(monkeypatch):
    conn = _use_conn(monkeypatch)

    with pytest.raises(tools.ValidationError):
        tools.search_issues("dsn", MagicMock(), "owner/repo", "repo:other/x y", "test")

    audit_inserts = [q for q in conn.queries if "INSERT INTO audit_log" in q[0]]
    assert audit_inserts and audit_inserts[-1][1][6] == "error"


@pytest.mark.parametrize("days", [0, -1, 366, "7", True])
def test_activity_summary_rejects_out_of_range_windows(monkeypatch, days):
    _use_conn(monkeypatch)

    with pytest.raises(tools.ValidationError, match="days"):
        tools.get_repo_activity_summary("dsn", MagicMock(), "owner/repo", days, "test")


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_activity_summary_counts_distinct_issues_with_comments_in_the_window(monkeypatch):
    _use_conn(monkeypatch)
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(days=1))
    old = _iso(now - timedelta(days=30))
    read_client = MagicMock()
    read_client.iter_issue_pages.return_value = [[
        {"number": 1, "created_at": recent, "closed_at": None, "labels": [{"name": "bug"}]},
        {"number": 2, "created_at": old, "closed_at": recent, "labels": []},
        {"number": 3, "created_at": recent, "closed_at": None, "labels": [], "pull_request": {}},
    ]]
    base = "https://api.github.com/repos/owner/repo/issues/"
    read_client.iter_repo_comment_pages.return_value = [[
        {"created_at": recent, "issue_url": base + "1"},
        {"created_at": recent, "issue_url": base + "1"},
        {"created_at": recent, "issue_url": base + "2"},
        {"created_at": recent, "issue_url": base + "3"},
        {"created_at": old, "issue_url": base + "1"},
        {"created_at": recent, "issue_url": base + "999"},
    ]]

    result = tools.get_repo_activity_summary("dsn", read_client, "owner/repo", 7, "test")

    assert result == {"opened": 1, "closed": 1, "commented": 2, "by_label": {"bug": 1}}


def _pages_then_limit(pages):
    def generator():
        for page in pages:
            yield page
        raise PaginationLimitExceededError("/x", 1)

    return generator()


def test_activity_summary_returns_partial_counts_flagged_as_truncated_instead_of_raising(monkeypatch):
    _use_conn(monkeypatch)
    recent = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    read_client = MagicMock()
    read_client.iter_issue_pages.return_value = _pages_then_limit(
        [[{"number": 1, "created_at": recent, "closed_at": None, "labels": []}]]
    )
    read_client.iter_repo_comment_pages.return_value = [[]]

    result = tools.get_repo_activity_summary("dsn", read_client, "owner/repo", 7, "test")

    assert result["opened"] == 1
    assert result["truncated"] is True
    assert "lower bounds" in result["note"]


def test_issue_plaintext_tolerates_null_comment_bodies_and_null_comment_lists():
    assert "t" in tools.issue_plaintext({"title": "t", "comments_detail": [{"body": None}]})
    assert "t" in tools.issue_plaintext({"title": "t", "comments_detail": None})


def test_list_issue_candidates_skips_pull_requests_and_excluded_numbers(monkeypatch):
    _use_conn(monkeypatch)
    read_client = MagicMock()
    read_client.iter_issue_pages.return_value = [
        [{"number": 1, "pull_request": {}}, {"number": 2}, {"number": 3}],
        [{"number": 4}],
    ]

    result = tools.list_issue_candidates("dsn", read_client, "Owner/Repo", "test", exclude={3})

    assert [i["number"] for i in result["issues"]] == [2, 4]
    assert result["skipped"] == 1
    assert result["truncated"] is False
    assert read_client.iter_issue_pages.call_args.args[0] == "owner/repo"


def test_list_issue_candidates_stops_as_soon_as_the_limit_is_reached(monkeypatch):
    _use_conn(monkeypatch)
    consumed = []

    def pages():
        for number in range(1, 6):
            consumed.append(number)
            yield [{"number": number}]

    read_client = MagicMock()
    read_client.iter_issue_pages.return_value = pages()

    result = tools.list_issue_candidates("dsn", read_client, "owner/repo", "test", limit=2)

    assert [i["number"] for i in result["issues"]] == [1, 2]
    assert consumed == [1, 2]


def test_list_issue_candidates_returns_what_it_has_when_the_page_limit_is_hit(monkeypatch):
    _use_conn(monkeypatch)
    read_client = MagicMock()
    read_client.iter_issue_pages.return_value = _pages_then_limit([[{"number": 1}, {"number": 2}]])

    result = tools.list_issue_candidates("dsn", read_client, "owner/repo", "test", max_pages=1)

    assert [i["number"] for i in result["issues"]] == [1, 2]
    assert result["truncated"] is True


def test_list_issue_candidates_is_recorded_in_the_audit_log(monkeypatch):
    conn = _use_conn(monkeypatch)
    read_client = MagicMock()
    read_client.iter_issue_pages.return_value = [[]]

    tools.list_issue_candidates("dsn", read_client, "owner/repo", "test")

    audit_inserts = [q for q in conn.queries if "INSERT INTO audit_log" in q[0]]
    assert audit_inserts and audit_inserts[-1][1][6] == "ok"


def test_read_tools_normalize_the_repo_name_before_the_allowlist_check(monkeypatch):
    conn = _use_conn(monkeypatch)
    read_client = MagicMock()
    read_client.get_issue.return_value = {"number": 1}

    tools.get_issue("dsn", read_client, " Owner/Repo ", 1, "test")

    allowlist_checks = [q for q in conn.queries if "FROM repo_allowlist" in q[0]]
    assert allowlist_checks[0][1] == ("owner/repo",)
    read_client.get_issue.assert_called_once_with("owner/repo", 1, include_comments=True)


def test_snapshot_issue_state_does_not_fetch_comments():
    read_client = MagicMock()
    read_client.get_issue.return_value = {"state": "open", "labels": [], "assignees": [], "title": "t", "body": "b"}

    snapshot = tools.snapshot_issue_state(read_client, "owner/repo", 1)

    read_client.get_issue.assert_called_once_with("owner/repo", 1, include_comments=False)
    assert snapshot["content_hash"] == tools.content_hash("t", "b")


def test_content_hash_changes_when_the_body_changes_and_tolerates_none():
    assert tools.content_hash("t", "a") != tools.content_hash("t", "b")
    assert tools.content_hash(None, None) == tools.content_hash("", "")


def test_list_handled_issue_numbers_returns_a_set_of_numbers(monkeypatch):
    conn = _use_conn(monkeypatch, FakeConn(handled_issue_numbers=[3, 5, 3]))

    assert tools.list_handled_issue_numbers("dsn", "owner/repo") == {3, 5}
    query, params = conn.queries[-1]
    assert params == ("owner/repo", "agent:%")
    assert "'rejected'" in query and "'executed'" in query and "'expired'" not in query


def test_get_issue_can_skip_comments(monkeypatch):
    _use_conn(monkeypatch)
    read_client = MagicMock()

    tools.get_issue("dsn", read_client, "owner/repo", 4, "test", include_comments=False)

    read_client.get_issue.assert_called_once_with("owner/repo", 4, include_comments=False)
