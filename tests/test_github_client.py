from unittest.mock import MagicMock

import pytest

from issueops.github_client import GitHubAPIError, GitHubReadClient, GitHubWriteClient


def _mock_response(status_code, json_data=None, text=""):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = json_data
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
