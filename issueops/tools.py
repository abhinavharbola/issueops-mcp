import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from issueops import limits
from issueops.db import sync_connection
from issueops.github_client import GitHubReadClient, PaginationLimitExceededError
from issueops.heuristics import flag_matches, is_heuristically_flagged

VALID_CLOSE_REASONS = {"completed", "not_planned", None}

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
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


class QueueFullError(ValidationError):
    def __init__(self, message: str, scope: str):
        self.scope = scope
        super().__init__(message)


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


def normalize_repo(repo: str) -> str:
    return repo.strip().lower()


def issue_plaintext(issue: dict) -> str:
    comments_text = " ".join((c.get("body") or "") for c in issue.get("comments_detail") or [])
    return f"{issue.get('title') or ''} {issue.get('body') or ''} {comments_text}"


def _issue_label_names(issue: dict) -> list[str]:
    return [l["name"] if isinstance(l, dict) else l for l in issue.get("labels") or []]


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


def _validate_list_limit(limit):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValidationError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")


def _collect_bounded(pages, limit: int):
    items = []
    try:
        for page in pages:
            items.extend(page)
            if len(items) > limit:
                break
    except PaginationLimitExceededError:
        return items[:limit], True
    return items[:limit], len(items) > limit


def list_issues(
    dsn, read_client: GitHubReadClient, repo, initiator, state="open", labels=None, since=None,
    max_pages=20, limit=DEFAULT_LIST_LIMIT,
):
    repo = normalize_repo(repo)
    arguments = {"state": state, "labels": labels, "since": since, "limit": limit}

    def collect():
        _validate_list_limit(limit)
        pages = read_client.iter_issue_pages(repo, state=state, labels=labels, since=since, max_pages=max_pages)
        issues, truncated = _collect_bounded(pages, limit)
        return {"issues": issues, "truncated": truncated}

    return _run_read_tool(dsn, "list_issues", repo, None, arguments, initiator, collect)


def list_issue_candidates(
    dsn, read_client: GitHubReadClient, repo, initiator, state="open", since=None,
    max_pages=20, limit=None, exclude=None, unchanged=None,
):
    repo = normalize_repo(repo)
    excluded = exclude or set()
    unchanged_hashes = unchanged or {}
    arguments = {"state": state, "since": since, "max_pages": max_pages, "limit": limit}

    def collect():
        issues = []
        skipped = 0
        try:
            for page in read_client.iter_issue_pages(repo, state=state, since=since, max_pages=max_pages):
                for issue in page:
                    if "pull_request" in issue:
                        continue
                    number = issue.get("number")
                    if number in excluded:
                        skipped += 1
                        continue
                    known_hash = unchanged_hashes.get(number)
                    if known_hash is not None and known_hash == content_hash(issue.get("title"), issue.get("body")):
                        skipped += 1
                        continue
                    issues.append(issue)
                    if limit is not None and len(issues) >= limit:
                        return {"issues": issues, "truncated": False, "skipped": skipped}
        except PaginationLimitExceededError:
            return {"issues": issues, "truncated": True, "skipped": skipped}
        return {"issues": issues, "truncated": False, "skipped": skipped}

    return _run_read_tool(dsn, "list_issues", repo, None, arguments, initiator, collect)


def get_issue(dsn, read_client: GitHubReadClient, repo, issue_number, initiator, include_comments=True):
    repo = normalize_repo(repo)
    return _run_read_tool(
        dsn, "get_issue", repo, issue_number, {}, initiator,
        lambda: read_client.get_issue(repo, issue_number, include_comments=include_comments),
    )


def list_pull_requests(
    dsn, read_client: GitHubReadClient, repo, initiator, state="open", max_pages=20, limit=DEFAULT_LIST_LIMIT,
):
    repo = normalize_repo(repo)
    arguments = {"state": state, "limit": limit}

    def collect():
        _validate_list_limit(limit)
        pages = read_client.iter_pull_request_pages(repo, state=state, max_pages=max_pages)
        pulls, truncated = _collect_bounded(pages, limit)
        return {"pull_requests": pulls, "truncated": truncated}

    return _run_read_tool(dsn, "list_pull_requests", repo, None, arguments, initiator, collect)


def search_issues(dsn, read_client: GitHubReadClient, repo, query, initiator):
    repo = normalize_repo(repo)
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


def _drain_pages(pages):
    items = []
    try:
        for page in pages:
            items.extend(page)
    except PaginationLimitExceededError:
        return items, True
    return items, False


def get_repo_activity_summary(dsn, read_client: GitHubReadClient, repo, days, initiator):
    repo = normalize_repo(repo)
    arguments = {"days": days}

    def compute():
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_ACTIVITY_WINDOW_DAYS:
            raise ValidationError(f"days must be an integer between 1 and {MAX_ACTIVITY_WINDOW_DAYS}")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        since = cutoff.isoformat()
        issues, issues_truncated = _drain_pages(read_client.iter_issue_pages(repo, state="all", since=since))
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
        comments, comments_truncated = _drain_pages(read_client.iter_repo_comment_pages(repo, since))
        for comment in comments:
            created_at = datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))
            if created_at < cutoff:
                continue
            number = _issue_number_from_url(comment.get("issue_url", ""))
            if number in issue_numbers:
                commented_numbers.add(number)

        result = {
            "opened": opened,
            "closed": closed,
            "commented": len(commented_numbers),
            "by_label": by_label,
        }
        if issues_truncated or comments_truncated:
            result["truncated"] = True
            result["note"] = (
                "the page limit was reached before every matching item was read, "
                "so these counts are lower bounds"
            )
        return result

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
    return list(_get_repo_label_names(read_client, normalize_repo(repo)))


def get_repo_assignable_logins(read_client: GitHubReadClient, repo: str) -> list[str]:
    return list(_get_repo_assignable_logins(read_client, normalize_repo(repo)))


def list_handled_issue_numbers(dsn, repo: str) -> set[int]:
    repo = normalize_repo(repo)
    with sync_connection(dsn) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT issue_number FROM pending_actions
            WHERE repo = %s AND requested_by LIKE %s
              AND status IN ('pending', 'approving', 'needs_review', 'rejected', 'executed')
            """,
            (repo, "agent:%"),
        ).fetchall()
    return {row["issue_number"] for row in rows}


DEFAULT_MAX_ERROR_ATTEMPTS = 3
TRIAGE_OUTCOMES = ("proposed", "no_action", "error")


def list_triage_skips(dsn, repo: str, max_error_attempts: int = DEFAULT_MAX_ERROR_ATTEMPTS) -> dict[int, str]:
    repo = normalize_repo(repo)
    with sync_connection(dsn) as conn:
        rows = conn.execute(
            """
            SELECT issue_number, content_hash FROM triage_attempts
            WHERE repo = %s AND (outcome = 'no_action' OR (outcome = 'error' AND attempts >= %s))
            """,
            (repo, max_error_attempts),
        ).fetchall()
    return {row["issue_number"]: row["content_hash"] for row in rows}


def record_triage_attempt(dsn, repo: str, issue_number: int, digest: str, outcome: str, error: str | None = None):
    if outcome not in TRIAGE_OUTCOMES:
        raise ValueError(f"outcome must be one of {TRIAGE_OUTCOMES}, got {outcome!r}")
    repo = normalize_repo(repo)
    with sync_connection(dsn) as conn:
        conn.execute(
            """
            INSERT INTO triage_attempts (repo, issue_number, content_hash, outcome, attempts, last_error, updated_at)
            VALUES (%s, %s, %s, %s, 1, %s, now())
            ON CONFLICT (repo, issue_number) DO UPDATE SET
                attempts = CASE
                    WHEN triage_attempts.content_hash = EXCLUDED.content_hash
                         AND triage_attempts.outcome = 'error' AND EXCLUDED.outcome = 'error'
                    THEN triage_attempts.attempts + 1
                    ELSE 1
                END,
                content_hash = EXCLUDED.content_hash,
                outcome = EXCLUDED.outcome,
                last_error = EXCLUDED.last_error,
                updated_at = now()
            """,
            (repo, issue_number, digest, outcome, error),
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


EXCERPT_TITLE_CHARS = limits.TITLE_CHARS
EXCERPT_BODY_CHARS = limits.BODY_CHARS
EXCERPT_COMMENT_CHARS = limits.COMMENT_CHARS
EXCERPT_MAX_COMMENTS = limits.MAX_COMMENTS
RATIONALE_MAX_CHARS = 2000
LOCK_NAMESPACE_INITIATOR = 1
LOCK_NAMESPACE_ISSUE = 2


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}[truncated {len(text) - limit} chars]"


def build_source_excerpt(issue: dict) -> dict:
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    comments = issue.get("comments_detail") or []
    recent = comments[-EXCERPT_MAX_COMMENTS:]
    shown_comments = [
        {
            "author": (c.get("user") or {}).get("login") or "unknown",
            "body": _clip(c.get("body") or "", EXCERPT_COMMENT_CHARS),
        }
        for c in recent
    ]
    shown_title = _clip(title, EXCERPT_TITLE_CHARS)
    shown_body = _clip(body, EXCERPT_BODY_CHARS)
    all_matches = flag_matches(issue_plaintext(issue))
    shown_text = " ".join([shown_title, shown_body] + [c["body"] for c in shown_comments])
    visible_matches = set(flag_matches(shown_text))
    hidden_matches = [phrase for phrase in all_matches if phrase not in visible_matches]
    text_cut = (
        len(title) > EXCERPT_TITLE_CHARS
        or len(body) > EXCERPT_BODY_CHARS
        or any(len(c.get("body") or "") > EXCERPT_COMMENT_CHARS for c in recent)
    )
    return {
        "title": shown_title,
        "body": shown_body,
        "comments": shown_comments,
        "comments_omitted": len(comments) - len(recent),
        "comments_truncated_by_fetcher": bool(issue.get("comments_truncated")),
        "text_truncated": text_cut,
        "flag_matches": all_matches,
        "flag_matches_not_shown": hidden_matches,
    }


def content_hash(title: str | None, body: str | None) -> str:
    return hashlib.sha256(f"{title or ''}\n{body or ''}".encode("utf-8")).hexdigest()


def _snapshot_from_issue(issue: dict) -> dict:
    return {
        "state": issue["state"],
        "labels": sorted(_issue_label_names(issue)),
        "assignees": sorted(a["login"] for a in issue.get("assignees") or []),
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
        raise QueueFullError(
            f"{repo}#{issue_number} already has {issue_row['n']} pending actions (limit {per_issue_limit}); "
            "resolve some before proposing more",
            "issue",
        )

    initiator_row = conn.execute(
        """
        SELECT count(*) AS n FROM pending_actions
        WHERE status IN ('pending', 'approving') AND requested_by = %s
        """,
        (initiator,),
    ).fetchone()
    if initiator_row["n"] >= per_initiator_limit:
        raise QueueFullError(
            f"{initiator} already has {initiator_row['n']} pending actions (limit {per_initiator_limit}); "
            "a human needs to work through the queue first",
            "initiator",
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
    state_check=None,
    rationale: str | None = None,
) -> tuple[str, str, bool]:
    start = _now_ts()
    with sync_connection(dsn) as conn:
        try:
            _require_active_repo(conn, repo)

            if validate_fn is not None:
                validated = validate_fn()
                if validated is not None:
                    arguments = validated

            existing_id = _find_existing_pending(conn, repo, issue_number, tool_name, arguments)
            if existing_id:
                return _record_dedup(conn, tool_name, repo, issue_number, arguments, existing_id, initiator, start)

            _enforce_pending_caps(conn, repo, issue_number, initiator)

            snapshot_issue = prefetched_issue if prefetched_issue is not None else read_client.get_issue(repo, issue_number)
            if state_check is not None:
                state_check(snapshot_issue, arguments)
            snapshot = _snapshot_from_issue(snapshot_issue)
            excerpt = build_source_excerpt(snapshot_issue)
            heuristic_flagged = heuristic_flagged or bool(excerpt["flag_matches"])
            stored_rationale = _clip(rationale, RATIONALE_MAX_CHARS) if rationale else None

            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (LOCK_NAMESPACE_INITIATOR, initiator),
                )
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (LOCK_NAMESPACE_ISSUE, f"{repo}:{issue_number}"),
                )

                existing_id = _find_existing_pending(conn, repo, issue_number, tool_name, arguments)
                if existing_id:
                    return _record_dedup(conn, tool_name, repo, issue_number, arguments, existing_id, initiator, start)

                _enforce_pending_caps(conn, repo, issue_number, initiator)

                row = conn.execute(
                    """
                    INSERT INTO pending_actions
                        (tool_name, repo, issue_number, arguments, issue_state_snapshot,
                         heuristic_flagged, requested_by, source_excerpt, rationale)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        tool_name, repo, issue_number, Jsonb(arguments),
                        Jsonb(snapshot), heuristic_flagged, initiator,
                        Jsonb(excerpt), stored_rationale,
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
    rationale: str | None = None,
):
    repo = normalize_repo(repo)
    body = body.strip()

    def validate():
        if not body:
            raise ValidationError("comment body cannot be empty")
        if len(body) > max_body_chars:
            raise ValidationError(f"comment body exceeds max length of {max_body_chars} characters")

    arguments = {"body": body}
    action_id, preview, _ = _queue_proposal(
        dsn, read_client, "propose_add_comment", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate, prefetched_issue=issue, rationale=rationale,
    )
    return {"id": action_id, "preview": f"Add comment on {repo}#{issue_number}: {preview}"}


def _resolve_known_labels(read_client, repo, labels) -> list[str]:
    if not labels:
        raise ValidationError("labels cannot be empty")

    def resolve(names):
        by_lower = {name.lower(): name for name in names}
        resolved = []
        unknown = []
        for label in labels:
            canonical = by_lower.get(str(label).lower())
            if canonical is None:
                unknown.append(label)
            elif canonical not in resolved:
                resolved.append(canonical)
        return resolved, unknown

    resolved, unknown = resolve(_get_repo_label_names(read_client, repo))
    if unknown:
        resolved, unknown = resolve(_get_repo_label_names(read_client, repo, force_refresh=True))
        if unknown:
            raise ValidationError(f"unknown labels for {repo}: {unknown}")
    return resolved


def _check_add_labels_state(issue: dict, arguments: dict):
    present = {name.lower() for name in _issue_label_names(issue)}
    if all(label.lower() in present for label in arguments["labels"]):
        raise ValidationError("all requested labels are already on the issue")


def _check_remove_labels_state(issue: dict, arguments: dict):
    present = {name.lower() for name in _issue_label_names(issue)}
    missing = [label for label in arguments["labels"] if label.lower() not in present]
    if missing:
        raise ValidationError(f"labels are not on the issue: {missing}")


def _check_assign_state(issue: dict, arguments: dict):
    assigned = {(a.get("login") or "").lower() for a in issue.get("assignees") or []}
    if arguments["assignee"].lower() in assigned:
        raise ValidationError(f"{arguments['assignee']} is already assigned to the issue")


def _check_close_state(issue: dict, arguments: dict):
    if issue.get("state") != "open":
        raise ValidationError("the issue is not open, so there is nothing to close")


def _propose_labels(
    tool_name, verb, state_check, dsn, read_client, repo, issue_number, labels, initiator,
    heuristic_flagged, issue, rationale=None,
):
    repo = normalize_repo(repo)
    resolved = {}

    def validate():
        resolved["labels"] = sorted(_resolve_known_labels(read_client, repo, labels))
        return {"labels": resolved["labels"]}

    action_id, preview, _ = _queue_proposal(
        dsn, read_client, tool_name, repo, issue_number, {"labels": sorted(labels or [])}, initiator,
        heuristic_flagged, validate_fn=validate, prefetched_issue=issue, state_check=state_check,
        rationale=rationale,
    )
    shown = resolved.get("labels", labels)
    return {"id": action_id, "preview": f"{verb} labels {shown} on {repo}#{issue_number}: {preview}"}


def propose_add_labels(dsn, read_client, repo, issue_number, labels, initiator, heuristic_flagged=False, issue: dict | None = None, rationale: str | None = None):
    return _propose_labels(
        "propose_add_labels", "Add", _check_add_labels_state, dsn, read_client, repo, issue_number,
        labels, initiator, heuristic_flagged, issue, rationale,
    )


def propose_remove_labels(dsn, read_client, repo, issue_number, labels, initiator, heuristic_flagged=False, issue: dict | None = None, rationale: str | None = None):
    return _propose_labels(
        "propose_remove_labels", "Remove", _check_remove_labels_state, dsn, read_client, repo,
        issue_number, labels, initiator, heuristic_flagged, issue, rationale,
    )


def propose_assign(dsn, read_client, repo, issue_number, assignee, initiator, heuristic_flagged=False, issue: dict | None = None, rationale: str | None = None):
    repo = normalize_repo(repo)
    resolved = {}

    def validate():
        if not assignee or not assignee.strip():
            raise ValidationError("assignee cannot be empty")
        if len(assignee) > 39 or not _GITHUB_LOGIN.match(assignee):
            raise ValidationError(f"{assignee} is not a syntactically valid GitHub login")
        wanted = assignee.lower()
        logins = {l.lower(): l for l in _get_repo_assignable_logins(read_client, repo)}
        if wanted not in logins:
            logins = {l.lower(): l for l in _get_repo_assignable_logins(read_client, repo, force_refresh=True)}
            if wanted not in logins:
                raise ValidationError(f"{assignee} is not an assignable user in {repo}")
        resolved["assignee"] = logins[wanted]
        return {"assignee": logins[wanted]}

    action_id, preview, _ = _queue_proposal(
        dsn, read_client, "propose_assign", repo, issue_number, {"assignee": assignee}, initiator,
        heuristic_flagged, validate_fn=validate, prefetched_issue=issue, state_check=_check_assign_state,
        rationale=rationale,
    )
    return {"id": action_id, "preview": f"Assign {resolved.get('assignee', assignee)} on {repo}#{issue_number}: {preview}"}


def propose_close(dsn, read_client, repo, issue_number, reason, initiator, heuristic_flagged=False, issue: dict | None = None, rationale: str | None = None):
    repo = normalize_repo(repo)

    def validate():
        if reason not in VALID_CLOSE_REASONS:
            raise ValidationError(f"reason must be one of {sorted(r for r in VALID_CLOSE_REASONS if r)} or omitted")

    arguments = {"reason": reason}
    action_id, preview, _ = _queue_proposal(
        dsn, read_client, "propose_close", repo, issue_number, arguments, initiator, heuristic_flagged,
        validate_fn=validate, prefetched_issue=issue, state_check=_check_close_state,
        rationale=rationale,
    )
    return {"id": action_id, "preview": f"Close {repo}#{issue_number}: {preview}"}
