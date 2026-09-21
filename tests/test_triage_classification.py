from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agent.triage as triage
from agent.triage import (
    ClassificationError,
    _parse_classification,
    _plan_from_classification,
    _sanitize_classification,
    classify_issue,
)


def test_sanitize_keeps_a_fully_valid_classification():
    data = {
        "labels_to_add": ["bug"],
        "comment": "thanks",
        "close_reason": "completed",
        "assign_to": "octocat",
        "rationale": "clear bug report",
    }
    assert _sanitize_classification(data) == data


def test_sanitize_drops_only_the_malformed_field():
    data = {
        "labels_to_add": ["bug"],
        "comment": "thanks",
        "close_reason": "not-a-real-reason",
        "assign_to": None,
        "rationale": "ok",
    }
    result = _sanitize_classification(data)
    assert result["labels_to_add"] == ["bug"]
    assert result["comment"] == "thanks"
    assert result["close_reason"] is None
    assert "dropped malformed field(s)" in result["rationale"]
    assert "close_reason" in result["rationale"]


def test_sanitize_rejects_non_dict_outright():
    with pytest.raises(ValueError):
        _sanitize_classification(["not", "a", "dict"])


def test_sanitize_drops_labels_that_are_not_a_list_of_strings():
    data = {
        "labels_to_add": "bug",
        "comment": None,
        "close_reason": None,
        "assign_to": None,
        "rationale": None,
    }
    result = _sanitize_classification(data)
    assert result["labels_to_add"] == []
    assert "labels_to_add" in result["rationale"]


def test_parse_classification_strips_markdown_code_fence():
    raw = (
        "```json\n"
        '{"labels_to_add": ["bug"], "comment": null, "close_reason": null, '
        '"assign_to": null, "rationale": "r"}\n'
        "```"
    )
    result = _parse_classification(raw)
    assert result["labels_to_add"] == ["bug"]


def test_parse_classification_recovers_an_object_wrapped_in_prose():
    raw = (
        'Here is the classification: {"labels_to_add": ["bug"], "comment": null, '
        '"close_reason": null, "assign_to": null, "rationale": "r"} Hope that helps.'
    )
    assert _parse_classification(raw)["labels_to_add"] == ["bug"]


@pytest.mark.parametrize("raw", ["no json here at all", "{ not valid json }", "[1, 2, 3]", "} reversed {"])
def test_parse_classification_rejects_output_with_no_usable_object(raw):
    with pytest.raises(ValueError):
        _parse_classification(raw)


def _response(content, choices=True):
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)] if choices else [])


@pytest.mark.parametrize(
    "response, expected",
    [
        (_response(None), "empty model output"),
        (_response(""), "empty model output"),
        (_response("I cannot help with that"), "unparseable model output"),
        (_response("", choices=False), "no choices"),
    ],
)
def test_classify_issue_raises_instead_of_reporting_no_action_on_unusable_output(monkeypatch, response, expected):
    monkeypatch.setattr(triage, "_create_completion", MagicMock(return_value=response))

    with pytest.raises(ClassificationError, match=expected):
        classify_issue(["client"], "model", {"title": "t", "body": "b", "state": "open"})


def test_classify_issue_returns_a_valid_classification(monkeypatch):
    raw = '{"labels_to_add": ["bug"], "comment": null, "close_reason": null, "assign_to": null, "rationale": "r"}'
    monkeypatch.setattr(triage, "_create_completion", MagicMock(return_value=_response(raw)))

    result = classify_issue(["client"], "model", {"title": "t", "body": "b", "state": "open"})

    assert result["labels_to_add"] == ["bug"]


def test_plan_from_classification_produces_one_action_per_field():
    classification = {
        "labels_to_add": ["bug"],
        "comment": "thanks for reporting",
        "close_reason": "completed",
        "assign_to": "octocat",
    }
    plan = _plan_from_classification(classification, "owner/repo", 1, allow_comment=True, allow_close=True)
    tool_names = [name for name, _ in plan]
    assert tool_names == [
        "propose_add_labels",
        "propose_add_comment",
        "propose_close",
        "propose_assign",
    ]


def test_plan_from_classification_never_comments_or_closes_by_default():
    classification = {
        "labels_to_add": ["bug"],
        "comment": "visit http://evil.example",
        "close_reason": "completed",
        "assign_to": "octocat",
    }
    plan = _plan_from_classification(classification, "owner/repo", 1)
    assert [name for name, _ in plan] == ["propose_add_labels", "propose_assign"]


def test_plan_from_classification_allows_comment_and_close_independently():
    classification = {"labels_to_add": [], "comment": "hi", "close_reason": "completed", "assign_to": None}
    only_comment = _plan_from_classification(classification, "owner/repo", 1, allow_comment=True)
    only_close = _plan_from_classification(classification, "owner/repo", 1, allow_close=True)
    assert [name for name, _ in only_comment] == ["propose_add_comment"]
    assert [name for name, _ in only_close] == ["propose_close"]


def test_plan_from_classification_skips_empty_fields():
    classification = {
        "labels_to_add": [],
        "comment": None,
        "close_reason": None,
        "assign_to": None,
    }
    assert _plan_from_classification(classification, "owner/repo", 1) == []
