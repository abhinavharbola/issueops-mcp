from types import SimpleNamespace
from unittest.mock import MagicMock

import agent.triage as triage


def _fake_config():
    return SimpleNamespace(
        neon_dsn="postgresql://fake",
        github_read_pat="fake-read-pat",
        github_write_pat=None,
        groq_api_key="fake-groq",
        groq_api_key_fallback=None,
        logfire_token=None,
    )


def _patch_common(monkeypatch):
    monkeypatch.setattr(triage, "load_config", lambda require_write_pat=False: _fake_config())
    monkeypatch.setattr(triage, "configure_logfire", lambda *a, **k: None)
    monkeypatch.setattr(triage, "GitHubReadClient", lambda pat: MagicMock())
    monkeypatch.setattr(triage, "build_groq_clients", lambda config: [MagicMock()])


def _empty_classification(groq_clients, model, issue):
    return {
        "labels_to_add": [],
        "comment": None,
        "close_reason": None,
        "assign_to": None,
        "rationale": "no action",
    }


def test_a_failing_issue_does_not_abort_the_batch(monkeypatch):
    _patch_common(monkeypatch)

    monkeypatch.setattr(
        triage.tools,
        "list_issues",
        lambda dsn, rc, repo, initiator, state="open", labels=None, since=None: [
            {"number": 1},
            {"number": 2},
            {"number": 3},
        ],
    )

    def fake_get_issue(dsn, rc, repo, issue_number, initiator):
        if issue_number == 2:
            raise RuntimeError("GitHub API exploded")
        return {"number": issue_number, "title": "t", "body": "b", "comments_detail": []}

    monkeypatch.setattr(triage.tools, "get_issue", fake_get_issue)
    monkeypatch.setattr(triage, "classify_issue", _empty_classification)

    results = triage.run_triage("owner/repo", initiator="test")

    assert [r["issue_number"] for r in results] == [1, 2, 3]
    assert results[0].get("error") is None
    assert "GitHub API exploded" in results[1]["error"]
    assert results[2].get("error") is None
    assert results[2]["classification"] is not None


def test_a_validation_error_on_one_proposal_does_not_abort_the_issue(monkeypatch):
    _patch_common(monkeypatch)

    monkeypatch.setattr(
        triage.tools,
        "list_issues",
        lambda dsn, rc, repo, initiator, state="open", labels=None, since=None: [{"number": 1}],
    )
    monkeypatch.setattr(
        triage.tools,
        "get_issue",
        lambda dsn, rc, repo, issue_number, initiator: {
            "number": 1, "title": "t", "body": "b", "comments_detail": [],
        },
    )
    monkeypatch.setattr(
        triage,
        "classify_issue",
        lambda groq_clients, model, issue: {
            "labels_to_add": [],
            "comment": None,
            "close_reason": "garbage-reason",
            "assign_to": None,
            "rationale": "r",
        },
    )

    def failing_propose_close(dsn, rc, repo, num, args, initiator, flagged):
        raise triage.tools.ValidationError("reason must be one of completed, not_planned")

    monkeypatch.setitem(triage.PROPOSE_DISPATCH, "propose_close", failing_propose_close)

    results = triage.run_triage("owner/repo", initiator="test")

    assert len(results) == 1
    assert results[0]["proposals"][0]["tool_name"] == "propose_close"
    assert "reason must be one of" in results[0]["proposals"][0]["error"]
