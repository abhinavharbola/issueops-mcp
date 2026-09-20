import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from issueops.db import sync_connection
from issueops.github_client import GitHubReadClient
from issueops.heuristics import is_heuristically_flagged

VALID_CLOSE_REASONS = {"completed", "not_planned", None}

DEFAULT_COMMENT_BODY_MAX_CHARS = 65536
DEFAULT_MAX_PENDING_PER_ISSUE = 10
DEFAULT_MAX_PENDING_PER_INITIATOR = 500
MAX_ACTIVITY_WINDOW_DAYS = 365

_SEARCH_SCOPE_QUALIFIER = re.compile(r"(?:^|[\s(])-?(?:repo|org|user|owner)\s*:", re.IGNORECASE)
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:-?[A-Za-z0-9])*$")


class _ProcessLocalTTLCache:
    def __init__(self, ttl_seconds: float):
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get_or_fetch(self, key: str, fetch_fn, force_refresh: bool = False):
        now = _now_ts()
        with self._lock:
            cached = self._entries.get(key)
        if not force_refresh and cached and now - cached[0] < self._ttl_seconds:
            return cached[1]
        value = fetch_fn()
        with self._lock:
            self._entries[key] = (now, value)
        return value

    def clear(self):
        with self._lock:
            self._entries.clear()


_label_cache = _ProcessLocalTTLCache(ttl_seconds=300)
_assignee_cache = _ProcessLocalTTLCache(ttl_seconds=300)


class RepoNotAllowedError(Exception):
    pass


class ValidationError(Exception):
    pass


def _now_ts() -> float:
    return time.monotonic()


def _pending_limit(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got: {raw!r}")
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got: {value}")
    return value


def issue_plaintext(issue: dict) -> str:
    comments_text = " ".join(c.get("body", "") for c in issue.get("comments_detail", []))
    return f"{issue.get('title', '')} {issue.get('body') or ''} {comments_text}"


def check_repo_active(conn, repo: str) -> bool:
    row = conn.execute(
        "SELECT active FROM repo_allowlist WHERE repo = %s", (repo,)
    ).fetchone()
    return bool(row and row["active"])


def _require_active_repo(conn, repo: str):
    if not check_repo_active(conn, repo):
        raise RepoNotAllowedError(f"repo not allowlisted or inactive: {repo}")


def write_audit_log(
    conn,
    tool_name: str,
    repo: str | None,
    issue_number: int | None,
    arguments: dict | None,
    pending_action_id: str | None,
    initiator: str,
    result_status: str,
    result_summary: str | None,
    latency_ms: int,
    trace_id: str | None = None,
):
    conn.execute(
        """
        INSERT INTO audit_log
            (tool_name, repo, issue_number, arguments, pending_action_id,
             initiator, result_status, result_summary, latency_ms, trace_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tool_name,
            repo,
            issue_number,
            Jsonb(arguments) if arguments is not None else None,
            pending_action_id,
            initiator,
            result_status,
            result_summary,
            latency_ms,
            trace_id,
        ),
    )


def _run_read_tool(dsn, tool_name, repo, issue_number, arguments, initiator, fn):
    start = _now_ts()
    with sync_connection(dsn) as conn:
        try:
            _require_active_repo(conn, repo)
            result = fn()
            latency_ms = int((_now_ts() - start) * 1000)
            write_audit_log(
                conn, tool_name, repo, issue_number, arguments, None,
                initiator, "ok", None, latency_ms,
            )
            return result
        except Exception as exc:
            latency_ms = int((_now_ts() - start) * 1000)
            try:
                write_audit_log(
                    conn, tool_name, repo, issue_number, arguments, None,
                    initiator, "error", str(exc), latency_ms,
                )
            except Exception:
                pass
            raise


def list_issues(dsn, read_client: GitHubReadClient, repo, initiator, state="open", labels=None, since=None, max_pages=20):
    arguments = {"state": state, "labels": labels, "since": since}
    return _run_read_tool(
        dsn, "list_issues", repo, None, arguments, initiator,
        lambda: read_client.list_issues(repo, state=state, labels=labels, since=since, max_pages=max_pages),
    )


def get_issue(dsn, read_client: GitHubReadClient, repo, issue_number, initiator, include_comments=True):
    return _run_read_tool(
        dsn, "get_issue", repo, issue_number, {}, initiator,
        lambda: read_client.get_issue(repo, issue_number, include_comments=include_comments),
    )


def list_pull_requests(dsn, read_client: GitHubReadClient, repo, initiator, state="open"):
    arguments = {"state": state}
    return _run_read_tool(
        dsn, "list_pull_requests", repo, None, arguments, initiator,
        lambda: read_client.list_pull_requests(repo, state=state),
    )


def search_issues(dsn, read_client: GitHubReadClient, repo, query, initiator):
    arguments = {"query": query}

    def run():
        if _SEARCH_SCOPE_QUALIFIER.search(query):
            raise ValidationError(
                "search queries may not contain repo:, org:, user:, or owner: qualifiers; "
                "the search is always scoped to the requested repo"
            )
        return read_client.search_issues(repo, query)

    return _run_read_tool(dsn, "search_issues", repo, None, arguments, initiator, run)


def _issue_number_from_url(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def get_repo_activity_summary(dsn, read_client: GitHubReadClient, repo, days, initiator):
    arguments = {"days": days}

    def compute():
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_ACTIVITY_WINDOW_DAYS:
            raise ValidationError(f"days must be an integer between 1 and {MAX_ACTIVITY_WINDOW_DAYS}")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        since = cutoff.isoformat()
        issues = read_client.list_issues(repo, state="all", since=since)
        opened = 0
        closed = 0
        by_label: dict[str, int] = {}
        issue_numbers = set()
        for issue in issues:
            if "pull_request" in issue:
                continue
            issue_numbers.add(issue["number"])
            created_at = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
            if created_at >= cutoff:
                opened += 1
                for label in issue.get("labels", []):
                    name = label["name"] if isinstance(label, dict) else label
                    by_label[name] = by_label.get(name, 0) + 1
            closed_at_raw = issue.get("closed_at")
            if closed_at_raw:
                closed_at = datetime.fromisoformat(closed_at_raw.replace("Z", "+00:00"))
                if closed_at >= cutoff:
                    closed += 1

        commented_numbers = set()
        for comment in read_client.list_repo_comments(repo, since):
            created_at = datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))
            if created_at < cutoff:
                continue
            number = _issue_number_from_url(comment.get("issue_url", ""))
            if number in issue_numbers:
                commented_numbers.add(number)

        return {
            "opened": opened,
            "closed": closed,
            "commented": len(commented_numbers),
            "by_label": by_label,
        }

    return _run_read_tool(dsn, "get_repo_activity_summary", repo, None, arguments, initiator, compute)


def _get_repo_label_names(read_client: GitHubReadClient, repo: str, force_refresh: bool = False) -> list[str]:
    return _label_cache.get_or_fetch(
        repo, lambda: [l["name"] for l in read_client.get_repo_labels(repo)],
        force_refresh=force_refresh,
    )


def _get_repo_assignable_logins(read_client: GitHubReadClient, repo: str, force_refresh: bool = False) -> list[str]:
    return _assignee_cache.get_or_fetch(
        repo, lambda: [u["login"] for u in read_client.get_repo_assignees(repo)],
        force_refresh=force_refresh,
    )


def get_repo_label_names(read_client: GitHubReadClient, repo: str) -> list[str]:
    return list(_get_repo_label_names(read_client, repo))


def get_repo_assignable_logins(read_client: GitHubReadClient, repo: str) -> list[str]:
    return list(_get_repo_assignable_logins(read_client, repo))


def list_handled_issue_numbers(dsn, repo: str) -> set[int]:
    with sync_connection(dsn) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT issue_number FROM pending_actions
            WHERE repo = %s AND requested_by LIKE %s
              AND status IN ('pending', 'approving', 'rejected', 'executed')
            """,
            (repo, "agent:%"),
        ).fetchall()
    return {row["issue_number"] for row in rows}


def _normalize_arguments(arguments: dict) -> str:
    return json.dumps(arguments, sort_keys=True)


def _find_existing_pending(conn, repo, issue_number, tool_name, arguments: dict):
    normalized = _normalize_arguments(arguments)
    rows = conn.execute(
        """
        SELECT id, arguments FROM pending_actions
        WHERE repo = %s AND issue_number = %s AND tool_name = %s AND status = 'pending'
        """,
        (repo, issue_number, tool_name),
    ).fetchall()
    for row in rows:
        if _normalize_arguments(row["arguments"]) == normalized:
            return row["id"]
    return None


def content_hash(title: str | None, body: str | None) -> str:
    return hashlib.sha256(f"{title or ''}\n{body or ''}".encode("utf-8")).hexdigest()


def _snapshot_from_issue(issue: dict) -> dict:
    return {
        "state": issue["state"],
        "labels": sorted(l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])),
        "assignees": sorted(a["login"] for a in issue.get("assignees", [])),
        "content_hash": content_hash(issue.get("title"), issue.get("body")),
    }


def snapshot_issue_state(read_client: GitHubReadClient, repo: str, issue_number: int) -> dict:
    issue = read_client.get_issue(repo, issue_number, include_comments=False)
    return _snapshot_from_issue(issue)


def _enforce_pending_caps(conn, repo: str, issue_number: int, initiator: str):
    per_issue_limit = _pending_limit("MAX_PENDING_PER_ISSUE", DEFAULT_MAX_PENDING_PER_ISSUE)
    per_initiator_limit = _pending_limit("MAX_PENDING_PER_INITIATOR", DEFAULT_MAX_PENDING_PER_INITIATOR)

    issue_row = conn.execute(
        """
        SELECT count(*) AS n FROM pending_actions
        WHERE status IN ('pending', 'approving') AND repo = %s AND issue_number = %s
        """,
        (repo, issue_number),
    ).fetchone()
    if issue_row["n"] >= per_issue_limit:
        raise ValidationError(
            f"{repo}#{issue_number} already has {issue_row['n']} pending actions (limit {per_issue_limit}); "
            "resolve some before proposing more"
        )

    initiator_row = conn.execute(
        """
        SELECT count(*) AS n FROM pending_actions
        WHERE status IN ('pending', 'approving') AND requested_by = %s
        """,
        (initiator,),
    ).fetchone()
    if initiator_row["n"] >= per_initiator_limit:
        raise ValidationError(
            f"{initiator} already has {initiator_row['n']} pending actions (limit {per_initiator_limit}); "
            "a human needs to work through the queue first"
        )


def _record_dedup(conn, tool_name, repo, issue_number, arguments, existing_id, initiator, start):
    latency_ms = int((_now_ts() - start) * 1000)
    write_audit_log(
        conn, tool_name, repo, issue_number, arguments, existing_id,
        initiator, "deduped", "matched existing pending action", latency_ms,
    )
    return existing_id, f"duplicate of existing pending action {existing_id}", False


def _queue_proposal(
    dsn,
    read_client: GitHubReadClient,
    tool_name: str,
    repo: str,
    issue_number: int,
    arguments: dict,
    initiator: str,
    heuristic_flagged: bool,
    validate_fn=None,
    prefetched_issue: dict | None = None,
) -> tuple[str, str, bool]:
    start = _now_ts()
    with sync_connection(dsn) as conn:
        try:
            _require_active_repo(conn, repo)

            if validate_fn is not None:
                validate_fn()

            existing_id = _find_existing_pending(conn, repo, issue_number, tool_name, arguments)
            if existing_id:
                return _record_dedup(conn, tool_name, repo, issue_number, arguments, existing_id, initiator, start)

            _enforce_pending_caps(conn, repo, issue_number, initiator)

            snapshot_issue = prefetched_issue if prefetched_issue is not None else read_client.get_issue(repo, issue_number)
            snapshot = _snapshot_from_issue(snapshot_issue)
            heuristic_flagged = heuristic_flagged or is_heuristically_flagged(
                issue_plaintext(snapshot_issue)
            )

            with conn.transaction():
                lock_key = f"{repo}:{issue_number}:{tool_name}"
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))

                existing_id = _find_existing_pending(conn, repo, issue_number, tool_name, arguments)
                if existing_id:
                    return _record_dedup(conn, tool_name, repo, issue_number, arguments, existing_id, initiator, start)

                _enforce_pending_caps(conn, repo, issue_number, initiator)

                row = conn.execute(
                    """
                    INSERT INTO pending_actions
                        (tool_name, repo, issue_number, arguments, issue_state_snapshot,
                         heuristic_flagged, requested_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        tool_name, repo, issue_number, Jsonb(arguments),
                        Jsonb(snapshot), heuristic_flagged, initiator,
                    ),
                ).fetchone()
                new_id = row["id"]

                latency_ms = int((_now_ts() - start) * 1000)
                write_audit_log(
                    conn, tool_name, repo, issue_number, arguments, new_id,
                    initiator, "proposed", None, latency_ms,
                )
                return new_id, "queued for approval", True
        except Exception as exc:
            latency_ms = int((_now_ts() - start) * 1000)
            try:
                write_audit_log(
                    conn, tool_name, repo, issue_number, arguments, None,
                    initiator, "error", str(exc), latency_ms,
                )
            except Exception:
                pass
            raise


def propose_add_comment(
    dsn, read_client, repo, issue_number, body, initiator, heuristic_flagged=False,
    max_body_chars: int = DEFAULT_COMMENT_BODY_MAX_CHARS, issue: dict | None = None,
):
    body = body.strip()

    def validate():
        if not body:
            raise ValidationError("comment body cannot be empty")
        if len(body) > max_body_chars:
            raise ValidationError(f"comment body exceeds max length of {max_body_chars} characters")

    arguments = {"body": body}
    action_id, preview, _ = _queue_proposal(
        dsn, read_client, "propose_add_comment", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate, prefetched_issue=issue,
    )
    return {"id": action_id, "preview": f"Add comment on {repo}#{issue_number}: {preview}"}


def _validate_known_labels(read_client, repo, labels):
    if not labels:
        raise ValidationError("labels cannot be empty")
    valid_labels = set(_get_repo_label_names(read_client, repo))
    unknown = [l for l in labels if l not in valid_labels]
    if unknown:
        valid_labels = set(_get_repo_label_names(read_client, repo, force_refresh=True))
        unknown = [l for l in labels if l not in valid_labels]
        if unknown:
            raise ValidationError(f"unknown labels for {repo}: {unknown}")


def propose_add_labels(dsn, read_client, repo, issue_number, labels, initiator, heuristic_flagged=False, issue: dict | None = None):
    def validate():
        _validate_known_labels(read_client, repo, labels)

    arguments = {"labels": sorted(labels)}
    action_id, preview, _ = _queue_proposal(
        dsn, read_client, "propose_add_labels", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate, prefetched_issue=issue,
    )
    return {"id": action_id, "preview": f"Add labels {labels} on {repo}#{issue_number}: {preview}"}


def propose_remove_labels(dsn, read_client, repo, issue_number, labels, initiator, heuristic_flagged=False, issue: dict | None = None):
    def validate():
        _validate_known_labels(read_client, repo, labels)

    arguments = {"labels": sorted(labels)}
    action_id, preview, _ = _queue_proposal(
        dsn, read_client, "propose_remove_labels", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate, prefetched_issue=issue,
    )
    return {"id": action_id, "preview": f"Remove labels {labels} on {repo}#{issue_number}: {preview}"}


def propose_assign(dsn, read_client, repo, issue_number, assignee, initiator, heuristic_flagged=False, issue: dict | None = None):
    def validate():
        if not assignee or not assignee.strip():
            raise ValidationError("assignee cannot be empty")
        if len(assignee) > 39 or not _GITHUB_LOGIN.match(assignee):
            raise ValidationError(f"{assignee} is not a syntactically valid GitHub login")
        wanted = assignee.lower()
        logins = {l.lower() for l in _get_repo_assignable_logins(read_client, repo)}
        if wanted not in logins:
            logins = {l.lower() for l in _get_repo_assignable_logins(read_client, repo, force_refresh=True)}
            if wanted not in logins:
                raise ValidationError(f"{assignee} is not an assignable user in {repo}")

    arguments = {"assignee": assignee}
    action_id, preview, _ = _queue_proposal(
        dsn, read_client, "propose_assign", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate, prefetched_issue=issue,
    )
    return {"id": action_id, "preview": f"Assign {assignee} on {repo}#{issue_number}: {preview}"}


def propose_close(dsn, read_client, repo, issue_number, reason, initiator, heuristic_flagged=False, issue: dict | None = None):
    def validate():
        if reason not in VALID_CLOSE_REASONS:
            raise ValidationError(f"reason must be one of {sorted(r for r in VALID_CLOSE_REASONS if r)} or omitted")

    arguments = {"reason": reason}
    action_id, preview, _ = _queue_proposal(
        dsn, read_client, "propose_close", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate, prefetched_issue=issue,
    )
    return {"id": action_id, "preview": f"Close {repo}#{issue_number}: {preview}"}
