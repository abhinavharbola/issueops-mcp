import requests

GITHUB_API_BASE = "https://api.github.com"


class GitHubAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"GitHub API error {status_code}: {message}")


class _BaseClient:
    def __init__(self, token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _request(self, method: str, path: str, **kwargs):
        response = self._session.request(method, f"{GITHUB_API_BASE}{path}", timeout=15, **kwargs)
        if response.status_code >= 400:
            raise GitHubAPIError(response.status_code, response.text)
        if response.status_code == 204:
            return None
        return response.json()


class GitHubReadClient(_BaseClient):
    def list_issues(self, repo: str, state: str = "open", labels: list[str] | None = None, since: str | None = None):
        params = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = since
        return self._request("GET", f"/repos/{repo}/issues", params=params)

    def get_issue(self, repo: str, issue_number: int):
        issue = self._request("GET", f"/repos/{repo}/issues/{issue_number}")
        comments = self._request("GET", f"/repos/{repo}/issues/{issue_number}/comments", params={"per_page": 100})
        issue["comments_detail"] = comments
        return issue

    def list_pull_requests(self, repo: str, state: str = "open"):
        return self._request("GET", f"/repos/{repo}/pulls", params={"state": state, "per_page": 100})

    def search_issues(self, repo: str, query: str):
        full_query = f"repo:{repo} {query}"
        return self._request("GET", "/search/issues", params={"q": full_query, "per_page": 100})

    def get_repo_labels(self, repo: str):
        return self._request("GET", f"/repos/{repo}/labels", params={"per_page": 100})

    def get_repo_collaborators(self, repo: str):
        return self._request("GET", f"/repos/{repo}/collaborators", params={"per_page": 100})


class GitHubWriteClient(_BaseClient):
    def add_comment(self, repo: str, issue_number: int, body: str):
        return self._request("POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body})

    def add_labels(self, repo: str, issue_number: int, labels: list[str]):
        return self._request("POST", f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": labels})

    def remove_label(self, repo: str, issue_number: int, label: str):
        return self._request("DELETE", f"/repos/{repo}/issues/{issue_number}/labels/{label}")

    def assign(self, repo: str, issue_number: int, assignee: str):
        return self._request("POST", f"/repos/{repo}/issues/{issue_number}/assignees", json={"assignees": [assignee]})

    def close(self, repo: str, issue_number: int, reason: str | None = None):
        payload = {"state": "closed"}
        if reason:
            payload["state_reason"] = reason
        return self._request("PATCH", f"/repos/{repo}/issues/{issue_number}", json=payload)
