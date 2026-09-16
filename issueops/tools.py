import json
import re
import time
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from issueops.db import sync_connection
from issueops.github_client import GitHubReadClient

VALID_CLOSE_REASONS = {"completed", "not_planned", None}

DEFAULT_COMMENT_BODY_MAX_CHARS = 65536

_label_cache: dict[str, tuple[float, list[str]]] = {}
_CACHE_TTL_SECONDS = 300


class RepoNotAllowedError(Exception):
    pass


class ValidationError(Exception):
    pass


def _now_ts() -> float:
    return time.monotonic()


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
            write_audit_log(
                conn, tool_name, repo, issue_number, arguments, None,
                initiator, "error", str(exc), latency_ms,
            )
            raise


def list_issues(dsn, read_client: GitHubReadClient, repo, initiator, state="open", labels=None, since=None):
    arguments = {"state": state, "labels": labels, "since": since}
    return _run_read_tool(
        dsn, "list_issues", repo, None, arguments, initiator,
        lambda: read_client.list_issues(repo, state=state, labels=labels, since=since),
    )


def get_issue(dsn, read_client: GitHubReadClient, repo, issue_number, initiator):
    return _run_read_tool(
        dsn, "get_issue", repo, issue_number, {}, initiator,
        lambda: read_client.get_issue(repo, issue_number),
    )


def list_pull_requests(dsn, read_client: GitHubReadClient, repo, initiator, state="open"):
    arguments = {"state": state}
    return _run_read_tool(
        dsn, "list_pull_requests", repo, None, arguments, initiator,
        lambda: read_client.list_pull_requests(repo, state=state),
    )


def search_issues(dsn, read_client: GitHubReadClient, repo, query, initiator):
    arguments = {"query": query}
    return _run_read_tool(
        dsn, "search_issues", repo, None, arguments, initiator,
        lambda: read_client.search_issues(repo, query),
    )


def get_repo_activity_summary(dsn, read_client: GitHubReadClient, repo, days, initiator):
    arguments = {"days": days}

    def compute():
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        issues = read_client.list_issues(repo, state="all", since=cutoff.isoformat())
        opened = 0
        closed = 0
        commented = 0
        by_label: dict[str, int] = {}
        for issue in issues:
            if "pull_request" in issue:
                continue
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
            if issue.get("comments", 0) > 0:
                commented += 1
        return {
            "opened": opened,
            "closed": closed,
            "commented_note": "commented counts issues updated in the window with at least one comment ever, not comments made within the window",
            "commented": commented,
            "by_label": by_label,
        }

    return _run_read_tool(dsn, "get_repo_activity_summary", repo, None, arguments, initiator, compute)


def _cached(cache: dict, key: str, fetch_fn, force_refresh: bool = False):
    now = _now_ts()
    cached = cache.get(key)
    if not force_refresh and cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    value = fetch_fn()
    cache[key] = (now, value)
    return value


def _get_repo_label_names(read_client: GitHubReadClient, repo: str, force_refresh: bool = False) -> list[str]:
    return _cached(
        _label_cache, repo, lambda: [l["name"] for l in read_client.get_repo_labels(repo)],
        force_refresh=force_refresh,
    )


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


def snapshot_issue_state(read_client: GitHubReadClient, repo: str, issue_number: int) -> dict:
    issue = read_client.get_issue(repo, issue_number)
    return {
        "state": issue["state"],
        "labels": sorted(l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])),
        "assignees": sorted(a["login"] for a in issue.get("assignees", [])),
    }


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
) -> tuple[str, str, bool]:
    start = _now_ts()
    with sync_connection(dsn) as conn:
        try:
            _require_active_repo(conn, repo)

            with conn.transaction():
                lock_key = f"{repo}:{issue_number}:{tool_name}"
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))

                if validate_fn is not None:
                    validate_fn()

                existing_id = _find_existing_pending(conn, repo, issue_number, tool_name, arguments)
                if existing_id:
                    latency_ms = int((_now_ts() - start) * 1000)
                    write_audit_log(
                        conn, tool_name, repo, issue_number, arguments, existing_id,
                        initiator, "deduped", "matched existing pending action", latency_ms,
                    )
                    return existing_id, f"duplicate of existing pending action {existing_id}", False

                snapshot = snapshot_issue_state(read_client, repo, issue_number)

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
            write_audit_log(
                conn, tool_name, repo, issue_number, arguments, None,
                initiator, "error", str(exc), latency_ms,
            )
            raise


def propose_add_comment(
    dsn, read_client, repo, issue_number, body, initiator, heuristic_flagged=False,
    max_body_chars: int = DEFAULT_COMMENT_BODY_MAX_CHARS,
):
    body = body.strip()

    def validate():
        if not body:
            raise ValidationError("comment body cannot be empty")
        if len(body) > max_body_chars:
            raise ValidationError(f"comment body exceeds max length of {max_body_chars} characters")

    arguments = {"body": body}
    action_id, preview, created = _queue_proposal(
        dsn, read_client, "propose_add_comment", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate,
    )
    return {"id": action_id, "preview": f"Add comment on {repo}#{issue_number}: {preview}"}


def propose_add_labels(dsn, read_client, repo, issue_number, labels, initiator, heuristic_flagged=False):
    def validate():
        if not labels:
            raise ValidationError("labels cannot be empty")
        valid_labels = set(_get_repo_label_names(read_client, repo))
        unknown = [l for l in labels if l not in valid_labels]
        if unknown:
            valid_labels = set(_get_repo_label_names(read_client, repo, force_refresh=True))
            unknown = [l for l in labels if l not in valid_labels]
            if unknown:
                raise ValidationError(f"unknown labels for {repo}: {unknown}")

    arguments = {"labels": sorted(labels)}
    action_id, preview, created = _queue_proposal(
        dsn, read_client, "propose_add_labels", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate,
    )
    return {"id": action_id, "preview": f"Add labels {labels} on {repo}#{issue_number}: {preview}"}


def propose_remove_labels(dsn, read_client, repo, issue_number, labels, initiator, heuristic_flagged=False):
    def validate():
        if not labels:
            raise ValidationError("labels cannot be empty")
        valid_labels = set(_get_repo_label_names(read_client, repo))
        unknown = [l for l in labels if l not in valid_labels]
        if unknown:
            valid_labels = set(_get_repo_label_names(read_client, repo, force_refresh=True))
            unknown = [l for l in labels if l not in valid_labels]
            if unknown:
                raise ValidationError(f"unknown labels for {repo}: {unknown}")

    arguments = {"labels": sorted(labels)}
    action_id, preview, created = _queue_proposal(
        dsn, read_client, "propose_remove_labels", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate,
    )
    return {"id": action_id, "preview": f"Remove labels {labels} on {repo}#{issue_number}: {preview}"}


def propose_assign(dsn, read_client, repo, issue_number, assignee, initiator, heuristic_flagged=False):
    def validate():
        if not assignee or not assignee.strip():
            raise ValidationError("assignee cannot be empty")
        if not re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$", assignee):
            raise ValidationError(f"{assignee} is not a syntactically valid GitHub login")

    arguments = {"assignee": assignee}
    action_id, preview, created = _queue_proposal(
        dsn, read_client, "propose_assign", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate,
    )
    return {"id": action_id, "preview": f"Assign {assignee} on {repo}#{issue_number}: {preview}"}


def propose_close(dsn, read_client, repo, issue_number, reason, initiator, heuristic_flagged=False):
    def validate():
        if reason not in VALID_CLOSE_REASONS:
            raise ValidationError(f"reason must be one of {sorted(r for r in VALID_CLOSE_REASONS if r)} or omitted")

    arguments = {"reason": reason}
    action_id, preview, created = _queue_proposal(
        dsn, read_client, "propose_close", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate,
    )
    return {"id": action_id, "preview": f"Close {repo}#{issue_number}: {preview}"}
