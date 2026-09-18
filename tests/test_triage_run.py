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
