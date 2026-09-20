import json
from unittest.mock import MagicMock

import pytest

import agent.triage as triage


def _issue(number, title="t", body="b"):
    return {
        "number": number,
        "title": title,
        "body": body,
        "comments_detail": [],
        "state": "open",
        "labels": [],
        "assignees": [],
    }


def _classification(**overrides):
    base = {
        "labels_to_add": [],
        "comment": None,
        "close_reason": None,
        "assign_to": None,
        "rationale": "r",
    }
    base.update(overrides)
    return base


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(triage, "load_config", MagicMock(return_value=MagicMock(
        neon_dsn="dsn", github_read_pat="pat", logfire_token=None,
        groq_api_key="k", groq_api_key_fallback=None,
    )))
    monkeypatch.setattr(triage, "configure_logfire", MagicMock())
    monkeypatch.setattr(triage, "GitHubReadClient", MagicMock())
    monkeypatch.setattr(triage, "build_groq_clients", MagicMock(return_value=["client"]))
    monkeypatch.setattr(triage.tools, "get_repo_label_names", MagicMock(return_value=["bug", "enhancement", "question"]))
    monkeypatch.setattr(triage.tools, "get_repo_assignable_logins", MagicMock(return_value=["octocat"]))
    monkeypatch.setattr(triage.tools, "list_handled_issue_numbers", MagicMock(return_value=set()))


def test_run_triage_skips_pull_requests(monkeypatch, patched):
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1, "pull_request": {}}]))
    results = triage.run_triage("owner/repo", "test")
    assert results == []


def test_run_triage_records_an_error_when_an_issue_has_no_number(monkeypatch, patched):
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"title": "no number field"}]))
    results = triage.run_triage("owner/repo", "test")
    assert len(results) == 1
    assert results[0]["issue_number"] is None
    assert "ValueError" in results[0]["error"]


def test_run_triage_calls_propose_add_labels_with_the_prefetched_issue(monkeypatch, patched):
    issue = _issue(1)
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1}]))
    monkeypatch.setattr(triage.tools, "get_issue", MagicMock(return_value=issue))
    monkeypatch.setattr(triage, "classify_issue", MagicMock(return_value=_classification(labels_to_add=["bug"])))

    fake_propose = MagicMock(return_value={"id": "p1", "preview": "..."})
    monkeypatch.setitem(triage.PROPOSE_DISPATCH, "propose_add_labels", fake_propose)

    results = triage.run_triage("owner/repo", "test")

    assert results[0]["proposals"][0]["result"]["id"] == "p1"
    fake_propose.assert_called_once_with("dsn", triage.GitHubReadClient.return_value, "owner/repo", 1, {"labels": ["bug"]}, "test", False, issue)


def test_run_triage_proposes_multiple_actions_for_one_issue(monkeypatch, patched):
    issue = _issue(1)
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1}]))
    monkeypatch.setattr(triage.tools, "get_issue", MagicMock(return_value=issue))
    monkeypatch.setattr(triage, "classify_issue", MagicMock(return_value=_classification(labels_to_add=["bug"], comment="hi")))

    fake_add_labels = MagicMock(return_value={"id": "p1", "preview": "..."})
    fake_add_comment = MagicMock(return_value={"id": "p2", "preview": "..."})
    monkeypatch.setitem(triage.PROPOSE_DISPATCH, "propose_add_labels", fake_add_labels)
    monkeypatch.setitem(triage.PROPOSE_DISPATCH, "propose_add_comment", fake_add_comment)

    results = triage.run_triage("owner/repo", "test")

    assert len(results[0]["proposals"]) == 2
    fake_add_labels.assert_called_once()
    fake_add_comment.assert_called_once()


def test_run_triage_records_a_validation_error_without_aborting_the_issue(monkeypatch, patched):
    issue = _issue(1)
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1}]))
    monkeypatch.setattr(triage.tools, "get_issue", MagicMock(return_value=issue))
    monkeypatch.setattr(triage, "classify_issue", MagicMock(return_value=_classification(close_reason="completed")))

    def failing_propose_close(dsn, rc, repo, num, args, initiator, flagged, issue):
        raise triage.tools.ValidationError("bad reason")

    monkeypatch.setitem(triage.PROPOSE_DISPATCH, "propose_close", failing_propose_close)

    results = triage.run_triage("owner/repo", "test")

    assert results[0]["proposals"][0]["error"] == "bad reason"


def test_run_triage_respects_max_issues(monkeypatch, patched):
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1}, {"number": 2}, {"number": 3}]))
    monkeypatch.setattr(triage.tools, "get_issue", MagicMock(side_effect=lambda *a, **k: _issue(a[3])))
    monkeypatch.setattr(triage, "classify_issue", MagicMock(return_value=_classification()))

    results = triage.run_triage("owner/repo", "test", max_issues=2)

    assert len(results) == 2


def test_run_triage_skips_issues_that_already_have_a_handled_agent_proposal(monkeypatch, patched):
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1}, {"number": 2}]))
    monkeypatch.setattr(triage.tools, "list_handled_issue_numbers", MagicMock(return_value={1}))
    get_issue = MagicMock(side_effect=lambda *a, **k: _issue(a[3]))
    monkeypatch.setattr(triage.tools, "get_issue", get_issue)
    monkeypatch.setattr(triage, "classify_issue", MagicMock(return_value=_classification()))

    results = triage.run_triage("owner/repo", "test")

    assert [r["issue_number"] for r in results] == [2]
    assert get_issue.call_count == 1


def test_run_triage_applies_max_issues_after_removing_handled_issues(monkeypatch, patched):
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1}, {"number": 2}, {"number": 3}]))
    monkeypatch.setattr(triage.tools, "list_handled_issue_numbers", MagicMock(return_value={1}))
    monkeypatch.setattr(triage.tools, "get_issue", MagicMock(side_effect=lambda *a, **k: _issue(a[3])))
    monkeypatch.setattr(triage, "classify_issue", MagicMock(return_value=_classification()))

    results = triage.run_triage("owner/repo", "test", max_issues=1)

    assert [r["issue_number"] for r in results] == [2]


def test_run_triage_passes_since_and_max_pages_to_the_issue_listing(monkeypatch, patched):
    list_issues = MagicMock(return_value=[])
    monkeypatch.setattr(triage.tools, "list_issues", list_issues)

    triage.run_triage("owner/repo", "test", since="2026-01-01T00:00:00Z", max_pages=50)

    kwargs = list_issues.call_args.kwargs
    assert kwargs["since"] == "2026-01-01T00:00:00Z"
    assert kwargs["max_pages"] == 50


def test_run_triage_gives_the_classifier_trusted_repo_context(monkeypatch, patched):
    issue = _issue(1)
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1}]))
    monkeypatch.setattr(triage.tools, "get_issue", MagicMock(return_value=issue))
    classify = MagicMock(return_value=_classification())
    monkeypatch.setattr(triage, "classify_issue", classify)

    triage.run_triage("owner/repo", "test")

    args = classify.call_args.args
    assert args[3] == ["bug", "enhancement", "question"]
    assert args[4] == ["octocat"]


def test_run_triage_continues_when_the_assignable_users_fetch_fails(monkeypatch, patched):
    monkeypatch.setattr(
        triage.tools, "get_repo_assignable_logins",
        MagicMock(side_effect=triage.GitHubAPIError(403, "forbidden")),
    )
    monkeypatch.setattr(triage.tools, "list_issues", MagicMock(return_value=[{"number": 1}]))
    monkeypatch.setattr(triage.tools, "get_issue", MagicMock(return_value=_issue(1)))
    classify = MagicMock(return_value=_classification())
    monkeypatch.setattr(triage, "classify_issue", classify)

    results = triage.run_triage("owner/repo", "test")

    assert results[0]["issue_number"] == 1
    assert classify.call_args.args[4] == []


def test_plan_drops_labels_already_on_the_issue():
    issue = _issue(1)
    issue["labels"] = [{"name": "Bug"}]
    plan = triage._plan_from_classification(_classification(labels_to_add=["bug", "enhancement"]), "o/r", 1, issue, ["bug", "enhancement"], None)
    assert plan == [("propose_add_labels", {"labels": ["enhancement"]})]


def test_plan_drops_labels_that_do_not_exist_on_the_repo():
    plan = triage._plan_from_classification(_classification(labels_to_add=["invented"]), "o/r", 1, _issue(1), ["bug"], None)
    assert plan == []


def test_plan_does_not_propose_closing_an_issue_that_is_not_open():
    issue = _issue(1)
    issue["state"] = "closed"
    plan = triage._plan_from_classification(_classification(close_reason="completed"), "o/r", 1, issue, None, None)
    assert plan == []


def test_plan_drops_an_assignee_who_is_not_assignable_or_already_assigned():
    issue = _issue(1)
    issue["assignees"] = [{"login": "octocat"}]
    not_assignable = triage._plan_from_classification(_classification(assign_to="stranger"), "o/r", 1, _issue(1), None, ["octocat"])
    already = triage._plan_from_classification(_classification(assign_to="OctoCat"), "o/r", 1, issue, None, ["octocat"])
    assert not_assignable == []
    assert already == []


def test_sanitize_drops_an_unhashable_close_reason_instead_of_raising():
    result = triage._sanitize_classification({"close_reason": ["completed"]})
    assert result["close_reason"] is None
    assert "close_reason" in result["rationale"]


def test_complete_once_falls_back_when_json_mode_is_rejected():
    client = MagicMock()
    rejected = triage.BadRequestError.__new__(triage.BadRequestError)
    client.chat.completions.create.side_effect = [rejected, "ok"]

    assert triage._complete_once(client, "m", []) == "ok"
    assert client.chat.completions.create.call_count == 2
    assert "response_format" not in client.chat.completions.create.call_args_list[1].kwargs
