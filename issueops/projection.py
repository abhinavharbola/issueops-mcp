LIST_BODY_CHARS = 500
DETAIL_BODY_CHARS = 12000
DETAIL_COMMENT_CHARS = 2500
DETAIL_MAX_COMMENTS = 30
TITLE_CHARS = 300


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}[truncated {len(text) - limit} chars]"


def _login(user) -> str | None:
    if isinstance(user, dict):
        return user.get("login")
    return None


def _label_names(labels) -> list[str]:
    names = []
    for label in labels or []:
        name = label.get("name") if isinstance(label, dict) else label
        if name:
            names.append(name)
    return names


def _logins(users) -> list[str]:
    return [login for login in (_login(user) for user in users or []) if login]


def summarize_issue(issue: dict, body_chars: int = LIST_BODY_CHARS) -> dict:
    return {
        "number": issue.get("number"),
        "title": _clip(issue.get("title") or "", TITLE_CHARS),
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "is_pull_request": "pull_request" in issue,
        "author": _login(issue.get("user")),
        "labels": _label_names(issue.get("labels")),
        "assignees": _logins(issue.get("assignees")),
        "comment_count": issue.get("comments"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "closed_at": issue.get("closed_at"),
        "html_url": issue.get("html_url"),
        "body_excerpt": _clip(issue.get("body") or "", body_chars),
    }


def summarize_pull_request(pull: dict, body_chars: int = LIST_BODY_CHARS) -> dict:
    return {
        "number": pull.get("number"),
        "title": _clip(pull.get("title") or "", TITLE_CHARS),
        "state": pull.get("state"),
        "draft": pull.get("draft"),
        "author": _login(pull.get("user")),
        "labels": _label_names(pull.get("labels")),
        "assignees": _logins(pull.get("assignees")),
        "head": (pull.get("head") or {}).get("ref"),
        "base": (pull.get("base") or {}).get("ref"),
        "created_at": pull.get("created_at"),
        "updated_at": pull.get("updated_at"),
        "closed_at": pull.get("closed_at"),
        "merged_at": pull.get("merged_at"),
        "html_url": pull.get("html_url"),
        "body_excerpt": _clip(pull.get("body") or "", body_chars),
    }


def present_issue_list(result: dict) -> dict:
    issues = [summarize_issue(issue) for issue in result["issues"]]
    return {"count": len(issues), "truncated": result["truncated"], "issues": issues}


def present_pull_request_list(result: dict) -> dict:
    pulls = [summarize_pull_request(pull) for pull in result["pull_requests"]]
    return {"count": len(pulls), "truncated": result["truncated"], "pull_requests": pulls}


def present_issue(issue: dict) -> dict:
    comments = issue.get("comments_detail") or []
    recent = comments[-DETAIL_MAX_COMMENTS:]
    detail = summarize_issue(issue, body_chars=DETAIL_BODY_CHARS)
    detail["body"] = detail.pop("body_excerpt")
    detail["comments"] = [
        {
            "id": comment.get("id"),
            "author": _login(comment.get("user")),
            "author_association": comment.get("author_association"),
            "created_at": comment.get("created_at"),
            "updated_at": comment.get("updated_at"),
            "body": _clip(comment.get("body") or "", DETAIL_COMMENT_CHARS),
        }
        for comment in recent
    ]
    detail["comments_omitted"] = len(comments) - len(recent)
    detail["comments_truncated_by_fetcher"] = bool(issue.get("comments_truncated"))
    return detail


def present_search(result: dict) -> dict:
    items = [summarize_issue(item) for item in result.get("items") or []]
    presented = {
        "total_count": result.get("total_count"),
        "incomplete_results": result.get("incomplete_results"),
        "count": len(items),
        "items": items,
    }
    if "filtered_out_other_repos" in result:
        presented["filtered_out_other_repos"] = result["filtered_out_other_repos"]
    return presented


