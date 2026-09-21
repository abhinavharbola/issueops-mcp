from unittest.mock import MagicMock

import pytest

from issueops.github_client import (
    GitHubAPIError,
    GitHubReadClient,
    GitHubWriteClient,
    PaginationLimitExceededError,
)


def _mock_response(status_code, json_data=None, text="", headers=None):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = json_data
    response.headers = headers if headers is not None else {}
    return response


def test_get_issue_merges_comments_into_the_issue():
    client = GitHubReadClient("fake-pat")
    issue_response = _mock_response(200, json_data={"number": 1, "title": "t"})
    comments_response = _mock_response(200, json_data=[{"body": "hi"}])
    client._session.request = MagicMock(side_effect=[issue_response, comments_response])

    issue = client.get_issue("owner/repo", 1)

    assert issue["comments_detail"] == [{"body": "hi"}]


def test_error_status_raises_github_api_error_with_the_status_code():
    client = GitHubReadClient("fake-pat")
    client._session.request = MagicMock(return_value=_mock_response(404, text="Not Found"))

    with pytest.raises(GitHubAPIError) as exc_info:
        client.get_repo_labels("owner/repo")

    assert exc_info.value.status_code == 404


def test_204_response_returns_none():
    client = GitHubWriteClient("fake-write-pat")
    client._session.request = MagicMock(return_value=_mock_response(204))

    result = client.remove_label("owner/repo", 1, "bug")

    assert result is None


def test_add_comment_sends_body_as_json_payload():
    client = GitHubWriteClient("fake-write-pat")
    mock_request = MagicMock(return_value=_mock_response(201, json_data={"id": 1}))
    client._session.request = mock_request

    client.add_comment("owner/repo", 5, "looks good")

    _, kwargs = mock_request.call_args
    assert kwargs["json"] == {"body": "looks good"}


def test_remove_label_url_encodes_a_label_with_a_space():
    client = GitHubWriteClient("fake-write-pat")
    mock_request = MagicMock(return_value=_mock_response(204))
    client._session.request = mock_request

    client.remove_label("owner/repo", 5, "good first issue")

    args, _ = mock_request.call_args
    url = args[1]
    assert "good%20first%20issue" in url
    assert " " not in url


def test_remove_label_url_encodes_a_label_with_a_slash():
    client = GitHubWriteClient("fake-write-pat")
    mock_request = MagicMock(return_value=_mock_response(204))
    client._session.request = mock_request

    client.remove_label("owner/repo", 5, "area/backend")

    args, _ = mock_request.call_args
    url = args[1]
    assert url.endswith("/labels/area%2Fbackend")


def test_list_issues_follows_link_header_pagination():
    client = GitHubReadClient("fake-pat")
    page1 = _mock_response(
        200,
        json_data=[{"number": 1}],
        headers={"Link": '<https://api.github.com/repos/owner/repo/issues?page=2>; rel="next"'},
    )
    page2 = _mock_response(200, json_data=[{"number": 2}], headers={})
    client._session.request = MagicMock(side_effect=[page1, page2])

    issues = client.list_issues("owner/repo")

    assert [i["number"] for i in issues] == [1, 2]


def test_list_issues_stops_when_there_is_no_next_link():
    client = GitHubReadClient("fake-pat")
    page1 = _mock_response(200, json_data=[{"number": 1}], headers={})
    client._session.request = MagicMock(return_value=page1)

    issues = client.list_issues("owner/repo")

    assert [i["number"] for i in issues] == [1]
    assert client._session.request.call_count == 1


def test_paginated_get_raises_instead_of_silently_truncating():
    client = GitHubReadClient("fake-pat")
    always_next = _mock_response(
        200,
        json_data=[{"number": 1}],
        headers={"Link": '<https://api.github.com/repos/owner/repo/issues?page=2>; rel="next"'},
    )
    client._session.request = MagicMock(return_value=always_next)

    with pytest.raises(PaginationLimitExceededError) as exc_info:
        client._paginated_get("/repos/owner/repo/issues", {}, max_pages=3)

    assert client._session.request.call_count == 3
    assert "3 pages" in str(exc_info.value)


def test_paginated_get_rejects_an_unexpected_response_shape():
    client = GitHubReadClient("fake-pat")
    client._session.request = MagicMock(return_value=_mock_response(200, json_data={"unexpected": "shape"}))

    with pytest.raises(GitHubAPIError):
        client._paginated_get("/repos/owner/repo/labels", {})


def test_get_issue_without_comments_makes_a_single_request():
    client = GitHubReadClient("fake-pat")
    client._session.request = MagicMock(return_value=_mock_response(200, json_data={"number": 1}))

    issue = client.get_issue("owner/repo", 1, include_comments=False)

    assert "comments_detail" not in issue
    assert client._session.request.call_count == 1


def test_search_issues_drops_results_from_other_repositories():
    client = GitHubReadClient("fake-pat")
    payload = {
        "total_count": 3,
        "items": [
            {"number": 1, "repository_url": "https://api.github.com/repos/owner/repo"},
            {"number": 2, "repository_url": "https://api.github.com/repos/other/private"},
            {"number": 3, "repository_url": "https://api.github.com/repos/Owner/Repo"},
        ],
    }
    client._session.request = MagicMock(return_value=_mock_response(200, json_data=payload))

    result = client.search_issues("owner/repo", "is:open")

    assert [i["number"] for i in result["items"]] == [1, 3]
    assert result["filtered_out_other_repos"] == 1
    assert client._session.request.call_args.kwargs["params"]["q"] == "repo:owner/repo is:open"


def test_search_issues_does_not_annotate_when_nothing_was_filtered():
    client = GitHubReadClient("fake-pat")
    payload = {"total_count": 1, "items": [{"number": 1, "repository_url": "https://api.github.com/repos/owner/repo"}]}
    client._session.request = MagicMock(return_value=_mock_response(200, json_data=payload))

    result = client.search_issues("owner/repo", "bug")

    assert "filtered_out_other_repos" not in result


def test_get_repo_assignees_uses_the_assignees_endpoint():
    client = GitHubReadClient("fake-pat")
    client._session.request = MagicMock(return_value=_mock_response(200, json_data=[{"login": "octocat"}]))

    result = client.get_repo_assignees("owner/repo")

    assert result == [{"login": "octocat"}]
    assert client._session.request.call_args.args[1].endswith("/repos/owner/repo/assignees")


def test_list_issues_passes_max_pages_to_the_pagination_guard():
    client = GitHubReadClient("fake-pat")
    first = _mock_response(200, json_data=[{"number": 1}], headers={"Link": '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'})
    client._session.request = MagicMock(return_value=first)

    with pytest.raises(PaginationLimitExceededError):
        client.list_issues("o/r", max_pages=1)


def test_link_header_with_a_comma_inside_the_url_still_paginates():
    client = GitHubReadClient("fake-pat")
    page1 = _mock_response(
        200,
        json_data=[{"number": 1}],
        headers={"Link": '<https://api.github.com/repos/o/r/issues?labels=a,b&page=2>; rel="next", <https://api.github.com/repos/o/r/issues?page=9>; rel="last"'},
    )
    page2 = _mock_response(200, json_data=[{"number": 2}], headers={})
    client._session.request = MagicMock(side_effect=[page1, page2])

    issues = client.list_issues("o/r")

    assert [i["number"] for i in issues] == [1, 2]
    assert client._session.request.call_args_list[1].args[1].endswith("labels=a,b&page=2")


def test_a_pagination_link_pointing_outside_the_api_is_rejected():
    client = GitHubReadClient("fake-pat")
    page1 = _mock_response(
        200, json_data=[{"number": 1}], headers={"Link": '<https://evil.example/x?page=2>; rel="next"'},
    )
    client._session.request = MagicMock(return_value=page1)

    with pytest.raises(GitHubAPIError):
        client.list_issues("o/r")


def test_iter_issue_pages_yields_page_by_page_and_stops_early_without_extra_requests():
    client = GitHubReadClient("fake-pat")
    page1 = _mock_response(
        200, json_data=[{"number": 1}],
        headers={"Link": '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'},
    )
    client._session.request = MagicMock(return_value=page1)

    pages = client.iter_issue_pages("o/r", max_pages=5)
    first = next(pages)
    pages.close()

    assert first == [{"number": 1}]
    assert client._session.request.call_count == 1


def test_iter_issue_pages_raises_only_after_the_last_allowed_page_was_yielded():
    client = GitHubReadClient("fake-pat")
    always_next = _mock_response(
        200, json_data=[{"number": 1}],
        headers={"Link": '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'},
    )
    client._session.request = MagicMock(return_value=always_next)
    seen = []

    with pytest.raises(PaginationLimitExceededError):
        for page in client.iter_issue_pages("o/r", max_pages=2):
            seen.append(page)

    assert len(seen) == 2


def test_get_issue_returns_partial_comments_flagged_when_the_comment_pages_exceed_the_limit():
    client = GitHubReadClient("fake-pat")
    issue_response = _mock_response(200, json_data={"number": 1})
    comment_page = _mock_response(
        200, json_data=[{"body": "c"}],
        headers={"Link": '<https://api.github.com/repos/o/r/issues/1/comments?page=2>; rel="next"'},
    )
    client._session.request = MagicMock(side_effect=[issue_response] + [comment_page] * 25)

    issue = client.get_issue("o/r", 1)

    assert len(issue["comments_detail"]) == 20
    assert issue["comments_truncated"] is True


def test_get_issue_comments_partial_mode_returns_what_was_read_instead_of_raising():
    client = GitHubReadClient("fake-pat")
    comment_page = _mock_response(
        200, json_data=[{"body": "c"}],
        headers={"Link": '<https://api.github.com/repos/o/r/issues/1/comments?page=2>; rel="next"'},
    )
    client._session.request = MagicMock(return_value=comment_page)

    comments = client.get_issue_comments("o/r", 1, max_pages=2, allow_partial=True)

    assert len(comments) == 2
    with pytest.raises(PaginationLimitExceededError):
        client.get_issue_comments("o/r", 1, max_pages=2)
