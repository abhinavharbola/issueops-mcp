import json

from issueops import projection


def _raw_issue(**overrides):
    issue = {
        "number": 12,
        "title": "crash on save",
        "state": "open",
        "state_reason": None,
        "user": {"login": "reporter", "id": 1, "node_id": "x", "avatar_url": "https://example/avatar"},
        "labels": [{"id": 5, "name": "bug", "color": "d73a4a", "description": "d", "url": "u"}],
        "assignees": [{"login": "octocat", "id": 2, "node_id": "y"}],
        "comments": 3,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": None,
        "html_url": "https://github.com/o/r/issues/12",
        "body": "steps to reproduce",
        "reactions": {"total_count": 9},
        "timeline_url": "https://api.github.com/x",
        "performed_via_github_app": None,
    }
    issue.update(overrides)
    return issue


def test_summarize_issue_keeps_only_the_fields_a_triager_needs():
    summary = projection.summarize_issue(_raw_issue())

    assert summary == {
        "number": 12,
        "title": "crash on save",
        "state": "open",
        "state_reason": None,
        "is_pull_request": False,
        "author": "reporter",
        "labels": ["bug"],
        "assignees": ["octocat"],
        "comment_count": 3,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": None,
        "html_url": "https://github.com/o/r/issues/12",
        "body_excerpt": "steps to reproduce",
    }


def test_summarize_issue_marks_pull_requests():
    assert projection.summarize_issue(_raw_issue(pull_request={"url": "u"}))["is_pull_request"] is True


def test_summarize_issue_clips_the_body_and_says_how_much_was_cut():
    body = "x" * (projection.LIST_BODY_CHARS + 40)

    excerpt = projection.summarize_issue(_raw_issue(body=body))["body_excerpt"]

    assert excerpt.startswith("x" * projection.LIST_BODY_CHARS)
    assert excerpt.endswith("[truncated 40 chars]")


def test_summarize_issue_tolerates_missing_and_null_fields():
    summary = projection.summarize_issue({"number": 1, "title": None, "body": None, "user": None, "labels": None})

    assert summary["title"] == ""
    assert summary["body_excerpt"] == ""
    assert summary["author"] is None
    assert summary["labels"] == []
    assert summary["assignees"] == []


def test_summarize_issue_accepts_labels_given_as_plain_strings():
    assert projection.summarize_issue(_raw_issue(labels=["bug", "help wanted"]))["labels"] == ["bug", "help wanted"]


def test_a_list_of_summaries_is_far_smaller_than_the_raw_list():
    raw = [_raw_issue(number=n, body="b" * 4000) for n in range(100)]
    raw_size = len(json.dumps(raw))

    presented = projection.present_issue_list({"issues": raw, "truncated": False})

    assert presented["count"] == 100
    assert len(json.dumps(presented)) < raw_size / 3


def test_present_issue_list_carries_the_truncated_flag():
    assert projection.present_issue_list({"issues": [], "truncated": True}) == {
        "count": 0, "truncated": True, "issues": [],
    }


def test_summarize_pull_request_reports_branches_and_merge_state():
    pull = {
        "number": 7,
        "title": "fix crash",
        "state": "closed",
        "draft": False,
        "user": {"login": "dev"},
        "labels": [{"name": "bug"}],
        "assignees": [],
        "head": {"ref": "fix-crash", "repo": {"huge": "object"}},
        "base": {"ref": "main"},
        "created_at": "a",
        "updated_at": "b",
        "closed_at": "c",
        "merged_at": "d",
        "html_url": "u",
        "body": "details",
        "requested_reviewers": [{"login": "r"}],
    }

    summary = projection.summarize_pull_request(pull)

    assert summary["head"] == "fix-crash"
    assert summary["base"] == "main"
    assert summary["merged_at"] == "d"
    assert summary["author"] == "dev"
    assert "requested_reviewers" not in summary


def test_present_pull_request_list_wraps_the_summaries():
    presented = projection.present_pull_request_list({"pull_requests": [{"number": 1, "title": "t"}], "truncated": False})

    assert presented["count"] == 1
    assert presented["pull_requests"][0]["number"] == 1
    assert presented["truncated"] is False


def _comment(n, body="c"):
    return {
        "id": n,
        "user": {"login": f"u{n}"},
        "author_association": "NONE",
        "created_at": "t1",
        "updated_at": "t2",
        "body": body,
        "reactions": {"total_count": 1},
    }


def test_present_issue_returns_the_newest_comments_and_counts_the_omitted_ones():
    total = projection.DETAIL_MAX_COMMENTS + 5
    issue = _raw_issue(comments_detail=[_comment(n) for n in range(total)])

    detail = projection.present_issue(issue)

    assert len(detail["comments"]) == projection.DETAIL_MAX_COMMENTS
    assert detail["comments"][-1]["id"] == total - 1
    assert detail["comments"][0]["id"] == 5
    assert detail["comments_omitted"] == 5


def test_present_issue_clips_long_comments_and_the_body():
    issue = _raw_issue(
        body="b" * (projection.DETAIL_BODY_CHARS + 10),
        comments_detail=[_comment(1, "c" * (projection.DETAIL_COMMENT_CHARS + 10))],
    )

    detail = projection.present_issue(issue)

    assert detail["body"].endswith("[truncated 10 chars]")
    assert detail["comments"][0]["body"].endswith("[truncated 10 chars]")
    assert "body_excerpt" not in detail


def test_present_issue_keeps_the_fetchers_truncation_flag_and_handles_no_comments():
    assert projection.present_issue(_raw_issue(comments_truncated=True))["comments_truncated_by_fetcher"] is True

    detail = projection.present_issue(_raw_issue(comments_detail=None))

    assert detail["comments"] == []
    assert detail["comments_omitted"] == 0
    assert detail["comments_truncated_by_fetcher"] is False


def test_present_issue_has_a_bounded_worst_case_size():
    issue = _raw_issue(
        body="b" * 100000,
        comments_detail=[_comment(n, "c" * 100000) for n in range(2000)],
    )

    size = len(json.dumps(projection.present_issue(issue)))

    limit = projection.DETAIL_BODY_CHARS + projection.DETAIL_MAX_COMMENTS * (projection.DETAIL_COMMENT_CHARS + 300) + 2000
    assert size < limit


def test_present_search_summarizes_items_and_keeps_the_filter_note():
    result = {
        "total_count": 40,
        "incomplete_results": False,
        "items": [_raw_issue(number=1), _raw_issue(number=2)],
        "filtered_out_other_repos": 3,
    }

    presented = projection.present_search(result)

    assert presented["count"] == 2
    assert presented["total_count"] == 40
    assert presented["filtered_out_other_repos"] == 3
    assert presented["items"][0]["labels"] == ["bug"]
    assert "reactions" not in presented["items"][0]


def test_present_search_omits_the_filter_note_when_nothing_was_filtered():
    assert "filtered_out_other_repos" not in projection.present_search({"total_count": 0, "items": []})
