from urllib.parse import quote

import requests

GITHUB_API_BASE = "https://api.github.com"


class GitHubAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"GitHub API error {status_code}: {message}")


class PaginationLimitExceededError(GitHubAPIError):
    def __init__(self, path: str, max_pages: int):
        self.path = path
        self.max_pages = max_pages
        super().__init__(
            0,
            f"{path} has more than {max_pages} pages of results; refusing to silently "
            f"truncate. Narrow the query (state, labels, since) or raise max_pages.",
        )


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

    def _request_raw(self, method: str, path: str, **kwargs):
        response = self._session.request(method, f"{GITHUB_API_BASE}{path}", timeout=15, **kwargs)
        if response.status_code >= 400:
            raise GitHubAPIError(response.status_code, response.text)
        return response

    def _paginated_get(self, path: str, params: dict, max_pages: int = 20):
        results = []
        page_params = dict(params)
        page_params.setdefault("per_page", 100)
        next_path = path
        next_params = page_params
        pages_fetched = 0

        while next_path is not None and pages_fetched < max_pages:
            response = self._request_raw("GET", next_path, params=next_params)
            data = response.json()
            if isinstance(data, dict) and "items" in data:
                results.extend(data["items"])
            elif isinstance(data, list):
                results.extend(data)
            else:
                raise GitHubAPIError(
                    response.status_code,
                    f"unexpected response shape for {next_path}: expected a list or a dict "
                    f"with an 'items' key, got {type(data).__name__}",
                )
            pages_fetched += 1

            next_path = None
            next_params = None
            link_header = response.headers.get("Link")
            if link_header:
                for part in link_header.split(","):
                    segment = part.strip()
                    if 'rel="next"' not in segment:
                        continue
                    start = segment.find("<")
                    end = segment.find(">")
                    if start == -1 or end == -1:
                        continue
                    next_url = segment[start + 1:end]
                    next_path = next_url[len(GITHUB_API_BASE):]
                    next_params = None
                    break

        if next_path is not None:
            raise PaginationLimitExceededError(path, max_pages)

        return results


class GitHubReadClient(_BaseClient):
    def list_issues(self, repo: str, state: str = "open", labels: list[str] | None = None, since: str | None = None):
        params = {"state": state}
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = since
        return self._paginated_get(f"/repos/{repo}/issues", params)

    def get_issue(self, repo: str, issue_number: int):
        issue = self._request("GET", f"/repos/{repo}/issues/{issue_number}")
        comments = self._paginated_get(f"/repos/{repo}/issues/{issue_number}/comments", {})
        issue["comments_detail"] = comments
        return issue

    def list_pull_requests(self, repo: str, state: str = "open"):
        return self._paginated_get(f"/repos/{repo}/pulls", {"state": state})

    def search_issues(self, repo: str, query: str):
        full_query = f"repo:{repo} {query}"
        return self._request("GET", "/search/issues", params={"q": full_query, "per_page": 100})

    def get_repo_labels(self, repo: str):
        return self._paginated_get(f"/repos/{repo}/labels", {})


class GitHubWriteClient(_BaseClient):
    def add_comment(self, repo: str, issue_number: int, body: str):
        return self._request("POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body})

    def add_labels(self, repo: str, issue_number: int, labels: list[str]):
        return self._request("POST", f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": labels})

    def remove_label(self, repo: str, issue_number: int, label: str):
        encoded_label = quote(label, safe="")
        return self._request("DELETE", f"/repos/{repo}/issues/{issue_number}/labels/{encoded_label}")

    def assign(self, repo: str, issue_number: int, assignee: str):
        return self._request("POST", f"/repos/{repo}/issues/{issue_number}/assignees", json={"assignees": [assignee]})

    def close(self, repo: str, issue_number: int, reason: str | None = None):
        payload = {"state": "closed"}
        if reason:
            payload["state_reason"] = reason
        return self._request("PATCH", f"/repos/{repo}/issues/{issue_number}", json=payload)
