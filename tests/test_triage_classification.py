import pytest

from agent.triage import _parse_classification, _plan_from_classification, _sanitize_classification


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


def test_plan_from_classification_produces_one_action_per_field():
    classification = {
        "labels_to_add": ["bug"],
        "comment": "thanks for reporting",
        "close_reason": "completed",
        "assign_to": "octocat",
    }
    plan = _plan_from_classification(classification, "owner/repo", 1)
    tool_names = [name for name, _ in plan]
    assert tool_names == [
        "propose_add_labels",
        "propose_add_comment",
        "propose_close",
        "propose_assign",
    ]


def test_plan_from_classification_skips_empty_fields():
    classification = {
        "labels_to_add": [],
        "comment": None,
        "close_reason": None,
        "assign_to": None,
    }
    assert _plan_from_classification(classification, "owner/repo", 1) == []
