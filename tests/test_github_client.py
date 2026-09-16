from unittest.mock import MagicMock

import pytest

from issueops.github_client import GitHubAPIError, GitHubReadClient, GitHubWriteClient


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



