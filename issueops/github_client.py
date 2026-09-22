import re
from urllib.parse import quote

import requests

GITHUB_API_BASE = "https://api.github.com"

_LINK_PATTERN = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


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

    @staticmethod
    def _next_path(response):
        link_header = response.headers.get("Link")
        if not link_header:
            return None
        for url, rel in _LINK_PATTERN.findall(link_header):
            if rel != "next":
                continue
            if not url.startswith(GITHUB_API_BASE):
                raise GitHubAPIError(
                    response.status_code, f"pagination link points outside the GitHub API: {url}"
                )
            return url[len(GITHUB_API_BASE):]
        return None

    def _paginated_iter(self, path: str, params: dict, max_pages: int = 20):
        page_params = dict(params)
        page_params.setdefault("per_page", 100)
        next_path = path
        next_params = page_params
        pages_fetched = 0

        while next_path is not None and pages_fetched < max_pages:
            response = self._request_raw("GET", next_path, params=next_params)
            data = response.json()
            if isinstance(data, dict) and "items" in data:
                items = data["items"]
            elif isinstance(data, list):
                items = data
            else:
                raise GitHubAPIError(
                    response.status_code,
                    f"unexpected response shape for {next_path}: expected a list or a dict "
                    f"with an 'items' key, got {type(data).__name__}",
                )
            pages_fetched += 1
            next_path = self._next_path(response)
            next_params = None
            yield items

        if next_path is not None:
            raise PaginationLimitExceededError(path, max_pages)

    def _paginated_get(self, path: str, params: dict, max_pages: int = 20):
        results = []
        for items in self._paginated_iter(path, params, max_pages=max_pages):
            results.extend(items)
        return results

    def _paginated_partial(self, path: str, params: dict, max_pages: int = 20):
        results = []
        try:
            for items in self._paginated_iter(path, params, max_pages=max_pages):
                results.extend(items)
        except PaginationLimitExceededError:
            return results, True
        return results, False


class GitHubReadClient(_BaseClient):
    @staticmethod
    def _issue_params(state: str, labels: list[str] | None, since: str | None) -> dict:
        params = {"state": state}
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = since
        return params

    def list_issues(
        self, repo: str, state: str = "open", labels: list[str] | None = None,
        since: str | None = None, max_pages: int = 20,
    ):
        return self._paginated_get(
            f"/repos/{repo}/issues", self._issue_params(state, labels, since), max_pages=max_pages
        )

    def iter_issue_pages(
        self, repo: str, state: str = "open", labels: list[str] | None = None,
        since: str | None = None, max_pages: int = 20,
    ):
        return self._paginated_iter(
            f"/repos/{repo}/issues", self._issue_params(state, labels, since), max_pages=max_pages
        )

    def get_issue(self, repo: str, issue_number: int, include_comments: bool = True):
        issue = self._request("GET", f"/repos/{repo}/issues/{issue_number}")
        if include_comments:
            comments, truncated = self._paginated_partial(
                f"/repos/{repo}/issues/{issue_number}/comments", {}
            )
            issue["comments_detail"] = comments
            if truncated:
                issue["comments_truncated"] = True
        return issue

    def get_issue_comments(
        self, repo: str, issue_number: int, max_pages: int = 20, allow_partial: bool = False
    ):
        path = f"/repos/{repo}/issues/{issue_number}/comments"
        if allow_partial:
            comments, _ = self._paginated_partial(path, {}, max_pages=max_pages)
            return comments
        return self._paginated_get(path, {}, max_pages=max_pages)

    def list_repo_comments(self, repo: str, since: str):
        return self._paginated_get(f"/repos/{repo}/issues/comments", {"since": since})

    def iter_repo_comment_pages(self, repo: str, since: str, max_pages: int = 20):
        return self._paginated_iter(f"/repos/{repo}/issues/comments", {"since": since}, max_pages=max_pages)

    def list_pull_requests(self, repo: str, state: str = "open"):
        return self._paginated_get(f"/repos/{repo}/pulls", {"state": state})

    def iter_pull_request_pages(self, repo: str, state: str = "open", max_pages: int = 20):
        return self._paginated_iter(f"/repos/{repo}/pulls", {"state": state}, max_pages=max_pages)

    def search_issues(self, repo: str, query: str):
        full_query = f"repo:{repo} {query}"
        data = self._request("GET", "/search/issues", params={"q": full_query, "per_page": 100})
        suffix = f"/repos/{repo}".lower()
        items = data.get("items", [])
        kept = [item for item in items if str(item.get("repository_url", "")).lower().endswith(suffix)]
        result = {**data, "items": kept}
        if len(kept) != len(items):
            result["filtered_out_other_repos"] = len(items) - len(kept)
        return result

    def get_repo_labels(self, repo: str):
        return self._paginated_get(f"/repos/{repo}/labels", {})

    def get_repo_assignees(self, repo: str):
        return self._paginated_get(f"/repos/{repo}/assignees", {})


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
